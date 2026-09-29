"""Находки (карточки доказательств) и работа инспектора с ними: решения, массовое подтверждение, привязка гипотез,
финализация протокола и её отмена."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector import verification
from inspector.api.deps import API_PREFIX, ApiError, DbDep, Reader, audit, get_process_or_404, iso, require
from inspector.api.processes import process_view
from inspector.models import Decision, Finding, Process, User
from inspector.verification import active_findings, card_of, cards_for

router = APIRouter(prefix=API_PREFIX, tags=["findings"])

PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def resolve_finding(db: Session, ref: str) -> Finding:
    """Находка по числовому id (id карточки) или по строковому finding_id (среди активных)."""
    finding = None
    if ref.isdigit():
        finding = db.get(Finding, int(ref))
    else:
        finding = db.scalars(select(Finding).where(Finding.finding_id == ref, Finding.active.is_(True))
                             .order_by(Finding.id.desc())).first()
    if finding is None:
        raise ApiError(404, "FINDING_NOT_FOUND", "Находка не найдена", details={"finding": ref})
    return finding


def decision_history(db: Session, finding: Finding) -> list[dict]:
    rows = db.execute(select(Decision, User.login).outerjoin(User, User.id == Decision.user_pk)
                      .where(Decision.finding_pk == finding.id).order_by(Decision.id)).all()
    return [{"decision": d.decision, "previous_status": d.previous_status, "reason_code": d.reason_code,
             "comment": d.comment, "user": login, "created_at": iso(d.created_at)} for d, login in rows]


def card_with_history(db: Session, finding: Finding) -> dict:
    return {**card_of(db, finding), "history": decision_history(db, finding)}


def _matches(card: dict, needle: str) -> bool:
    hay = [card["code"], card["finding_id"], card["parameter_name"], card["location"], card["expected_value"],
           *card["actual_values"].values()]
    return any(needle in str(v).casefold() for v in hay if v)


@router.get("/processes/{process_id}/findings", summary="Находки проверки (карточки): фильтры и поиск")
def list_findings(process_id: str, _: Reader, db: DbDep,
                  status: str | None = Query(default=None, description="статусы через запятую, с учётом решений"),
                  section: str | None = None, source: str | None = None, priority: str | None = None,
                  q: str | None = None, limit: int | None = Query(default=None, ge=1, le=10000),
                  offset: int = Query(default=0, ge=0)) -> dict:
    process = get_process_or_404(db, process_id)
    wanted = {s.strip().upper() for s in (status or "").split(",") if s.strip()}
    if wanted - set(S.FINDING_STATUSES):
        raise ApiError(422, "INVALID_STATUS", "Неизвестный статус находки",
                       details={"invalid": sorted(wanted - set(S.FINDING_STATUSES)),
                                "allowed": list(S.FINDING_STATUSES)})
    cards = cards_for(db, process, active_findings(db, process.id))
    if wanted:
        cards = [c for c in cards if c["status"] in wanted]
    if section:
        cards = [c for c in cards if c["section"] == section]
    if source:
        cards = [c for c in cards if c["source"] == source]
    if priority:
        cards = [c for c in cards if c["review_priority"] == priority.upper()]
    if q and q.strip():
        needle = q.strip().casefold()
        cards = [c for c in cards if _matches(c, needle)]
    cards.sort(key=lambda c: (PRIORITY_RANK.get(c["review_priority"], 3), c["code"], c["id"]))
    total = len(cards)
    cards = cards[offset:offset + limit] if limit else cards[offset:]
    return {"items": cards, "total": total}


@router.get("/findings/{finding_ref}", summary="Карточка доказательства с историей решений")
def get_finding(finding_ref: str, _: Reader, db: DbDep) -> dict:
    return card_with_history(db, resolve_finding(db, finding_ref))


# ---------------------------------------------------------------- решения инспектора


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(description="CONFIRMED_VIOLATION | NEGATIVE_VERIFIED | CLARIFICATION_REQUIRED")
    reason_code: str | None = Field(default=None, description="обязательна для NEGATIVE_VERIFIED")
    comment: str | None = Field(default=None, max_length=5000, description="обязателен для NEGATIVE_VERIFIED")


class BulkConfirmIn(BaseModel):
    """Массово — только подтверждение: поля decision/reason_code здесь нет (лишние поля → 422)."""
    model_config = ConfigDict(extra="forbid")
    finding_ids: list[int | str] = Field(min_length=1, max_length=1000)
    comment: str | None = Field(default=None, max_length=5000)


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=5000)


class UnfinalizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=5000)


def _object_code(db: Session, process_id: str) -> str | None:
    process = db.get(Process, process_id)
    return process.object.object_code if process else None


@router.post("/findings/{finding_ref}/decision", summary="Решение инспектора по кандидату")
def post_decision(finding_ref: str, body: DecisionIn, request: Request, db: DbDep,
                  user: User = Depends(require("decide"))) -> dict:
    finding = resolve_finding(db, finding_ref)
    decision = body.decision.strip().upper()
    row, process, pending = verification.decide(db, finding, user, decision, body.reason_code, body.comment)
    audit(db, request, user, "decision", process_id=process.id, object_code=process.object.object_code,
          details={"finding_pk": finding.id, "finding_id": finding.finding_id, "code": finding.param_code,
                   "decision": decision, "previous_status": row.previous_status, "reason_code": row.reason_code,
                   "comment": row.comment})
    db.commit()
    return {**card_with_history(db, finding), "process_status": process.status, "candidates_pending": pending}


@router.post("/findings/bulk-confirm", summary="Массовое подтверждение кандидатов (отклонение — только по одному)")
def post_bulk_confirm(body: BulkConfirmIn, request: Request, db: DbDep,
                      user: User = Depends(require("decide"))) -> dict:
    updated, skipped, by_process = verification.bulk_confirm(db, body.finding_ids, user, body.comment)
    for process_id, finding_ids in by_process.items():
        audit(db, request, user, "bulk_confirm", process_id=process_id, object_code=_object_code(db, process_id),
              details={"finding_pks": finding_ids, "skipped": len(skipped), "comment": body.comment})
    db.commit()
    return {"updated": updated, "skipped": skipped}


@router.post("/findings/{finding_ref}/promote", summary="Привязать гипотезу свободного поиска: SUSPICION → CANDIDATE")
def post_promote(finding_ref: str, request: Request, db: DbDep, user: User = Depends(require("decide")),
                 body: CommentIn | None = None) -> dict:
    finding = resolve_finding(db, finding_ref)
    comment = body.comment if body else None
    process, pending = verification.promote(db, finding, user, comment)
    audit(db, request, user, "promote", process_id=process.id, object_code=process.object.object_code,
          details={"finding_pk": finding.id, "finding_id": finding.finding_id, "code": finding.param_code,
                   "comment": comment})
    db.commit()
    return {**card_with_history(db, finding), "process_status": process.status, "candidates_pending": pending}


# ---------------------------------------------------------------- финализация


@router.post("/processes/{process_id}/finalize", summary="Финализировать протокол (все кандидаты решены)")
def post_finalize(process_id: str, request: Request, db: DbDep, user: User = Depends(require("decide")),
                  body: CommentIn | None = None) -> dict:
    process = get_process_or_404(db, process_id)
    pv, n_items = verification.finalize(db, process, user)
    audit(db, request, user, "finalize", process_id=process.id, object_code=process.object.object_code,
          details={"protocol_version": pv.version, "dataset_items": n_items,
                   "comment": body.comment if body else None})
    db.commit()
    return process_view(db, process)


@router.post("/processes/{process_id}/unfinalize", summary="Отменить финализацию (supervisor/admin, с причиной)")
def post_unfinalize(process_id: str, request: Request, db: DbDep, user: User = Depends(require("unfinalize")),
                    body: UnfinalizeIn | None = None) -> dict:
    process = get_process_or_404(db, process_id)
    reason = body.reason if body else None
    pv, removed, cancelled = verification.unfinalize(db, process, user, reason)
    audit(db, request, user, "unfinalize", process_id=process.id, object_code=process.object.object_code,
          details={"reason": verification.clean_text(reason), "protocol_version": pv.version,
                   "dataset_items_removed": removed, "sync_retries_cancelled": cancelled})
    db.commit()
    return process_view(db, process)
