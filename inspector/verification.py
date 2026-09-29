"""Верификация находок инспектором (ТЗ 9.3): карточки доказательств, решения и их история, массовое подтверждение,
привязка гипотез свободного поиска, финализация и её отмена.

Проверки правил — чистые функции (check_*) без БД; функции с сессией применяют решение и пишут историю. Машинные поля
находки не перезаписываются: решение — в inspector_* и строке Decision (исключение по ТЗ — «привязать» гипотезу:
SUSPICION → CANDIDATE)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.models import (Decision, Document, File, Finding, Page, Param, Process, ProtocolVersion, SyncAttempt,
                              User, utcnow)
from inspector.protocol import effective_status, finding_card, save_snapshot


class DomainError(Exception):
    """Нарушение правила предметной области: HTTP-статус, машинный код и сообщение для пользователя."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------- карточки


@dataclass
class CardContext:
    """Всё, что нужно protocol.finding_card: файлы, документы страниц-доказательств, матрица, пользователи."""
    process: Process
    files: dict[int, File]
    docs: dict[tuple[int, int], Document]
    params: dict[str, Param]
    users: dict[int, str]

    @classmethod
    def load(cls, db: Session, process: Process, findings: list[Finding]) -> "CardContext":
        files = {f.id: f for f in db.scalars(select(File).where(File.process_id == process.id))}
        pairs = {(e.file_pk, e.page_no) for f in findings for e in f.evidence}
        docs: dict[tuple[int, int], Document] = {}
        if pairs:
            rows = db.execute(select(Page.file_pk, Page.page_no, Document)
                              .join(Document, Page.document_pk == Document.id)
                              .where(Page.file_pk.in_({p[0] for p in pairs}),
                                     Page.page_no.in_({p[1] for p in pairs}))).all()
            docs = {(fp, pn): d for fp, pn, d in rows if (fp, pn) in pairs}
        params = {p.code: p for p in db.scalars(select(Param))}
        users = {u.id: u.login for u in db.scalars(select(User))}
        return cls(process, files, docs, params, users)

    def card(self, f: Finding) -> dict:
        card = finding_card(f, self.files, self.docs, self.params, self.process.object.object_code, self.users)
        card["process_id"] = f.process_id
        card["active"] = f.active
        return card


def active_findings(db: Session, process_id: str) -> list[Finding]:
    return list(db.scalars(select(Finding).where(Finding.process_id == process_id, Finding.active.is_(True))
                           .order_by(Finding.id)))


def cards_for(db: Session, process: Process, findings: list[Finding]) -> list[dict]:
    ctx = CardContext.load(db, process, findings)
    return [ctx.card(f) for f in findings]


def card_of(db: Session, finding: Finding) -> dict:
    process = db.get(Process, finding.process_id)
    return CardContext.load(db, process, [finding]).card(finding)


# ---------------------------------------------------------------- правила (чистые функции)


def clean_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def check_open(process_status: str) -> None:
    if process_status not in S.DECISIONS_ALLOWED:
        message = ("Протокол финализирован — решения менять нельзя (отмена финализации — у супервизора)"
                   if process_status == S.FINALIZED else
                   "Решения принимаются, когда протокол готов (READY / VERIFYING / COMPLETED)")
        raise DomainError(409, "DECISION_NOT_ALLOWED", message, {"process_status": process_status})


def _check_active(active: bool) -> None:
    if not active:
        raise DomainError(409, "FINDING_INACTIVE", "Находка устарела: после повторной обработки откройте "
                                                   "актуальную карточку")


def check_decision(*, process_status: str, machine_status: str, active: bool, decision: str,
                   reason_code: str | None, comment: str | None) -> None:
    """Решение по кандидату: протокол открыт, находка актуальна и машинно — CANDIDATE; «Отклонить» — только с
    причиной из справочника и непустым комментарием."""
    check_open(process_status)
    _check_active(active)
    if machine_status != S.CANDIDATE:
        raise DomainError(409, "NOT_A_CANDIDATE", "Решение принимается только по кандидатам в нарушение",
                          {"status": machine_status})
    if decision not in S.DECISIONS:
        raise DomainError(422, "INVALID_DECISION", "Решение: подтвердить, отклонить или требует уточнения",
                          {"allowed": list(S.DECISIONS)})
    if decision == S.NEGATIVE_VERIFIED and (reason_code not in S.REASON_CODES or not clean_text(comment)):
        raise DomainError(422, "REASON_REQUIRED", "Для отклонения укажите причину из списка и комментарий",
                          {"reason_codes": S.REASON_CODES})
    if reason_code is not None and reason_code not in S.REASON_CODES:
        raise DomainError(422, "INVALID_REASON_CODE", "Неизвестная причина", {"reason_codes": S.REASON_CODES})


def check_promote(*, process_status: str, machine_status: str, active: bool, evidence_pages: list) -> None:
    """«Привязать» гипотезу свободного поиска (SUSPICION → CANDIDATE) — только при страницах-доказательствах."""
    check_open(process_status)
    _check_active(active)
    if machine_status != S.SUSPICION:
        raise DomainError(409, "NOT_A_SUSPICION", "Привязать можно только гипотезу свободного поиска",
                          {"status": machine_status})
    if not any(isinstance(p, int) and p >= 1 for p in evidence_pages):
        raise DomainError(409, "NO_EVIDENCE_PAGES", "У гипотезы нет страниц-доказательств — привязать нельзя")


def check_finalize(*, process_status: str, protocol_version: int, pending_ids: list[int]) -> None:
    if process_status == S.FINALIZED:
        raise DomainError(409, "ALREADY_FINALIZED", "Протокол уже финализирован")
    if process_status not in S.DECISIONS_ALLOWED or not protocol_version:
        raise DomainError(409, "FINALIZE_NOT_ALLOWED", "Финализировать можно готовый протокол",
                          {"process_status": process_status})
    if pending_ids:
        raise DomainError(409, "PENDING_CANDIDATES",
                          f"Остались нерешённые кандидаты: {len(pending_ids)} — примите решение или перенесите их "
                          f"в «Требует уточнения»", {"pending": len(pending_ids), "finding_ids": pending_ids})


def check_unfinalize(*, process_status: str, reason: str | None) -> None:
    if process_status != S.FINALIZED:
        raise DomainError(409, "NOT_FINALIZED", "Протокол не финализирован", {"process_status": process_status})
    if not clean_text(reason):
        raise DomainError(422, "REASON_REQUIRED", "Укажите причину отмены финализации")


# ---------------------------------------------------------------- действия (сессия; коммитит вызывающий)


def pending_candidate_ids(db: Session, process_id: str) -> list[int]:
    return list(db.scalars(select(Finding.id).where(
        Finding.process_id == process_id, Finding.active.is_(True), Finding.status == S.CANDIDATE,
        Finding.inspector_status == S.INSPECTOR_PENDING).order_by(Finding.id)))


def refresh_process_status(db: Session, process: Process) -> int:
    """Статус проверки после решения: есть нерешённые кандидаты — VERIFYING, нет — COMPLETED."""
    db.flush()
    pending = len(pending_candidate_ids(db, process.id))
    process.status = S.process_status_after_decision(process.status, pending)
    return pending


def apply_decision(db: Session, finding: Finding, user: User, decision: str, reason_code: str | None,
                   comment: str | None) -> Decision:
    row = Decision(finding_pk=finding.id, user_pk=user.id, decision=decision, reason_code=reason_code,
                   comment=comment, previous_status=effective_status(finding.status, finding.inspector_status))
    finding.inspector_status = decision
    finding.decided_by = user.id
    finding.decided_at = utcnow()
    finding.reason_code = reason_code
    finding.comment = comment
    db.add(row)
    return row


def decide(db: Session, finding: Finding, user: User, decision: str, reason_code: str | None,
           comment: str | None) -> tuple[Decision, Process, int]:
    process = db.get(Process, finding.process_id)
    reason_code, comment = clean_text(reason_code), clean_text(comment)
    check_decision(process_status=process.status, machine_status=finding.status, active=finding.active,
                   decision=decision, reason_code=reason_code, comment=comment)
    row = apply_decision(db, finding, user, decision, reason_code, comment)
    return row, process, refresh_process_status(db, process)


def _find(db: Session, ref: int | str) -> Finding | None:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return db.get(Finding, int(ref))
    return db.scalars(select(Finding).where(Finding.finding_id == ref, Finding.active.is_(True))
                      .order_by(Finding.id.desc())).first()


def bulk_confirm(db: Session, refs: list[int | str], user: User,
                 comment: str | None) -> tuple[list[int], list[dict], dict[str, list[int]]]:
    """Массово — только подтверждение (ТЗ 9.3). -> (обновлённые id, пропущенные с причиной, {process_id: [id]})."""
    comment = clean_text(comment)
    updated: list[int] = []
    skipped: list[dict] = []
    by_process: dict[str, list[int]] = {}
    processes: dict[str, Process] = {}
    for ref in refs:
        finding = _find(db, ref)
        if finding is None:
            skipped.append({"id": ref, "reason_code": "FINDING_NOT_FOUND", "reason": "Находка не найдена"})
            continue
        process = processes.setdefault(finding.process_id, db.get(Process, finding.process_id))
        if finding.inspector_status == S.CONFIRMED_VIOLATION and finding.status == S.CANDIDATE:
            skipped.append({"id": finding.id, "reason_code": "ALREADY_CONFIRMED", "reason": "Уже подтверждено"})
            continue
        try:
            check_decision(process_status=process.status, machine_status=finding.status, active=finding.active,
                           decision=S.CONFIRMED_VIOLATION, reason_code=None, comment=comment)
        except DomainError as exc:
            skipped.append({"id": finding.id, "reason_code": exc.code, "reason": exc.message})
            continue
        apply_decision(db, finding, user, S.CONFIRMED_VIOLATION, None, comment)
        updated.append(finding.id)
        by_process.setdefault(process.id, []).append(finding.id)
    for pid in by_process:
        refresh_process_status(db, processes[pid])
    if not updated and any(s["reason_code"] == "DECISION_NOT_ALLOWED" for s in skipped):
        raise DomainError(409, "DECISION_NOT_ALLOWED", "Протокол финализирован или не готов — решения не приняты",
                          {"skipped": skipped})
    return updated, skipped, by_process


def promote(db: Session, finding: Finding, user: User, comment: str | None) -> tuple[Process, int]:
    process = db.get(Process, finding.process_id)
    check_promote(process_status=process.status, machine_status=finding.status, active=finding.active,
                  evidence_pages=[e.page_no for e in finding.evidence])
    db.add(Decision(finding_pk=finding.id, user_pk=user.id, decision="PROMOTE", previous_status=S.SUSPICION,
                    comment=clean_text(comment)))
    finding.status = S.CANDIDATE
    finding.inspector_status = S.INSPECTOR_PENDING
    return process, refresh_process_status(db, process)


def finalize(db: Session, process: Process, user: User) -> tuple[ProtocolVersion, int]:
    """Финализация: снимок протокола kind=final и черновик GOLD. Статус отправки в РиН сбрасывается: новая
    финальная версия ещё не отправлена."""
    from inspector import gold

    check_finalize(process_status=process.status, protocol_version=process.protocol_version,
                   pending_ids=pending_candidate_ids(db, process.id))
    process.status = S.FINALIZED
    process.finalized_at = utcnow()
    process.sync_status = "NONE"
    pv = save_snapshot(db, process, kind="final", user_pk=user.id)
    return pv, gold.create_draft_items(db, process)


def unfinalize(db: Session, process: Process, user: User, reason: str | None) -> tuple[ProtocolVersion, int, int]:
    """Отмена финализации (supervisor/admin, с причиной) → COMPLETED: черновик GOLD проверки снимается,
    запланированные повторы отправки в РиН отменяются. -> (снимок, снято элементов GOLD, отменено повторов)."""
    from inspector import gold

    check_unfinalize(process_status=process.status, reason=reason)
    process.status = S.COMPLETED
    process.finalized_at = None
    removed = gold.drop_draft_items(db, process)
    cancelled = 0
    for attempt in db.scalars(select(SyncAttempt).where(SyncAttempt.process_id == process.id,
                                                        SyncAttempt.status == "SCHEDULED")):
        attempt.status = "FAILED"
        attempt.error = ((attempt.error or "") + " | отменено: финализация отменена").strip(" |")
        cancelled += 1
    if process.sync_status == "PENDING_SYNC":
        process.sync_status = "NONE"
    pv = save_snapshot(db, process, kind="unfinalize", user_pk=user.id)
    return pv, removed, cancelled
