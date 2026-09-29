"""GOLD (ТЗ 9.4): черновик из решений финализированных протоколов → версия с разбиением по объектам и SHA-256 →
выгрузка JSONL; отчёт по дообучению; реестр версий моделей."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inspector import gold
from inspector.api.deps import (API_PREFIX, DbDep, Reader, attachment_header, audit, iso, require, require_media)
from inspector.models import DatasetItem, DatasetVersion, ModelVersion, User
from inspector.protocol import model_version

router = APIRouter(prefix=API_PREFIX, tags=["dataset"])


def _logins(db: Session) -> dict[int, str]:
    return {u.id: u.login for u in db.scalars(select(User))}


def item_out(item: DatasetItem, users: dict[int, str]) -> dict:
    # object_id — как в карточке и сабмите, object_code — как в БД (интерфейс читает любое из двух)
    return {"id": item.id, "finding_pk": item.finding_pk, "evidence_group_id": item.evidence_group_id,
            "object_id": item.object_code, "object_code": item.object_code, "param_code": item.param_code,
            "gold_label": item.gold_label, "reason_code": item.reason_code,
            "expert": users.get(item.expert_pk) if item.expert_pk else None, "expert_pk": item.expert_pk,
            "dataset_version": item.dataset_version, "split": item.split, "created_at": iso(item.created_at),
            "card": item.card}


def version_out(db: Session, dv: DatasetVersion, users: dict[int, str]) -> dict:
    counts = dict(db.execute(select(DatasetItem.split, func.count()).where(DatasetItem.dataset_version == dv.version)
                             .group_by(DatasetItem.split)).all())
    return {"version": dv.version, "items": dv.items, "positives": dv.positives, "negatives": dv.negatives,
            "sha256": dv.sha256, "split_hashes": dv.split_hashes, "split_objects": dv.split_objects,
            "split_counts": {s: counts.get(s, 0) for s in gold.SPLITS},
            "created_by": users.get(dv.created_by) if dv.created_by else None, "created_at": iso(dv.created_at)}


@router.get("/dataset/items", summary="Элементы GOLD: черновик (version=draft) или опубликованной версии")
def list_items(db: DbDep, _: User = Depends(require("dataset")), version: str = "draft",
               limit: int = Query(default=500, ge=1, le=10000), offset: int = Query(default=0, ge=0)) -> dict:
    cond = DatasetItem.dataset_version.is_(None) if version == "draft" else DatasetItem.dataset_version == version
    total = db.scalar(select(func.count()).select_from(DatasetItem).where(cond)) or 0
    users = _logins(db)
    rows = db.scalars(select(DatasetItem).where(cond).order_by(DatasetItem.id).limit(limit).offset(offset))
    return {"items": [item_out(i, users) for i in rows], "total": total}


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str | None = Field(default=None, max_length=40, description="по умолчанию gold-v<N>")


@router.post("/dataset/versions", status_code=201, summary="Опубликовать черновик как новую версию GOLD")
def publish(request: Request, db: DbDep, user: User = Depends(require("dataset")),
            body: PublishIn | None = None) -> dict:
    dv = gold.publish_version(db, user.id, body.version if body else None)
    audit(db, request, user, "dataset_publish",
          details={"version": dv.version, "items": dv.items, "positives": dv.positives, "negatives": dv.negatives,
                   "sha256": dv.sha256})
    db.commit()
    return version_out(db, dv, _logins(db))


@router.get("/dataset/versions", summary="Версии GOLD (новые сверху)")
def list_versions(db: DbDep, _: User = Depends(require("dataset"))) -> dict:
    users = _logins(db)
    items = [version_out(db, dv, users) for dv in db.scalars(select(DatasetVersion).order_by(DatasetVersion.id.desc()))]
    return {"items": items, "total": len(items)}


@router.get("/dataset/versions/{version}/export", response_class=Response,
            summary="JSONL версии (карточка, метка, набор, причина); токен можно передать ?access_token=")
def export_version(version: str, db: DbDep, _: User = Depends(require_media("dataset"))) -> Response:
    data = gold.export_bytes(db, version)
    dv = gold.get_version(db, version)
    return Response(data, media_type="application/x-ndjson",
                    headers={"Content-Disposition": attachment_header(f"{version}.jsonl"),
                             "X-Content-SHA256": dv.sha256})


@router.get("/reports/retraining", summary="Отчёт по дообучению: отклонения по причинам, разделам, параметрам")
def retraining(db: DbDep, _: User = Depends(require("report")),
               days: int = Query(default=7, ge=1, le=3650)) -> dict:
    return gold.retraining_report(db, days)


@router.get("/models", summary="Реестр версий моделей")
def models(_: Reader, db: DbDep) -> dict:
    current = model_version(db)
    items = [{"id": m.id, "model_version": m.model_version, "artifact": m.artifact,
              "dataset_version": m.dataset_version, "metrics": m.metrics, "approval_status": m.approval_status,
              "approved_by": m.approved_by, "deployed_at": iso(m.deployed_at), "rollback_to": m.rollback_to,
              "is_current": m.model_version == current}
             for m in db.scalars(select(ModelVersion).order_by(ModelVersion.id.desc()))]
    return {"items": items, "total": len(items)}
