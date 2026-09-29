"""Нормативная база (матрица параметров: чтение всем, правка — admin) и журнал аудита."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, or_, select

from inspector.api.deps import API_PREFIX, ApiError, CurrentUser, DbDep, Reader, audit, has_perm, iso, require
from inspector.models import AuditLog, Param, User

router = APIRouter(prefix=API_PREFIX, tags=["admin"])

PARAM_FIELDS = ("code", "section", "parameter_name", "unit", "source_pd", "source_rd", "source_id", "trigger_logic",
                "criticality", "review_priority", "data_type", "min_value", "max_value", "regex_pattern",
                "sp_reference", "gost_reference", "fz_reference", "other_normative", "is_active")
TEXT_FIELDS = ("trigger_logic", "sp_reference", "gost_reference", "fz_reference", "other_normative")


def param_out(p: Param) -> dict:
    out = {f: getattr(p, f) for f in PARAM_FIELDS}
    out["updated_at"] = iso(p.updated_at)
    return out


def _get_param(db, code: str) -> Param:
    p = db.scalars(select(Param).where(Param.code == code)).first()
    if p is None:
        raise ApiError(404, "PARAM_NOT_FOUND", "Параметр не найден", details={"code": code})
    return p


@router.get("/params", summary="Матрица контроля (нормативная база)")
def list_params(_: Reader, db: DbDep, section: str | None = None, q: str | None = None,
                active: bool | None = None) -> dict:
    stmt = select(Param).order_by(Param.id)
    if section:
        stmt = stmt.where(Param.section == section)
    if active is not None:
        stmt = stmt.where(Param.is_active.is_(active))
    items = [param_out(p) for p in db.scalars(stmt)]
    if q:
        needle = q.strip().casefold()
        items = [p for p in items if needle in p["code"].casefold() or needle in p["parameter_name"].casefold()]
    return {"items": items, "total": len(items)}


@router.get("/params/{code}", summary="Параметр матрицы")
def get_param(code: str, _: Reader, db: DbDep) -> dict:
    return param_out(_get_param(db, code))


class ParamPatch(BaseModel):
    """Правятся только триггер, пороги, активность, приоритет и ссылки на нормы (ТЗ 8.1)."""
    model_config = ConfigDict(extra="forbid")
    trigger_logic: str | None = Field(default=None, max_length=5000)
    min_value: float | None = None
    max_value: float | None = None
    is_active: StrictBool | None = None
    review_priority: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    sp_reference: str | None = Field(default=None, max_length=5000)
    gost_reference: str | None = Field(default=None, max_length=5000)
    fz_reference: str | None = Field(default=None, max_length=5000)
    other_normative: str | None = Field(default=None, max_length=5000)


@router.patch("/params/{code}", summary="Изменить параметр (admin)")
def patch_param(code: str, body: ParamPatch, request: Request, db: DbDep,
                user: User = Depends(require("params"))) -> dict:
    p = _get_param(db, code)
    sent = body.model_dump(exclude_unset=True)
    for field in ("is_active", "review_priority"):
        if field in sent and sent[field] is None:
            raise ApiError(422, "VALIDATION_ERROR", f"Поле {field} не может быть пустым", details=[{"loc": [field]}])
    new = {k: ("" if (k in TEXT_FIELDS and v is None) else v) for k, v in sent.items()}
    lo = new.get("min_value", p.min_value)
    hi = new.get("max_value", p.max_value)
    if lo is not None and hi is not None and lo > hi:
        raise ApiError(422, "INVALID_RANGE", "Минимум больше максимума", details={"min_value": lo, "max_value": hi})
    changes = {k: {"old": getattr(p, k), "new": v} for k, v in new.items() if getattr(p, k) != v}
    for k in changes:
        setattr(p, k, new[k])
    if changes:
        audit(db, request, user, "params_patch", details={"code": code, "changes": changes})
    db.commit()
    db.refresh(p)
    return param_out(p)


@router.get("/audit", summary="Журнал действий")
def list_audit(user: CurrentUser, db: DbDep, process_id: str | None = None,
               user_login: str | None = Query(default=None, alias="user"), action: str | None = None,
               limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)) -> dict:
    """admin, supervisor, ml — весь журнал. Остальные роли — только журнал одной проверки (вкладка «Журнал»),
    без IP и User-Agent других пользователей."""
    full = has_perm(user.role, "audit")
    if not full and not (process_id and has_perm(user.role, "read")):
        raise ApiError(403, "FORBIDDEN", "Журнал доступен администратору, супервизору и ML-инженеру",
                       details={"role": user.role, "required": "audit"})
    conds = []
    if process_id:
        conds.append(AuditLog.process_id == process_id)
    if user_login:
        conds.append(AuditLog.user_login == user_login)
    if action:
        conds.append(or_(*[AuditLog.action == a.strip() for a in action.split(",") if a.strip()]))
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*conds)) or 0
    rows = db.scalars(select(AuditLog).where(*conds).order_by(AuditLog.id.desc()).limit(limit).offset(offset))
    items = []
    for r in rows:
        item = {"id": r.id, "user": r.user_login, "action": r.action, "object_code": r.object_code,
                "process_id": r.process_id, "details": r.details, "created_at": iso(r.created_at)}
        if full:
            item.update(ip=r.ip, user_agent=r.user_agent)
        items.append(item)
    return {"items": items, "total": total}
