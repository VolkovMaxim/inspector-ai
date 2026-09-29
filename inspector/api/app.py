"""Приложение FastAPI: /api/v1 (REST JSON), /health, /metrics, статика React-сборки (SPA).

`create_app()` ничего не подключает при импорте: БД и начальное заполнение — на старте (lifespan). Объект `app` для
`uvicorn inspector.api.app:app` создаётся лениво при первом обращении к атрибуту модуля."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inspector.api import (admin, auth, dataset, docs, files, findings, integration, ml, monitoring, processes,
                           protocol_api)
from inspector.api.deps import API_PREFIX, ApiError, error_body
from inspector.config import Settings, get_settings
from inspector.db import init_engine, session_scope
from inspector.logging_setup import bind_request, reset_request, setup_logging
from inspector.seed import seed_all
from inspector.verification import DomainError

log = logging.getLogger("inspector.api")

UPLOAD_PATH = f"{API_PREFIX}/documents/upload"
MB = 1024 * 1024
MULTIPART_MARGIN = 1 * MB            # заголовки частей multipart сверх суммы файлов (точная сумма проверяется в ручке)
JSON_BODY_LIMIT = 16 * MB            # прочие запросы — небольшие JSON
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
DEFAULT_JWT_SECRET = "change-me-in-production"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
QUIET_PATHS = {"/health", "/metrics"}

HTTP_ERRORS = {
    400: ("BAD_REQUEST", "Некорректный запрос"),
    401: ("AUTH_REQUIRED", "Требуется вход в систему"),
    403: ("FORBIDDEN", "Недостаточно прав для этого действия"),
    404: ("NOT_FOUND", "Не найдено"),
    405: ("METHOD_NOT_ALLOWED", "Метод не поддерживается"),
    409: ("CONFLICT", "Конфликт состояния"),
    413: ("PAYLOAD_TOO_LARGE", "Слишком большой запрос"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Неподдерживаемый тип содержимого"),
    429: ("TOO_MANY_REQUESTS", "Слишком много запросов"),
}


# ---------------------------------------------------------------- middleware


def _content_length(headers: Headers) -> int | None:
    try:
        return int(headers["content-length"])
    except (KeyError, ValueError):
        return None


async def _send_json(send: Send, status: int, body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]})
    await send({"type": "http.response.body", "body": raw})


class RequestContextMiddleware:
    """X-Request-ID, лимит тела запроса (до чтения — по Content-Length, при чтении — по факту), метрики Prometheus,
    строка access-лога (путь без строки запроса: там может быть access_token) и JSON-ответ 500 на необработанную
    ошибку (с request_id для поиска в логах)."""

    def __init__(self, app: ASGIApp, settings: Settings, metrics: monitoring.Metrics) -> None:
        self.app = app
        self.settings = settings
        self.metrics = metrics

    def body_limit(self, path: str) -> tuple[int, str, str]:
        if path == UPLOAD_PATH:
            limit_mb = self.settings.max_upload_mb
            return (limit_mb * MB + MULTIPART_MARGIN, "UPLOAD_TOO_LARGE",
                    f"Суммарный размер загрузки больше {limit_mb} МБ")
        return JSON_BODY_LIMIT, "PAYLOAD_TOO_LARGE", "Слишком большой запрос"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        incoming = headers.get("x-request-id", "")
        rid = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = bind_request(rid)
        started = time.perf_counter()
        state = {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"], state["started"] = message["status"], True
                MutableHeaders(scope=message).append("X-Request-ID", rid)
            await send(message)

        try:
            limit, code, message = self.body_limit(scope["path"])
            declared = _content_length(headers)
            if declared is not None and declared > limit:
                await _send_json(send_wrapper, 413, error_body(code, message, {"limit_bytes": limit,
                                                                                "content_length": declared}))
                return
            received = 0

            async def receive_wrapper() -> Message:
                nonlocal received
                msg = await receive()
                if msg["type"] == "http.request":
                    received += len(msg.get("body", b""))
                    if received > limit:
                        raise ApiError(413, code, message, details={"limit_bytes": limit})
                return msg

            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception:
            log.exception("Необработанная ошибка", extra={"method": scope.get("method"), "path": scope.get("path")})
            if not state["started"]:
                await _send_json(send_wrapper, 500, error_body("INTERNAL_ERROR", "Внутренняя ошибка сервера",
                                                                {"request_id": rid}))
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            try:
                self.metrics.observe(scope.get("method", ""), route, state["status"], elapsed)
            except Exception:  # noqa: BLE001 — метрики не должны ломать ответ
                pass
            level = logging.DEBUG if scope["path"] in QUIET_PATHS else logging.INFO
            log.log(level, f"{scope.get('method')} {scope['path']} {state['status']}",
                    extra={"method": scope.get("method"), "path": scope["path"], "route": route,
                           "status": state["status"], "duration_ms": round(elapsed * 1000, 1)})
            reset_request(token)


# ---------------------------------------------------------------- ошибки и SPA


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/") or path in QUIET_PATHS


def _spa_response(request: Request) -> Response | None:
    """Файл сборки интерфейса или index.html для неизвестных путей вне /api (маршрутизация React)."""
    if request.method not in ("GET", "HEAD") or _is_api_path(request.url.path):
        return None
    dist = Path(request.app.state.settings.web_dist)
    index = dist / "index.html"
    if not index.is_file():
        return None
    rel = request.url.path.lstrip("/")
    if rel:
        try:
            root = dist.resolve()
            candidate = (root / rel).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        except (OSError, ValueError):
            pass
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    if isinstance(exc, ApiError):
        return JSONResponse(error_body(exc.code, exc.message, exc.details), status_code=exc.status_code,
                            headers=exc.headers)
    if exc.status_code == 404:
        spa = _spa_response(request)
        if spa is not None:
            return spa
        if not _is_api_path(request.url.path) and request.method in ("GET", "HEAD"):
            return JSONResponse(error_body("WEB_NOT_BUILT", "Интерфейс не собран: выполните npm run build в "
                                                            "service/web"), status_code=404)
    code, message = HTTP_ERRORS.get(exc.status_code, (f"HTTP_{exc.status_code}", str(exc.detail)))
    details = {"reason": str(exc.detail)} if exc.detail and exc.status_code == 400 else None
    return JSONResponse(error_body(code, message, details), status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(error_body(exc.code, exc.message, exc.details), status_code=exc.status_code)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # без поля input: в нём могут оказаться присланные данные (например, пароль)
    details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
               for e in exc.errors()]
    return JSONResponse(error_body("VALIDATION_ERROR", "Ошибка в данных запроса", details), status_code=422)


# ---------------------------------------------------------------- приложение


def _startup(settings: Settings) -> None:
    init_engine(settings.db_url)
    try:
        with session_scope() as db:
            seed_all(db, settings)
    except IntegrityError:          # два процесса api стартовали одновременно — заполнил другой
        log.warning("Начальное заполнение выполнено другим процессом")
    if settings.jwt_secret == DEFAULT_JWT_SECRET:
        log.warning("JWT_SECRET по умолчанию — в эксплуатации задайте свой секрет (переменная JWT_SECRET)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(_startup, app.state.settings)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(title="Инспектор ИИ — API", version="1.0.0",
                  description="Сверка ПД/РД/ИД: загрузка пакетов, протокол с карточками доказательств, решения "
                              "инспектора, GOLD. Ошибки: {\"error\": {\"code\", \"message\", \"details\"}}.",
                  openapi_url=f"{API_PREFIX}/openapi.json", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.metrics = monitoring.Metrics()
    app.state.llm_transport = None       # тесты подменяют на httpx.MockTransport
    app.state.rin_transport = None
    app.state.login_throttle = auth.LoginThrottle(settings.login_max_failures, settings.login_window_s)

    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(DomainError, domain_error_handler)

    for module in (auth, processes, files, protocol_api, findings, integration, dataset, ml, admin, monitoring,
                   docs):
        app.include_router(module.router)          # префикс /api/v1 задан в самих роутерах; docs — Swagger UI

    app.add_middleware(RequestContextMiddleware, settings=settings, metrics=app.state.metrics)
    app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_credentials=True, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["X-Request-ID", "Content-Disposition",
                                                            "X-Content-SHA256"])
    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """`app` создаётся при первом обращении (uvicorn inspector.api.app:app), а не при импорте модуля."""
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
