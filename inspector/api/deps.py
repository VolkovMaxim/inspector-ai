"""Общие зависимости API: ошибки в формате {"error": …}, сессия БД, текущий пользователь (JWT), права ролей, аудит."""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Annotated, Any, Callable
from urllib.parse import quote

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from inspector.config import Settings
from inspector.db import get_db
from inspector.logging_setup import set_user
from inspector.models import AuditLog, Process, User

JWT_ALG = "HS256"
API_PREFIX = "/api/v1"          # полный префикс задаётся в каждом роутере: так маршрут в метриках — с /api/v1


class ApiError(StarletteHTTPException):
    """Ошибка API с машинным кодом. Наследник HTTPException: FastAPI пробрасывает её как есть даже из чтения тела."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None,
                 headers: dict[str, str] | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message
        self.details = details


def error_body(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def attachment_header(filename: str) -> str:
    """Content-Disposition для скачивания: ASCII-замена для старых клиентов + имя в UTF-8 (RFC 6266/5987)."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename) or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def iso(ts: dt.datetime | None) -> str | None:
    """Время в ISO 8601 с зоной: SQLite возвращает «наивное» UTC — дописываем зону."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.isoformat()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- настройки и БД

def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbDep = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------- JWT

def _signing_key(settings: Settings) -> bytes:
    """Ключ HMAC — SHA-256 от секрета: длина ключа всегда 32 байта (требование HS256, PyJWT предупреждает о коротких)."""
    return hashlib.sha256(settings.jwt_secret.encode("utf-8")).digest()


def issue_token(user: User, settings: Settings) -> str:
    now = utc_now()
    payload = {"sub": str(user.id), "login": user.login, "role": user.role, "iat": now,
               "exp": now + dt.timedelta(hours=settings.jwt_ttl_hours)}
    return jwt.encode(payload, _signing_key(settings), algorithm=JWT_ALG)


def _auth_error(code: str, message: str) -> ApiError:
    return ApiError(401, code, message, headers={"WWW-Authenticate": "Bearer"})


def _bearer_from_header(request: Request) -> str | None:
    value = request.headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _user_from_token(token: str | None, db: Session, settings: Settings) -> User:
    if not token:
        raise _auth_error("AUTH_REQUIRED", "Требуется вход в систему")
    try:
        payload = jwt.decode(token, _signing_key(settings), algorithms=[JWT_ALG],
                             options={"require": ["exp", "iat", "sub"]})
    except jwt.ExpiredSignatureError:
        raise _auth_error("TOKEN_EXPIRED", "Срок действия входа истёк — войдите снова") from None
    except jwt.InvalidTokenError:
        raise _auth_error("AUTH_INVALID", "Недействительный токен доступа") from None
    try:
        user = db.get(User, int(payload["sub"]))
    except (TypeError, ValueError):
        user = None
    if user is None or not user.is_active:
        raise _auth_error("AUTH_INVALID", "Пользователь не найден или отключён")
    set_user(user.id)
    return user


# Только объявление схемы в OpenAPI (кнопка Authorize в Swagger); токен разбирает _user_from_token, ошибки — наши.
BEARER = HTTPBearer(auto_error=False, description="access_token из POST /api/v1/auth/login")
BearerDoc = Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)]


def current_user(request: Request, db: DbDep, settings: SettingsDep, _: BearerDoc) -> User:
    return _user_from_token(_bearer_from_header(request), db, settings)


def current_user_media(request: Request, db: DbDep, settings: SettingsDep, _: BearerDoc) -> User:
    """Для картинок страниц и скачиваний: <img src> и ссылки не умеют слать заголовок, поэтому токен можно передать
    параметром ?access_token=… (в логи строка запроса не пишется)."""
    token = _bearer_from_header(request) or request.query_params.get("access_token")
    return _user_from_token(token, db, settings)


CurrentUser = Annotated[User, Depends(current_user)]
MediaUser = Annotated[User, Depends(current_user_media)]

# ---------------------------------------------------------------- роли

ROLES = ("inspector", "supervisor", "admin", "ml")
PERMISSIONS: dict[str, set[str]] = {
    # inspector: просмотр, загрузка, решения, финализация, отправка в РиН, правка типа страницы
    "inspector": {"read", "upload", "decide", "sync", "label"},
    # supervisor: всё, что inspector, + отмена финализации и журнал
    "supervisor": {"read", "upload", "decide", "sync", "unfinalize", "audit", "label"},
    # ml: просмотр, GOLD, отчёт по дообучению, журнал, разметка страниц, дообучение и версии классификатора
    "ml": {"read", "dataset", "report", "audit", "label", "retrain"},
    "admin": {"*"},
}


def has_perm(role: str, perm: str) -> bool:
    allowed = PERMISSIONS.get(role, set())
    return "*" in allowed or perm in allowed


def require(perm: str) -> Callable[..., User]:
    def dependency(user: CurrentUser) -> User:
        if not has_perm(user.role, perm):
            raise ApiError(403, "FORBIDDEN", "Недостаточно прав для этого действия",
                           details={"role": user.role, "required": perm})
        return user
    dependency.__name__ = f"require_{perm}"
    return dependency


def require_media(perm: str) -> Callable[..., User]:
    def dependency(user: MediaUser) -> User:
        if not has_perm(user.role, perm):
            raise ApiError(403, "FORBIDDEN", "Недостаточно прав для этого действия",
                           details={"role": user.role, "required": perm})
        return user
    dependency.__name__ = f"require_media_{perm}"
    return dependency


Reader = Annotated[User, Depends(require("read"))]
MediaReader = Annotated[User, Depends(require_media("read"))]

# ---------------------------------------------------------------- аудит


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def audit(db: Session, request: Request, user: User | None, action: str, *, process_id: str | None = None,
          object_code: str | None = None, details: dict | None = None, login: str | None = None) -> AuditLog:
    """Строка журнала в той же транзакции, что и само действие (коммитит вызывающий)."""
    row = AuditLog(user_pk=user.id if user else None,
                   user_login=((user.login if user else login) or "anonymous")[:64],
                   action=action, object_code=object_code, process_id=process_id, details=details or {},
                   ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300] or None)
    db.add(row)
    return row


# ---------------------------------------------------------------- поиск сущностей

def get_process_or_404(db: Session, process_id: str) -> Process:
    process = db.get(Process, process_id)
    if process is None:
        raise ApiError(404, "PROCESS_NOT_FOUND", "Проверка не найдена", details={"process_id": process_id})
    return process
