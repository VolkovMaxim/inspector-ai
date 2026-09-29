#!/bin/sh
# Точка входа образа «Инспектор ИИ» (ENTRYPOINT, под tini).
#   api          — HTTP API + интерфейс: uvicorn inspector.api.app:app, порт 8080
#   worker       — обработчик очереди задач (конвейер): python -m inspector.worker
#   healthcheck  — проверка для HEALTHCHECK: api — GET /health, worker — процесс жив
#   <команда>    — выполнить как есть: bash, python -m ..., soffice --version
# Дополнительные аргументы передаются дальше: `api --workers 2`, `worker --once` и т. п.
set -eu

ROLE_FILE=/tmp/inspector-role

log() {  # log LEVEL MESSAGE → строка JSON в stderr (формат как у логов приложения)
  printf '{"timestamp": "%s", "level": "%s", "service": "inspector-entrypoint", "message": "%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S.%3N+00:00)" "$1" "$2" >&2
}

# Пустой или демо-JWT_SECRET («change-me…») заменяется случайным ключом из тома данных: токены переживают
# перезапуск, а подделать токен по ключу из открытого репозитория нельзя. Свой ключ — JWT_SECRET в .env.
ensure_jwt_secret() {
  case "${JWT_SECRET:-}" in
    "" | change-me*) ;;
    *) return 0 ;;
  esac
  secret_file="${DATA_DIR:-/var/inspector}/.jwt_secret"
  if [ ! -s "$secret_file" ]; then
    tmp="$secret_file.tmp.$$"
    (umask 077 && python -c 'import secrets; print(secrets.token_hex(32))' > "$tmp")
    mv -n "$tmp" "$secret_file" 2>/dev/null || true   # api и worker стартуют вместе: побеждает первый
    rm -f "$tmp"
    log WARNING "JWT_SECRET не задан (или демо-значение): создан случайный ключ в $secret_file"
  fi
  JWT_SECRET="$(cat "$secret_file")"
  export JWT_SECRET
}

# Таблицы создаются до старта процесса, на Postgres — под advisory-блокировкой: api и worker стартуют
# одновременно, и параллельный CREATE TABLE без блокировки падает. Ждёт БД до 120 с.
init_db() {
  python - <<'PY'
import datetime as dt
import json
import sys
import time

from sqlalchemy import text

from inspector.config import get_settings
from inspector.db import make_engine
from inspector.models import Base


def log(level: str, message: str) -> None:
    print(json.dumps({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                      "level": level, "service": "inspector-entrypoint", "message": message},
                     ensure_ascii=False), file=sys.stderr, flush=True)


engine = make_engine(get_settings().db_url)
deadline = time.monotonic() + 120
while True:
    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(7102026)"))
            Base.metadata.create_all(conn)
        break
    except Exception as exc:  # БД ещё поднимается — ждём; иначе выходим с ошибкой
        if time.monotonic() > deadline:
            log("ERROR", f"БД недоступна: {type(exc).__name__}: {exc}"[:500])
            sys.exit(1)
        log("WARNING", f"жду БД: {type(exc).__name__}")
        time.sleep(2)
engine.dispose()
log("INFO", f"схема БД готова ({engine.dialect.name})")
PY
}

# worker ждёт, пока модели ответят на GET /models (vLLM грузит веса 5–15 мин), чтобы задачи, поставленные
# сразу после старта, не падали. WAIT_FOR_MODELS_S — предел ожидания (0 — не ждать); после него worker
# стартует всё равно, а недоступность модели видна в прогрессе задачи и в /health.
wait_models() {
  python - <<'PY'
import datetime as dt
import json
import os
import sys
import time

import httpx

from inspector.config import get_settings


def log(level: str, message: str) -> None:
    print(json.dumps({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                      "level": level, "service": "inspector-entrypoint", "message": message},
                     ensure_ascii=False), file=sys.stderr, flush=True)


s = get_settings()
limit = float(os.environ.get("WAIT_FOR_MODELS_S") or 0)
if limit <= 0:
    sys.exit(0)
pending = [s.llm_url.rstrip("/")]
if s.ocr_engine == "vlm" and s.ocr_url_eff.rstrip("/") not in pending:
    pending.append(s.ocr_url_eff.rstrip("/"))
headers = {"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {}
start, next_log = time.monotonic(), 0.0
while pending:
    for url in list(pending):
        try:
            if httpx.get(f"{url}/models", headers=headers, timeout=5).status_code == 200:
                pending.remove(url)
                log("INFO", f"модель отвечает: {url}")
        except httpx.HTTPError:
            pass
    if not pending:
        break
    waited = time.monotonic() - start
    if waited >= limit:
        log("WARNING", f"модели не ответили за {int(limit)} с: {', '.join(pending)}; worker запускается, "
                       "задачи без модели завершатся ошибкой после повторов")
        break
    if waited >= next_log:
        log("INFO", f"жду модели: {', '.join(pending)} (прошло {int(waited)} с из {int(limit)})")
        next_log = waited + 60
    time.sleep(5)
PY
}

healthcheck() {
  role="$(cat "$ROLE_FILE" 2>/dev/null || true)"
  case "$role" in
    api) exec curl -fsS -o /dev/null --max-time 8 http://127.0.0.1:8080/health ;;
    *) exit 0 ;;   # worker и прочие команды: контейнер жив, пока жив процесс
  esac
}

cmd="${1:-api}"
if [ $# -gt 0 ]; then shift; fi

case "$cmd" in
  api)
    echo api > "$ROLE_FILE" 2>/dev/null || true
    ensure_jwt_secret
    init_db
    log INFO "старт api: uvicorn inspector.api.app:app, 0.0.0.0:8080"
    exec uvicorn inspector.api.app:app --host 0.0.0.0 --port 8080 --proxy-headers --no-server-header "$@"
    ;;
  worker)
    echo worker > "$ROLE_FILE" 2>/dev/null || true
    ensure_jwt_secret
    init_db
    wait_models
    log INFO "старт worker: python -m inspector.worker"
    exec python -m inspector.worker "$@"
    ;;
  healthcheck)
    healthcheck
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
