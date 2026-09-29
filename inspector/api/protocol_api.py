"""Протокол сверки: живой (по текущему состоянию БД) и снимки версий, история версий, выгрузки PDF/DOCX/XML (поток D),
сабмит по схеме организаторов. Просмотр и скачивание протокола пишутся в журнал аудита (кто, когда, какая версия и
формат); список версий — оглавление без содержимого находок, его чтение не журналируется."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.api.deps import (API_PREFIX, ApiError, DbDep, MediaReader, Reader, attachment_header, audit,
                                get_process_or_404, iso)
from inspector.models import Process, ProtocolVersion, User
from inspector.protocol import build_protocol, build_submission, validate_submission

log = logging.getLogger("inspector.api.protocol")
router = APIRouter(prefix=API_PREFIX, tags=["protocol"])

EXPORT_MEDIA = {"pdf": "application/pdf",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xml": "application/xml"}


def snapshot(db: Session, process: Process, version: int) -> dict:
    pv = db.scalars(select(ProtocolVersion).where(ProtocolVersion.process_id == process.id,
                                                  ProtocolVersion.version == version)).first()
    if pv is None:
        raise ApiError(404, "PROTOCOL_VERSION_NOT_FOUND", "Такой версии протокола нет",
                       details={"version": version})
    path = Path(pv.json_path)
    if not path.is_file():
        raise ApiError(404, "SNAPSHOT_MISSING", "Файл снимка протокола отсутствует в хранилище",
                       details={"version": version})
    return json.loads(path.read_text(encoding="utf-8"))


def protocol_for(db: Session, process: Process, version: int | None) -> dict:
    return snapshot(db, process, version) if version else build_protocol(db, process)


def log_access(db: Session, request: Request, user: User, process: Process, action: str, protocol: dict,
               version: int | None, **details) -> None:
    """Чтение протокола — в журнал аудита: номер версии и что открыто — снимок (?version=N) или живой протокол
    (текущее состояние с решениями инспектора); для выгрузок — формат."""
    audit(db, request, user, action, process_id=process.id, object_code=process.object.object_code,
          details={"protocol_version": protocol.get("version"), "live": version is None, **details})
    db.commit()


@router.get("/processes/{process_id}/protocol", summary="Протокол: живой или снимок версии (?version=N)")
def get_protocol(process_id: str, user: Reader, request: Request, db: DbDep,
                 version: int | None = Query(default=None, ge=1)) -> dict:
    process = get_process_or_404(db, process_id)
    protocol = protocol_for(db, process, version)
    log_access(db, request, user, process, "protocol_view", protocol, version)
    return protocol


@router.get("/processes/{process_id}/protocol/versions", summary="История версий протокола (новые сверху)")
def protocol_versions(process_id: str, _: Reader, db: DbDep) -> dict:
    process = get_process_or_404(db, process_id)
    rows = db.scalars(select(ProtocolVersion).where(ProtocolVersion.process_id == process.id)
                      .order_by(ProtocolVersion.version.desc()))
    items = [{"version": pv.version, "kind": pv.kind, "status": pv.status, "created_at": iso(pv.created_at),
              "finalized_at": iso(pv.finalized_at), "summary": pv.summary, "matrix_version": pv.matrix_version,
              "model_version": pv.model_version, "dataset_version": pv.dataset_version,
              "input_manifest_hash": pv.input_manifest_hash} for pv in rows]
    return {"items": items, "total": len(items)}


def _export(db: Session, request: Request, user: User, process_id: str, fmt: str, version: int | None) -> Response:
    process = get_process_or_404(db, process_id)
    try:                             # поток D может быть ещё не готов: импорт — здесь, а не при старте приложения
        from inspector.render import render_docx, render_pdf, render_xml
    except ImportError:
        raise ApiError(503, "RENDER_UNAVAILABLE", "Выгрузка протокола в этот формат пока недоступна") from None
    renderer = {"pdf": render_pdf, "docx": render_docx, "xml": render_xml}[fmt]
    protocol = protocol_for(db, process, version)
    try:
        data = renderer(protocol)
    except Exception as exc:  # noqa: BLE001 — ошибка вёрстки не должна выглядеть как «нет модуля»
        log.exception("Сбой выгрузки протокола", extra={"process_id": process.id, "format": fmt})
        raise ApiError(500, "RENDER_FAILED", "Не удалось сформировать файл протокола",
                       details={"format": fmt, "reason": f"{type(exc).__name__}: {exc}"[:300]}) from None
    number = protocol.get("version") or process.protocol_version
    filename = f"protocol_{protocol['object']['object_id']}_v{number}.{fmt}"
    log_access(db, request, user, process, "protocol_download", protocol, version, format=fmt, file=filename)
    return Response(data, media_type=EXPORT_MEDIA[fmt], headers={"Content-Disposition": attachment_header(filename)})


@router.get("/processes/{process_id}/protocol.pdf", response_class=Response, summary="Протокол в PDF")
def protocol_pdf(process_id: str, user: MediaReader, request: Request, db: DbDep,
                 version: int | None = Query(default=None, ge=1)) -> Response:
    return _export(db, request, user, process_id, "pdf", version)


@router.get("/processes/{process_id}/protocol.docx", response_class=Response, summary="Протокол в DOCX")
def protocol_docx(process_id: str, user: MediaReader, request: Request, db: DbDep,
                  version: int | None = Query(default=None, ge=1)) -> Response:
    return _export(db, request, user, process_id, "docx", version)


@router.get("/processes/{process_id}/protocol.xml", response_class=Response, summary="Протокол в XML")
def protocol_xml(process_id: str, user: MediaReader, request: Request, db: DbDep,
                 version: int | None = Query(default=None, ge=1)) -> Response:
    return _export(db, request, user, process_id, "xml", version)


@router.get("/processes/{process_id}/submission.json", summary="Сабмит по схеме организаторов (?machine=1 — без "
                                                                "решений инспектора)")
def submission(process_id: str, user: MediaReader, request: Request, db: DbDep, machine: bool = False,
               version: int | None = Query(default=None, ge=1)) -> JSONResponse:
    process = get_process_or_404(db, process_id)
    protocol = protocol_for(db, process, version)
    sub = build_submission(protocol, apply_decisions=not machine)
    errors = validate_submission(sub)
    if errors:
        log.error("Сабмит не прошёл схему", extra={"process_id": process.id, "errors": errors[:20]})
        raise ApiError(500, "SUBMISSION_INVALID", "Сабмит не соответствует схеме организаторов", details=errors[:100])
    number = protocol.get("version") or process.protocol_version
    filename = f"submission_{sub['object_id']}_v{number}.json"
    log_access(db, request, user, process, "protocol_download", protocol, version, format="submission",
               file=filename, machine=machine)
    return JSONResponse(sub, headers={"Content-Disposition": attachment_header(filename)})
