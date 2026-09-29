"""Интеграция с ИАИС «РиН» (заглушка, ТЗ 9.6): отправка финализированного протокола — только подтверждённые
инспектором записи с версиями протокола, матрицы, модели и реестром входных файлов.

Первая попытка — синхронно в POST /inspection/{process_id}. При 5xx, 429, таймауте или сетевой ошибке — до 3 повторных
попыток с паузами из RIN_RETRY_DELAYS_S (1, 5, 15 мин), статус PENDING_SYNC; повторы выполняет worker через
retry_sync(db, attempt). Ответ 4xx повторять бессмысленно — сразу FAILED. Каждая попытка — строка SyncAttempt.

Отправитель и приёмник-заглушка знают общий токен (RIN_TOKEN, заголовок Authorization: Bearer …): заглушка без
токена или с чужим отвечает 401 и ничего не пишет в БД."""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.api.deps import API_PREFIX, ApiError, DbDep, Reader, SettingsDep, audit, get_process_or_404, iso, require
from inspector.config import Settings, get_settings
from inspector.models import AuditLog, Process, RinMessage, SyncAttempt, User, utcnow
from inspector.protocol import build_protocol

log = logging.getLogger("inspector.api.integration")
router = APIRouter(prefix=API_PREFIX, tags=["integration"])

RIN_TIMEOUT_S = 30.0
SYNCED, PENDING_SYNC, FAILED, SCHEDULED = "SYNCED", "PENDING_SYNC", "FAILED", "SCHEDULED"
RIN_TOKEN_FILE = ".rin_token"


@dataclass(frozen=True)
class SendResult:
    ok: bool
    http_status: int | None
    error: str | None
    retryable: bool


def retry_delays(settings: Settings) -> list[int]:
    out = []
    for part in settings.rin_retry_delays_s.split(","):
        try:
            out.append(max(0, int(float(part))))
        except ValueError:
            continue
    return out


def rin_token(settings: Settings) -> str:
    """Общий токен ИАИС РиН: RIN_TOKEN; пусто — случайный, создаётся один раз в каталоге данных (api и worker видят
    один том). Так заглушка не принимает анонимные запросы и в демо-развёртывании, а отправитель знает тот же токен
    без настройки. Файл создаётся атомарно (link без перезаписи): api и worker стартуют вместе — побеждает первый."""
    if settings.rin_token.strip():
        return settings.rin_token.strip()
    path = settings.data_dir / RIN_TOKEN_FILE
    token = path.read_text(encoding="utf-8", errors="replace").strip() if path.is_file() else ""
    if token:
        return token
    tmp = path.with_name(f"{RIN_TOKEN_FILE}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(secrets.token_hex(32))
    try:
        if path.exists():
            os.replace(tmp, path)                 # пустой файл (сбой записи) — заменяем
        else:
            os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return path.read_text(encoding="utf-8", errors="replace").strip()


def build_payload(db: Session, process: Process) -> dict:
    protocol = build_protocol(db, process)
    trace = protocol["traceability"]
    return {
        "process_id": process.id, "object_id": protocol["object"]["object_id"], "protocol_id": protocol["protocol_id"],
        "protocol_version": process.protocol_version, "matrix_version": trace["matrix_version"],
        "model_version": trace["model_version"], "dataset_version": trace["dataset_version"],
        "input_manifest_sha256": trace["input_manifest_sha256"], "finalized_at": iso(process.finalized_at),
        "input_files": [{"file_id": f["file_id"], "sha256": f["sha256"], "stage": f["stage"]}
                        for f in protocol["files"] if f["status"] == "ok"],
        "confirmed": [c for c in protocol["findings"] if c["status"] == S.CONFIRMED_VIOLATION],
    }


def send_payload(url: str, payload: dict, transport: httpx.BaseTransport | None = None,
                 timeout: float = RIN_TIMEOUT_S, token: str = "") -> SendResult:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        return SendResult(False, None, f"таймаут ({type(exc).__name__})", True)
    except httpx.HTTPError as exc:
        return SendResult(False, None, f"{type(exc).__name__}: {exc}"[:300], True)
    if 200 <= resp.status_code < 300:
        return SendResult(True, resp.status_code, None, False)
    return SendResult(False, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:200]}",
                      resp.status_code >= 500 or resp.status_code == 429)


def record_attempt(db: Session, process: Process, attempt_no: int, result: SendResult, settings: Settings,
                   row: SyncAttempt | None = None) -> SyncAttempt:
    """Итог попытки: успех → SYNCED; повторяемый сбой → FAILED + следующая SCHEDULED (пока есть паузы);
    иначе → статус проверки FAILED и запись RIN_SYNC_FAILED в журнал."""
    if row is None:
        row = SyncAttempt(process_id=process.id, attempt=attempt_no)
        db.add(row)
    row.http_status, row.error = result.http_status, result.error
    if result.ok:
        row.status = SYNCED
        process.sync_status = SYNCED
        return row
    row.status = FAILED
    delays = retry_delays(settings)
    if result.retryable and attempt_no <= len(delays):
        db.add(SyncAttempt(process_id=process.id, attempt=attempt_no + 1, status=SCHEDULED,
                           next_try_at=utcnow() + dt.timedelta(seconds=delays[attempt_no - 1])))
        process.sync_status = PENDING_SYNC
    else:
        process.sync_status = FAILED
        db.add(AuditLog(user_login="system", action="RIN_SYNC_FAILED", process_id=process.id,
                        object_code=process.object.object_code,
                        details={"attempts": attempt_no, "http_status": result.http_status, "error": result.error,
                                 "note": "отправка в ИАИС РиН не удалась; решение инспектора не изменено"}))
        log.error("Отправка в ИАИС РиН не удалась", extra={"process_id": process.id, "attempts": attempt_no})
    return row


def retry_sync(db: Session, attempt: SyncAttempt, transport: httpx.BaseTransport | None = None,
               settings: Settings | None = None) -> None:
    """Повтор отправки по строке SyncAttempt(status=SCHEDULED, next_try_at <= now). Вызывает worker; коммитит
    вызывающий. Если финализацию отменили — повтор не выполняется."""
    settings = settings or get_settings()
    process = db.get(Process, attempt.process_id)
    if process is None or process.status != S.FINALIZED:
        attempt.status = FAILED
        attempt.error = "отменено: протокол не финализирован"
        if process is not None and process.sync_status == PENDING_SYNC:
            process.sync_status = "NONE"
        return
    result = send_payload(settings.rin_url, build_payload(db, process), transport, token=rin_token(settings))
    record_attempt(db, process, attempt.attempt, result, settings, row=attempt)


def inspection_status(db: Session, process: Process) -> dict:
    rows = db.scalars(select(SyncAttempt).where(SyncAttempt.process_id == process.id).order_by(SyncAttempt.id))
    return {"process_id": process.id, "sync_status": process.sync_status,
            "attempts": [{"id": a.id, "attempt": a.attempt, "status": a.status, "http_status": a.http_status,
                          "error": a.error, "next_try_at": iso(a.next_try_at), "created_at": iso(a.created_at)}
                         for a in rows]}


@router.post("/inspection/{process_id}", summary="Отправить финализированный протокол в ИАИС РиН (заглушка)")
def send_inspection(process_id: str, request: Request, db: DbDep, settings: SettingsDep,
                    user: User = Depends(require("sync"))) -> dict:
    process = get_process_or_404(db, process_id)
    if process.status != S.FINALIZED:
        raise ApiError(409, "NOT_FINALIZED", "В ИАИС РиН отправляется только финализированный протокол",
                       details={"process_status": process.status})
    for scheduled in db.scalars(select(SyncAttempt).where(SyncAttempt.process_id == process.id,
                                                          SyncAttempt.status == SCHEDULED)):
        scheduled.status = FAILED
        scheduled.error = "заменено ручной отправкой"
    payload = build_payload(db, process)
    db.commit()                                   # транзакцию не держим открытой на время сетевого запроса
    result = send_payload(settings.rin_url, payload, request.app.state.rin_transport, token=rin_token(settings))
    record_attempt(db, process, 1, result, settings)
    audit(db, request, user, "sync", process_id=process.id, object_code=process.object.object_code,
          details={"ok": result.ok, "http_status": result.http_status, "error": result.error,
                   "sync_status": process.sync_status, "confirmed": len(payload["confirmed"])})
    db.commit()
    return inspection_status(db, process)


@router.get("/inspection/{process_id}", summary="Статус отправки в ИАИС РиН и журнал попыток")
def get_inspection(process_id: str, _: Reader, db: DbDep) -> dict:
    return inspection_status(db, get_process_or_404(db, process_id))


def require_rin_token(request: Request, settings: SettingsDep) -> None:
    """Приёмник пускает только с общим токеном (сравнение за постоянное время); иначе 401 с WWW-Authenticate.
    Зависимость, а не вызов в ручке: токен проверяется раньше, чем тело запроса."""
    scheme, _, sent = (request.headers.get("authorization") or "").partition(" ")
    sent = sent.strip() if scheme.lower() == "bearer" else ""
    if not sent:
        raise ApiError(401, "AUTH_REQUIRED", "Нужен токен ИАИС РиН: заголовок Authorization: Bearer …",
                       headers={"WWW-Authenticate": "Bearer"})
    if not hmac.compare_digest(sent.encode("utf-8"), rin_token(settings).encode("utf-8")):
        raise ApiError(401, "AUTH_INVALID", "Неверный токен ИАИС РиН", headers={"WWW-Authenticate": "Bearer"})


@router.post("/mock-rin/receive", summary="Приёмник-заглушка ИАИС РиН (токен RIN_TOKEN в Authorization: Bearer; "
                                          "MOCK_RIN_FAIL=1 — отказ)")
def mock_rin_receive(db: DbDep, _: None = Depends(require_rin_token), payload: dict[str, Any] = Body(...)) -> dict:
    if os.environ.get("MOCK_RIN_FAIL", "").strip().lower() in ("1", "true", "yes"):
        raise ApiError(503, "RIN_UNAVAILABLE", "ИАИС РиН недоступна (заглушка: MOCK_RIN_FAIL=1)")
    msg = RinMessage(process_id=str(payload.get("process_id") or "")[:36], payload=payload)
    db.add(msg)
    db.commit()
    return {"received": True, "id": msg.id}
