"""JSON-логи сервиса: одна строка — один объект {timestamp, level, service, message, request_id, user_id, …}.

request_id и user_id берутся из контекста запроса (contextvars). В контексте лежит изменяемый словарь: FastAPI
выполняет синхронные зависимости в пуле потоков с копией контекста, и присваивание переменной там не было бы видно
middleware; изменение общего словаря — видно. Секреты (пароли, токены) в логи не пишутся: middleware логирует путь без
строки запроса, тела запросов не логируются."""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from contextvars import ContextVar, Token

SERVICE_NAME = "inspector-api"

_request_ctx: ContextVar[dict | None] = ContextVar("inspector_request_ctx", default=None)

# стандартные атрибуты LogRecord — всё остальное считается полями extra=…
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName", "color_message"}


def bind_request(request_id: str) -> Token:
    return _request_ctx.set({"request_id": request_id, "user_id": None})


def reset_request(token: Token) -> None:
    _request_ctx.reset(token)


def set_user(user_id: int | str | None) -> None:
    ctx = _request_ctx.get()
    if ctx is not None:
        ctx["user_id"] = user_id


def current_request_id() -> str | None:
    ctx = _request_ctx.get()
    return ctx.get("request_id") if ctx else None


def current_user_id() -> int | str | None:
    ctx = _request_ctx.get()
    return ctx.get("user_id") if ctx else None


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = SERVICE_NAME):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = _request_ctx.get() or {}
        out = {
            "timestamp": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": ctx.get("request_id"),
            "user_id": ctx.get("user_id"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_") and key not in out:
                out[key] = _jsonable(value)
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


class _StdoutHandler(logging.StreamHandler):
    """Пишет в текущий sys.stdout (а не в тот, что был при создании): так не ломается перехват вывода в тестах."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property  # type: ignore[override]
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


def setup_logging(level: str = "INFO", service: str = SERVICE_NAME) -> None:
    """Корневой логгер пишет JSON в stdout; повторный вызов не плодит обработчики. Логи uvicorn идут тем же форматом,
    его access-лог отключён: запросы логирует middleware приложения (с request_id и user_id, без строки запроса)."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = next((h for h in root.handlers if isinstance(h, _StdoutHandler)), None)
    if handler is None:
        handler = _StdoutHandler()
        root.addHandler(handler)
    handler.setFormatter(JsonFormatter(service))
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)      # исходящие запросы (проверка модели, РиН) — без шума
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True
