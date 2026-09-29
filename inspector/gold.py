"""GOLD (ТЗ 9.4): эталон из решений инспектора по финализированным протоколам.

Черновик — строки DatasetItem без версии: CONFIRMED_VIOLATION → POSITIVE, отклонённый кандидат (NEGATIVE_VERIFIED по
решению инспектора) → NEGATIVE; карточка сохраняется целиком на момент решения. Спорные (CLARIFICATION_REQUIRED) и
машинные статусы без решения в эталон не идут."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections import Counter

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.models import DatasetItem, DatasetVersion, Decision, Finding, Param, Process
from inspector.verification import CardContext, DomainError

log = logging.getLogger("inspector.gold")

POSITIVE, NEGATIVE = "POSITIVE", "NEGATIVE"


def gold_label(machine_status: str, inspector_status: str | None) -> str | None:
    if machine_status != S.CANDIDATE:
        return None
    return {S.CONFIRMED_VIOLATION: POSITIVE, S.NEGATIVE_VERIFIED: NEGATIVE}.get(inspector_status or "")


def _process_finding_ids(db: Session, process: Process):
    return select(Finding.id).where(Finding.process_id == process.id)


def drop_draft_items(db: Session, process: Process) -> int:
    """Снять неопубликованные элементы проверки (при отмене финализации и перед пересборкой черновика)."""
    result = db.execute(delete(DatasetItem).where(DatasetItem.dataset_version.is_(None),
                                                  DatasetItem.finding_pk.in_(_process_finding_ids(db, process))))
    return result.rowcount or 0


def create_draft_items(db: Session, process: Process) -> int:
    """Черновик GOLD по решениям проверки (при финализации). Повторный вызов пересобирает черновик; уже
    опубликованная запись с той же меткой и причиной заново не предлагается. -> число черновых элементов."""
    drop_draft_items(db, process)
    findings = [f for f in db.scalars(select(Finding).where(Finding.process_id == process.id,
                                                            Finding.active.is_(True)).order_by(Finding.id))
                if gold_label(f.status, f.inspector_status)]
    published = {(i.finding_pk, i.gold_label, i.reason_code) for i in db.scalars(
        select(DatasetItem).where(DatasetItem.dataset_version.is_not(None),
                                  DatasetItem.finding_pk.in_([f.id for f in findings])))} if findings else set()
    ctx = CardContext.load(db, process, findings)
    added = 0
    for f in findings:
        label = gold_label(f.status, f.inspector_status)
        if (f.id, label, f.reason_code) in published:
            continue
        db.add(DatasetItem(finding_pk=f.id, evidence_group_id=f.finding_id, object_code=process.object.object_code,
                           param_code=f.param_code, gold_label=label, expert_pk=f.decided_by,
                           reason_code=f.reason_code, card=ctx.card(f)))
        added += 1
    db.flush()
    return added


# ---------------------------------------------------------------- версии: разбиение по объектам, SHA-256

SPLITS = ("train", "validation", "test")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def split_for_object(object_code: str) -> str:
    """Все элементы объекта — в одном наборе: корзина int(sha256(код)[:8], 16) % 100 → <70 train, <85 validation,
    иначе test. Детерминированно: тот же объект в следующих версиях попадёт в тот же набор."""
    bucket = int(hashlib.sha256(object_code.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 70 else "validation" if bucket < 85 else "test"


def item_record(item: DatasetItem) -> dict:
    """Строка JSONL версии: только содержимое (без id БД и имени версии) — хеш зависит лишь от данных."""
    return {"evidence_group_id": item.evidence_group_id, "object_id": item.object_code, "param_code": item.param_code,
            "gold_label": item.gold_label, "reason_code": item.reason_code, "split": item.split, "card": item.card}


def _dump(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def version_lines(items: list[DatasetItem]) -> list[str]:
    """Канонические строки JSONL в детерминированном порядке (набор, объект, параметр, группа, содержимое)."""
    keyed = []
    for item in items:
        record = item_record(item)
        keyed.append(((record["split"] or "", record["object_id"], record["param_code"],
                       record["evidence_group_id"]), _dump(record)))
    return [line for _, line in sorted(keyed)]


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("".join(line + "\n" for line in lines).encode("utf-8")).hexdigest()


def next_version_name(db: Session) -> str:
    numbers = [int(m.group(1)) for v in db.scalars(select(DatasetVersion.version))
               if (m := re.fullmatch(r"gold-v(\d+)", v))]
    return f"gold-v{max(numbers, default=0) + 1}"


def publish_version(db: Session, user_pk: int | None, version: str | None = None) -> DatasetVersion:
    """Все черновые элементы → новая версия (gold-vN или заданное имя): набор по объекту, SHA-256 версии и наборов."""
    drafts = list(db.scalars(select(DatasetItem).where(DatasetItem.dataset_version.is_(None))
                             .order_by(DatasetItem.id)))
    if not drafts:
        raise DomainError(409, "NO_DRAFT_ITEMS", "Черновик GOLD пуст — публиковать нечего")
    if version is not None:
        version = version.strip()
        if not VERSION_RE.match(version):
            raise DomainError(422, "INVALID_VERSION", "Имя версии: латиница, цифры, «.», «_», «-», до 40 символов",
                              {"version": version})
        if db.scalar(select(DatasetVersion.id).where(DatasetVersion.version == version)) is not None:
            raise DomainError(409, "VERSION_EXISTS", "Такая версия уже опубликована", {"version": version})
    else:
        version = next_version_name(db)
    for item in drafts:
        item.split = split_for_object(item.object_code)
        item.dataset_version = version
    by_split = {s: [i for i in drafts if i.split == s] for s in SPLITS}
    dv = DatasetVersion(version=version, items=len(drafts),
                        positives=sum(i.gold_label == POSITIVE for i in drafts),
                        negatives=sum(i.gold_label == NEGATIVE for i in drafts),
                        sha256=_digest(version_lines(drafts)),
                        split_hashes={s: _digest(version_lines(items)) for s, items in by_split.items()},
                        split_objects={s: sorted({i.object_code for i in items}) for s, items in by_split.items()},
                        created_by=user_pk)
    db.add(dv)
    db.flush()
    return dv


def get_version(db: Session, version: str) -> DatasetVersion:
    dv = db.scalars(select(DatasetVersion).where(DatasetVersion.version == version)).first()
    if dv is None:
        raise DomainError(404, "VERSION_NOT_FOUND", "Версия GOLD не найдена", {"version": version})
    return dv


def export_bytes(db: Session, version: str) -> bytes:
    """JSONL версии; его SHA-256 совпадает с DatasetVersion.sha256 (иначе — предупреждение в журнал)."""
    dv = get_version(db, version)
    items = list(db.scalars(select(DatasetItem).where(DatasetItem.dataset_version == version)))
    data = "".join(line + "\n" for line in version_lines(items)).encode("utf-8")
    if hashlib.sha256(data).hexdigest() != dv.sha256:
        log.warning("SHA-256 выгрузки GOLD не совпал с сохранённым", extra={"version": version})
    return data


# ---------------------------------------------------------------- отчёт по дообучению

REASON_RULES = (
    ("OCR_ERROR", "Проверить OCR на сканах ИД: {share} отклонений — ошибки распознавания (OCR_ERROR)"),
    ("BINDING_ERROR", "Уточнить привязку к локациям (конструкция, помещение): {share} отклонений — "
                      "ошибка привязки (BINDING_ERROR)"),
    ("ACTUAL_REVISION_WRONG", "Проверить выбор актуальных редакций документов: {share} отклонений — "
                              "неверная редакция (ACTUAL_REVISION_WRONG)"),
)
REASON_SHARE_THRESHOLD = 0.20
PARAM_REJECTIONS_THRESHOLD = 3


def retraining_report(db: Session, days: int = 7) -> dict:
    """Отклонения инспекторов за период: по причинам, разделам, параметрам; доля подтверждений; рекомендации.
    По каждой находке учитывается последнее решение в периоде («привязка» гипотезы решением не считается)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    rows = db.execute(select(Decision, Finding.param_code, Finding.source).join(Finding, Finding.id == Decision.finding_pk)
                      .where(Decision.created_at >= since, Decision.decision.in_(S.DECISIONS))
                      .order_by(Decision.created_at, Decision.id)).all()
    latest: dict[int, tuple[Decision, str, str]] = {}
    for decision, code, source in rows:
        latest[decision.finding_pk] = (decision, code, source)
    sections = dict(db.execute(select(Param.code, Param.section)).all())
    confirmed = sum(1 for d, _, _ in latest.values() if d.decision == S.CONFIRMED_VIOLATION)
    clarification = sum(1 for d, _, _ in latest.values() if d.decision == S.CLARIFICATION_REQUIRED)
    rejected = [(d, code, source) for d, code, source in latest.values() if d.decision == S.NEGATIVE_VERIFIED]
    by_reason = Counter(d.reason_code or "OTHER" for d, _, _ in rejected)
    by_section = Counter(sections.get(code) or ("АР" if source != "matrix" else "—") for _, code, source in rejected)
    by_param = sorted(Counter(code for _, code, _ in rejected).items(), key=lambda kv: (-kv[1], kv[0]))
    decided = confirmed + len(rejected)
    recommendations = []
    for reason, text in REASON_RULES:
        share = by_reason.get(reason, 0) / len(rejected) if rejected else 0.0
        if share > REASON_SHARE_THRESHOLD:
            recommendations.append(text.format(share=f"{share:.0%}"))
    for code, n in by_param:
        if n >= PARAM_REJECTIONS_THRESHOLD:
            recommendations.append(f"Пересмотреть рецепт параметра {code}: {n} отклонений за {days} дн.")
    latest_version = db.scalars(select(DatasetVersion.version).order_by(DatasetVersion.id.desc())).first()
    drafts = db.scalar(select(func.count()).select_from(DatasetItem).where(DatasetItem.dataset_version.is_(None)))
    return {"period_days": days,
            "decisions": {"confirmed": confirmed, "rejected": len(rejected), "clarification": clarification},
            "rejection_by_reason": dict(by_reason), "rejection_by_section": dict(by_section),
            "rejection_by_param": [{"code": c, "count": n} for c, n in by_param],
            "confirm_rate": round(confirmed / decided, 3) if decided else None,
            "recommendations": recommendations,
            "dataset": {"draft_items": drafts or 0, "latest_version": latest_version}}
