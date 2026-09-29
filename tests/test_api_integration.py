"""A6: отправка финализированного протокола в ИАИС РиН (заглушка), повторы 1/5/15 мин (ТЗ 9.6, п. «до 3 повторных
попыток»), приёмник-заглушка. Сеть не используется: httpx.MockTransport."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from inspector import db as db_module
from inspector import statuses as S
from inspector.models import AuditLog, Finding, Process, RinMessage, SyncAttempt


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
def finalized(db, example_process):
    """Пример в состоянии FINALIZED: KR-057 подтверждён, KR-059 отклонён."""
    for f in db.scalars(select(Finding).where(Finding.process_id == example_process.id)):
        if f.param_code == "KR-057":
            f.inspector_status = S.CONFIRMED_VIOLATION
        elif f.param_code == "KR-059":
            f.inspector_status, f.reason_code, f.comment = S.NEGATIVE_VERIFIED, "OCR_ERROR", "ошибка OCR"
    example_process.status = S.FINALIZED
    example_process.finalized_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return example_process


class Receiver:
    """Подменный приёмник РиН: отвечает заданными кодами по очереди и запоминает, что получил."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"ok": answer < 300})


def rin_headers() -> dict:
    """Заголовок с общим токеном РиН — тем же, что передаёт отправитель."""
    from inspector.api.integration import rin_token
    from inspector.config import get_settings

    return {"Authorization": f"Bearer {rin_token(get_settings())}"}


def attempts(process_id: str) -> list[tuple]:
    with db_module.session_scope() as s:
        return [(a.attempt, a.status, a.http_status) for a in
                s.scalars(select(SyncAttempt).where(SyncAttempt.process_id == process_id).order_by(SyncAttempt.id))]


def sync_status(process_id: str) -> str:
    with db_module.session_scope() as s:
        return s.get(Process, process_id).sync_status


def test_only_finalized_protocol_is_sent(client, example_process):
    r = client.post(f"/api/v1/inspection/{example_process.id}", headers=login(client))
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_FINALIZED"
    assert attempts(example_process.id) == []


def test_success_marks_synced_and_sends_confirmed_only(client, settings, finalized):
    receiver = Receiver(200)
    client.app.state.rin_transport = httpx.MockTransport(receiver)
    r = client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sync_status"] == "SYNCED"
    assert [(a["attempt"], a["status"], a["http_status"]) for a in body["attempts"]] == [(1, "SYNCED", 200)]
    assert len(receiver.seen) == 1 and str(receiver.seen[0].url) == settings.rin_url
    assert receiver.seen[0].headers["authorization"] == rin_headers()["Authorization"]   # общий токен РиН
    payload = __import__("json").loads(receiver.seen[0].content)
    assert payload["process_id"] == finalized.id and payload["object_id"] == "OBJ-NOVOSLOBODSKAYA"
    assert payload["protocol_version"] == 1 and payload["matrix_version"] == "matrix-132-r1.1"
    assert payload["model_version"]
    assert sorted(f["file_id"] for f in payload["input_files"]) == ["F0068", "F0105", "F0137"]   # без битого файла
    assert {"file_id", "sha256", "stage"} <= set(payload["input_files"][0])
    assert [c["code"] for c in payload["confirmed"]] == ["KR-057"]          # только подтверждённые инспектором
    assert payload["confirmed"][0]["status"] == S.CONFIRMED_VIOLATION
    assert sync_status(finalized.id) == "SYNCED"
    with db_module.session_scope() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "sync")).one()
        assert row.user_login == "inspector" and row.details["ok"] is True


@pytest.mark.parametrize("failure", [503, 502, 429, httpx.ReadTimeout("timeout"), httpx.ConnectError("refused")])
def test_server_error_or_timeout_schedules_retry_in_one_minute(client, finalized, failure):
    client.app.state.rin_transport = httpx.MockTransport(Receiver(failure))
    before = dt.datetime.now(dt.timezone.utc)
    r = client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    assert r.status_code == 200
    assert r.json()["sync_status"] == "PENDING_SYNC"
    rows = attempts(finalized.id)
    assert [(a, s) for a, s, _ in rows] == [(1, "FAILED"), (2, "SCHEDULED")]
    with db_module.session_scope() as s:
        nxt = s.scalars(select(SyncAttempt).where(SyncAttempt.status == "SCHEDULED")).one().next_try_at
    nxt = nxt if nxt.tzinfo else nxt.replace(tzinfo=dt.timezone.utc)
    assert 55 <= (nxt - before).total_seconds() <= 70


def test_client_error_fails_without_retries(client, finalized):
    client.app.state.rin_transport = httpx.MockTransport(Receiver(400))
    r = client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    assert r.json()["sync_status"] == "FAILED"
    assert attempts(finalized.id) == [(1, "FAILED", 400)]
    with db_module.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "RIN_SYNC_FAILED")) == 1


def test_retry_sync_from_worker_succeeds(client, finalized):
    from inspector.api.integration import retry_sync

    client.app.state.rin_transport = httpx.MockTransport(Receiver(503))
    client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    ok = Receiver(200)
    with db_module.session_scope() as s:
        due = s.scalars(select(SyncAttempt).where(SyncAttempt.status == "SCHEDULED")).one()
        assert retry_sync(s, due, transport=httpx.MockTransport(ok)) is None
    assert len(ok.seen) == 1 and ok.seen[0].headers["authorization"] == rin_headers()["Authorization"]
    assert attempts(finalized.id) == [(1, "FAILED", 503), (2, "SYNCED", 200)]
    assert sync_status(finalized.id) == "SYNCED"


def test_three_retries_with_growing_delays_then_failed(client, finalized):
    from inspector.api.integration import retry_sync

    always_down = httpx.MockTransport(Receiver(503))
    client.app.state.rin_transport = always_down
    client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    delays = []
    for _ in range(3):
        with db_module.session_scope() as s:
            due = s.scalars(select(SyncAttempt).where(SyncAttempt.status == "SCHEDULED")).one()
            created = due.created_at if due.created_at.tzinfo else due.created_at.replace(tzinfo=dt.timezone.utc)
            planned = due.next_try_at if due.next_try_at.tzinfo else due.next_try_at.replace(tzinfo=dt.timezone.utc)
            delays.append(round((planned - created).total_seconds() / 60))
            retry_sync(s, due, transport=always_down)
    assert delays == [1, 5, 15]
    assert [(a, s) for a, s, _ in attempts(finalized.id)] == [(1, "FAILED"), (2, "FAILED"), (3, "FAILED"),
                                                              (4, "FAILED")]
    assert sync_status(finalized.id) == "FAILED"
    with db_module.session_scope() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "RIN_SYNC_FAILED")).one()
        assert row.process_id == finalized.id and row.details["attempts"] == 4


def test_retry_is_cancelled_if_protocol_no_longer_finalized(client, db, finalized):
    from inspector.api.integration import retry_sync

    client.app.state.rin_transport = httpx.MockTransport(Receiver(503))
    client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    with db_module.session_scope() as s:
        s.get(Process, finalized.id).status = S.COMPLETED
    never = Receiver(200)
    with db_module.session_scope() as s:
        due = s.scalars(select(SyncAttempt).where(SyncAttempt.status == "SCHEDULED")).one()
        retry_sync(s, due, transport=httpx.MockTransport(never))
    assert never.seen == []
    assert attempts(finalized.id)[-1][1] == "FAILED"


def test_manual_resend_replaces_scheduled_retry(client, finalized):
    client.app.state.rin_transport = httpx.MockTransport(Receiver(503))
    h = login(client)
    client.post(f"/api/v1/inspection/{finalized.id}", headers=h)
    client.app.state.rin_transport = httpx.MockTransport(Receiver(200))
    r = client.post(f"/api/v1/inspection/{finalized.id}", headers=h)
    assert r.json()["sync_status"] == "SYNCED"
    assert [s for _, s, _ in attempts(finalized.id)] == ["FAILED", "FAILED", "SYNCED"]


def test_status_endpoint_and_roles(client, finalized):
    client.app.state.rin_transport = httpx.MockTransport(Receiver(200))
    assert client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client, "ml")).status_code == 403
    client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client, "supervisor"))
    r = client.get(f"/api/v1/inspection/{finalized.id}", headers=login(client, "ml"))
    assert r.status_code == 200
    body = r.json()
    assert body["process_id"] == finalized.id and body["sync_status"] == "SYNCED"
    assert {"attempt", "status", "http_status", "error", "next_try_at", "created_at"} <= set(body["attempts"][0])


# ---------------------------------------------------------------- приёмник-заглушка


def test_mock_receiver_stores_message(client):
    r = client.post("/api/v1/mock-rin/receive", json={"process_id": "p-1", "confirmed": []}, headers=rin_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["received"] is True and isinstance(body["id"], int)
    with db_module.session_scope() as s:
        msg = s.get(RinMessage, body["id"])
        assert msg.process_id == "p-1" and msg.payload == {"process_id": "p-1", "confirmed": []}
    bad = client.post("/api/v1/mock-rin/receive", json=[1, 2, 3], headers=rin_headers())
    assert bad.status_code == 422


def test_mock_receiver_can_simulate_outage(client, monkeypatch):
    monkeypatch.setenv("MOCK_RIN_FAIL", "1")
    r = client.post("/api/v1/mock-rin/receive", json={"process_id": "p-1"}, headers=rin_headers())
    assert r.status_code == 503 and r.json()["error"]["code"] == "RIN_UNAVAILABLE"
    with db_module.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(RinMessage)) == 0


def test_end_to_end_through_mock_receiver(client, finalized):
    """Отправка идёт в собственный приёмник-заглушку приложения (как в compose по умолчанию)."""
    def forward(request: httpx.Request) -> httpx.Response:
        r = client.post("/api/v1/mock-rin/receive", content=request.content,
                        headers={"content-type": "application/json",
                                 "authorization": request.headers.get("authorization", "")})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    client.app.state.rin_transport = httpx.MockTransport(forward)
    r = client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    assert r.json()["sync_status"] == "SYNCED"
    with db_module.session_scope() as s:
        msg = s.scalars(select(RinMessage)).one()
        assert msg.process_id == finalized.id and [c["code"] for c in msg.payload["confirmed"]] == ["KR-057"]


@pytest.mark.parametrize("headers, code", [({}, "AUTH_REQUIRED"),
                                           ({"Authorization": "Bearer "}, "AUTH_REQUIRED"),
                                           ({"Authorization": "Basic abc"}, "AUTH_REQUIRED"),
                                           ({"Authorization": "Bearer not-the-token"}, "AUTH_INVALID")])
def test_mock_receiver_rejects_missing_or_wrong_token(client, headers, code):
    r = client.post("/api/v1/mock-rin/receive", json=[1, 2, 3], headers=headers)   # токен проверяется раньше тела
    assert r.status_code == 401 and r.json()["error"]["code"] == code
    assert r.headers["www-authenticate"].lower().startswith("bearer")
    with db_module.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(RinMessage)) == 0


def test_sender_without_token_gets_401_and_fails_without_retries(client, finalized):
    """Если токены отправителя и приёмника разошлись, 401 — ошибка настройки: не повторяется, статус FAILED."""
    def forward_without_token(request: httpx.Request) -> httpx.Response:
        r = client.post("/api/v1/mock-rin/receive", content=request.content,
                        headers={"content-type": "application/json"})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    client.app.state.rin_transport = httpx.MockTransport(forward_without_token)
    r = client.post(f"/api/v1/inspection/{finalized.id}", headers=login(client))
    assert r.json()["sync_status"] == "FAILED"
    assert attempts(finalized.id) == [(1, "FAILED", 401)]


def test_configured_rin_token_is_used_and_no_file_is_created(settings, monkeypatch):
    from inspector import config as config_module
    from inspector.api.app import create_app
    from inspector.api.integration import RIN_TOKEN_FILE

    monkeypatch.setenv("RIN_TOKEN", "  s3cret-rin-token  ")
    config_module.get_settings.cache_clear()
    with TestClient(create_app()) as c:
        ok = c.post("/api/v1/mock-rin/receive", json={"process_id": "p"},
                    headers={"Authorization": "Bearer s3cret-rin-token"})
        other = c.post("/api/v1/mock-rin/receive", json={"process_id": "p"}, headers=rin_headers())
    assert ok.status_code == 200 and rin_headers()["Authorization"] == "Bearer s3cret-rin-token"
    assert other.status_code == 200
    assert not (settings.data_dir / RIN_TOKEN_FILE).exists()


def test_generated_rin_token_is_private_stable_and_repaired(settings):
    from inspector.api.integration import RIN_TOKEN_FILE, rin_token

    path = settings.data_dir / RIN_TOKEN_FILE
    first = rin_token(settings)
    assert len(first) == 64 and rin_token(settings) == first               # один раз на том данных
    assert path.read_text().strip() == first and (path.stat().st_mode & 0o777) == 0o600
    assert not list(settings.data_dir.glob(f"{RIN_TOKEN_FILE}.*.tmp"))
    path.write_text("")                                                     # пустой файл после сбоя записи
    repaired = rin_token(settings)
    assert len(repaired) == 64 and repaired != first and path.read_text().strip() == repaired
