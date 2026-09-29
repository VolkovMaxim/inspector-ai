"""A7 через API: черновик GOLD из финализированного протокола → версия → JSONL; отчёт по дообучению; реестр моделей."""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from inspector import db as db_module
from inspector import statuses as S
from inspector.models import AuditLog, Finding


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
def finalized(client, db, example_process):
    """Пример, доведённый через API до FINALIZED: KR-057 подтверждён, KR-059 отклонён (EXTRACTION_ERROR)."""
    ids = {f.param_code: f.id for f in db.scalars(select(Finding).where(Finding.process_id == example_process.id))}
    h = login(client)
    assert client.post(f"/api/v1/findings/{ids['KR-057']}/decision", headers=h,
                       json={"decision": S.CONFIRMED_VIOLATION}).status_code == 200
    assert client.post(f"/api/v1/findings/{ids['KR-059']}/decision", headers=h,
                       json={"decision": S.NEGATIVE_VERIFIED, "reason_code": "EXTRACTION_ERROR",
                             "comment": "другая таблица"}).status_code == 200
    assert client.post(f"/api/v1/processes/{example_process.id}/finalize", headers=h).status_code == 200
    return example_process


def test_draft_publish_export_flow(client, finalized):
    ml = login(client, "ml")
    draft = client.get("/api/v1/dataset/items", headers=ml).json()
    assert draft["total"] == 2
    item = next(i for i in draft["items"] if i["param_code"] == "KR-059")
    assert item["gold_label"] == "NEGATIVE" and item["reason_code"] == "EXTRACTION_ERROR"
    assert item["object_id"] == item["object_code"] == "OBJ-NOVOSLOBODSKAYA"       # оба имени: как в карточке и в БД
    assert item["dataset_version"] is None and item["split"] is None
    assert item["expert"] == "inspector" and item["card"]["code"] == "KR-059"
    r = client.post("/api/v1/dataset/versions", headers=ml, json={})
    assert r.status_code == 201, r.text
    v = r.json()
    assert (v["version"], v["items"], v["positives"], v["negatives"]) == ("gold-v1", 2, 1, 1)
    assert v["split_objects"]["train"] == ["OBJ-NOVOSLOBODSKAYA"]           # sha256-корзина 39 → train
    assert v["split_counts"] == {"train": 2, "validation": 0, "test": 0}
    assert len(v["sha256"]) == 64 and set(v["split_hashes"]) == {"train", "validation", "test"}
    assert v["created_by"] == "ml"
    assert client.get("/api/v1/dataset/items", headers=ml).json()["total"] == 0
    published = client.get("/api/v1/dataset/items", params={"version": "gold-v1"}, headers=ml).json()
    assert published["total"] == 2 and all(i["split"] == "train" for i in published["items"])
    versions = client.get("/api/v1/dataset/versions", headers=ml).json()["items"]
    assert [x["version"] for x in versions] == ["gold-v1"] and versions[0]["sha256"] == v["sha256"]
    export = client.get("/api/v1/dataset/versions/gold-v1/export", headers=ml)
    assert export.status_code == 200
    assert "gold-v1.jsonl" in export.headers["content-disposition"]
    assert hashlib.sha256(export.content).hexdigest() == v["sha256"] == export.headers["x-content-sha256"]
    lines = [json.loads(x) for x in export.content.decode().splitlines()]
    assert sorted(l["gold_label"] for l in lines) == ["NEGATIVE", "POSITIVE"]
    assert all({"card", "gold_label", "split", "reason_code"} <= set(l) for l in lines)
    assert client.get("/api/v1/dataset/versions/gold-v1/export", headers=ml).content == export.content
    again = client.post("/api/v1/dataset/versions", headers=ml)
    assert again.status_code == 409 and again.json()["error"]["code"] == "NO_DRAFT_ITEMS"
    with db_module.session_scope() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "dataset_publish")).one()
        assert row.user_login == "ml" and row.details["version"] == "gold-v1" and row.details["sha256"] == v["sha256"]


def test_named_version_and_unknown_export(client, finalized):
    admin = login(client, "admin")
    r = client.post("/api/v1/dataset/versions", headers=admin, json={"version": "gold-2026-09-25"})
    assert r.status_code == 201 and r.json()["version"] == "gold-2026-09-25"
    missing = client.get("/api/v1/dataset/versions/gold-v9/export", headers=admin)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "VERSION_NOT_FOUND"
    token = admin["Authorization"].split()[1]
    via_link = client.get("/api/v1/dataset/versions/gold-2026-09-25/export", params={"access_token": token})
    assert via_link.status_code == 200


@pytest.mark.parametrize("role", ["inspector", "supervisor"])
def test_dataset_is_for_ml_and_admin_only(client, finalized, role):
    h = login(client, role)
    assert client.get("/api/v1/dataset/items", headers=h).status_code == 403
    assert client.post("/api/v1/dataset/versions", headers=h, json={}).status_code == 403
    assert client.get("/api/v1/dataset/versions", headers=h).status_code == 403
    assert client.get("/api/v1/reports/retraining", headers=h).status_code == 403
    with db_module.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "dataset_publish")) == 0


def test_retraining_report_endpoint(client, finalized):
    r = client.get("/api/v1/reports/retraining", params={"days": 7}, headers=login(client, "ml"))
    assert r.status_code == 200
    rep = r.json()
    assert rep["period_days"] == 7
    assert rep["decisions"] == {"confirmed": 1, "rejected": 1, "clarification": 0}
    assert rep["rejection_by_reason"] == {"EXTRACTION_ERROR": 1}
    assert rep["rejection_by_section"] == {"КР": 1}
    assert rep["rejection_by_param"] == [{"code": "KR-059", "count": 1}]
    assert rep["confirm_rate"] == 0.5
    assert rep["dataset"] == {"draft_items": 2, "latest_version": None}
    assert isinstance(rep["recommendations"], list)
    bad = client.get("/api/v1/reports/retraining", params={"days": 0}, headers=login(client, "ml"))
    assert bad.status_code == 422


def test_models_registry_marks_current(client):
    r = client.get("/api/v1/models", headers=login(client))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["is_current"] is True
    assert items[0]["approval_status"] == "APPROVED" and "llm=" in items[0]["model_version"]
    for key in ("artifact", "dataset_version", "metrics", "approved_by", "deployed_at", "rollback_to"):
        assert key in items[0]
