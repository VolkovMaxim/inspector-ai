"""Проверки (процессы): создание, список и карточка с индикатором, загрузка пакетов (50 МБ на файл, 200 МБ на
запрос), дозагрузка (новая партия → задача incremental), запуск конвейера, импорт пакета с диска сервера (admin)."""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, BinaryIO, Callable, Iterable

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from inspector import jobs
from inspector import statuses as S
from inspector.api.deps import (API_PREFIX, ApiError, DbDep, Reader, SettingsDep, audit, get_process_or_404, iso,
                                require)
from inspector.config import Settings
from inspector.models import ConstructionObject, File, Finding, Page, Process, User
from inspector.protocol import effective_status, upload_statuses
from inspector.storage import FileTooLarge, safe_ext, store_stream

router = APIRouter(prefix=API_PREFIX, tags=["processes"])

MB = 1024 * 1024
# ТЗ 9.1 + манифест организатора (jsonl) и открепленные подписи (sig)
ALLOWED_EXT = frozenset({".pdf", ".docx", ".doc", ".xml", ".xls", ".xlsx", ".rtf", ".odt", ".jpg", ".jpeg", ".png",
                         ".tif", ".tiff", ".zip", ".7z", ".rar", ".sig", ".jsonl"})
UPLOAD_STAGES = ("PD", "RD", "ID", "AUTO")
FILE_FIELDS = ("files", "files[]", "file")
MAX_FILES_PER_REQUEST = 2000
MAX_IMPORT_FILES = 20000
OBJECT_ID_RE = re.compile(r"^[^\s/\\]{1,120}$")
COUNT_KEYS = ("candidates", "candidates_pending", "confirmed", "negatives", "suspicions", "not_comparable", "missing",
              "clarifications")
EFFECTIVE_KEYS = {S.CONFIRMED_VIOLATION: "confirmed", S.NEGATIVE_VERIFIED: "negatives", S.SUSPICION: "suspicions",
                  S.NOT_COMPARABLE: "not_comparable", S.MISSING_EVIDENCE: "missing",
                  S.CLARIFICATION_REQUIRED: "clarifications"}

# ---------------------------------------------------------------- код объекта

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya"})


def latin_slug(name: str, max_len: int = 48) -> str:
    """«Жилой дом, Новослободская» → «ZHILOY-DOM-NOVOSLOBODSKAYA»."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(_TRANSLIT)).strip("-").upper()
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s


def new_object_code(db: Session, name: str) -> str:
    base = latin_slug(name)
    prefix = f"OBJ-{base}-" if base else "OBJ-"
    for _ in range(20):
        code = prefix + secrets.token_hex(2).upper()
        if db.scalar(select(ConstructionObject.id).where(ConstructionObject.object_code == code)) is None:
            return code
    return prefix + secrets.token_hex(6).upper()


OBJECT_FIELDS = ("name", "address", "customer", "contractor", "permit_number")


def get_or_create_object(db: Session, fields: dict[str, Any]) -> ConstructionObject:
    """Существующий object_id переиспользуется (пустые поля дополняются), иначе — новый объект с кодом из названия."""
    code = str(fields.get("object_id") or "").strip()
    name = str(fields.get("name") or "").strip()
    values = {k: str(fields.get(k) or "").strip() for k in OBJECT_FIELDS}
    if code:
        if not OBJECT_ID_RE.match(code):
            raise ApiError(422, "INVALID_OBJECT_ID", "Код объекта: до 120 символов без пробелов и «/»",
                           details={"object_id": code})
        obj = db.scalars(select(ConstructionObject).where(ConstructionObject.object_code == code)).first()
        if obj is not None:
            for k, v in values.items():
                if v and not getattr(obj, k):
                    setattr(obj, k, v)
            return obj
    elif not name:
        raise ApiError(422, "OBJECT_REQUIRED", "Укажите название объекта (name) или его код (object_id)")
    obj = ConstructionObject(object_code=code or new_object_code(db, name), **{**values, "name": name or code})
    db.add(obj)
    db.flush()
    return obj


def new_process(db: Session, obj: ConstructionObject, user: User) -> Process:
    process = Process(object_pk=obj.id, status=S.PENDING, created_by=user.id)
    db.add(process)
    db.flush()
    return process


def object_out(obj: ConstructionObject) -> dict:
    return {"object_id": obj.object_code, "name": obj.name, "address": obj.address, "customer": obj.customer,
            "contractor": obj.contractor, "permit_number": obj.permit_number}


# ---------------------------------------------------------------- счётчики и индикатор (чистые функции)

FindingRow = tuple[str, str | None, str | None, int]      # (status, inspector_status, review_priority, число)


def finding_counts(rows: Iterable[FindingRow]) -> dict[str, int]:
    """candidates — все машинные кандидаты, candidates_pending — ещё без решения; остальное — по статусу с учётом
    решения инспектора (effective_status)."""
    out = dict.fromkeys(COUNT_KEYS, 0)
    for status, inspector_status, _priority, n in rows:
        if status == S.CANDIDATE:
            out["candidates"] += n
            if (inspector_status or S.INSPECTOR_PENDING) == S.INSPECTOR_PENDING:
                out["candidates_pending"] += n
        key = EFFECTIVE_KEYS.get(effective_status(status, inspector_status))
        if key:
            out[key] += n
    return out


def process_indicator(protocol_version: int, rows: Iterable[FindingRow]) -> str:
    """gray — протокола нет; red — есть подтверждённое нарушение или нерешённый кандидат HIGH; yellow — есть
    кандидат, NOT_COMPARABLE или CLARIFICATION_REQUIRED; иначе green."""
    if not protocol_version:
        return "gray"
    yellow = False
    for status, inspector_status, priority, n in rows:
        if not n:
            continue
        eff = effective_status(status, inspector_status)
        if eff == S.CONFIRMED_VIOLATION or (eff == S.CANDIDATE and priority == "HIGH"):
            return "red"
        if eff in (S.CANDIDATE, S.NOT_COMPARABLE, S.CLARIFICATION_REQUIRED):
            yellow = True
    return "yellow" if yellow else "green"


def upload_status_rows(files: list, pages_by_file: dict[int, tuple[int, int]]) -> list[dict]:
    """Статус загрузки по стадиям, как в протоколе. Файлы, ещё не обработанные конвейером (pending), считаются
    загруженными (иначе у новой проверки все стадии выглядели бы «отсутствуют»), и это видно в pending_files."""
    view = [SimpleNamespace(id=f.id, doc_stage=f.doc_stage, original_name=f.original_name,
                            status="ok" if f.status == "pending" else f.status) for f in files]
    rows = upload_statuses(view, pages_by_file)
    for row in rows:
        pending = sum(1 for f in files if f.status == "pending" and f.doc_stage == row["stage"])
        row["pending_files"] = pending
        if pending:
            row["comment"] = (f"Загружено, ожидают обработки: {pending}" if pending == row["files"] else
                              f"{row['comment']}; ожидают обработки: {pending}")
    return rows


@dataclass
class ProcessStats:
    files: list = field(default_factory=list)
    pages: dict = field(default_factory=dict)
    finding_rows: list = field(default_factory=list)


def load_stats(db: Session, process_ids: list[str]) -> dict[str, ProcessStats]:
    stats = {pid: ProcessStats() for pid in process_ids}
    if not process_ids:
        return stats
    for fid, pid, stage, status, name in db.execute(
            select(File.id, File.process_id, File.doc_stage, File.status, File.original_name)
            .where(File.process_id.in_(process_ids))):
        stats[pid].files.append(SimpleNamespace(id=fid, doc_stage=stage, status=status, original_name=name))
    n_ocr = func.sum(case((Page.text_source.in_(("ocr", "rapidocr")), 1), else_=0))
    for fpk, pid, n, ocr in db.execute(
            select(Page.file_pk, File.process_id, func.count(), n_ocr).join(File, File.id == Page.file_pk)
            .where(File.process_id.in_(process_ids)).group_by(Page.file_pk, File.process_id)):
        stats[pid].pages[fpk] = (int(n), int(ocr or 0))
    for pid, status, inspector_status, priority, n in db.execute(
            select(Finding.process_id, Finding.status, Finding.inspector_status, Finding.review_priority,
                   func.count())
            .where(Finding.process_id.in_(process_ids), Finding.active.is_(True))
            .group_by(Finding.process_id, Finding.status, Finding.inspector_status, Finding.review_priority)):
        stats[pid].finding_rows.append((status, inspector_status, priority, int(n)))
    return stats


def process_out(process: Process, st: ProcessStats) -> dict:
    return {
        "id": process.id, "object": object_out(process.object), "status": process.status, "stage": process.stage,
        "progress": process.progress or {}, "scenario": process.scenario, "error": process.error,
        "sync_status": process.sync_status, "protocol_version": process.protocol_version, "batch": process.batch,
        "created_at": iso(process.created_at), "updated_at": iso(process.updated_at),
        "finalized_at": iso(process.finalized_at),
        "counts": {"files": sum(1 for f in st.files if f.status != "archive"),
                   "pages": sum(n for n, _ in st.pages.values()), **finding_counts(st.finding_rows)},
        "upload_status": upload_status_rows(st.files, st.pages),
        "indicator": process_indicator(process.protocol_version, st.finding_rows),
    }


def process_view(db: Session, process: Process) -> dict:
    return process_out(process, load_stats(db, [process.id])[process.id])


# ---------------------------------------------------------------- приём файлов (общий для upload и import-local)


@dataclass
class Incoming:
    name: str                          # имя файла без каталогов
    rel_path: str                      # путь внутри пакета
    size: int
    open: Callable[[], BinaryIO]
    owned: bool = False                # открыть и закрыть здесь (файл с диска); загрузку закрывает Starlette


def split_upload_name(raw: str | None) -> tuple[str, str]:
    """Имя из multipart может содержать каталоги (загрузка папки) и «..»: оставляем безопасный относительный путь."""
    parts = []
    for part in PurePosixPath((raw or "").replace("\\", "/")).parts:
        part = re.sub(r"[\x00-\x1f\x7f]", "", part).strip()
        if part and part not in (".", "..", "/"):
            parts.append(part[:255])
    if not parts:
        return "без_имени", "без_имени"
    return parts[-1][:500], "/".join(parts)[-2000:]


def parse_stage(value: str | None) -> str:
    stage = (value or "AUTO").strip().upper()
    if stage not in UPLOAD_STAGES:
        raise ApiError(422, "INVALID_STAGE", "Стадия: PD, RD, ID или AUTO", details={"stage": value})
    return stage


def precheck(name: str, size: int, settings: Settings) -> tuple[str, str] | None:
    ext = safe_ext(name)
    if ext not in ALLOWED_EXT:
        allowed = ", ".join(sorted(e[1:] for e in ALLOWED_EXT))
        return "UNSUPPORTED_FORMAT", f"Формат «{ext[1:] if ext else 'без расширения'}» не поддерживается; " \
                                     f"допустимы: {allowed}"
    if size == 0:
        return "EMPTY_FILE", "Пустой файл"
    if size > settings.max_file_mb * MB:
        return "FILE_TOO_LARGE", f"Файл {size / MB:.1f} МБ больше лимита {settings.max_file_mb} МБ"
    return None


def check_upload_allowed(process: Process) -> None:
    if process.status == S.FINALIZED:
        raise ApiError(409, "PROCESS_FINALIZED", "Протокол финализирован: дозагрузка невозможна, новая проверка — "
                                                 "отдельным процессом", details={"process_status": process.status})
    if process.status not in S.UPLOAD_ALLOWED:
        raise ApiError(409, "PROCESS_BUSY", "Идёт обработка пакета — дождитесь её окончания",
                       details={"process_status": process.status})


def ingest(db: Session, process: Process, items: list[Incoming], stage: str, settings: Settings,
           rejected: list[dict] | None = None) -> dict:
    """Сохранить файлы и строки File. В процессе с протоколом (protocol_version ≥ 1) — новая партия и сразу задача
    incremental. При любой ошибке — откат транзакции и удаление файлов, впервые записанных этим вызовом."""
    incremental = process.protocol_version >= 1
    batch = process.batch + 1 if incremental else process.batch
    declared = None if stage == "AUTO" else stage
    accepted: list[dict] = []
    rejected = list(rejected or [])
    created: list[Path] = []
    job_id = None
    try:
        for it in items:
            problem = precheck(it.name, it.size, settings)
            if problem:
                rejected.append({"name": it.name, "reason_code": problem[0], "reason": problem[1]})
                continue
            fh = it.open()
            try:
                stored = store_stream(fh, it.name, settings.max_file_mb * MB, settings)
            except FileTooLarge:
                rejected.append({"name": it.name, "reason_code": "FILE_TOO_LARGE",
                                 "reason": f"Файл больше лимита {settings.max_file_mb} МБ"})
                continue
            finally:
                if it.owned:
                    fh.close()
            if stored.created:
                created.append(stored.path)
            f = File(process_id=process.id, file_id=f"LOCAL-{stored.sha256[:8]}", original_name=it.name,
                     rel_path=it.rel_path, ext=safe_ext(it.name), size=stored.size, sha256=stored.sha256,
                     storage_path=str(stored.path), declared_stage=declared, doc_stage=declared,
                     stage_source="declared" if declared else None, status="pending", batch=batch)
            db.add(f)
            db.flush()
            accepted.append({"file_pk": f.id, "name": it.name, "rel_path": it.rel_path, "size": stored.size,
                             "stage": stage, "status": f.status})
        if incremental and accepted:
            process.batch = batch
            try:
                job_id = jobs.enqueue(db, process, kind="incremental").id
            except jobs.JobConflict as exc:
                raise ApiError(409, "PROCESS_BUSY", str(exc)) from None
    except BaseException:
        db.rollback()
        for path in created:
            # хранилище общее (по SHA-256): тот же файл мог уже закоммитить другой запрос — его блоб не трогаем
            if db.scalars(select(File.id).where(File.storage_path == str(path)).limit(1)).first() is None:
                path.unlink(missing_ok=True)
        raise
    return {"process_id": process.id, "batch": process.batch, "job_id": job_id, "accepted": accepted,
            "rejected": rejected}


def _upload_size(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    pos = upload.file.tell()
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(pos)
    return size


# ---------------------------------------------------------------- ручки: проверки


class ProcessCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    object_id: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=500)
    customer: str | None = Field(default=None, max_length=300)
    contractor: str | None = Field(default=None, max_length=300)
    permit_number: str | None = Field(default=None, max_length=120)


@router.post("/processes", status_code=201, summary="Создать проверку объекта (без файлов)")
def create_process(body: ProcessCreate, request: Request, db: DbDep,
                   user: User = Depends(require("upload"))) -> dict:
    obj = get_or_create_object(db, body.model_dump())
    process = new_process(db, obj, user)
    audit(db, request, user, "process_create", process_id=process.id, object_code=obj.object_code)
    db.commit()
    return process_view(db, process)


def _day_start(day: dt.date, tz_offset_min: int = 0) -> dt.datetime:
    """Полночь дня в поясе клиента, в UTC. tz_offset_min — как getTimezoneOffset() в браузере (Москва: −180)."""
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=tz_offset_min)


@router.get("/processes", summary="Список проверок (фильтры: статус, поиск, даты)")
def list_processes(_: Reader, db: DbDep, status: str | None = Query(default=None, description="через запятую"),
                   q: str | None = None, date_from: dt.date | None = None, date_to: dt.date | None = None,
                   tz_offset_min: int = Query(default=0, ge=-840, le=840,
                                              description="пояс клиента для границ дат: getTimezoneOffset() в JS"),
                   limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> dict:
    conds = []
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        bad = [s for s in wanted if s not in S.PROCESS_STATUSES]
        if bad:
            raise ApiError(422, "INVALID_STATUS", "Неизвестный статус проверки",
                           details={"invalid": bad, "allowed": list(S.PROCESS_STATUSES)})
        conds.append(Process.status.in_(wanted))
    if q and q.strip():
        needle = q.strip()
        conds.append(or_(ConstructionObject.name.icontains(needle, autoescape=True),
                         ConstructionObject.object_code.icontains(needle, autoescape=True),
                         ConstructionObject.address.icontains(needle, autoescape=True),
                         Process.id.startswith(needle, autoescape=True)))
    if date_from:
        conds.append(Process.created_at >= _day_start(date_from, tz_offset_min))
    if date_to:
        conds.append(Process.created_at < _day_start(date_to + dt.timedelta(days=1), tz_offset_min))
    base = select(Process.id).join(ConstructionObject, ConstructionObject.id == Process.object_pk).where(*conds)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    ids = list(db.scalars(base.order_by(Process.created_at.desc(), Process.id.desc()).limit(limit).offset(offset)))
    by_id = {p.id: p for p in db.scalars(select(Process).where(Process.id.in_(ids)))} if ids else {}
    stats = load_stats(db, ids)
    return {"items": [process_out(by_id[i], stats[i]) for i in ids], "total": total}


@router.get("/processes/{process_id}", summary="Проверка: статус, этап, прогресс, счётчики, индикатор")
def get_process(process_id: str, _: Reader, db: DbDep) -> dict:
    return process_view(db, get_process_or_404(db, process_id))


@router.post("/processes/{process_id}/start", status_code=202, summary="Поставить проверку в очередь конвейера")
def start_process(process_id: str, request: Request, db: DbDep, user: User = Depends(require("upload"))) -> dict:
    process = get_process_or_404(db, process_id)
    if process.status == S.FINALIZED:
        raise ApiError(409, "PROCESS_FINALIZED", "Протокол финализирован: новая проверка — отдельным процессом")
    if db.scalar(select(func.count()).select_from(File).where(File.process_id == process.id)) == 0:
        raise ApiError(409, "NO_FILES", "В проверке нет файлов — сначала загрузите пакет")
    try:
        job = jobs.enqueue(db, process, kind="full")
    except jobs.JobConflict as exc:
        raise ApiError(409, "PROCESS_BUSY", str(exc)) from None
    audit(db, request, user, "start", process_id=process.id, object_code=process.object.object_code,
          details={"job_id": job.id, "kind": job.kind, "batch": job.batch})
    db.commit()
    return {"job_id": job.id, "process_id": process.id, "status": process.status}


# ---------------------------------------------------------------- ручки: загрузка

UPLOAD_OPENAPI = {"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
    "type": "object", "required": ["files"],
    "properties": {
        "files": {"type": "array", "items": {"type": "string", "format": "binary"},
                  "description": "Файлы (поле повторяется; допускается имя files[]). 50 МБ на файл, 200 МБ на запрос"},
        "stage": {"type": "string", "enum": list(UPLOAD_STAGES), "default": "AUTO"},
        "process_id": {"type": "string", "description": "Дозагрузка в существующую проверку"},
        "object_id": {"type": "string"}, "name": {"type": "string", "description": "Название объекта"},
        "address": {"type": "string"}, "customer": {"type": "string"}, "contractor": {"type": "string"},
        "permit_number": {"type": "string"}}}}}}}


def _upload_sync(db: Session, request: Request, user: User, settings: Settings, uploads: list[UploadFile],
                 fields: dict[str, str]) -> dict:
    stage = parse_stage(fields.get("stage"))
    if not uploads:
        raise ApiError(422, "NO_FILES", "В запросе нет файлов (поле files)")
    sizes = [_upload_size(u) for u in uploads]
    total, limit = sum(sizes), settings.max_upload_mb * MB
    if total > limit:              # пакет целиком отклоняется: ни файлов, ни строк в БД
        raise ApiError(413, "UPLOAD_TOO_LARGE",
                       f"Суммарный размер файлов {total / MB:.1f} МБ больше лимита {settings.max_upload_mb} МБ "
                       f"на запрос — разделите пакет", details={"limit_bytes": limit, "total_bytes": total})
    process_id = (fields.get("process_id") or "").strip()
    if process_id:
        process = get_process_or_404(db, process_id)
        check_upload_allowed(process)
    else:
        process = new_process(db, get_or_create_object(db, fields), user)
    items = []
    for upload, size in zip(uploads, sizes):
        name, rel_path = split_upload_name(upload.filename)
        items.append(Incoming(name=name, rel_path=rel_path, size=size, open=lambda u=upload: u.file))
    result = ingest(db, process, items, stage, settings)
    audit(db, request, user, "upload", process_id=process.id, object_code=process.object.object_code,
          details={"stage": stage, "accepted": len(result["accepted"]), "rejected": len(result["rejected"]),
                   "rejected_files": [{"name": r["name"], "reason_code": r["reason_code"]}
                                      for r in result["rejected"][:50]],
                   "batch": result["batch"], "job_id": result["job_id"]})
    db.commit()
    return result


@router.post("/documents/upload", summary="Загрузка файлов (multipart): новая проверка или дозагрузка",
             openapi_extra=UPLOAD_OPENAPI)
async def upload_documents(request: Request, db: DbDep, settings: SettingsDep,
                           user: User = Depends(require("upload"))) -> dict:
    # тело читается здесь, после проверки токена и прав: без входа сервер не принимает 200 МБ во временные файлы
    form = await request.form(max_files=MAX_FILES_PER_REQUEST, max_fields=100)
    try:
        uploads = [v for k, v in form.multi_items() if k in FILE_FIELDS and isinstance(v, UploadFile)]
        fields = {k: v for k, v in form.multi_items() if isinstance(v, str)}
        return await run_in_threadpool(_upload_sync, db, request, user, settings, uploads, fields)
    finally:
        await form.close()


# ---------------------------------------------------------------- ручки: импорт с диска сервера


class ImportLocalIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    stage: str = "AUTO"


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def collect_local(base: Path, roots: list[Path]) -> tuple[list[Incoming], list[dict]]:
    if base.is_file():
        candidates = [(base, base.name)]
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                candidates.append((p, p.relative_to(base).as_posix()))
                if len(candidates) > MAX_IMPORT_FILES:
                    raise ApiError(422, "TOO_MANY_FILES", f"В каталоге больше {MAX_IMPORT_FILES} файлов")
    items, rejected = [], []
    for p, rel in candidates:
        real = p.resolve()
        if not _within(real, roots) or not real.is_file():
            rejected.append({"name": p.name, "reason_code": "PATH_NOT_ALLOWED",
                             "reason": "Ссылка ведёт за пределы разрешённых каталогов импорта"})
            continue
        items.append(Incoming(name=p.name, rel_path=rel, size=real.stat().st_size,
                              open=lambda real=real: open(real, "rb"), owned=True))
    return items, rejected


@router.post("/processes/{process_id}/import-local", summary="Импорт пакета с диска сервера (admin)")
def import_local(process_id: str, body: ImportLocalIn, request: Request, db: DbDep, settings: SettingsDep,
                 user: User = Depends(require("import"))) -> dict:
    stage = parse_stage(body.stage)
    process = get_process_or_404(db, process_id)
    check_upload_allowed(process)
    roots = [Path(r).expanduser().resolve() for r in settings.import_roots.split(":") if r.strip()]
    if not roots:
        raise ApiError(403, "IMPORT_DISABLED", "Импорт с диска сервера не настроен (переменная IMPORT_ROOTS)")
    target = Path(body.path).expanduser().resolve()
    if not _within(target, roots):
        raise ApiError(403, "PATH_NOT_ALLOWED", "Путь вне разрешённых каталогов импорта (IMPORT_ROOTS)",
                       details={"path": body.path})
    if not target.exists():
        raise ApiError(404, "PATH_NOT_FOUND", "Путь не найден", details={"path": body.path})
    items, rejected = collect_local(target, roots)
    result = ingest(db, process, items, stage, settings, rejected=rejected)
    audit(db, request, user, "import_local", process_id=process.id, object_code=process.object.object_code,
          details={"path": str(target), "stage": stage, "accepted": len(result["accepted"]),
                   "rejected": len(result["rejected"]), "batch": result["batch"], "job_id": result["job_id"]})
    db.commit()
    return result
