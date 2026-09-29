"""Реестр версий обучаемых моделей (model_versions) и журнал итераций (ml_retraining_log), общий для классификатора
страниц (page-cls:…) и отбора страниц (page-router:…): действующая версия, регистрация, очередь итераций для worker,
ручное утверждение и отклонение (Q&A №24: автоматическая публикация запрещена), откат к утверждённой версии."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.models import ModelVersion, RetrainingLog, utcnow


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class Kind:
    prefix: str          # имя версии в реестре: «<prefix>:<версия>»
    log_kind: str        # ml_retraining_log.model_kind
    dataset: str         # model_versions.dataset_version — на чём учится


def version_name(kind: Kind, version: str) -> str:
    return f"{kind.prefix}:{version}"


def deployed(db: Session, kind: Kind) -> ModelVersion | None:
    """Действующая версия: последняя опубликованная утверждённая."""
    return db.scalars(select(ModelVersion).where(ModelVersion.model_version.like(f"{kind.prefix}:%"),
                                                 ModelVersion.approval_status == "APPROVED",
                                                 ModelVersion.deployed_at.is_not(None))
                      .order_by(ModelVersion.deployed_at.desc(), ModelVersion.id.desc())).first()


def register(db: Session, kind: Kind, version: str, artifact: str, status: str, deploy: bool,
             metrics: dict | None = None, approved_by: str = "", rollback_to: str | None = None) -> ModelVersion:
    name = version_name(kind, version)
    mv = db.scalars(select(ModelVersion).where(ModelVersion.model_version == name)).first()
    if mv is None:
        mv = ModelVersion(model_version=name, artifact=artifact, dataset_version=kind.dataset)
        db.add(mv)
    mv.approval_status, mv.approved_by, mv.metrics = status, approved_by, metrics or {}
    if deploy:
        mv.deployed_at, mv.rollback_to = utcnow(), rollback_to
    db.flush()
    return mv


def taken(db: Session, kind: Kind, version: str) -> bool:
    return db.scalars(select(ModelVersion.id).where(ModelVersion.model_version == version_name(kind, version))
                      ).first() is not None


def versions(db: Session, kind: Kind) -> list[dict]:
    cur = deployed(db, kind)
    return [{"model_version": m.model_version, "approval_status": m.approval_status, "approved_by": m.approved_by,
             "deployed_at": m.deployed_at, "rollback_to": m.rollback_to, "metrics": m.metrics, "artifact": m.artifact,
             "is_current": cur is not None and m.id == cur.id}
            for m in db.scalars(select(ModelVersion).where(ModelVersion.model_version.like(f"{kind.prefix}:%"))
                                .order_by(ModelVersion.id.desc()))]


# ------------------------------------------------------------------------------------------------ итерации
def request(db: Session, kind: Kind, requested_by: str) -> RetrainingLog:
    """Поставить итерацию в очередь (одна за раз; её выполнит worker)."""
    busy = db.scalars(select(RetrainingLog).where(RetrainingLog.model_kind == kind.log_kind,
                                                  RetrainingLog.status.in_(("QUEUED", "RUNNING")))).first()
    if busy is not None:
        return busy
    row = RetrainingLog(model_kind=kind.log_kind, status="QUEUED", requested_by=requested_by[:64])
    db.add(row)
    db.flush()
    return row


def queued(db: Session, kind: Kind) -> list[RetrainingLog]:
    return list(db.scalars(select(RetrainingLog).where(RetrainingLog.model_kind == kind.log_kind,
                                                       RetrainingLog.status == "QUEUED").order_by(RetrainingLog.id)))


def fail_interrupted(db: Session, kind: Kind) -> int:
    """На старте worker: итерации, оставшиеся RUNNING, прерваны его падением (итерация идёт внутри worker синхронно) ->
    FAILED. Иначе request вечно возвращал бы такую строку как занятую, и дообучение больше не запускалось бы."""
    rows = list(db.scalars(select(RetrainingLog).where(RetrainingLog.model_kind == kind.log_kind,
                                                       RetrainingLog.status == "RUNNING")))
    for row in rows:
        row.status, row.finished_at = "FAILED", utcnow()
        row.error = "итерация прервана: worker остановлен во время дообучения (модель прежняя)"
    return len(rows)


def log_rows(db: Session, kind: Kind, limit: int = 20) -> list[RetrainingLog]:
    return list(db.scalars(select(RetrainingLog).where(RetrainingLog.model_kind == kind.log_kind)
                           .order_by(RetrainingLog.id.desc()).limit(limit)))


# ------------------------------------------------------------------------------------------------ решения
def approve(db: Session, kind: Kind, version: str, user_login: str) -> ModelVersion:
    """Утвердить черновую версию (подпись ответственного) и опубликовать её."""
    mv = db.scalars(select(ModelVersion).where(ModelVersion.model_version == version)).first()
    if mv is None or not version.startswith(f"{kind.prefix}:"):
        raise ModelError(f"нет такой версии: {version}")
    if mv.approval_status == "REJECTED":
        raise ModelError("версия не прошла приёмку — публиковать нельзя")
    prev = deployed(db, kind)
    mv.approval_status, mv.approved_by = "APPROVED", user_login
    mv.deployed_at, mv.rollback_to = utcnow(), prev.model_version if prev else None
    db.add(RetrainingLog(model_kind=kind.log_kind, status="APPROVED", base_version=prev.model_version if prev else None,
                         new_version=version, artifact=mv.artifact, requested_by=user_login, decided_by=user_login,
                         rule="утверждено ответственным", finished_at=utcnow()))
    db.flush()
    return mv


def reject(db: Session, kind: Kind, version: str, user_login: str) -> ModelVersion:
    """Отклонить версию-кандидата (ручное reject, Q&A №24): её нельзя опубликовать, действующая не меняется."""
    mv = db.scalars(select(ModelVersion).where(ModelVersion.model_version == version)).first()
    if mv is None or not version.startswith(f"{kind.prefix}:") or mv.approval_status != "DRAFT":
        raise ModelError(f"нет версии, ждущей решения: {version}")
    mv.approval_status, mv.approved_by = "REJECTED", user_login
    cur = deployed(db, kind)
    db.add(RetrainingLog(model_kind=kind.log_kind, status="REJECTED", base_version=cur.model_version if cur else None,
                         new_version=version, artifact=mv.artifact, requested_by=user_login, decided_by=user_login,
                         rule="отклонено ответственным", finished_at=utcnow()))
    db.flush()
    return mv


def rollback(db: Session, kind: Kind, version: str, user_login: str) -> ModelVersion:
    """Сделать действующей прежнюю утверждённую версию."""
    mv = db.scalars(select(ModelVersion).where(ModelVersion.model_version == version,
                                               ModelVersion.approval_status == "APPROVED")).first()
    if mv is None or not version.startswith(f"{kind.prefix}:"):
        raise ModelError(f"нет утверждённой версии {version}")
    prev = deployed(db, kind)
    mv.deployed_at, mv.rollback_to = utcnow(), prev.model_version if prev else None
    db.add(RetrainingLog(model_kind=kind.log_kind, status="ROLLBACK", base_version=prev.model_version if prev else None,
                         new_version=version, artifact=mv.artifact, requested_by=user_login, decided_by=user_login,
                         rule="откат к утверждённой версии", finished_at=utcnow()))
    db.flush()
    return mv
