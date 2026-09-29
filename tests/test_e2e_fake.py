"""Сквозной прогон конвейера на поддельной модели: ПД (слой) + РД (слой) + ИД (скан -> OCR) -> кандидат KR-057 с
тремя доказательствами и рамками; протокол и сабмит по схеме; дозагрузка -> новая версия; модель недоступна -> FAILED."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from inspector import jobs
from inspector import statuses as S
from inspector.models import AuditLog, ConstructionObject, File, Finding, Job, Page, Process, ProtocolVersion
from inspector.pipeline.intake import store_local
from inspector.pipeline.runner import run_job
from inspector.protocol import build_protocol, build_submission, validate_submission
from inspector.testing import make_pdf, make_scan_pdf
from tests.fake_llm import FakeLLM

PD_TEXT = ("Проектная документация. Раздел 4. Конструктивные решения\n"
           "Ограждение котлована. Форшахта\n"
           "Арматура форшахты класса А400 по ГОСТ 34028-2016\n"
           "Бетон форшахты В30 W8 F150")
RD_TEXT = ("Рабочая документация КЖ. Форшахта\n"
           "Спецификация: Арматура форшахты Ø12 А240 шаг 200\n"
           "Бетон В30")
ID_TEXT = ("АКТ освидетельствования скрытых работ № 1АФ от 11.02.2026\n"
           "Форшахта: арматура класса А240С диаметр 12 мм")


def add_file(db, process: Process, path: Path, stage: str | None, batch: int = 1, rel: str | None = None) -> File:
    from inspector.config import get_settings
    sha, size, stored = store_local(path, get_settings().data_dir, path.suffix.lower())
    f = File(process_id=process.id, file_id="LOCAL-" + sha[:8], original_name=path.name, rel_path=rel or path.name,
             ext=path.suffix.lower(), size=size, sha256=sha, storage_path=stored, declared_stage=stage,
             doc_stage=stage, stage_source="declared" if stage else None, status="pending", batch=batch)
    db.add(f)
    db.flush()
    return f


@pytest.fixture()
def pipeline_env(settings, db, tmp_path, monkeypatch):
    monkeypatch.setenv("BBOX_ENGINE", "rapidocr")
    monkeypatch.setenv("LLM_RETRIES", "1")
    from inspector import config
    config.get_settings.cache_clear()
    obj = ConstructionObject(object_code="OBJ-TEST", name="Тестовый объект")
    db.add(obj)
    db.flush()
    process = Process(object_pk=obj.id)
    db.add(process)
    db.flush()
    docs = tmp_path / "docs"
    add_file(db, process, make_pdf(docs / "КР1.pdf", [PD_TEXT]), "PD")
    add_file(db, process, make_pdf(docs / "КЖ.pdf", [RD_TEXT]), "RD")
    add_file(db, process, make_scan_pdf(docs / "АОСР 1АФ.pdf", [ID_TEXT]), "ID")
    db.commit()
    return process


def ready_rows(db, process: Process) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.action == "protocol_ready",
                                                  AuditLog.process_id == process.id).order_by(AuditLog.id)))


def run(db, process: Process, fake: FakeLLM, kind: str = "full") -> str | None:
    job = jobs.enqueue(db, process, kind)
    db.commit()
    claimed = jobs.claim_next(db)                      # как worker: queued -> running
    assert claimed.id == job.id
    error = run_job(job.id, llm_transport=fake.transport, ocr_transport=fake.transport)
    db.expire_all()
    jobs.finish(db, db.get(Job, job.id), error)
    return error


def test_full_pipeline_finds_rebar_downgrade(db, pipeline_env):
    process = pipeline_env
    fake = FakeLLM(ocr_text=ID_TEXT)
    assert run(db, process, fake) is None
    process = db.get(Process, process.id)
    assert process.status == S.READY and process.scenario == "FULL" and process.protocol_version == 1
    assert fake.calls["ocr"] >= 1 and fake.calls["page"] == 3 and fake.calls["judge"] >= 1
    scan = db.scalars(select(Page).join(File, Page.file_pk == File.id).where(File.doc_stage == "ID")).one()
    assert scan.text_source == "ocr" and "А240С" in scan.text
    f = db.scalars(select(Finding).where(Finding.param_code == "KR-057", Finding.active.is_(True),
                                         Finding.location != "")).one()
    assert f.status == S.CANDIDATE and f.expected_value == "А400" and f.actual_rd.startswith("А240")
    roles = {e.role: e for e in f.evidence}
    assert set(roles) == {"expected", "actual_rd", "actual_id"}
    assert roles["expected"].bbox_source == "layer" and roles["expected"].bbox is not None
    assert roles["actual_id"].bbox_source in ("rapidocr", "page")
    concrete = db.scalars(select(Finding).where(Finding.param_code == "KR-055", Finding.active.is_(True),
                                                Finding.location != "")).one()
    assert concrete.status == S.NEGATIVE_VERIFIED
    proto = build_protocol(db, process)
    assert proto["summary"]["params_total"] == 132 and proto["summary"]["CANDIDATE"] == 1
    assert {u["status"] for u in proto["upload_status"]} == {"PD_UPLOADED", "RD_UPLOADED", "ID_UPLOADED"}
    sub = build_submission(proto)
    assert validate_submission(sub) == []
    kr = next(c for c in sub["checks"] if c["parameter_code"] == "KR-057")
    assert kr["violation_label"] == "VIOLATION_PRESENT" and len(kr["evidence"]) == 3
    assert db.scalars(select(ProtocolVersion).where(ProtocolVersion.process_id == process.id)).one().version == 1
    progress = process.progress
    assert all(progress[s]["status"] == "done" for s in ("intake", "pages", "ocr", "classify", "documents", "select",
                                                         "extract", "compare", "freesearch", "protocol"))
    ready = ready_rows(db, process)                                         # уведомление инспектора
    assert [(r.user_login, r.user_pk, r.object_code) for r in ready] == [("system", None, "OBJ-TEST")]
    assert ready[0].details["protocol_version"] == 1 and ready[0].details["kind"] == "machine"
    assert ready[0].details["candidate_params"] == 1


def test_incremental_upload_keeps_decision_and_adds_version(db, pipeline_env, tmp_path):
    process = pipeline_env
    fake = FakeLLM(ocr_text=ID_TEXT)
    assert run(db, process, fake) is None
    process = db.get(Process, process.id)
    cand = db.scalars(select(Finding).where(Finding.param_code == "KR-057", Finding.active.is_(True),
                                            Finding.location != "")).one()
    cand.inspector_status = S.CONFIRMED_VIOLATION
    process.status = S.COMPLETED
    process.batch = 2
    add_file(db, process, make_pdf(tmp_path / "more" / "КЖ2.pdf", ["Рабочая документация. Плита\nБетон В25 плиты"]),
             "RD", batch=2)
    db.commit()
    assert run(db, process, fake, kind="incremental") is None
    process = db.get(Process, process.id)
    assert process.protocol_version == 2
    active = db.scalars(select(Finding).where(Finding.param_code == "KR-057", Finding.active.is_(True),
                                              Finding.location != "")).all()
    assert len(active) == 1 and active[0].inspector_status == S.CONFIRMED_VIOLATION   # решение перенесено
    versions = db.scalars(select(ProtocolVersion).where(ProtocolVersion.process_id == process.id)).all()
    assert [v.kind for v in versions] == ["machine", "incremental"]
    assert process.status != S.READY                                        # решения уже есть — не «готов к верификации»
    assert [r.details["protocol_version"] for r in ready_rows(db, process)] == [1]


def test_model_down_fails_process_with_message(db, pipeline_env):
    process = pipeline_env
    error = run(db, process, FakeLLM(fail=True))
    assert error and "модель недоступна" in error
    process = db.get(Process, process.id)
    assert process.status == S.FAILED and "модель недоступна" in process.error
    assert ready_rows(db, process) == []                                    # сбой — не «протокол готов»
    failed = [s for s, v in process.progress.items() if v.get("status") == "failed"]
    assert failed == ["ocr"]


def test_broken_and_unsupported_files_do_not_stop_pipeline(db, pipeline_env, tmp_path):
    process = pipeline_env
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not a pdf")
    add_file(db, process, bad, "ID")
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(b"AC1027 fake")
    add_file(db, process, dwg, "RD")
    db.commit()
    assert run(db, process, FakeLLM(ocr_text=ID_TEXT)) is None
    files = {f.original_name: f for f in db.scalars(select(File).where(File.process_id == process.id))}
    assert files["bad.pdf"].status == "broken" and "PDF" in files["bad.pdf"].error
    assert files["plan.dwg"].status == "unsupported"
    process = db.get(Process, process.id)
    assert process.scenario == "PARTIALLY_LOADED"
    proto = build_protocol(db, process)
    assert {u["stage"]: u["status"] for u in proto["upload_status"]}["ID"] == "ID_PARTIAL"


def test_judge_outage_fails_job_and_keeps_previous_findings(db, pipeline_env, tmp_path):
    process = pipeline_env
    assert run(db, process, FakeLLM(ocr_text=ID_TEXT)) is None
    before = {f.id for f in db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True)))}
    process = db.get(Process, process.id)
    process.batch = 2
    add_file(db, process, make_pdf(tmp_path / "more" / "КЖ2.pdf", ["Рабочая документация\nБетон В25 плиты"]), "RD", batch=2)
    db.commit()
    error = run(db, process, FakeLLM(ocr_text=ID_TEXT, fail_on={"judge"}), kind="incremental")
    assert error and "этап compare" in error
    after = {f.id for f in db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True)))}
    assert after == before                                   # прежний протокол не испорчен
    assert db.get(Process, process.id).status == S.FAILED


EXPL_PD = ("Проектная документация. Архитектурные решения\nЭкспликация помещений\n"
           "1\nТамбур\n12,40\n2\nКоридор\n30,10\n3\nПомещение\n18,00\n4\nСанузел\n4,50\nИтого:\n65,00")
EXPL_RD = ("Рабочая документация АР\nЭкспликация помещений\n"
           "1\nТамбур\n12,40\n2\nКоридор\n30,10\n3\nРаздевалка женская\n18,00\n4\nСанузел\n4,50\nИтого:\n65,00")


def test_freesearch_decision_survives_rerun(db, settings, tmp_path):
    obj = ConstructionObject(object_code="OBJ-EXPL", name="Экспликации")
    db.add(obj)
    db.flush()
    process = Process(object_pk=obj.id)
    db.add(process)
    db.flush()
    add_file(db, process, make_pdf(tmp_path / "АР_ПД.pdf", [EXPL_PD]), "PD")
    add_file(db, process, make_pdf(tmp_path / "АР_РД.pdf", [EXPL_RD]), "RD")
    db.commit()
    assert run(db, process, FakeLLM()) is None
    sus = db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True),
                                           Finding.status == S.SUSPICION)).one()
    assert sus.param_code == "FREE-AR-EXPLICATION-FUNCTION" and sus.actual_rd == "Раздевалка женская"
    sus.status, sus.inspector_status = S.CANDIDATE, S.CONFIRMED_VIOLATION      # привязал и подтвердил
    process = db.get(Process, process.id)
    process.status, process.batch = S.COMPLETED, 2
    add_file(db, process, make_pdf(tmp_path / "ещё.pdf", ["Рабочая документация\nБетон В30"]), "RD", batch=2)
    db.commit()
    assert run(db, process, FakeLLM(), kind="incremental") is None
    again = db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True),
                                             Finding.param_code == "FREE-AR-EXPLICATION-FUNCTION")).one()
    assert again.id != sus.id and again.status == S.CANDIDATE and again.inspector_status == S.CONFIRMED_VIOLATION


def test_page_classifier_version_goes_into_model_version(db, pipeline_env, tmp_path, monkeypatch):
    """Страницы размечала модель — её версия попадает в строку моделей прогона и протокола (трассируемость)."""
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location("tpc", Path(__file__).parent / "test_page_classifier.py")
    tpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tpc)
    model = tmp_path / "model"
    model.mkdir()
    (model / "predict.py").write_text(tpc.PREDICT, encoding="utf-8")
    (model / "manifest.json").write_text(_json.dumps({"version": "fake@v0", "fallback_threshold": 0.5}),
                                         encoding="utf-8")
    monkeypatch.setenv("PAGE_CLASSIFIER_DIR", str(model))
    monkeypatch.setenv("PAGE_CLASSIFIER_DEVICE", "cpu")
    from inspector import config
    from inspector.ml import page_classifier as PC
    config.get_settings.cache_clear()
    PC.load_classifier.cache_clear()
    process = pipeline_env
    assert run(db, process, FakeLLM(ocr_text=ID_TEXT)) is None
    process = db.get(Process, process.id)
    assert process.status == S.READY
    assert process.options["model_version"].endswith(";pagecls=page-cls:fake@v0")
    assert build_protocol(db, process)["traceability"]["model_version"].endswith(";pagecls=page-cls:fake@v0")
    PC.load_classifier.cache_clear()
