# Образ «Инспектор ИИ»: один образ на две роли — api (FastAPI + собранный интерфейс) и worker (конвейер).
# Роль задаёт команда контейнера (см. docker/entrypoint.sh): api | worker | любая другая команда.
#
#   docker build -t inspector:latest .                           # с интерфейсом (нужен web/)
#   docker build --build-arg SKIP_WEB=1 -t inspector:dev .       # без интерфейса: web/ не читается, dist — заглушка
#
# Требуется BuildKit (по умолчанию в Docker ≥ 23 и в docker compose): стадия интерфейса выбирается по SKIP_WEB,
# ненужная стадия не собирается.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.7
# 0 — собрать интерфейс из web/ (npm), 1 — положить заглушку index.html
ARG SKIP_WEB=0

FROM ${UV_IMAGE} AS uv

# ---------- 1. Интерфейс: React + Vite → /web/dist ----------
FROM ${NODE_IMAGE} AS web-0
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build && test -f dist/index.html

# SKIP_WEB=1: интерфейс не собирается, api отдаёт страницу-заглушку со ссылкой на OpenAPI
FROM ${PYTHON_IMAGE} AS web-1
RUN mkdir -p /web/dist && printf '%s\n' \
    '<!doctype html>' \
    '<html lang="ru"><head><meta charset="utf-8"><title>Инспектор ИИ</title></head>' \
    '<body><h1>Инспектор ИИ</h1><p>Интерфейс не собран (образ собран с SKIP_WEB=1).</p>' \
    '<p>API: <a href="/api/docs">/api/docs</a></p></body></html>' > /web/dist/index.html

FROM web-${SKIP_WEB} AS web

# ---------- 2. Среда выполнения: Python 3.12 + LibreOffice + bsdtar ----------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="inspector" \
      org.opencontainers.image.description="Инспектор ИИ: сверка ПД/РД/ИД (ЛЦТ-2026, задача 10) — api и worker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# LibreOffice (без GUI) — конвертация DOC/DOCX/RTF/ODT/XLS/XLSX → PDF; bsdtar — распаковка 7z/rar/zip;
# шрифты: DejaVu (кириллица) + метрически совместимые с Arial/Times/Courier (Liberation) и Calibri/Cambria
# (Carlito/Caladea), чтобы вёрстка сконвертированных документов совпадала с оригиналом;
# libgl1 + libglib2.0-0 — их требует opencv-python (зависимость rapidocr); tini — init (сбор зомби soffice).
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui \
      libarchive-tools \
      fonts-dejavu-core fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea \
      curl ca-certificates tini \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Зависимости — отдельным слоем (пересобирается только при смене pyproject.toml / uv.lock).
# Сам пакет не устанавливается: код лежит в /app/inspector, SERVICE_ROOT = /app (web_dist = /app/web/dist).
COPY pyproject.toml uv.lock ./
# classifier: torch (CUDA 12.8) + transformers для классификатора типа страницы (PAGE_CLASSIFIER_DIR)
RUN uv sync --frozen --no-dev --extra ocrbox --extra classifier --no-install-project \
 && python -c "import cv2, onnxruntime, rapidocr, pymupdf, fastapi, sqlalchemy, psycopg, uvicorn, torch, transformers; assert 'CUDAExecutionProvider' in onnxruntime.get_available_providers(), 'onnxruntime без CUDA'; print('python deps: ok')"

RUN groupadd --gid 1000 inspector \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/inspector --shell /usr/sbin/nologin inspector \
 && mkdir -p /var/inspector /import \
 && chown inspector:inspector /var/inspector

COPY inspector ./inspector
COPY scripts ./scripts
COPY docker ./docker
COPY --from=web /web/dist ./web/dist
RUN chmod 0755 docker/entrypoint.sh \
 && find scripts -name '*.sh' -exec chmod 0755 {} + \
 && python -m compileall -q inspector

ENV PYTHONPATH=/app \
    DATA_DIR=/var/inspector \
    HOME=/home/inspector

USER 1000:1000
EXPOSE 8080

# Роль-зависимая проверка: api — GET /health, worker — жив ли процесс (см. entrypoint.sh healthcheck)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["/app/docker/entrypoint.sh", "healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["api"]
