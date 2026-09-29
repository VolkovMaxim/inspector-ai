"""Классификатор типа страницы с дообучением (inspector/ml/page_classifier.py, api/ml.py): модель размечает уверенные
страницы, неуверенные остаются Qwen; правка разметчика сразу меняет метку и идёт в обучение; итерация дообучения ->
приёмка на замороженном наборе -> публикация или отказ; без набора — утверждение человеком; откат. Модель в тестах —
поддельная, но по тому же интерфейсу (predict / retrain_heads), что у моделей ночного опыта."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inspector import config as config_module
from inspector.ml import page_classifier as PC
from inspector.models import File, ModelVersion, Page, PageCorrection, PageLabel, RetrainingLog

PREDICT = '''
import json, shutil
from pathlib import Path

RULES = [("акт", "АОСР"), ("паспорт", "ПАСПОРТ_ИЗДЕЛИЯ"), ("план", "ПЛАН")]


def load(model_dir, device="cpu"):
    return Classifier(model_dir)


class Classifier:
    def __init__(self, model_dir):
        d = Path(model_dir)
        self.manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        ov = d / "overrides.json"
        self.overrides = json.loads(ov.read_text(encoding="utf-8")) if ov.exists() else {}

    def one(self, page):
        key = f"{Path(page['pdf_path']).name}#{page['page_no']}"
        if key in self.overrides:
            o = self.overrides[key]
            cls, p, first, section = o.get("cls", "ПРОЧЕЕ"), 0.99, o.get("first", True), o.get("section", "НЕТ")
        else:
            cls, p = "ПРОЧЕЕ", 0.3
            for kw, c in RULES:
                if kw in page["text"].lower():
                    cls, p = c, 0.9
                    break
            first, section = True, "НЕТ"
        return {"cls": cls, "cls_p": p, "cls_top3": [[cls, p], ["ТЕКСТ", 0.3], ["ПРОЧЕЕ", 0.1]], "first": first,
                "first_p": 0.9, "section": section, "section_p": 0.9, "quality": "хорошее", "quality_p": 0.99}

    def predict(self, pages):
        return [self.one(p) for p in pages]

    def features(self, pages):
        raise NotImplementedError

    def predict_from_features(self, feats):
        raise NotImplementedError


def retrain_heads(model_dir, corrections, out_dir):
    src, dst = Path(model_dir), Path(out_dir)
    shutil.copytree(src, dst)
    ov_path = src / "overrides.json"
    ov = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    for c in corrections:
        ov[f"{Path(c['page']['pdf_path']).name}#{c['page']['page_no']}"] = {k: c[k] for k in ("cls", "first", "section") if k in c}
    (dst / "overrides.json").write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
    man = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    n = man.get("iteration", 0) + 1
    man.update(version=f"fake@v{n}", iteration=n)
    (dst / "manifest.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    return {"seconds": 0.01, "n_corrections": len(corrections), "version": man["version"]}
'''


@pytest.fixture()
def model_dir(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "predict.py").write_text(PREDICT, encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({"name": "fake", "version": "fake@v0", "fallback_threshold": 0.5}),
                                     encoding="utf-8")
    return d


@pytest.fixture()
def cls_settings(settings, monkeypatch, model_dir):
    monkeypatch.setenv("PAGE_CLASSIFIER_DIR", str(model_dir))
    monkeypatch.setenv("PAGE_CLASSIFIER_DEVICE", "cpu")
    monkeypatch.setenv("PAGE_CLASSIFIER_FIRST_TO_QWEN", "off")     # заглушка ставит «первая» всем страницам
    config_module.get_settings.cache_clear()
    PC.load_classifier.cache_clear()
    s = config_module.get_settings()
    yield s
    config_module.get_settings.cache_clear()
    PC.load_classifier.cache_clear()


def id_pages(db, process_id):
    f = db.scalars(select(File).where(File.process_id == process_id, File.file_id == "F0068")).one()
    f.pdf_path = "/data/АОСР.pdf"
    pages = list(db.scalars(select(Page).where(Page.file_pk == f.id).order_by(Page.page_no)))
    texts = ["Акт освидетельствования скрытых работ № 1", "Приложение 1. Паспорт изделия",
             "План этажа", "что-то неразборчивое"]
    for p, t in zip(pages, texts + [""] * len(pages)):
        p.text = t
    db.flush()
    return f, pages


def test_model_labels_confident_pages_rest_left_for_qwen(cls_settings, db, example_process):
    f, pages = id_pages(db, example_process.id)
    done, stats = PC.classify_with_model(db, cls_settings, pages[:4], {f.id: f})
    assert stats["model"] == "page-cls:fake@v0" and stats["by_model"] == 3 and stats["to_qwen"] == 1
    assert [p.page_class for p in pages[:3]] == ["АОСР", "ПАСПОРТ_ИЗДЕЛИЯ", "ПЛАН"]
    assert pages[3].page_class is None and (f.id, 4) not in done
    assert pages[1].title == "Приложение 1. Паспорт изделия"          # правило «Приложение» видит заголовок
    assert pages[0].other_classes == ["ТЕКСТ"]
    labels = list(db.scalars(select(PageLabel).where(PageLabel.source == "model").order_by(PageLabel.id)))
    assert [lb.confidence for lb in labels] == pytest.approx([0.9, 0.9, 0.9, 0.3])   # неуверенная — только в истории
    assert pages[3].page_class is None
    mv = db.scalars(select(ModelVersion).where(ModelVersion.model_version.like("page-cls:%"))).one()   # начальная
    assert mv.model_version == "page-cls:fake@v0" and mv.deployed_at is not None


def test_no_model_dir_means_qwen_only(settings, db, example_process):
    f, pages = id_pages(db, example_process.id)
    done, stats = PC.classify_with_model(db, settings, pages[:2], {f.id: f})
    assert done == set() and stats["model"] is None


def test_broken_model_sends_everything_to_qwen(cls_settings, db, example_process, model_dir):
    (model_dir / "predict.py").write_text("raise RuntimeError('нет весов')", encoding="utf-8")
    PC.load_classifier.cache_clear()
    f, pages = id_pages(db, example_process.id)
    done, stats = PC.classify_with_model(db, cls_settings, pages[:3], {f.id: f})
    assert done == set() and stats["model_errors"] == 3


@pytest.fixture()
def client(cls_settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_correction_changes_label_and_is_stored(client, db, example_process):
    f, pages = id_pages(db, example_process.id)
    PC.classify_with_model(db, config_module.get_settings(), pages[:4], {f.id: f})
    db.commit()
    h = login(client, "inspector")
    r = client.post(f"/api/v1/processes/{example_process.id}/pages/corrections", headers=h,
                    json={"items": [{"page_pk": pages[2].id, "page_class": "ИСПОЛНИТЕЛЬНАЯ_СХЕМА",
                                     "is_first_page": False}], "comment": "схема к акту"})
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["before"]["page_class"] == "ПЛАН"
    assert r.json()["items"][0]["before"]["source"] == "model"
    db.expire_all()
    assert db.get(Page, pages[2].id).page_class == "ИСПОЛНИТЕЛЬНАЯ_СХЕМА"
    rows = client.get(f"/api/v1/processes/{example_process.id}/pages", headers=h,
                      params={"file_pk": f.id}).json()["items"]
    row = next(x for x in rows if x["page_pk"] == pages[2].id)
    assert row["corrected"] and row["label_source"] == "inspector"
    low = client.get(f"/api/v1/processes/{example_process.id}/pages", headers=h,
                     params={"max_confidence": 0.95, "source": "model"}).json()
    assert {x["page_no"] for x in low["items"]} == {1, 2, 4}   # 4 — неуверенная: пока только метка модели (ждёт Qwen)


def test_correction_rejects_unknown_class_and_foreign_page(client, db, example_process):
    f, pages = id_pages(db, example_process.id)
    db.commit()
    h = login(client, "inspector")
    bad = client.post(f"/api/v1/processes/{example_process.id}/pages/corrections", headers=h,
                      json={"items": [{"page_pk": pages[0].id, "page_class": "НЕТ_ТАКОГО"}]})
    assert bad.status_code == 422
    foreign = client.post(f"/api/v1/processes/{example_process.id}/pages/corrections", headers=h,
                          json={"items": [{"page_pk": 999999, "page_class": "АОСР"}]})
    assert foreign.status_code == 404


@pytest.mark.parametrize("field", ["page_class", "section"])
def test_correction_rejects_empty_class_or_section(client, db, example_process, field):
    """Пустая строка — не тип и не раздел: она не должна стать меткой страницы и обучающей правкой."""
    f, pages = id_pages(db, example_process.id)
    db.commit()
    r = client.post(f"/api/v1/processes/{example_process.id}/pages/corrections", headers=login(client, "inspector"),
                    json={"items": [{"page_pk": pages[0].id, field: ""}]})
    assert r.status_code == 422
    db.expire_all()
    assert getattr(db.get(Page, pages[0].id), field) != "" and db.scalars(select(PageCorrection)).first() is None


def test_inspector_cannot_retrain(client):
    r = client.post("/api/v1/ml/page-classifier/retrain", headers=login(client, "inspector"))
    assert r.status_code == 403


def run_iteration(client, db) -> dict:
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-classifier/retrain", headers=h).status_code == 202
    PC.run_pending(db, config_module.get_settings())
    return client.get("/api/v1/ml/page-classifier", headers=h).json()


def correct(client, db, process_id, page, cls):
    r = client.post(f"/api/v1/processes/{process_id}/pages/corrections", headers=login(client, "inspector"),
                    json={"items": [{"page_pk": page.id, "page_class": cls}]})
    assert r.status_code == 201, r.text


def test_retrain_without_acceptance_set_waits_for_approval_then_rollback(client, db, example_process):
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[3], "ЖУРНАЛ")
    st = run_iteration(client, db)
    it = st["log"][0]
    assert it["status"] == "DRAFT" and it["n_corrections"] == 1 and len(it["corrections_sha256"]) == 64
    assert it["metrics_before"]["corrections"]["cls"]["acc"] == 0.0
    assert it["metrics_after"]["corrections"]["cls"]["acc"] == 1.0
    assert st["current"] == "page-cls:fake@v0" and st["corrections"]["unused"] == 0
    h = login(client, "ml")
    r = client.post("/api/v1/ml/page-classifier/approve", headers=h, json={"version": "page-cls:fake@v1"})
    assert r.status_code == 200 and r.json()["rollback_to"] == "page-cls:fake@v0"
    assert client.get("/api/v1/ml/page-classifier", headers=h).json()["current"] == "page-cls:fake@v1"
    r = client.post("/api/v1/ml/page-classifier/rollback", headers=h, json={"version": "page-cls:fake@v0"})
    assert r.status_code == 200
    st = client.get("/api/v1/ml/page-classifier", headers=h).json()
    assert st["current"] == "page-cls:fake@v0"
    assert [x["status"] for x in st["log"]][:3] == ["ROLLBACK", "APPROVED", "DRAFT"]


def acceptance_file(tmp_path) -> Path:
    """Замороженный набор из одной страницы, которую модель знает (План этажа -> ПЛАН). Правка другой страницы набор не
    задевает; правка этой же страницы на другой класс переучит её — новая версия станет хуже на наборе."""
    path = tmp_path / "acceptance.jsonl"
    item = {"page": {"pdf_path": "/data/АОСР.pdf", "page_no": 3, "width_pt": 595, "height_pt": 842,
                     "text": "План этажа", "words": []}, "cls": "ПЛАН"}
    path.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def with_acceptance(monkeypatch, path: Path):
    monkeypatch.setenv("PAGE_CLS_ACCEPTANCE", str(path))
    config_module.get_settings.cache_clear()


def test_retrain_accepted_when_frozen_set_not_worse(client, db, example_process, tmp_path, monkeypatch):
    with_acceptance(monkeypatch, acceptance_file(tmp_path))
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[3], "ЖУРНАЛ")         # другая страница — набор не задет
    st = run_iteration(client, db)
    it = st["log"][0]
    assert it["status"] == "ACCEPTED" and "замороженном наборе" in it["rule"] and "ждёт утверждения" in it["rule"]
    # Q&A №24: автоматическая публикация запрещена — прошедшая приёмку версия ждёт человека
    assert it["metrics_after"]["frozen"]["cls"]["acc"] == 1.0 and st["current"] == "page-cls:fake@v0"
    assert db.scalars(select(PageCorrection)).one().used_in_version == "page-cls:fake@v1"
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-classifier/approve", headers=h,
                       json={"version": "page-cls:fake@v1"}).status_code == 200
    assert client.get("/api/v1/ml/page-classifier", headers=h).json()["current"] == "page-cls:fake@v1"


def test_candidate_can_be_rejected_by_hand(client, db, example_process):
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[3], "ЖУРНАЛ")
    run_iteration(client, db)
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-classifier/reject", headers=login(client, "inspector"),
                       json={"version": "page-cls:fake@v1"}).status_code == 403
    r = client.post("/api/v1/ml/page-classifier/reject", headers=h, json={"version": "page-cls:fake@v1"})
    assert r.status_code == 200 and r.json() == {"current": "page-cls:fake@v0", "rejected": "page-cls:fake@v1"}
    st = client.get("/api/v1/ml/page-classifier", headers=h).json()
    assert st["log"][0]["status"] == "REJECTED" and st["log"][0]["decided_by"] == "ml"
    assert client.post("/api/v1/ml/page-classifier/approve", headers=h,
                       json={"version": "page-cls:fake@v1"}).status_code == 409
    assert client.post("/api/v1/ml/page-classifier/reject", headers=h,
                       json={"version": "page-cls:fake@v1"}).status_code == 409


def test_retrain_rejected_when_frozen_set_gets_worse(client, db, example_process, tmp_path, monkeypatch):
    with_acceptance(monkeypatch, acceptance_file(tmp_path))
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[2], "СХЕМА")          # переучит страницу из набора: ПЛАН -> СХЕМА
    st = run_iteration(client, db)
    assert st["log"][0]["status"] == "REJECTED" and st["current"] == "page-cls:fake@v0"
    assert client.post("/api/v1/ml/page-classifier/approve", headers=login(client, "ml"),
                       json={"version": "page-cls:fake@v1"}).status_code == 409


def current_artifact(st: dict) -> str:
    return next(v["artifact"] for v in st["versions"] if v["is_current"])


def test_second_draft_from_same_base_is_its_own_version(client, db, example_process):
    """Имя версии даёт retrain_heads модели (здесь «итерация базы + 1»): пока черновик не утверждён, база та же, и вторая
    итерация называется так же. Утвердить нужно именно ту модель, что обучена второй итерацией, на обеих правках."""
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[3], "ЖУРНАЛ")
    first = run_iteration(client, db)["log"][0]
    correct(client, db, example_process.id, pages[0], "АКТ_ПРОЧИЙ")
    second = run_iteration(client, db)["log"][0]
    assert second["status"] == "DRAFT" and second["new_version"] != first["new_version"]
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-classifier/approve", headers=h,
                       json={"version": second["new_version"]}).status_code == 200
    st = client.get("/api/v1/ml/page-classifier", headers=h).json()
    assert st["current"] == second["new_version"] and current_artifact(st) == second["artifact"]


def test_accepted_iteration_publishes_its_own_model_not_an_earlier_rejected_one(client, db, example_process, tmp_path,
                                                                               monkeypatch):
    with_acceptance(monkeypatch, acceptance_file(tmp_path))
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[2], "СХЕМА")          # итерация 1 портит набор -> отказ
    rejected = run_iteration(client, db)["log"][0]
    correct(client, db, example_process.id, pages[2], "ПЛАН")           # итерация 2 возвращает как было -> приёмка
    st = run_iteration(client, db)
    accepted = st["log"][0]
    assert rejected["status"] == "REJECTED" and accepted["status"] == "ACCEPTED"
    assert client.post("/api/v1/ml/page-classifier/approve", headers=login(client, "ml"),   # публикует человек
                       json={"version": accepted["new_version"]}).status_code == 200
    st = client.get("/api/v1/ml/page-classifier", headers=login(client, "ml")).json()
    assert st["current"] == accepted["new_version"] and current_artifact(st) == accepted["artifact"]
    assert {v["model_version"]: v["approval_status"] for v in st["versions"]}[rejected["new_version"]] == "REJECTED"


def test_rejected_iteration_does_not_overwrite_earlier_accepted_version(client, db, example_process, tmp_path,
                                                                        monkeypatch):
    with_acceptance(monkeypatch, acceptance_file(tmp_path))
    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[3], "ЖУРНАЛ")
    accepted = run_iteration(client, db)["log"][0]
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-classifier/approve", headers=h,
                       json={"version": accepted["new_version"]}).status_code == 200
    assert client.post("/api/v1/ml/page-classifier/rollback", headers=h,
                       json={"version": "page-cls:fake@v0"}).status_code == 200
    correct(client, db, example_process.id, pages[2], "СХЕМА")          # итерация 2 от той же базы v0 -> отказ
    assert run_iteration(client, db)["log"][0]["status"] == "REJECTED"
    r = client.post("/api/v1/ml/page-classifier/rollback", headers=h, json={"version": accepted["new_version"]})
    assert r.status_code == 200 and r.json()["current"] == accepted["new_version"]


def test_worker_restart_frees_iteration_left_running(client, db, example_process):
    """Итерация идёт внутри worker синхронно. Если его убили посреди неё (перезапуск контейнера, OOM), строка журнала
    осталась RUNNING, и «Дообучить» навсегда возвращало бы её же как занятую. Старт worker закрывает такую строку."""
    from inspector import worker

    f, pages = id_pages(db, example_process.id)
    db.commit()
    correct(client, db, example_process.id, pages[3], "ЖУРНАЛ")
    db.add(RetrainingLog(status="RUNNING", requested_by="ml"))
    db.commit()
    assert worker.recover_retraining() == 1
    st = run_iteration(client, db)
    assert [x["status"] for x in st["log"]][:2] == ["DRAFT", "FAILED"]
    assert "прервана" in st["log"][1]["error"] and st["log"][1]["finished_at"] is not None


def test_rollback_accepts_only_page_classifier_versions(client, db):
    """В реестре моделей есть и версии LLM (утверждены при старте); «откат классификатора» на них — ошибка ввода, а не
    успех с чужой версией в ответе и испорченной записью LLM."""
    db.add(ModelVersion(model_version="llm=qwen;ocr=qwen", artifact="open weights", dataset_version="-",
                        approval_status="APPROVED"))
    db.commit()
    r = client.post("/api/v1/ml/page-classifier/rollback", headers=login(client, "ml"),
                    json={"version": "llm=qwen;ocr=qwen"})
    assert r.status_code == 409
    db.expire_all()
    llm = db.scalars(select(ModelVersion).where(ModelVersion.model_version == "llm=qwen;ocr=qwen")).one()
    assert llm.deployed_at is None and llm.rollback_to is None


def test_retrain_refused_until_set_loaded_and_corrected(client, db, example_process):
    """Дообучение — только на загруженном наборе и после ручных правок: иначе отказ сразу, в очередь ничего не идёт."""
    h = login(client, "ml")
    r = client.post("/api/v1/ml/page-classifier/retrain", headers=h)
    assert r.status_code == 409 and "набор документов не загружен" in r.json()["error"]["message"]
    f, pages = id_pages(db, example_process.id)
    PC.classify_with_model(db, config_module.get_settings(), pages[:4], {f.id: f})
    db.commit()
    st = client.get("/api/v1/ml/page-classifier", headers=h).json()
    assert st["dataset"] == {"pages": 3, "corrections": 0}
    r = client.post("/api/v1/ml/page-classifier/retrain", headers=h)
    assert r.status_code == 409 and "правок разметчиков нет" in r.json()["error"]["message"]
    assert db.scalars(select(RetrainingLog)).first() is None


def test_iteration_trains_on_all_labeled_pages_corrected_ones_weighted(client, db, example_process, monkeypatch):
    """В дообучение идут все размеченные страницы загруженных проверок: исправленная — с меткой правки и весом правки
    (его задаёт модель), остальные — с текущей меткой и весом 1."""
    f, pages = id_pages(db, example_process.id)
    PC.classify_with_model(db, config_module.get_settings(), pages[:4], {f.id: f})
    db.commit()
    correct(client, db, example_process.id, pages[0], "АКТ_ПРОЧИЙ")
    got = {}
    real = PC.training_items

    def spy(*a, **kw):
        got["items"] = real(*a, **kw)
        return got["items"]
    monkeypatch.setattr(PC, "training_items", spy)
    it = run_iteration(client, db)["log"][0]
    assert it["status"] == "DRAFT" and it["n_corrections"] == 1
    assert it["metrics_after"]["train"] == {"pages": 3, "corrected": 1}
    by_page = {x["page"]["page_no"]: x for x in got["items"]}
    assert by_page[1]["cls"] == "АКТ_ПРОЧИЙ" and "weight" not in by_page[1]
    assert by_page[2]["cls"] == "ПАСПОРТ_ИЗДЕЛИЯ" and by_page[2]["weight"] == 1.0
    assert 4 not in by_page                                   # без метки (ждёт Qwen) — не в наборе


def test_pages_list_filters_and_taxonomy(client, db, example_process):
    f, pages = id_pages(db, example_process.id)
    PC.classify_with_model(db, config_module.get_settings(), pages[:4], {f.id: f})
    db.commit()
    h = login(client, "inspector")
    url = f"/api/v1/processes/{example_process.id}/pages"
    client.post(f"{url}/corrections", headers=h, json={"items": [{"page_pk": pages[0].id, "page_class": "АКТ_ПРОЧИЙ"}]})
    fixed = client.get(url, headers=h, params={"corrected": True}).json()["items"]
    assert [x["page_no"] for x in fixed] == [1]
    assert fixed[0]["model_class"] == "АОСР" and fixed[0]["model_confidence"] == pytest.approx(0.9)
    assert fixed[0]["label_source"] == "inspector"
    total = client.get(url, headers=h).json()["total"]
    unlabeled = client.get(url, headers=h, params={"source": "none", "limit": 5000}).json()
    assert unlabeled["total"] == total - 4 and all(x["label_source"] is None for x in unlabeled["items"])
    tax = client.get("/api/v1/ml/page-taxonomy", headers=h).json()
    codes = {c["code"]: c["group"] for c in tax["classes"]}
    assert codes["АОСР"] == "id" and codes["ПЛАН"] == "sheet" and codes["ПУСТАЯ"] == "other"
    assert "НЕТ" in tax["sections"]


def test_status_lists_confusions(client, db, example_process):
    f, pages = id_pages(db, example_process.id)
    PC.classify_with_model(db, config_module.get_settings(), pages[:3], {f.id: f})
    db.commit()
    h = login(client, "inspector")
    client.post(f"/api/v1/processes/{example_process.id}/pages/corrections", headers=h,
                json={"items": [{"page_pk": pages[2].id, "page_class": "ИСПОЛНИТЕЛЬНАЯ_СХЕМА"},
                                {"page_pk": pages[1].id, "is_first_page": False}]})
    st = client.get("/api/v1/ml/page-classifier", headers=login(client, "ml")).json()
    assert st["corrections"]["total"] == 2
    assert st["corrections"]["confusions"] == [{"from": "ПЛАН", "to": "ИСПОЛНИТЕЛЬНАЯ_СХЕМА", "n": 1}]


def test_first_pages_of_id_go_to_qwen_for_the_title(cls_settings, db, example_process, monkeypatch):
    """У модели нет заголовка, а номер акта ИД берётся из заголовка Qwen: первая страница ИД уходит к Qwen, даже если
    модель уверена; не первые и первые страницы ПД/РД модель размечает сама."""
    f, pages = id_pages(db, example_process.id)
    ov = {"АОСР.pdf#1": {"cls": "АОСР", "first": True}, "АОСР.pdf#2": {"cls": "ПАСПОРТ_ИЗДЕЛИЯ", "first": False}}
    (Path(cls_settings.page_classifier_dir) / "overrides.json").write_text(json.dumps(ov, ensure_ascii=False),
                                                                          encoding="utf-8")
    monkeypatch.setattr(cls_settings, "page_classifier_first_to_qwen", "id")
    PC.load_classifier.cache_clear()
    done, stats = PC.classify_with_model(db, cls_settings, pages[:2], {f.id: f})
    assert f.doc_stage == "ID" and done == {(f.id, 2)} and stats["first_to_qwen"] == 1
    assert pages[0].page_class is None and pages[1].page_class == "ПАСПОРТ_ИЗДЕЛИЯ"
    hist = {lb.page_pk: lb for lb in db.scalars(select(PageLabel).where(PageLabel.source == "model"))}
    assert hist[pages[0].id].page_class == "АОСР" and hist[pages[0].id].confidence == pytest.approx(0.99)
    f.doc_stage = "RD"                                                   # в РД заголовок не нужен — модель сама
    PC.load_classifier.cache_clear()
    done, stats = PC.classify_with_model(db, cls_settings, pages[:1], {f.id: f})
    assert done == {(f.id, 1)} and stats["first_to_qwen"] == 0


def test_hybrid_threshold_from_manifest_metrics(settings, model_dir):
    man = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    man.pop("fallback_threshold")
    man["metrics"] = {"hybrid_threshold": 0.86}
    (model_dir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    assert PC.threshold(settings, model_dir) == pytest.approx(0.86)


def test_acceptance_set_resolves_relative_pdf_paths(settings, tmp_path, monkeypatch):
    """Набор приёмки переносим: относительный путь страницы — от каталога набора (контейнер на другом хосте)."""
    (tmp_path / "pages").mkdir()
    path = tmp_path / "page409.jsonl"
    path.write_text(json.dumps({"page": {"pdf_path": "pages/p001.pdf", "page_no": 1}, "cls": "ПЛАН"}) + "\n"
                    + json.dumps({"page": {"pdf_path": "/abs/x.pdf", "page_no": 3}, "cls": "АОСР"}) + "\n",
                    encoding="utf-8")
    monkeypatch.setenv("PAGE_CLS_ACCEPTANCE", str(path))
    config_module.get_settings.cache_clear()
    items = PC.load_acceptance(config_module.get_settings())
    assert [it["page"]["pdf_path"] for it in items] == [str(tmp_path / "pages/p001.pdf"), "/abs/x.pdf"]
    config_module.get_settings.cache_clear()

