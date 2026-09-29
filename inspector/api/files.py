"""Файлы проверки с паспортом, собранные документы, страница (текст и разметка), картинка страницы «как видна»
(с учётом Rotate — для панелей ПД/РД/ИД), скачивание исходника (пишется в журнал аудита)."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.api.deps import (API_PREFIX, ApiError, DbDep, MediaReader, Reader, SettingsDep, attachment_header,
                                audit, get_process_or_404, iso)
from inspector.models import Document, File, Page, Process
from inspector.storage import PageOutOfRange, PdfUnreadable, render_page_png

router = APIRouter(prefix=API_PREFIX, tags=["files"])


def file_out(f: File) -> dict:
    return {"file_pk": f.id, "file_id": f.file_id, "name": f.original_name, "rel_path": f.rel_path, "ext": f.ext,
            "size": f.size, "sha256": f.sha256, "stage": f.doc_stage, "declared_stage": f.declared_stage,
            "stage_source": f.stage_source, "status": f.status, "error": f.error, "pages": f.pages,
            "ocr_pages": f.ocr_pages, "document_code": f.document_code, "revision": f.revision,
            "approval_status": f.approval_status, "approval_date": f.approval_date, "discipline": f.discipline,
            "has_signature": f.has_signature,
            "batch": f.batch, "uploaded_at": iso(f.uploaded_at)}


def page_out(p: Page) -> dict:
    return {"page_no": p.page_no, "width": p.width, "height": p.height, "rotation": p.rotation,
            "text_source": p.text_source, "text": p.text, "page_class": p.page_class,
            "other_classes": p.other_classes or [], "is_first_page": p.is_first_page, "section": p.section,
            "title": p.title, "quality_label": p.quality_label, "low_quality": p.low_quality,
            "document_pk": p.document_pk}


def get_file_or_404(db: Session, file_pk: int) -> File:
    f = db.get(File, file_pk)
    if f is None:
        raise ApiError(404, "FILE_NOT_FOUND", "Файл не найден", details={"file_pk": file_pk})
    return f


def renderable_pdf(f: File) -> str | None:
    """PDF, по которому рисуются страницы: результат конвертации (DOCX, картинки) или сам исходник-PDF."""
    if f.pdf_path:
        return f.pdf_path
    if f.ext == ".pdf" and f.storage_path:
        return f.storage_path
    return None


@router.get("/processes/{process_id}/files", summary="Файлы проверки с паспортом")
def list_files(process_id: str, _: Reader, db: DbDep) -> dict:
    get_process_or_404(db, process_id)
    items = [file_out(f) for f in db.scalars(select(File).where(File.process_id == process_id).order_by(File.id))]
    return {"items": items, "total": len(items)}


@router.get("/processes/{process_id}/documents", summary="Документы, собранные из страниц, и их паспорт")
def list_documents(process_id: str, _: Reader, db: DbDep) -> dict:
    get_process_or_404(db, process_id)
    rows = db.execute(select(Document, File.file_id, File.original_name).join(File, File.id == Document.file_pk)
                      .where(Document.process_id == process_id).order_by(Document.file_pk, Document.first_page)).all()
    items = [{"id": d.id, "file_pk": d.file_pk, "file_id": file_id, "file_name": name, "first_page": d.first_page,
              "last_page": d.last_page, "doc_type": d.doc_type, "stage": d.stage, "section": d.section,
              "title": d.title, "document_code": d.document_code, "sheet": d.sheet, "revision": d.revision,
              "approval_status": d.approval_status, "is_actual": d.is_actual,
              "superseded_by_pk": d.superseded_by_pk, "clarification": d.clarification,
              "clarification_reason": d.clarification_reason} for d, file_id, name in rows]
    return {"items": items, "total": len(items)}


def _page(db: Session, file_pk: int, page_no: int) -> dict:
    f = get_file_or_404(db, file_pk)
    p = db.scalars(select(Page).where(Page.file_pk == f.id, Page.page_no == page_no)).first()
    if p is None:
        raise ApiError(404, "PAGE_NOT_FOUND", "Страница не найдена (файл ещё не разобран или номер вне файла)",
                       details={"file_pk": file_pk, "page": page_no, "pages": f.pages})
    return page_out(p)


@router.get("/files/{file_pk}/pages/{page_no}", summary="Страница: текст, класс, признаки")
def get_page(file_pk: int, page_no: int, _: Reader, db: DbDep) -> dict:
    return _page(db, file_pk, page_no)


@router.get("/files/{file_pk}/pages/{page_no}/text", summary="Текст страницы (то же, что /pages/{n})")
def get_page_text(file_pk: int, page_no: int, _: Reader, db: DbDep) -> dict:
    return _page(db, file_pk, page_no)


@router.get("/files/{file_pk}/pages/{page_no}/image", response_class=Response,
            summary="PNG страницы так, как она видна (с учётом поворота); токен можно передать ?access_token=",
            responses={200: {"content": {"image/png": {}}}})
def page_image(file_pk: int, _: MediaReader, db: DbDep, settings: SettingsDep, page_no: int = PathParam(ge=1),
               width: int = Query(default=1400, ge=200, le=3000)) -> Response:
    f = get_file_or_404(db, file_pk)
    pdf = renderable_pdf(f)
    if pdf is None:
        raise ApiError(409, "NOT_CONVERTED", "Файл ещё не преобразован в PDF — страницы появятся после разбора",
                       details={"file_pk": file_pk, "ext": f.ext, "status": f.status})
    if not Path(pdf).is_file():
        raise ApiError(404, "FILE_MISSING", "Файл отсутствует в хранилище", details={"file_pk": file_pk})
    try:
        png = render_page_png(pdf, page_no, width, cache_key=f.sha256 or None, settings=settings)
    except PageOutOfRange as exc:
        raise ApiError(404, "PAGE_NOT_FOUND", "Страница вне файла", details={"page": page_no, "reason": str(exc)})
    except PdfUnreadable as exc:
        raise ApiError(422, "FILE_BROKEN", "PDF не читается", details={"reason": str(exc)[:300]})
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/files/{file_pk}/download", summary="Скачать исходный файл; токен можно передать ?access_token=")
def download_file(file_pk: int, user: MediaReader, request: Request, db: DbDep) -> FileResponse:
    f = get_file_or_404(db, file_pk)
    path = Path(f.storage_path) if f.storage_path else None
    if path is None or not path.is_file():
        raise ApiError(404, "FILE_MISSING", "Файл отсутствует в хранилище", details={"file_pk": file_pk})
    process = db.get(Process, f.process_id)
    audit(db, request, user, "file_download", process_id=f.process_id,
          object_code=process.object.object_code if process else None,
          details={"file_pk": f.id, "file_id": f.file_id, "name": f.original_name, "sha256": f.sha256,
                   "stage": f.doc_stage})
    db.commit()
    media = mimetypes.guess_type(f.original_name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, headers={"Content-Disposition": attachment_header(f.original_name)})
