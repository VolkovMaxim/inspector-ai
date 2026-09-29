"""Статусы ТЗ 9.1–9.3 и их соответствие полям карточки и сабмита организаторов. Чистая логика, без I/O."""
from __future__ import annotations

from dataclasses import dataclass

# процесс проверки (ТЗ 9.1)
PENDING, PARSING, READY, VERIFYING, COMPLETED, FINALIZED, FAILED = (
    "PENDING", "PARSING", "READY", "VERIFYING", "COMPLETED", "FINALIZED", "FAILED")
PROCESS_STATUSES = (PENDING, PARSING, READY, VERIFYING, COMPLETED, FINALIZED, FAILED)
UPLOAD_ALLOWED = {PENDING, READY, VERIFYING, COMPLETED, FAILED}
DECISIONS_ALLOWED = {READY, VERIFYING, COMPLETED}

# статус параметра/находки в протоколе (ТЗ 9.2, таблица «Статусы параметров»)
NEGATIVE_VERIFIED = "NEGATIVE_VERIFIED"
CANDIDATE = "CANDIDATE"
CONFIRMED_VIOLATION = "CONFIRMED_VIOLATION"
MISSING_EVIDENCE = "MISSING_EVIDENCE"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_COMPARABLE = "NOT_COMPARABLE"
CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
SUSPICION = "SUSPICION"
FINDING_STATUSES = (NEGATIVE_VERIFIED, CANDIDATE, CONFIRMED_VIOLATION, MISSING_EVIDENCE, NOT_APPLICABLE,
                    NOT_COMPARABLE, CLARIFICATION_REQUIRED, SUSPICION)

# решения инспектора (ТЗ 9.3)
INSPECTOR_PENDING = "PENDING"
DECISIONS = (CONFIRMED_VIOLATION, NEGATIVE_VERIFIED, CLARIFICATION_REQUIRED)
REASON_CODES = {
    "ACTUAL_REVISION_WRONG": "Актуальная редакция выбрана неверно",
    "APPROVED_CHANGE": "Есть согласованное изменение",
    "OCR_ERROR": "Ошибка распознавания (OCR)",
    "BINDING_ERROR": "Ошибка привязки (не та локация/элемент)",
    "EXTRACTION_ERROR": "Значение извлечено неверно",
    "NOT_APPLICABLE": "Параметр неприменим к объекту",
    "OTHER": "Другое",
}

# сценарии загрузки (ТЗ 9.2)
SCENARIOS = ("FULL", "PD_RD_ONLY", "PD_ID_ONLY", "RD_ID_ONLY", "SINGLE_ONLY", "PARTIALLY_LOADED")
STAGES = ("PD", "RD", "ID")
OPTIONAL_SECTIONS = {"ТХ", "ПОД"}          # раздел может отсутствовать у объекта -> NOT_APPLICABLE


def review_priority(criticality: str | None) -> str:
    """HIGH/MEDIUM/LOW — только очерёдность рассмотрения (ТЗ 8.1), из критичности матрицы."""
    c = (criticality or "").lower()
    if c.startswith("критич"):
        return "HIGH"
    if c.startswith("существ"):
        return "MEDIUM"
    return "LOW"


def violation_protocol_status(criticality: str | None) -> str:
    return "CRITICAL" if (criticality or "").lower().startswith("критич") else "WARNING"


@dataclass(frozen=True)
class CardStatus:
    status: str                    # одно из FINDING_STATUSES
    finding_status: str | None     # NEGATIVE_VERIFIED | CANDIDATE | CONFIRMED_VIOLATION | SUSPICION | None
    completeness_status: str       # COMPLETE | MISSING_EVIDENCE | NOT_APPLICABLE | NOT_COMPARABLE | CLARIFICATION_REQUIRED


def card_status(status: str) -> CardStatus:
    finding = status if status in (NEGATIVE_VERIFIED, CANDIDATE, CONFIRMED_VIOLATION, SUSPICION) else None
    completeness = "COMPLETE" if finding else status
    return CardStatus(status, finding, completeness)


@dataclass(frozen=True)
class MachineVerdict:
    status: str
    submission_label: str | None
    submission_status: str | None
    submission_include: bool


def machine_verdict(*, judge_verdict: str | None, verified: bool | None, has_pd: bool, has_rd_value: bool,
                    has_id_value: bool, object_has_rd: bool, object_has_id: bool, clarification: bool,
                    criticality: str | None) -> MachineVerdict:
    """Сводит судью, верификатора и комплектность к статусу протокола и метке сабмита (таблица раздела 3 дизайна).

    judge_verdict: VIOLATION | NO_VIOLATION | UNCLEAR | None (сравнивать было не с чем);
    verified: итог верификатора для VIOLATION (None — не запускался)."""
    if not has_pd:
        return MachineVerdict(MISSING_EVIDENCE, None, None, False)
    if clarification:
        return MachineVerdict(CLARIFICATION_REQUIRED, "COMPARISON_IMPOSSIBLE", "COMPARISON_IMPOSSIBLE", True)
    if not has_rd_value and not has_id_value:
        if not object_has_rd and not object_has_id:
            # у объекта нет ни РД, ни ИД — это «отсутствует документ», а не «не нашли значение»
            return MachineVerdict(MISSING_EVIDENCE, "MISSING_DOCUMENT", "RD_MISSING", True)
        # РД или ИД у объекта есть, значения на них не нашли: это пробел отбора, а не отсутствие документа.
        # В сабмит не идёт (иначе ошибка статуса против отрицательных точек gold; merge.drop_unfound)
        return MachineVerdict(MISSING_EVIDENCE, None, None, False)
    if judge_verdict == "VIOLATION":
        if verified:
            return MachineVerdict(CANDIDATE, "VIOLATION_PRESENT", violation_protocol_status(criticality), True)
        return MachineVerdict(NOT_COMPARABLE, "COMPARISON_IMPOSSIBLE", "COMPARISON_IMPOSSIBLE", True)
    if judge_verdict == "NO_VIOLATION":
        return MachineVerdict(NEGATIVE_VERIFIED, "NO_VIOLATION", "OK", True)
    return MachineVerdict(NOT_COMPARABLE, "COMPARISON_IMPOSSIBLE", "COMPARISON_IMPOSSIBLE", True)


def scenario(stages_present: set[str], partial: bool = False) -> str:
    """Сценарий загрузки по стадиям, у которых есть хотя бы один читаемый файл (ТЗ 9.2)."""
    if partial:
        return "PARTIALLY_LOADED"
    s = stages_present & set(STAGES)
    if s == {"PD", "RD", "ID"}:
        return "FULL"
    if s == {"PD", "RD"}:
        return "PD_RD_ONLY"
    if s == {"PD", "ID"}:
        return "PD_ID_ONLY"
    if s == {"RD", "ID"}:
        return "RD_ID_ONLY"
    return "SINGLE_ONLY"


def upload_status(stage: str, ok_files: int, problem_files: int) -> str:
    """PD_UPLOADED / PD_PARTIAL / PD_MISSING. Реестра ожидаемых файлов (РиН) нет — по вебинару система вправе
    считать комплект полным; PARTIAL — если часть файлов стадии не читается (битые, неподдерживаемые)."""
    if ok_files == 0:
        return f"{stage}_MISSING"
    return f"{stage}_PARTIAL" if problem_files else f"{stage}_UPLOADED"


def can_finalize(candidate_inspector_statuses: list[str]) -> bool:
    """Финализация — только когда по каждому CANDIDATE есть решение (в т.ч. перенос в CLARIFICATION_REQUIRED)."""
    return all(s != INSPECTOR_PENDING for s in candidate_inspector_statuses)


def process_status_after_decision(current: str, pending_candidates: int) -> str:
    if current == FINALIZED:
        return current
    return COMPLETED if pending_candidates == 0 else VERIFYING
