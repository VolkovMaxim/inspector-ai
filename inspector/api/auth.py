"""Вход по логину и паролю → JWT (HS256); текущий пользователь; защита входа от перебора паролей."""
from __future__ import annotations

import datetime as dt
import math
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Callable

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from inspector.api.deps import (API_PREFIX, ApiError, CurrentUser, DbDep, SettingsDep, audit, client_ip, issue_token,
                                iso, utc_now)
from inspector.models import User
from inspector.seed import check_password, hash_password

router = APIRouter(prefix=f"{API_PREFIX}/auth", tags=["auth"])


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: int
    login: str
    role: str
    full_name: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserOut


def user_out(u: User) -> dict:
    return {"id": u.id, "login": u.login, "role": u.role, "full_name": u.full_name}


@lru_cache
def _dummy_hash() -> str:
    return hash_password("timing-equalizer")


class LoginThrottle:
    """Защита входа от перебора (в памяти процесса): неудачные попытки по паре (логин, IP) в скользящем окне.
    После max_failures неудач за window_s секунд вход для пары закрыт — даже с верным паролем, — пока самая старая
    из них не выйдет из окна (429 + Retry-After). Успешный вход сбрасывает счётчик. У каждого процесса api свой
    счётчик: при uvicorn --workers N предел — N × max_failures. max_failures <= 0 — защита выключена."""

    MAX_TRACKED = 10_000             # пар в памяти; сверх — выбрасываются устаревшие, затем давно не встречавшиеся

    def __init__(self, max_failures: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.max_failures, self.window_s, self._clock = max_failures, window_s, clock
        self._fails: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()          # ручка входа синхронная — выполняется в пуле потоков

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        q = self._fails.get(key) or deque()
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q

    def retry_after(self, key: tuple[str, str]) -> int:
        """Через сколько секунд паре можно пробовать снова; 0 — можно сейчас."""
        if self.max_failures <= 0:
            return 0
        with self._lock:
            now = self._clock()
            q = self._recent(key, now)
            if len(q) < self.max_failures:
                return 0
            return max(1, math.ceil(q[-self.max_failures] + self.window_s - now))

    def failed(self, key: tuple[str, str]) -> bool:
        """Учесть неудачную попытку. -> True, если именно она закрыла вход для пары."""
        if self.max_failures <= 0:
            return False
        with self._lock:
            return len(self._add(key, self._clock())) == self.max_failures

    def acquire(self, key: tuple[str, str]) -> tuple[int, bool]:
        """Проверка «вход закрыт» и учёт попытки одним шагом: попытка сразу считается неудачей, успешный вход снимает
        её reset(). Иначе пачка параллельных запросов проходит проверку вся, пока идёт bcrypt, и пароль проверяется у
        каждого. -> (секунд до открытия, 0 — попытка разрешена; эта попытка заняла последнее место в окне)."""
        if self.max_failures <= 0:
            return 0, False
        with self._lock:
            now = self._clock()
            q = self._recent(key, now)
            if len(q) >= self.max_failures:
                return max(1, math.ceil(q[-self.max_failures] + self.window_s - now)), False
            return 0, len(self._add(key, now)) == self.max_failures

    def _add(self, key: tuple[str, str], now: float) -> deque[float]:
        q = self._recent(key, now)
        q.append(now)
        self._fails.pop(key, None)
        self._fails[key] = q                     # в конец словаря: при переполнении вытесняются давние пары
        if len(self._fails) > self.MAX_TRACKED:
            self._prune(now)
        return q

    def reset(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._fails.items() if not q or q[-1] <= now - self.window_s]:
            del self._fails[key]
        # сверх предела — сначала давние пары, для которых вход открыт: иначе поток неудач с другими логинами вытеснил бы
        # закрытую пару и снял с неё блокировку
        for key in [k for k, q in self._fails.items() if len(q) < self.max_failures]:
            if len(self._fails) <= self.MAX_TRACKED:
                break
            del self._fails[key]
        while len(self._fails) > self.MAX_TRACKED:
            del self._fails[next(iter(self._fails))]


@router.post("/login", response_model=LoginOut, summary="Вход: логин и пароль → токен (перебор паролей → 429)",
             responses={429: {"description": "Слишком много неудачных попыток; заголовок Retry-After, с"}})
def login(body: LoginIn, request: Request, db: DbDep, settings: SettingsDep) -> dict:
    name = body.login.strip()
    key = (name, client_ip(request) or "")
    throttle: LoginThrottle = request.app.state.login_throttle
    wait, last = throttle.acquire(key)
    if wait:                         # без проверки пароля и без записи в БД: закрытие входа уже в журнале
        raise ApiError(429, "TOO_MANY_LOGIN_ATTEMPTS", "Слишком много неудачных попыток входа — повторите позже",
                       details={"retry_after_s": wait}, headers={"Retry-After": str(wait)})
    user = db.scalars(select(User).where(User.login == name)).first()
    ok = check_password(body.password, user.password_hash if user else _dummy_hash())
    if user is None or not ok or not user.is_active:
        audit(db, request, None, "login_failed", login=name,
              details={"reason": "inactive" if (user is not None and ok) else "bad_credentials"})
        if last:
            audit(db, request, None, "login_locked", login=name,
                  details={"failures": throttle.max_failures, "window_s": throttle.window_s,
                           "retry_after_s": throttle.retry_after(key)})
        db.commit()
        raise ApiError(401, "AUTH_FAILED", "Неверный логин или пароль")
    throttle.reset(key)
    token = issue_token(user, settings)
    audit(db, request, user, "login")
    db.commit()
    expires = utc_now() + dt.timedelta(hours=settings.jwt_ttl_hours)
    return {"access_token": token, "token_type": "bearer", "expires_at": iso(expires), "user": user_out(user)}


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
def me(user: CurrentUser) -> dict:
    return user_out(user)
