"""Swagger UI без интернета: /api/docs — страница FastAPI, скрипт, стили и значок — из inspector/resources/swagger
(swagger-ui-dist 5.33.0, Apache-2.0: LICENSE и NOTICE рядом, лицензии встроенных библиотек —
swagger-ui-bundle.js.LICENSE.txt). У стенда жюри выхода в интернет нет — CDN (cdn.jsdelivr.net) недоступен.
Проверка спецификации онлайн-валидатором (validator.swagger.io) выключена: она тоже ходила бы наружу."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse

from inspector.api.deps import ApiError
from inspector.config import RESOURCES

router = APIRouter(tags=["docs"], include_in_schema=False)

DOCS_PATH = "/api/docs"
STATIC_PATH = f"{DOCS_PATH}/static"
SWAGGER_DIR = RESOURCES / "swagger"
SWAGGER_UI_VERSION = "5.33.0"              # при обновлении файлов — сменить: ?v= сбрасывает кэш браузера
STATIC_FILES = {"swagger-ui-bundle.js": "text/javascript; charset=utf-8",
                "swagger-ui.css": "text/css; charset=utf-8",
                "favicon-32x32.png": "image/png"}


@router.get(DOCS_PATH, response_class=HTMLResponse)
def swagger_ui(request: Request) -> HTMLResponse:
    root = request.scope.get("root_path", "").rstrip("/")      # как у встроенной страницы FastAPI (за прокси)
    static = f"{root}{STATIC_PATH}"
    return get_swagger_ui_html(openapi_url=f"{root}{request.app.openapi_url}",
                               title=f"{request.app.title} — Swagger UI",
                               swagger_js_url=f"{static}/swagger-ui-bundle.js?v={SWAGGER_UI_VERSION}",
                               swagger_css_url=f"{static}/swagger-ui.css?v={SWAGGER_UI_VERSION}",
                               swagger_favicon_url=f"{static}/favicon-32x32.png",
                               swagger_ui_parameters={"validatorUrl": None})


@router.get(f"{STATIC_PATH}/{{name}}")
def swagger_static(name: str) -> FileResponse:
    """Только файлы из списка: имя из запроса в путь к файлу не подставляется."""
    media = STATIC_FILES.get(name)
    if media is None:
        raise ApiError(404, "NOT_FOUND", "Не найдено", details={"name": name})
    return FileResponse(SWAGGER_DIR / name, media_type=media, headers={"Cache-Control": "public, max-age=86400"})
