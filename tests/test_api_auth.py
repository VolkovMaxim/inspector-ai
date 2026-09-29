"""A1: приложение, авторизация, роли, формат ошибок, X-Request-ID, JSON-логи, статика SPA, OpenAPI."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inspector import config as config_module
from inspector import db as db_module
from inspector.models import AuditLog


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str = "inspector") -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_returns_bearer_token_and_user(client):
    r = client.post("/api/v1/auth/login", json={"login": "inspector", "password": "inspector"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"].count(".") == 2
    assert body["user"]["login"] == "inspector"
    assert body["user"]["role"] == "inspector"
    assert body["user"]["full_name"] == "Инспектор (демо)"
    assert isinstance(body["user"]["id"], int)


@pytest.mark.parametrize("payload", [{"login": "inspector", "password": "wrong"},
                                     {"login": "nobody", "password": "inspector"}])
def test_login_with_bad_credentials_is_401_auth_failed(client, payload):
    r = client.post("/api/v1/auth/login", json=payload)
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "AUTH_FAILED"
    assert err["message"]


# ---------------------------------------------------------------- защита входа от перебора


def bad_login(client, user: str = "inspector"):
    return client.post("/api/v1/auth/login", json={"login": user, "password": "Wr0ng-Pa55-zz"})


def test_login_is_blocked_after_max_failures_even_with_right_password(client):
    assert [bad_login(client).status_code for _ in range(5)] == [401] * 5
    r = client.post("/api/v1/auth/login", json={"login": "inspector", "password": "inspector"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
    wait = int(r.headers["retry-after"])
    assert 290 <= wait <= 300 and r.json()["error"]["details"] == {"retry_after_s": wait}
    assert bad_login(client).status_code == 429                          # пароль уже не проверяется
    assert client.post("/api/v1/auth/login", json={"login": "supervisor", "password": "supervisor"}).status_code == 200
    with db_module.session_scope() as s:
        rows = [(r.action, r.user_login) for r in s.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert rows.count(("login_failed", "inspector")) == 5                 # заблокированные попытки БД не нагружают
    assert rows.count(("login_locked", "inspector")) == 1


def test_successful_login_resets_failure_counter(client):
    assert [bad_login(client).status_code for _ in range(4)] == [401] * 4
    login(client)
    assert [bad_login(client).status_code for _ in range(4)] == [401] * 4   # счёт начат заново
    assert bad_login(client).status_code == 401                            # пятая неудача подряд закрывает вход
    assert bad_login(client).status_code == 429


def test_login_throttle_settings_come_from_env(settings, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "2")
    monkeypatch.setenv("LOGIN_WINDOW_S", "60")
    config_module.get_settings.cache_clear()
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        assert [bad_login(c).status_code for _ in range(3)] == [401, 401, 429]
        assert 1 <= int(bad_login(c).headers["retry-after"]) <= 60
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "0")                          # 0 — защита выключена
    config_module.get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert {bad_login(c).status_code for _ in range(8)} == {401}


def test_login_throttle_window_slides_and_pairs_are_independent():
    from inspector.api.auth import LoginThrottle

    now = [1000.0]
    t = LoginThrottle(3, 300, clock=lambda: now[0])
    key, other_ip = ("inspector", "10.0.0.1"), ("inspector", "10.0.0.2")
    assert [t.failed(key) for _ in range(2)] == [False, False] and t.retry_after(key) == 0
    now[0] += 100
    assert t.failed(key) is True and t.retry_after(key) == 200             # старейшая неудача выйдет через 200 с
    assert t.retry_after(other_ip) == 0 and t.retry_after(("admin", "10.0.0.1")) == 0
    now[0] += 199.5
    assert t.retry_after(key) == 1                                          # округление вверх, не 0
    now[0] += 0.5
    assert t.retry_after(key) == 0                                          # две неудачи в окне — можно пробовать
    t.reset(key)
    assert t.failed(key) is False


def test_login_throttle_memory_is_bounded(monkeypatch):
    from inspector.api.auth import LoginThrottle

    now = [0.0]
    t = LoginThrottle(5, 60, clock=lambda: now[0])
    monkeypatch.setattr(LoginThrottle, "MAX_TRACKED", 100)
    for i in range(150):
        t.failed((f"user{i}", "1.2.3.4"))
    assert len(t._fails) == 100 and ("user149", "1.2.3.4") in t._fails       # вытеснены самые давние пары
    now[0] += 61                                                            # все прежние неудачи вне окна
    t.failed(("fresh", "1.2.3.4"))
    assert list(t._fails) == [("fresh", "1.2.3.4")]                         # переполнение чистит устаревшие


def test_me_returns_user_of_the_token(client):
    r = client.get("/api/v1/auth/me", headers=login(client, "supervisor"))
    assert r.status_code == 200
    assert r.json()["login"] == "supervisor" and r.json()["role"] == "supervisor"


def test_processes_without_token_is_401(client):
    r = client.get("/api/v1/processes")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"
    assert r.headers["www-authenticate"].lower().startswith("bearer")


def test_garbage_token_is_401_auth_invalid(client):
    r = client.get("/api/v1/processes", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"


def test_expired_token_is_rejected(settings, monkeypatch):
    monkeypatch.setenv("JWT_TTL_HOURS", "-1")          # выданный токен уже просрочен
    config_module.get_settings.cache_clear()
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/v1/auth/me", headers=login(c))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_token_signed_with_another_secret_is_rejected(settings, monkeypatch):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        foreign = login(c)
    monkeypatch.setenv("JWT_SECRET", "a-completely-different-secret-value-0123456789")
    config_module.get_settings.cache_clear()
    with TestClient(create_app()) as c:
        r = c.get("/api/v1/auth/me", headers=foreign)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"


def test_inactive_user_token_is_rejected(client):
    headers = login(client, "ml")
    with db_module.session_scope() as s:
        from inspector.models import User
        s.scalars(select(User).where(User.login == "ml")).one().is_active = False
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    r = client.post("/api/v1/auth/login", json={"login": "ml", "password": "ml"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_FAILED"


def test_inspector_cannot_patch_params(client):
    r = client.patch("/api/v1/params/KR-057", json={"is_active": False}, headers=login(client, "inspector"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_login_is_audited_with_ip_and_user_agent(client):
    login(client, "inspector")
    client.post("/api/v1/auth/login", json={"login": "inspector", "password": "Wr0ng-Pa55-zz"})
    with db_module.session_scope() as s:
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        got = [(r.action, r.user_login, r.ip, r.user_agent) for r in rows]
    assert ("login", "inspector", "testclient", "testclient") in got
    assert ("login_failed", "inspector", "testclient", "testclient") in got
    assert all("Wr0ng-Pa55-zz" not in json.dumps(r.details, ensure_ascii=False) for r in rows)   # пароль не пишется


def test_request_id_is_generated_or_echoed(client):
    r = client.get("/api/v1/auth/me")                       # даже у ответа 401 есть X-Request-ID
    assert r.status_code == 401 and len(r.headers["x-request-id"]) >= 16
    r2 = client.get("/api/v1/auth/me", headers={"X-Request-ID": "trace-42"})
    assert r2.headers["x-request-id"] == "trace-42"
    r3 = client.get("/api/v1/auth/me", headers={"X-Request-ID": "bad id with spaces"})
    assert r3.headers["x-request-id"] != "bad id with spaces"


def test_unknown_api_route_is_404_in_error_format(client):
    r = client.get("/api/v1/definitely-not-here", headers=login(client))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_validation_error_format_does_not_echo_input(client):
    r = client.post("/api/v1/auth/login", json={"login": "inspector"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert any("password" in ".".join(map(str, d["loc"])) for d in err["details"])
    assert all("input" not in d for d in err["details"])


def test_json_access_log_has_request_and_user(client):
    from inspector.logging_setup import JsonFormatter

    records: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = ListHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("inspector")
    logger.addHandler(handler)
    try:
        headers = login(client, "supervisor")
        client.get("/api/v1/auth/me", headers={**headers, "X-Request-ID": "req-777"})
    finally:
        logger.removeHandler(handler)
    token = headers["Authorization"].split()[1]
    lines = [json.loads(x) for x in records]
    mine = [x for x in lines if x.get("request_id") == "req-777"]
    assert mine, lines
    entry = mine[-1]
    assert entry["service"] == "inspector-api"
    assert entry["level"] == "INFO"
    assert entry["user_id"] is not None
    assert entry["timestamp"].endswith("+00:00") or entry["timestamp"].endswith("Z")
    assert entry["message"]
    assert all(token not in raw for raw in records)             # токен в логи не попадает


def test_json_formatter_includes_extra_fields_and_exceptions():
    from inspector.logging_setup import JsonFormatter, bind_request, reset_request, set_user

    token = bind_request("rid-1")
    try:
        set_user(5)
        try:
            raise ValueError("boom")
        except ValueError:
            rec = logging.getLogger("x").makeRecord("x", logging.ERROR, __file__, 1, "сбой %s", ("этапа",),
                                                     exc_info=__import__("sys").exc_info(), extra={"stage": "ocr"})
        out = json.loads(JsonFormatter().format(rec))
    finally:
        reset_request(token)
    assert out["message"] == "сбой этапа"
    assert out["request_id"] == "rid-1" and out["user_id"] == 5
    assert out["stage"] == "ocr"
    assert "ValueError: boom" in out["exc_info"]


def test_cors_allows_vite_dev_origin(client):
    r = client.options("/api/v1/processes", headers={"Origin": "http://localhost:5173",
                                                     "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r2 = client.options("/api/v1/processes", headers={"Origin": "http://evil.example",
                                                      "Access-Control-Request-Method": "GET"})
    assert r2.headers.get("access-control-allow-origin") is None


def test_openapi_and_docs_are_served(client):
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/v1/auth/login" in paths and "/api/v1/auth/me" in paths
    assert client.get("/api/docs").status_code == 200
    assert not [p for p in paths if p.startswith("/api/docs")]                # страница и статика — вне схемы


def test_swagger_ui_works_without_internet(client):
    """Стенд жюри без интернета: страница /api/docs не ссылается на внешние хосты, файлы отдаёт само приложение."""
    import re

    from inspector.config import RESOURCES

    page = client.get("/api/docs")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    assert re.findall(r"(?:https?:)?//[\w.-]+", html) == []                  # ни CDN, ни значка с fastapi.tiangolo.com
    assert "'/api/v1/openapi.json'" in html and '"validatorUrl": null' in html   # без онлайн-валидатора
    for name, kind in (("swagger-ui-bundle.js", "text/javascript"), ("swagger-ui.css", "text/css"),
                       ("favicon-32x32.png", "image/png")):
        assert f"/api/docs/static/{name}" in html
        r = client.get(f"/api/docs/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
        assert r.content == (RESOURCES / "swagger" / name).read_bytes()
    assert b"SwaggerUIBundle" in client.get("/api/docs/static/swagger-ui-bundle.js").content
    license_text = (RESOURCES / "swagger" / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text and "Version 2.0" in license_text
    assert (RESOURCES / "swagger" / "NOTICE").is_file()


@pytest.mark.parametrize("path", ["/api/docs/static/LICENSE", "/api/docs/static/nope.js",
                                  "/api/docs/static/..%2F..%2Fconfig.py", "/api/docs/static/%2E%2E"])
def test_swagger_static_serves_only_listed_files(client, path):
    r = client.get(path)
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    assert b"Settings" not in r.content


def test_spa_static_and_index_fallback(settings, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>inspector-spa</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    monkeypatch.setenv("WEB_DIST", str(dist))
    config_module.get_settings.cache_clear()
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        root = c.get("/")
        deep = c.get("/processes/123/findings/5")
        asset = c.get("/assets/app.js")
        escape = c.get("/assets/..%2F..%2Fsecret.txt")
        api = c.get("/api/v1/nope")
    assert root.status_code == 200 and "inspector-spa" in root.text
    assert deep.status_code == 200 and "inspector-spa" in deep.text
    assert asset.status_code == 200 and asset.text == "console.log(1)"
    assert "top secret" not in escape.text
    assert api.status_code == 404 and api.json()["error"]["code"] == "NOT_FOUND"


def test_without_web_build_root_is_json_404(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_DIST", str(tmp_path / "no-build"))        # сборки интерфейса нет
    config_module.get_settings.cache_clear()
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        r = c.get("/some/page")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WEB_NOT_BUILT"


def test_module_level_app_is_created_lazily(settings):
    import inspector.api.app as app_module
    from fastapi import FastAPI

    assert isinstance(app_module.app, FastAPI)
    assert app_module.app is app_module.app


def test_openapi_declares_bearer_for_swagger_authorize(client):
    """Swagger UI показывает «Authorize» только если схема объявлена; иначе «Try it out» у защищённых методов — 401."""
    spec = client.get("/api/v1/openapi.json").json()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert {"HTTPBearer": []} in spec["paths"]["/api/v1/processes"]["get"]["security"]
    assert "security" not in spec["paths"]["/api/v1/auth/login"]["post"]
    r = client.get("/api/v1/processes")
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_REQUIRED"

