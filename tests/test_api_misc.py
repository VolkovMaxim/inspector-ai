"""A8 (нормативная база, журнал аудита, /health, /metrics) и A6 (заглушка ИАИС РиН)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inspector import db as db_module
from inspector.models import AuditLog, Param


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str = "inspector") -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------- нормативная база


def test_params_list_is_readable_by_inspector(client):
    r = client.get("/api/v1/params", headers=login(client))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 132 and len(body["items"]) == 132
    kr = next(p for p in body["items"] if p["code"] == "KR-057")
    assert kr["section"] == "КР" and kr["is_active"] is True
    for field in ("parameter_name", "trigger_logic", "min_value", "max_value", "review_priority", "criticality",
                  "sp_reference", "gost_reference", "fz_reference", "other_normative", "unit"):
        assert field in kr


def test_params_filter_by_section_and_query(client):
    h = login(client)
    kr = client.get("/api/v1/params", params={"section": "КР"}, headers=h).json()
    assert kr["total"] > 0 and all(p["section"] == "КР" for p in kr["items"])
    one = client.get("/api/v1/params", params={"q": "KR-057"}, headers=h).json()
    assert [p["code"] for p in one["items"]] == ["KR-057"]


def test_admin_patch_param_updates_and_audits_old_and_new(client):
    h = login(client, "admin")
    before = client.get("/api/v1/params/KR-057", headers=h).json()
    r = client.patch("/api/v1/params/KR-057", headers=h, json={
        "trigger_logic": "Класс арматуры в РД/ИД ниже, чем в ПД", "min_value": 1.5, "max_value": 7,
        "is_active": False, "review_priority": "LOW", "sp_reference": "СП 63.13330.2018"})
    assert r.status_code == 200, r.text
    after = client.get("/api/v1/params/KR-057", headers=h).json()
    assert after["trigger_logic"] == "Класс арматуры в РД/ИД ниже, чем в ПД"
    assert after["min_value"] == 1.5 and after["max_value"] == 7.0
    assert after["is_active"] is False and after["review_priority"] == "LOW"
    assert after["sp_reference"] == "СП 63.13330.2018"
    with db_module.session_scope() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "params_patch")).one()
        changes = row.details["changes"]
        assert row.user_login == "admin" and row.details["code"] == "KR-057"
    assert changes["is_active"] == {"old": True, "new": False}
    assert changes["trigger_logic"]["old"] == before["trigger_logic"]
    assert "gost_reference" not in changes          # не присланное поле не трогается


def test_patch_param_null_clears_threshold(client):
    h = login(client, "admin")
    client.patch("/api/v1/params/KR-057", headers=h, json={"min_value": 3})
    r = client.patch("/api/v1/params/KR-057", headers=h, json={"min_value": None})
    assert r.status_code == 200 and r.json()["min_value"] is None


@pytest.mark.parametrize("payload, code", [
    ({"min_value": 10, "max_value": 1}, "INVALID_RANGE"),
    ({"review_priority": "URGENT"}, "VALIDATION_ERROR"),
    ({"parameter_name": "hack"}, "VALIDATION_ERROR"),       # поле вне разрешённого списка
    ({"is_active": "maybe"}, "VALIDATION_ERROR"),
])
def test_patch_param_rejects_bad_values(client, payload, code):
    h = login(client, "admin")
    r = client.patch("/api/v1/params/KR-057", headers=h, json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code
    with db_module.session_scope() as s:
        p = s.scalars(select(Param).where(Param.code == "KR-057")).one()
        assert p.parameter_name != "hack" and p.min_value is None and p.review_priority == "HIGH"


def test_patch_unknown_param_is_404(client):
    r = client.patch("/api/v1/params/XX-999", headers=login(client, "admin"), json={"is_active": False})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PARAM_NOT_FOUND"


@pytest.mark.parametrize("role", ["inspector", "supervisor", "ml"])
def test_only_admin_patches_params(client, role):
    r = client.patch("/api/v1/params/KR-057", headers=login(client, role), json={"is_active": False})
    assert r.status_code == 403


# ---------------------------------------------------------------- журнал аудита


def test_audit_readable_by_admin_supervisor_ml_with_filters(client):
    login(client, "inspector")
    client.post("/api/v1/auth/login", json={"login": "inspector", "password": "nope"})
    for role in ("admin", "supervisor", "ml"):
        r = client.get("/api/v1/audit", params={"action": "login", "user": "inspector"}, headers=login(client, role))
        assert r.status_code == 200, (role, r.text)
        items = r.json()["items"]
        assert items and all(i["action"] == "login" and i["user"] == "inspector" for i in items)
        assert {"id", "created_at", "ip", "user_agent", "details", "process_id", "object_code"} <= set(items[0])
    limited = client.get("/api/v1/audit", params={"limit": 1}, headers=login(client, "admin")).json()
    assert len(limited["items"]) == 1 and limited["total"] > 1
    newest_first = client.get("/api/v1/audit", headers=login(client, "admin")).json()["items"]
    assert [i["id"] for i in newest_first] == sorted((i["id"] for i in newest_first), reverse=True)


def test_inspector_sees_only_process_journal_without_ip(client):
    h = login(client, "inspector")
    assert client.get("/api/v1/audit", headers=h).status_code == 403
    r = client.get("/api/v1/audit", params={"process_id": "no-such-process"}, headers=h)
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------- /health и /metrics


def _llm_ok(request: "httpx.Request") -> "httpx.Response":
    import httpx

    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen", "object": "model"}]})
    return httpx.Response(404)


def _llm_down(request: "httpx.Request") -> "httpx.Response":
    import httpx

    raise httpx.ConnectError("connection refused", request=request)


def test_health_reports_db_llm_and_worker_without_auth(client):
    import httpx

    from inspector.models import ConstructionObject, Job, Process, utcnow

    with db_module.session_scope() as s:
        obj = ConstructionObject(object_code="OBJ-HEALTH", name="h")
        s.add(obj)
        s.flush()
        p = Process(object_pk=obj.id)
        s.add(p)
        s.flush()
        beat = utcnow()
        s.add_all([Job(process_id=p.id, status="queued"), Job(process_id=p.id, status="queued"),
                   Job(process_id=p.id, status="running", heartbeat_at=beat), Job(process_id=p.id, status="done")])
    client.app.state.llm_transport = httpx.MockTransport(_llm_ok)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    assert body["llm"]["url"] == "http://fake-llm/v1" and body["llm"]["reachable"] is True
    assert body["worker"]["queued_jobs"] == 2 and body["worker"]["running_jobs"] == 1
    assert body["worker"]["last_heartbeat"][:19] == beat.isoformat()[:19]


def test_health_is_degraded_when_llm_unreachable(client):
    import httpx

    client.app.state.llm_transport = httpx.MockTransport(_llm_down)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["llm"]["reachable"] is False
    assert body["worker"] == {"queued_jobs": 0, "running_jobs": 0, "last_heartbeat": None}


def test_health_checks_separate_ocr_url_used_by_reocr(settings, monkeypatch):
    """OCR_ENGINE=rapidocr (как по умолчанию): отобранные сканы перечитывает модель по OCR_URL (этап reocr). Если это
    свой адрес и он не отвечает, reocr молча оставит текст RapidOCR — /health должен это показать, а не «ok»."""
    import httpx

    from inspector import config as config_module
    from inspector.api.app import create_app

    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    monkeypatch.setenv("OCR_URL", "http://fake-ocr/v1")
    config_module.get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fake-ocr":
            raise httpx.ConnectError("connection refused", request=request)
        return _llm_ok(request)

    with TestClient(create_app()) as c:
        c.app.state.llm_transport = httpx.MockTransport(handler)
        body = c.get("/health").json()
    assert body["llm"]["reachable"] is True
    assert body.get("ocr", {}).get("reachable") is False and body["status"] == "degraded"


def test_metrics_count_requests_by_route_template_and_status(client):
    client.get("/api/v1/auth/me")                                        # 401
    client.get("/api/v1/auth/me")
    login(client)
    text = client.get("/metrics").text
    assert 'inspector_http_requests_total{method="GET",route="/api/v1/auth/me",status="401"} 2.0' in text
    assert 'inspector_http_requests_total{method="POST",route="/api/v1/auth/login",status="200"} 1.0' in text
    assert 'inspector_http_request_duration_seconds_count{method="GET",route="/api/v1/auth/me"} 2.0' in text
    assert 'inspector_jobs{status="queued"} 0.0' in text
    assert 'inspector_processes{status="READY"} 0.0' in text


def test_metrics_use_route_template_not_raw_path(client):
    h = login(client)
    client.get("/api/v1/params/KR-057", headers=h)
    client.get("/api/v1/params/KR-058", headers=h)
    text = client.get("/metrics").text
    assert 'route="/api/v1/params/{code}",status="200"} 2.0' in text
    assert "KR-057" not in text


def test_metrics_include_host_load_memory_disk_and_active_users(client):
    import datetime as dt

    from inspector.models import User

    login(client, "inspector")
    login(client, "inspector")
    login(client, "supervisor")
    client.post("/api/v1/auth/login", json={"login": "admin", "password": "nope"})      # неудача — не активность
    with db_module.session_scope() as s:
        ml = s.scalars(select(User).where(User.login == "ml")).one()
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)
        s.add_all([AuditLog(user_pk=ml.id, user_login="ml", action="login", created_at=old),   # вне 15 мин
                   AuditLog(user_login="system", action="PIPELINE_FAILED")])
    text = client.get("/metrics").text
    assert "inspector_active_users 2.0" in text
    for period in ("1m", "5m", "15m"):
        assert f'inspector_host_load_average{{period="{period}"}}' in text
    assert "inspector_host_cpus " in text
    for kind in ("total", "used", "free"):
        assert f'inspector_data_disk_bytes{{kind="{kind}"}}' in text
    if Path("/proc/meminfo").is_file():
        assert 'inspector_host_memory_bytes{kind="total"}' in text
        assert 'inspector_host_memory_bytes{kind="available"}' in text
    free = float(next(line.split()[-1] for line in text.splitlines()
                      if line.startswith('inspector_data_disk_bytes{kind="free"}')))
    assert free > 0


def test_read_meminfo_parses_kilobytes_and_skips_bad_lines(tmp_path):
    from inspector.api.monitoring import read_meminfo

    f = tmp_path / "meminfo"
    f.write_text("MemTotal:       32768 kB\nMemFree:  100 kB\nMemAvailable:   xx kB\nGarbage\n", encoding="ascii")
    assert read_meminfo(f) == {"total": 32768 * 1024}                    # битое MemAvailable пропущено
    assert read_meminfo(tmp_path / "no-such-file") == {}


def test_metrics_are_served_when_host_sources_are_unavailable(client, monkeypatch, tmp_path):
    import os

    from inspector.api import monitoring

    def no_loadavg():
        raise OSError("нет в этой ОС")

    def no_disk(path):
        raise OSError("том не смонтирован")

    monkeypatch.setattr(os, "getloadavg", no_loadavg)
    monkeypatch.setattr(monitoring.shutil, "disk_usage", no_disk)
    monkeypatch.setattr(monitoring, "MEMINFO", tmp_path / "absent")                  # не Linux
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "inspector_host_load_average{" not in r.text and "inspector_data_disk_bytes{" not in r.text
    assert "inspector_host_memory_bytes{" not in r.text
    assert "inspector_http_requests_total" in r.text and "inspector_active_users" in r.text
