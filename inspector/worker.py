"""Worker: забирает задачи конвейера из очереди в БД и выполняет их; повторяет отправки в ИАИС РиН по расписанию.
Запуск: python -m inspector.worker"""
from __future__ import annotations

import logging
import signal
import time

from sqlalchemy import select

from inspector import jobs, maintenance
from inspector.config import get_settings
from inspector.db import init_engine, session_factory, session_scope
from inspector.models import AuditLog, Job, SyncAttempt, utcnow

log = logging.getLogger("inspector.worker")
_stop = False


def _setup_logging() -> None:
    try:
        from inspector.logging_setup import setup_logging

        setup_logging(level=get_settings().log_level, service="inspector-worker")
    except Exception:  # noqa: BLE001 — модуль логирования API может быть ещё не готов
        logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def run_one() -> bool:
    """Одна задача, если есть. -> была ли задача."""
    from inspector.pipeline.runner import run_job

    db = session_factory()()
    try:
        job = jobs.claim_next(db)
        if job is None:
            return False
        job_id, attempts = job.id, job.attempts
    finally:
        db.close()
    log.info("job started", extra={"job_id": job_id, "attempt": attempts})
    error = run_job(job_id)
    with session_scope() as db:
        job = db.get(Job, job_id)
        if error and job.attempts < jobs.MAX_ATTEMPTS:
            job.status, job.error = "queued", f"попытка {job.attempts}: {error}"
            log.warning("job requeued", extra={"job_id": job_id, "error": error})
        else:
            jobs.finish(db, job, error)
            if error:
                db.add(AuditLog(user_login="system", action="PIPELINE_FAILED", process_id=job.process_id,
                                details={"job_id": job_id, "error": error, "attempts": job.attempts,
                                         "note": "уведомление администратора: проверка не выполнена"}))
    log.info("job finished", extra={"job_id": job_id, "error": error})
    return True


def rin_retries() -> int:
    """Запланированные повторы отправки в ИАИС РиН (ТЗ 9.6: 3 попытки, 1/5/15 мин). Логику отправки даёт API."""
    try:
        from inspector.api.integration import retry_sync  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0
    done = 0
    with session_scope() as db:
        q = select(SyncAttempt).where(SyncAttempt.status == "SCHEDULED", SyncAttempt.next_try_at <= utcnow())
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)      # несколько worker не отправят одно и то же дважды
        due = list(db.scalars(q))
        for attempt in due:
            try:
                retry_sync(db, attempt)
                done += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("rin retry failed", extra={"process_id": attempt.process_id, "error": str(exc)[:200]})
    return done


def retrain_pending() -> int:
    """Итерации дообучения, поставленные в очередь: классификатор страниц (inspector/ml/page_classifier.py) и отбор
    страниц (inspector/ml/page_router.py)."""
    from inspector.ml import page_classifier, page_router

    with session_scope() as db:
        return page_classifier.run_pending(db, get_settings()) + page_router.run_pending(db, get_settings())


def recover_retraining() -> int:
    """Старт worker: итерации дообучения, прерванные его прошлой остановкой (RUNNING), -> FAILED."""
    from inspector.ml import page_classifier, page_router

    with session_scope() as db:
        return page_classifier.fail_interrupted(db) + page_router.fail_interrupted(db)


def main() -> None:
    global _stop
    _setup_logging()
    settings = get_settings()
    init_engine()
    with session_scope() as db:
        from inspector.seed import seed_all

        seed_all(db, settings)
    recover_retraining()

    def stop(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker started", extra={"llm": settings.llm_url, "ocr": settings.ocr_url_eff})
    while not _stop:
        try:
            busy = run_one()
            rin_retries()
            busy = retrain_pending() > 0 or busy
            maintenance.tick(settings)
        except Exception as exc:  # noqa: BLE001 — цикл worker не должен умирать
            log.exception("worker loop error: %s", exc)
            busy = False
        if not busy:
            time.sleep(settings.worker_poll_s)


if __name__ == "__main__":
    main()
