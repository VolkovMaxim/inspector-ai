"""A5: решения инспектора через API — Review Focus 2 (отклонение без причины/комментария) и 3 (финализация),
история решений, массовое подтверждение, привязка гипотез, финализация и её отмена, блокировка после финализации."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from inspector import db as db_module
from inspector import statuses as S
from inspector.models import AuditLog, DatasetItem, Decision, EvidenceFragment, Finding, Process, ProtocolVersion


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str = "inspector") -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def ids(example_process, db) -> dict[str, int]:
    return {f.param_code: f.id for f in db.scalars(select(Finding).where(Finding.process_id == example_process.id))}


def fresh(model, pk):
    with db_module.session_scope() as s:
        obj = s.get(model, pk)
        s.expunge(obj)
        return obj


def count(model, *where) -> int:
    with db_module.session_scope() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def decide(client, headers, fid, decision, reason=None, comment=None):
    body = {"decision": decision}
    if reason is not None:
        body["reason_code"] = reason
    if comment is not None:
        body["comment"] = comment
    return client.post(f"/api/v1/findings/{fid}/decision", headers=headers, json=body)


REJECT = dict(decision=S.NEGATIVE_VERIFIED, reason="EXTRACTION_ERROR", comment="значение извлечено из другой таблицы")

# ---------------------------------------------------------------- решения


def test_confirm_updates_card_history_audit_and_process(client, example_process, ids):
    r = decide(client, login(client), ids["KR-057"], S.CONFIRMED_VIOLATION, comment="Совпадает с АОСР")
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["status"] == S.CONFIRMED_VIOLATION and card["machine_status"] == S.CANDIDATE
    assert card["inspector"]["status"] == S.CONFIRMED_VIOLATION and card["inspector"]["user"] == "inspector"
    assert card["inspector"]["comment"] == "Совпадает с АОСР" and card["inspector"]["decided_at"]
    assert [(h["decision"], h["previous_status"]) for h in card["history"]] == [(S.CONFIRMED_VIOLATION,
                                                                                 S.CANDIDATE)]
    assert card["process_status"] == S.VERIFYING and card["candidates_pending"] == 1
    assert fresh(Process, example_process.id).status == S.VERIFYING
    with db_module.session_scope() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "decision")).one()
        assert row.process_id == example_process.id and row.details["decision"] == S.CONFIRMED_VIOLATION
        assert row.details["finding_pk"] == ids["KR-057"]


@pytest.mark.parametrize("reason, comment", [(None, "комментарий есть"), ("BAD_CODE", "комментарий есть"),
                                             ("OCR_ERROR", None), ("OCR_ERROR", "   ")])
def test_reject_without_reason_or_comment_is_422_and_changes_nothing(client, example_process, ids, reason, comment):
    r = decide(client, login(client), ids["KR-057"], S.NEGATIVE_VERIFIED, reason=reason, comment=comment)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "REASON_REQUIRED"
    f = fresh(Finding, ids["KR-057"])
    assert f.inspector_status == "PENDING" and f.reason_code is None and f.decided_by is None
    assert count(Decision) == 0 and count(AuditLog, AuditLog.action == "decision") == 0
    assert fresh(Process, example_process.id).status == S.READY


def test_reject_with_reason_and_comment(client, ids):
    r = decide(client, login(client), ids["KR-059"], **REJECT)
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["status"] == S.NEGATIVE_VERIFIED and card["inspector"]["reason_code"] == "EXTRACTION_ERROR"
    assert card["history"][0]["reason_code"] == "EXTRACTION_ERROR"


def test_changed_decision_keeps_history_of_two(client, ids):
    h = login(client)
    decide(client, h, ids["KR-057"], S.CONFIRMED_VIOLATION)
    r = decide(client, login(client, "supervisor"), ids["KR-057"], S.NEGATIVE_VERIFIED, reason="APPROVED_CHANGE",
               comment="Согласованное изменение № 12")
    assert r.status_code == 200
    card = r.json()
    assert card["status"] == S.NEGATIVE_VERIFIED
    assert [(x["decision"], x["previous_status"], x["user"]) for x in card["history"]] == [
        (S.CONFIRMED_VIOLATION, S.CANDIDATE, "inspector"),
        (S.NEGATIVE_VERIFIED, S.CONFIRMED_VIOLATION, "supervisor")]
    back = decide(client, h, ids["KR-057"], S.CONFIRMED_VIOLATION).json()
    assert back["inspector"]["reason_code"] is None and len(back["history"]) == 3


def test_decision_on_non_candidate_or_invalid_value(client, ids):
    h = login(client)
    r = decide(client, h, ids["KR-055"], S.CONFIRMED_VIOLATION)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_A_CANDIDATE"
    r2 = decide(client, h, ids["KR-057"], "MAYBE")
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "INVALID_DECISION"
    r3 = decide(client, login(client, "ml"), ids["KR-057"], S.CONFIRMED_VIOLATION)
    assert r3.status_code == 403
    assert count(Decision) == 0


def test_decisions_need_open_protocol_and_active_finding(client, db, example_process, ids):
    example_process.status = S.PARSING
    db.commit()
    r = decide(client, login(client), ids["KR-057"], S.CONFIRMED_VIOLATION)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DECISION_NOT_ALLOWED"
    example_process.status = S.READY
    f = db.get(Finding, ids["KR-059"])
    f.active = False
    db.commit()
    r2 = decide(client, login(client), ids["KR-059"], S.CONFIRMED_VIOLATION)
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "FINDING_INACTIVE"


# ---------------------------------------------------------------- массовое подтверждение


def test_bulk_confirm_updates_candidates_and_skips_the_rest(client, example_process, ids):
    r = client.post("/api/v1/findings/bulk-confirm", headers=login(client),
                    json={"finding_ids": [ids["KR-057"], ids["KR-055"], ids["KR-059"], 999999],
                          "comment": "проверено по АОСР"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == [ids["KR-057"], ids["KR-059"]]
    skipped = {s["id"]: s for s in body["skipped"]}
    assert set(skipped) == {ids["KR-055"], 999999}
    assert skipped[ids["KR-055"]]["reason_code"] == "NOT_A_CANDIDATE" and skipped[ids["KR-055"]]["reason"]
    assert skipped[999999]["reason_code"] == "FINDING_NOT_FOUND"
    assert fresh(Finding, ids["KR-057"]).inspector_status == S.CONFIRMED_VIOLATION
    assert fresh(Finding, ids["KR-057"]).comment == "проверено по АОСР"
    assert fresh(Process, example_process.id).status == S.COMPLETED
    assert count(Decision) == 2 and count(AuditLog, AuditLog.action == "bulk_confirm") == 1


def test_bulk_reject_is_impossible(client, ids):
    h = login(client)
    r = client.post("/api/v1/findings/bulk-confirm", headers=h,
                    json={"finding_ids": [ids["KR-057"]], "decision": S.NEGATIVE_VERIFIED,
                          "reason_code": "OTHER", "comment": "массово"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.post("/api/v1/findings/bulk-reject", headers=h, json={"finding_ids": [ids["KR-057"]]}) \
        .status_code in (404, 405)
    assert fresh(Finding, ids["KR-057"]).inspector_status == "PENDING" and count(Decision) == 0


# ---------------------------------------------------------------- привязка гипотезы


def test_promote_suspicion_to_candidate(client, example_process, ids):
    fid = ids["FREE-AR-EXPLICATION-AREA"]
    r = client.post(f"/api/v1/findings/{fid}/promote", headers=login(client), json={"comment": "та же комната 104"})
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["status"] == S.CANDIDATE and card["machine_status"] == S.CANDIDATE
    assert card["inspector"]["status"] == "PENDING"
    assert [(x["decision"], x["previous_status"], x["comment"]) for x in card["history"]] == [
        ("PROMOTE", S.SUSPICION, "та же комната 104")]
    assert card["process_status"] == S.VERIFYING and card["candidates_pending"] == 3
    assert count(AuditLog, AuditLog.action == "promote") == 1
    again = client.post(f"/api/v1/findings/{fid}/promote", headers=login(client))
    assert again.status_code == 409 and again.json()["error"]["code"] == "NOT_A_SUSPICION"


def test_promote_without_evidence_pages_is_refused(client, db, example_process):
    f = Finding(process_id=example_process.id, finding_id="FND-FREE-X", source="apartments",
                param_code="FREE-AR-APARTMENT-AREA", location="кв. 5", status=S.SUSPICION)
    db.add(f)
    db.commit()
    r = client.post(f"/api/v1/findings/{f.id}/promote", headers=login(client))
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_EVIDENCE_PAGES"
    assert fresh(Finding, f.id).status == S.SUSPICION


# ---------------------------------------------------------------- финализация (Review Focus 3)


def test_finalize_with_pending_candidates_is_409_with_count(client, example_process, ids):
    h = login(client)
    r = client.post(f"/api/v1/processes/{example_process.id}/finalize", headers=h)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PENDING_CANDIDATES" and err["details"]["pending"] == 2
    assert sorted(err["details"]["finding_ids"]) == sorted([ids["KR-057"], ids["KR-059"]])
    decide(client, h, ids["KR-057"], S.CONFIRMED_VIOLATION)
    r2 = client.post(f"/api/v1/processes/{example_process.id}/finalize", headers=h)
    assert r2.status_code == 409 and r2.json()["error"]["details"]["pending"] == 1
    assert fresh(Process, example_process.id).status == S.VERIFYING


def finalize_example(client, headers, ids, process_id):
    assert decide(client, headers, ids["KR-057"], S.CONFIRMED_VIOLATION).status_code == 200
    assert decide(client, headers, ids["KR-059"], **REJECT).status_code == 200
    r = client.post(f"/api/v1/processes/{process_id}/finalize", headers=headers, json={"comment": "итог"})
    assert r.status_code == 200, r.text
    return r.json()


def test_finalize_snapshots_creates_gold_drafts_and_locks_process(client, example_process, ids, pdf_bytes):
    h = login(client)
    proc = finalize_example(client, h, ids, example_process.id)
    assert proc["status"] == S.FINALIZED and proc["finalized_at"] and proc["protocol_version"] == 2
    with db_module.session_scope() as s:
        pv = s.scalars(select(ProtocolVersion).where(ProtocolVersion.process_id == example_process.id)).one()
        assert (pv.version, pv.kind, pv.status) == (2, "final", S.FINALIZED) and pv.finalized_at is not None
        items = {i.param_code: i for i in s.scalars(select(DatasetItem))}
        assert set(items) == {"KR-057", "KR-059"}
        assert items["KR-057"].gold_label == "POSITIVE" and items["KR-059"].gold_label == "NEGATIVE"
        assert items["KR-059"].reason_code == "EXTRACTION_ERROR" and items["KR-057"].dataset_version is None
        assert items["KR-057"].object_code == "OBJ-NOVOSLOBODSKAYA" and items["KR-057"].expert_pk is not None
        assert items["KR-057"].card["inspector"]["status"] == S.CONFIRMED_VIOLATION
        assert items["KR-057"].card["sources"][0]["sha256"] == "a" * 64
    assert count(AuditLog, AuditLog.action == "finalize") == 1
    # после финализации: решения, массовое подтверждение, привязка, загрузка, повторная финализация — 409
    r = decide(client, h, ids["KR-057"], S.NEGATIVE_VERIFIED, reason="OTHER", comment="передумал")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DECISION_NOT_ALLOWED"
    bulk = client.post("/api/v1/findings/bulk-confirm", headers=h, json={"finding_ids": [ids["KR-059"]]})
    assert bulk.status_code == 409 and bulk.json()["error"]["code"] == "DECISION_NOT_ALLOWED"
    promote = client.post(f"/api/v1/findings/{ids['FREE-AR-EXPLICATION-AREA']}/promote", headers=h)
    assert promote.status_code == 409
    up = client.post("/api/v1/documents/upload", headers=h, data={"process_id": example_process.id, "stage": "RD"},
                     files=[("files", ("new.pdf", pdf_bytes, "application/pdf"))])
    assert up.status_code == 409 and up.json()["error"]["code"] == "PROCESS_FINALIZED"
    again = client.post(f"/api/v1/processes/{example_process.id}/finalize", headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "ALREADY_FINALIZED"
    assert fresh(Finding, ids["KR-057"]).inspector_status == S.CONFIRMED_VIOLATION


@pytest.fixture()
def pdf_bytes(tmp_path) -> bytes:
    from inspector.testing import make_pdf

    return make_pdf(tmp_path / "x.pdf", ["x"]).read_bytes()


def test_unfinalize_rules_and_refinalize_rebuilds_drafts(client, example_process, ids):
    finalize_example(client, login(client), ids, example_process.id)
    url = f"/api/v1/processes/{example_process.id}/unfinalize"
    assert client.post(url, headers=login(client, "inspector"), json={"reason": "ошибка"}).status_code == 403
    assert client.post(url, headers=login(client, "ml"), json={"reason": "ошибка"}).status_code == 403
    no_reason = client.post(url, headers=login(client, "supervisor"), json={"reason": "  "})
    assert no_reason.status_code == 422 and no_reason.json()["error"]["code"] == "REASON_REQUIRED"
    no_body = client.post(url, headers=login(client, "supervisor"))
    assert no_body.status_code == 422
    ok = client.post(url, headers=login(client, "supervisor"), json={"reason": "Неверно выбрана редакция РД"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == S.COMPLETED and ok.json()["finalized_at"] is None
    assert ok.json()["protocol_version"] == 3
    with db_module.session_scope() as s:
        kinds = [pv.kind for pv in s.scalars(select(ProtocolVersion).order_by(ProtocolVersion.version))]
        row = s.scalars(select(AuditLog).where(AuditLog.action == "unfinalize")).one()
        assert row.details["reason"] == "Неверно выбрана редакция РД" and row.user_login == "supervisor"
    assert kinds == ["final", "unfinalize"]
    assert count(DatasetItem) == 0                                    # черновики GOLD сняты вместе с финализацией
    twice = client.post(url, headers=login(client, "admin"), json={"reason": "ещё раз"})
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "NOT_FINALIZED"
    h = login(client)
    assert decide(client, h, ids["KR-059"], S.CONFIRMED_VIOLATION).status_code == 200
    refin = client.post(f"/api/v1/processes/{example_process.id}/finalize", headers=h)
    assert refin.status_code == 200
    with db_module.session_scope() as s:
        labels = sorted((i.param_code, i.gold_label) for i in s.scalars(select(DatasetItem)))
    assert labels == [("KR-057", "POSITIVE"), ("KR-059", "POSITIVE")]


def test_evidence_fragment_pages_are_what_promote_checks(db, example_process, ids):
    """Страховка фикстуры: у гипотезы примера есть доказательства со страницами (иначе тест привязки бессмыслен)."""
    pages = [e.page_no for e in db.scalars(select(EvidenceFragment)
                                           .where(EvidenceFragment.finding_pk == ids["FREE-AR-EXPLICATION-AREA"]))]
    assert pages == [20, 4]
