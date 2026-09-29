"""Чистые правила верификации (ТЗ 9.3): решение по кандидату, привязка гипотезы, финализация и её отмена."""
from __future__ import annotations

import pytest

from inspector import statuses as S
from inspector.verification import (DomainError, check_decision, check_finalize, check_promote, check_unfinalize,
                                    clean_text)


def raises(code: str, fn, *args, **kwargs) -> DomainError:
    with pytest.raises(DomainError) as info:
        fn(*args, **kwargs)
    assert info.value.code == code, (info.value.code, info.value.message)
    return info.value


# ---------------------------------------------------------------- решение по кандидату

OK = dict(process_status=S.READY, machine_status=S.CANDIDATE, active=True)


@pytest.mark.parametrize("decision, reason, comment", [
    (S.CONFIRMED_VIOLATION, None, None),
    (S.CONFIRMED_VIOLATION, None, "подтверждаю"),
    (S.CLARIFICATION_REQUIRED, None, None),
    (S.NEGATIVE_VERIFIED, "OCR_ERROR", "в ИД на самом деле А400"),
    (S.NEGATIVE_VERIFIED, "OTHER", "x"),
])
def test_valid_decisions_pass(decision, reason, comment):
    assert check_decision(**OK, decision=decision, reason_code=reason, comment=comment) is None


@pytest.mark.parametrize("reason, comment", [
    (None, "есть комментарий"),                 # нет причины
    ("NOT_A_REASON", "есть комментарий"),       # причина не из справочника
    ("OCR_ERROR", None),                        # нет комментария
    ("OCR_ERROR", ""),
    ("OCR_ERROR", "   \n\t "),                  # комментарий из пробелов
])
def test_rejection_needs_reason_from_list_and_comment(reason, comment):
    err = raises("REASON_REQUIRED", check_decision, **OK, decision=S.NEGATIVE_VERIFIED, reason_code=reason,
                 comment=comment)
    assert err.status_code == 422
    assert set(err.details["reason_codes"]) == set(S.REASON_CODES)


@pytest.mark.parametrize("status", [S.PENDING, S.PARSING, S.FINALIZED, S.FAILED])
def test_decision_only_while_protocol_open(status):
    err = raises("DECISION_NOT_ALLOWED", check_decision, process_status=status, machine_status=S.CANDIDATE,
                 active=True, decision=S.CONFIRMED_VIOLATION, reason_code=None, comment=None)
    assert err.status_code == 409


@pytest.mark.parametrize("status", [S.VERIFYING, S.COMPLETED])
def test_decision_can_change_until_finalization(status):
    assert check_decision(process_status=status, machine_status=S.CANDIDATE, active=True,
                          decision=S.NEGATIVE_VERIFIED, reason_code="APPROVED_CHANGE", comment="изм. 2") is None


@pytest.mark.parametrize("machine_status", [S.NEGATIVE_VERIFIED, S.SUSPICION, S.MISSING_EVIDENCE, S.NOT_COMPARABLE,
                                            S.NOT_APPLICABLE, S.CLARIFICATION_REQUIRED])
def test_decision_only_for_machine_candidates(machine_status):
    err = raises("NOT_A_CANDIDATE", check_decision, process_status=S.READY, machine_status=machine_status,
                 active=True, decision=S.CONFIRMED_VIOLATION, reason_code=None, comment=None)
    assert err.status_code == 409


def test_inactive_finding_and_unknown_decision_and_bad_reason():
    raises("FINDING_INACTIVE", check_decision, process_status=S.READY, machine_status=S.CANDIDATE, active=False,
           decision=S.CONFIRMED_VIOLATION, reason_code=None, comment=None)
    err = raises("INVALID_DECISION", check_decision, **OK, decision="PENDING", reason_code=None, comment=None)
    assert err.status_code == 422
    raises("INVALID_REASON_CODE", check_decision, **OK, decision=S.CONFIRMED_VIOLATION, reason_code="NOPE",
           comment=None)


def test_clean_text():
    assert clean_text("  текст \n") == "текст"
    assert clean_text("   ") is None and clean_text(None) is None


# ---------------------------------------------------------------- привязка гипотезы (SUSPICION → CANDIDATE)


def test_promote_needs_suspicion_with_evidence_page():
    assert check_promote(process_status=S.READY, machine_status=S.SUSPICION, active=True, evidence_pages=[20, 4]) \
        is None
    raises("NO_EVIDENCE_PAGES", check_promote, process_status=S.READY, machine_status=S.SUSPICION, active=True,
           evidence_pages=[])
    raises("NO_EVIDENCE_PAGES", check_promote, process_status=S.READY, machine_status=S.SUSPICION, active=True,
           evidence_pages=[0, None])
    raises("NOT_A_SUSPICION", check_promote, process_status=S.READY, machine_status=S.CANDIDATE, active=True,
           evidence_pages=[3])
    raises("DECISION_NOT_ALLOWED", check_promote, process_status=S.FINALIZED, machine_status=S.SUSPICION,
           active=True, evidence_pages=[3])
    raises("FINDING_INACTIVE", check_promote, process_status=S.READY, machine_status=S.SUSPICION, active=False,
           evidence_pages=[3])


# ---------------------------------------------------------------- финализация и отмена


def test_finalize_blocked_by_pending_candidates_with_count():
    err = raises("PENDING_CANDIDATES", check_finalize, process_status=S.VERIFYING, protocol_version=1,
                 pending_ids=[17, 18])
    assert err.status_code == 409 and err.details["pending"] == 2 and err.details["finding_ids"] == [17, 18]


@pytest.mark.parametrize("status", [S.READY, S.VERIFYING, S.COMPLETED])
def test_finalize_allowed_when_all_candidates_decided(status):
    assert check_finalize(process_status=status, protocol_version=1, pending_ids=[]) is None


def test_finalize_not_allowed_without_protocol_or_twice():
    raises("ALREADY_FINALIZED", check_finalize, process_status=S.FINALIZED, protocol_version=2, pending_ids=[])
    for status in (S.PENDING, S.PARSING, S.FAILED):
        raises("FINALIZE_NOT_ALLOWED", check_finalize, process_status=status, protocol_version=1, pending_ids=[])
    raises("FINALIZE_NOT_ALLOWED", check_finalize, process_status=S.READY, protocol_version=0, pending_ids=[])


def test_unfinalize_needs_finalized_process_and_reason():
    assert check_unfinalize(process_status=S.FINALIZED, reason="ошибка в выборе редакции") is None
    err = raises("REASON_REQUIRED", check_unfinalize, process_status=S.FINALIZED, reason="  ")
    assert err.status_code == 422
    raises("NOT_FINALIZED", check_unfinalize, process_status=S.COMPLETED, reason="причина")
