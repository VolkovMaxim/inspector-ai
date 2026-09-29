"""Очередь задач конвейера в БД (вместо RabbitMQ): API ставит задачу, worker забирает."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.models import Job, Process, utcnow

STALE_AFTER = dt.timedelta(minutes=15)       # нет heartbeat дольше — задача считается упавшей и перезапускается
MAX_ATTEMPTS = 3                              # ТЗ 9.1: повтор обработки до 2 раз


class JobConflict(RuntimeError):
    pass


def enqueue(session: Session, process: Process, kind: str = "full") -> Job:
    """Поставить конвейер процесса в очередь. kind: full | incremental (новая партия файлов process.batch)."""
    if process.status == S.FINALIZED:
        raise JobConflict("Протокол финализирован: новая проверка — отдельным процессом")
    active = session.scalars(select(Job).where(Job.process_id == process.id,
                                               Job.status.in_(("queued", "running")))).first()
    if active is not None:
        raise JobConflict("Проверка уже выполняется")
    job = Job(process_id=process.id, kind=kind, batch=process.batch)
    session.add(job)
    process.status = S.PARSING
    process.error = None
    session.flush()
    return job


def claim_next(session: Session) -> Job | None:
    """Забрать следующую задачу (или зависшую). На Postgres — SKIP LOCKED, чтобы несколько worker не делили одну."""
    now = utcnow()
    stale = session.scalars(select(Job).where(Job.status == "running", Job.heartbeat_at < now - STALE_AFTER)).all()
    for job in stale:
        job.status = "queued" if job.attempts < MAX_ATTEMPTS else "failed"
        job.error = (job.error or "") + " | перезапуск: нет heartbeat"
    q = select(Job).where(Job.status == "queued").order_by(Job.id).limit(1)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    job = session.scalars(q).first()
    if job is None:
        session.commit()
        return None
    job.status, job.started_at, job.heartbeat_at = "running", now, now
    job.attempts += 1
    session.commit()
    return job


def heartbeat(session: Session, job_id: int) -> None:
    session.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=utcnow()))
    session.commit()


def finish(session: Session, job: Job, error: str | None = None) -> None:
    job.status = "failed" if error else "done"
    job.error = error
    job.finished_at = utcnow()
    session.commit()
