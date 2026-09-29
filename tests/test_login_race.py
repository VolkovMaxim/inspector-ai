"""Защита входа (api/auth.py) при одновременных попытках: проверка «вход закрыт» и учёт попытки должны быть одним
шагом. Иначе пачка параллельных запросов успевает пройти проверку вся (между проверкой и учётом — bcrypt), и предел
неудач за окно обходится: пароль проверяется у каждого запроса пачки."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inspector import db as db_module
from inspector.models import AuditLog


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_parallel_wrong_passwords_are_checked_at_most_max_failures_times(client, monkeypatch):
    from inspector.api import auth as auth_mod

    real, checks = auth_mod.check_password, []

    def slow_check(password: str, hashed: str) -> bool:
        checks.append(password)
        time.sleep(0.3)                      # bcrypt небыстрый: остальные запросы пачки приходят, пока идёт проверка
        return real(password, hashed)

    monkeypatch.setattr(auth_mod, "check_password", slow_check)
    codes: list[int] = []

    def attempt() -> None:
        codes.append(client.post("/api/v1/auth/login", json={"login": "admin", "password": "Wr0ng-Pa55-zz"}).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checks) == 5 and sorted(codes) == [401] * 5 + [429] * 7
    with db_module.session_scope() as s:
        rows = [(r.action, r.user_login) for r in s.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert rows.count(("login_failed", "admin")) == 5 and rows.count(("login_locked", "admin")) == 1


def test_flood_of_other_logins_does_not_unlock_a_locked_pair(monkeypatch):
    """Память счётчика ограничена (MAX_TRACKED пар). Вытеснять сверх предела нужно сначала пары, для которых вход
    открыт: иначе поток неудач с другими логинами «освобождает» закрытую пару, и перебор её пароля продолжается."""
    from inspector.api.auth import LoginThrottle

    now = [0.0]
    t = LoginThrottle(5, 300, clock=lambda: now[0])
    monkeypatch.setattr(LoginThrottle, "MAX_TRACKED", 50)
    admin = ("admin", "1.2.3.4")
    for _ in range(5):
        t.failed(admin)
    assert t.retry_after(admin) > 0
    for i in range(200):
        now[0] += 0.01
        t.failed((f"junk{i}", "1.2.3.4"))
    assert t.retry_after(admin) > 0 and len(t._fails) <= 50
