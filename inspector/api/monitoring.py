"""Мониторинг без префикса /api/v1: /health (БД, модель, очередь) и /metrics (Prometheus: HTTP, очередь, хост —
загрузка CPU, память, место на томе данных, активные пользователи)."""
from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import distinct, func, select
from starlette.concurrency import run_in_threadpool

from inspector import statuses as S
from inspector.api.deps import iso
from inspector.db import session_factory
from inspector.models import AuditLog, Job, Process, utcnow

router = APIRouter(tags=["monitoring"])

JOB_STATUSES = ("queued", "running", "done", "failed")
LLM_PROBE_TIMEOUT_S = 2.0
MEMINFO = Path("/proc/meminfo")
MEMINFO_FIELDS = {"MemTotal": "total", "MemAvailable": "available"}
ACTIVE_USERS_WINDOW = dt.timedelta(minutes=15)


class Metrics:
    """Свой реестр на каждое приложение: в тестах create_app() вызывается много раз, общий реестр дал бы дубли."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter("inspector_http_requests", "HTTP-запросы по маршруту и коду ответа",
                                ["method", "route", "status"], registry=self.registry)
        self.latency = Histogram("inspector_http_request_duration_seconds", "Время обработки запроса, с",
                                 ["method", "route"], registry=self.registry)
        self.jobs = Gauge("inspector_jobs", "Задачи конвейера по статусам", ["status"], registry=self.registry)
        self.processes = Gauge("inspector_processes", "Проверки по статусам", ["status"], registry=self.registry)
        self.load = Gauge("inspector_host_load_average", "Средняя загрузка CPU хоста (load average) за 1, 5, 15 мин",
                          ["period"], registry=self.registry)
        self.cpus = Gauge("inspector_host_cpus", "Логических CPU хоста (для сравнения с load average)",
                          registry=self.registry)
        self.memory = Gauge("inspector_host_memory_bytes", "Память хоста по /proc/meminfo: total, available",
                            ["kind"], registry=self.registry)
        self.disk = Gauge("inspector_data_disk_bytes", "Том данных (DATA_DIR): total, used, free", ["kind"],
                          registry=self.registry)
        self.active_users = Gauge("inspector_active_users", "Пользователи с действиями в журнале аудита за 15 мин",
                                  registry=self.registry)

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        self.requests.labels(method, route, str(status)).inc()
        self.latency.labels(method, route).observe(seconds)


def _queue_stats() -> dict:
    s = session_factory()()
    try:
        jobs = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        processes = dict(s.execute(select(Process.status, func.count()).group_by(Process.status)).all())
        last = s.scalar(select(func.max(Job.heartbeat_at)))
        return {"jobs": jobs, "processes": processes, "last_heartbeat": last}
    finally:
        s.close()


def _active_users() -> int:
    """Разные пользователи с записями журнала за 15 мин (вход, просмотр, решения); system и анонимные не в счёт."""
    s = session_factory()()
    try:
        since = utcnow() - ACTIVE_USERS_WINDOW
        return s.scalar(select(func.count(distinct(AuditLog.user_pk)))
                        .where(AuditLog.user_pk.is_not(None), AuditLog.created_at >= since)) or 0
    finally:
        s.close()


def read_meminfo(path: Path | None = None) -> dict[str, int]:
    """MemTotal и MemAvailable в байтах (в файле — кБ); нет файла (не Linux) или поля — поле пропускается."""
    try:
        text = (path or MEMINFO).read_text(encoding="ascii", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in MEMINFO_FIELDS:
            try:
                out[MEMINFO_FIELDS[name]] = int(rest.split()[0]) * 1024
            except (IndexError, ValueError):
                continue
    return out


def host_stats(data_dir: Path) -> dict:
    """Загрузка CPU (os.getloadavg), память (/proc/meminfo), место на томе данных; недоступное — пропускается."""
    stats: dict = {"cpus": os.cpu_count(), "memory": read_meminfo()}
    try:
        stats["load"] = dict(zip(("1m", "5m", "15m"), os.getloadavg()))
    except (OSError, AttributeError):          # нет в ОС (Windows) или ядро не отдаёт
        pass
    try:
        usage = shutil.disk_usage(data_dir)
        stats["disk"] = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        pass
    return stats


async def probe_models(url: str, api_key: str = "", transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Доступность OpenAI-совместимого сервера модели: GET {url}/models, таймаут 2 с."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=LLM_PROBE_TIMEOUT_S, transport=transport) as client:
            resp = await client.get(url.rstrip("/") + "/models", headers=headers)
        return {"url": url, "reachable": resp.status_code < 500, "status_code": resp.status_code}
    except httpx.HTTPError as exc:
        return {"url": url, "reachable": False, "error": type(exc).__name__}


@router.get("/health", summary="Здоровье: БД, модель, очередь задач")
async def health(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    transport = request.app.state.llm_transport
    try:
        stats = await run_in_threadpool(_queue_stats)
        db_state = "ok"
    except Exception as exc:  # noqa: BLE001 — любая ошибка БД = «БД недоступна»
        stats, db_state = {"jobs": {}, "last_heartbeat": None}, f"error: {type(exc).__name__}"
    body = {
        "status": "ok",
        "db": db_state,
        "llm": await probe_models(settings.llm_url, settings.llm_api_key, transport),
        "worker": {"queued_jobs": stats["jobs"].get("queued", 0), "running_jobs": stats["jobs"].get("running", 0),
                   "last_heartbeat": iso(stats["last_heartbeat"])},
    }
    # модель по OCR_URL читает все сканы (vlm) или перечитывает отобранные (rapidocr + reocr)
    uses_ocr_url = settings.ocr_engine == "vlm" or (settings.ocr_engine == "rapidocr" and settings.reocr_selected)
    if uses_ocr_url and settings.ocr_url_eff.rstrip("/") != settings.llm_url.rstrip("/"):
        body["ocr"] = await probe_models(settings.ocr_url_eff, settings.llm_api_key, transport)
    if db_state != "ok":
        body["status"] = "error"
        return JSONResponse(body, status_code=503)
    if not body["llm"]["reachable"] or not body.get("ocr", {"reachable": True})["reachable"]:
        body["status"] = "degraded"
    return JSONResponse(body)


@router.get("/metrics", summary="Метрики Prometheus")
async def metrics(request: Request) -> Response:
    m: Metrics = request.app.state.metrics
    try:
        stats = await run_in_threadpool(_queue_stats)
        for status in JOB_STATUSES:
            m.jobs.labels(status).set(stats["jobs"].get(status, 0))
        for status in S.PROCESS_STATUSES:
            m.processes.labels(status).set(stats["processes"].get(status, 0))
        m.active_users.set(await run_in_threadpool(_active_users))
    except Exception:  # noqa: BLE001 — метрики HTTP отдаём, даже если БД недоступна
        pass
    host = await run_in_threadpool(host_stats, request.app.state.settings.data_dir)
    if host["cpus"]:
        m.cpus.set(host["cpus"])
    for period, value in host.get("load", {}).items():
        m.load.labels(period).set(value)
    for kind, value in host["memory"].items():
        m.memory.labels(kind).set(value)
    for kind, value in host.get("disk", {}).items():
        m.disk.labels(kind).set(value)
    return Response(generate_latest(m.registry), media_type=CONTENT_TYPE_LATEST)
