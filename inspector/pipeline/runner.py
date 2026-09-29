"""Оркестратор конвейера: задача из очереди -> этапы 1–10 -> снимок протокола. Прогресс каждого этапа пишется в
processes.progress ({этап: {status, done, total, seconds, stats}}), он же служит heartbeat задачи. Ошибка этапа ->
процесс FAILED с понятным сообщением (задачу worker перезапустит до 3 раз)."""
from __future__ import annotations

import logging
import time
import traceback

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.config import Settings, get_settings
from inspector.db import session_factory
from inspector.llm import llm_client, ocr_client
from inspector.models import Document, Extraction, File, Finding, Job, Page, Param, Process, utcnow
from inspector.pipeline.classify import run_classify
from inspector.pipeline.compare import run_compare
from inspector.pipeline.documents import run_documents
from inspector.pipeline.extract import run_extract
from inspector.pipeline.freesearch import run_freesearch
from inspector.pipeline.intake import run_intake
from inspector.pipeline.ocr import run_ocr, run_reocr
from inspector.pipeline.pages import run_pages
from inspector.ml.page_router import current_model as current_router
from inspector.pipeline.router import select_fused
from inspector.pipeline.select import PageInfo, select_candidates
from inspector.protocol import save_snapshot

log = logging.getLogger("inspector.runner")
STAGES = ("intake", "pages", "ocr", "classify", "documents", "select", "reocr", "extract", "compare", "freesearch",
          "protocol")


class Progress:
    """Прогресс этапов в БД (раз в секунду или по завершении); заодно heartbeat задачи. Пишет через ту же сессию
    и коммитит: на SQLite вторая пишущая сессия в том же потоке упёрлась бы в блокировку."""

    def __init__(self, db: Session, process_id: str, job_id: int | None):
        self.db, self.process_id, self.job_id = db, process_id, job_id
        self.state: dict = {}
        self.current: str | None = None
        self._t0 = 0.0
        self._last = 0.0

    def start(self, stage: str) -> None:
        self.current, self._t0 = stage, time.time()
        self.state[stage] = {"status": "running", "done": 0, "total": None, "seconds": 0.0}
        self.write(force=True)

    def __call__(self, done: int, total: int | None) -> None:
        st = self.state[self.current]
        st["done"], st["total"], st["seconds"] = done, total, round(time.time() - self._t0, 1)
        self.write()

    def finish(self, stats: dict | None = None, status: str = "done") -> None:
        st = self.state[self.current]
        st["status"], st["seconds"] = status, round(time.time() - self._t0, 1)
        if stats is not None:
            st["stats"] = stats
        self.write(force=True)

    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last < 1.0:
            return
        self._last = now
        self.db.execute(update(Process).where(Process.id == self.process_id)
                        .values(progress=dict(self.state), stage=self.current, updated_at=utcnow()))
        if self.job_id is not None:
            self.db.execute(update(Job).where(Job.id == self.job_id).values(heartbeat_at=utcnow()))
        self.db.commit()


def _check_model_alive(name: str, stats: dict) -> None:
    """Если на этапе с моделью не прошёл ни один вызов — модель недоступна: это сбой проверки, а не пустой протокол."""
    ok = {"ocr": "recognized", "classify": "classified"}.get(name)
    if ok and stats.get("errors") and not stats.get(ok) and not stats.get("from_cache") and not stats.get("by_model"):
        raise RuntimeError(f"модель недоступна: этап {name}, ошибок {stats['errors']}, успешных ответов 0")
    if name == "extract" and stats.get("calls") and stats.get("errors") == stats.get("calls"):
        raise RuntimeError(f"модель недоступна: этап extract, ошибок {stats['errors']} из {stats['calls']}")
    if name == "compare" and stats.get("judged") and stats.get("judge_errors") == stats.get("judged"):
        raise RuntimeError(f"модель недоступна: этап compare, ошибок {stats['judge_errors']} из {stats['judged']}")


def _page_infos(db: Session, process: Process, batch: int | None) -> list[PageInfo]:
    files = {f.id: f for f in db.scalars(select(File).where(File.process_id == process.id, File.status == "ok",
                                                            File.doc_stage.in_(("PD", "RD", "ID"))))}
    if batch is not None:
        files = {k: f for k, f in files.items() if f.batch == batch}
    docs = {d.id: d for d in db.scalars(select(Document).where(Document.process_id == process.id))}
    out = []
    for p in db.scalars(select(Page).where(Page.file_pk.in_(list(files) or [-1])).order_by(Page.file_pk, Page.page_no)):
        d = docs.get(p.document_pk)
        if d is not None and not d.is_actual:
            continue
        out.append(PageInfo(p.file_pk, p.page_no, files[p.file_pk].doc_stage, p.text or "", p.page_class,
                            list(p.other_classes or []), p.section, d.doc_type if d else None))
    return out


def _params(db: Session) -> dict[str, dict]:
    return {p.code: {"code": p.code, "parameter_name": p.parameter_name, "unit": p.unit, "source_pd": p.source_pd,
                     "source_rd": p.source_rd, "source_id": p.source_id}
            for p in db.scalars(select(Param).where(Param.is_active.is_(True)))}


def _scenario(db: Session, process: Process) -> str:
    files = list(db.scalars(select(File).where(File.process_id == process.id)))
    present = {f.doc_stage for f in files if f.status == "ok" and f.doc_stage}
    partial = any(f.status in ("broken", "unsupported") and f.doc_stage for f in files)
    return S.scenario(present, partial=partial and bool(present))


def run_pipeline(db: Session, process: Process, batch: int, kind: str, settings: Settings,
                 llm_transport: httpx.AsyncBaseTransport | None = None,
                 ocr_transport: httpx.AsyncBaseTransport | None = None, job_id: int | None = None) -> dict:
    """Все этапы для партии batch. kind=full — сравнение по всем параметрам; incremental — только по затронутым."""
    progress = Progress(db, process.id, job_id)
    progress.state = dict(process.progress or {}) if kind == "incremental" else {}
    summary: dict = {}
    process.status, process.error = S.PARSING, None
    from inspector.protocol import model_version
    process.options = {**(process.options or {}), "model_version": model_version(db)}   # фактические модели прогона
    db.commit()

    def stage(name: str, fn):
        progress.start(name)
        stats = fn()
        try:
            _check_model_alive(name, stats)
        except RuntimeError:
            db.rollback()          # ничего из этапа с недоступной моделью не сохраняем
            raise
        db.commit()
        progress.finish(stats)
        summary[name] = stats
        log.info("stage done", extra={"stage": name, "process_id": process.id, "stats": stats})
        return stats

    stage("intake", lambda: run_intake(db, process, batch, settings, progress))
    stage("pages", lambda: run_pages(db, process, batch, settings, progress))
    stage("ocr", lambda: run_ocr(db, process, batch, settings,
                                 ocr_client(settings, ocr_transport) if settings.ocr_engine == "vlm" else None, progress))
    cls_stats = stage("classify", lambda: run_classify(db, process, batch, settings,
                                                       llm_client(settings, llm_transport, settings.classify_concurrency),
                                                       progress))
    mv = (process.options or {}).get("model_version") or ""
    if cls_stats.get("model") and ";pagecls=" not in mv:   # версия классификатора страниц — в строку моделей прогона
        process.options = {**process.options, "model_version": f"{mv};pagecls={cls_stats['model']}"}
        db.commit()
    stage("documents", lambda: run_documents(db, process, batch, settings, progress))
    params = _params(db)
    candidates: list = []

    def do_select():
        nonlocal candidates
        pages = _page_infos(db, process, batch if kind == "incremental" else None)
        idf_pages = _page_infos(db, process, None) if kind == "incremental" else None
        router = current_router(db, settings)                   # действующая версия (после дообучения — утверждённая)
        if router is not None:
            candidates = select_fused(pages, list(params.values()), settings.top_k_pages, idf_pages, router)
        else:
            candidates = select_candidates(pages, list(params.values()), settings.top_k_pages, idf_pages)
        return {"pages": len(pages), "candidates": len(candidates), "params": len({c.param_code for c in candidates}),
                "mode": f"fused:{router.version}" if router is not None else "lexical", "top_k": settings.top_k_pages}
    stage("select", do_select)
    if settings.ocr_engine == "rapidocr" and settings.reocr_selected:
        stage("reocr", lambda: run_reocr(db, process, candidates, settings, ocr_client(settings, ocr_transport),
                                         progress))
    stage("extract", lambda: run_extract(db, process, candidates, params, settings, llm_client(settings, llm_transport),
                                         batch, progress))
    version = (process.protocol_version or 0) + 1
    if kind == "incremental":
        affected = {c for (c,) in db.execute(select(Extraction.param_code).where(
            Extraction.process_id == process.id, Extraction.batch == batch).distinct()).all()}
    else:
        affected = None
    stage("compare", lambda: run_compare(db, process, settings, llm_client(settings, llm_transport), affected, version,
                                         progress))
    stage("freesearch", lambda: run_freesearch(db, process, version))

    def do_protocol():
        process.scenario = _scenario(db, process)
        decided = db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True),
                                                   Finding.inspector_status != S.INSPECTOR_PENDING)).first()
        pending = db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True),
                                                   Finding.status == S.CANDIDATE,
                                                   Finding.inspector_status == S.INSPECTOR_PENDING)).all()
        process.status = S.READY if decided is None else S.process_status_after_decision(S.VERIFYING, len(pending))
        pv = save_snapshot(db, process, "machine" if kind == "full" else "incremental")
        _render_pdf_snapshot(db, process, pv)
        if process.status == S.READY:        # серверная часть уведомления инспектора: протокол ждёт верификации
            from inspector.models import AuditLog

            db.add(AuditLog(user_login="system", action="protocol_ready", process_id=process.id,
                            object_code=process.object.object_code,
                            details={"protocol_version": pv.version, "kind": pv.kind,
                                     "candidate_params": (pv.summary or {}).get(S.CANDIDATE, 0),
                                     "note": "уведомление инспектора: протокол готов к верификации"}))
        return {"version": pv.version, "status": process.status, "scenario": process.scenario}
    stage("protocol", do_protocol)
    process.stage = None
    db.commit()
    return summary


def _render_pdf_snapshot(db: Session, process: Process, pv) -> None:
    """PDF протокола рядом с JSON (если модуль выгрузок доступен); сбой рендера не валит конвейер."""
    try:
        import json
        from pathlib import Path

        from inspector.render import render_pdf

        proto = json.loads(Path(pv.json_path).read_text(encoding="utf-8"))
        path = Path(pv.json_path).with_suffix(".pdf")
        path.write_bytes(render_pdf(proto))
        pv.pdf_path = str(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("pdf snapshot skipped", extra={"process_id": process.id, "error": str(exc)[:200]})


def run_job(job_id: int, llm_transport: httpx.AsyncBaseTransport | None = None,
            ocr_transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Выполнить задачу очереди. -> текст ошибки или None. Статус задачи выставляет вызывающий (worker)."""
    settings = get_settings()
    db = session_factory()()
    try:
        job = db.get(Job, job_id)
        process = db.get(Process, job.process_id)
        run_pipeline(db, process, job.batch, job.kind, settings, llm_transport, ocr_transport, job_id)
        return None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        err = f"{type(exc).__name__}: {exc}"[:1000]
        log.error("pipeline failed", extra={"job_id": job_id, "error": err, "trace": traceback.format_exc()[-3000:]})
        process = db.get(Process, db.get(Job, job_id).process_id)
        stage = process.stage
        progress = dict(process.progress or {})
        if stage:
            progress[stage] = {**progress.get(stage, {}), "status": "failed", "error": err}
        db.execute(update(Process).where(Process.id == process.id).values(
            status=S.FAILED, error=f"Этап {stage or '?'}: {err}", progress=progress))
        db.commit()
        return err
    finally:
        db.close()
