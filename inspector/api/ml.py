"""Обучаемые модели с дообучением (ТЗ 9.4, Q&A №24). Классификатор типа страницы: страницы с метками для разметки,
правки разметчиков. Отбор страниц F40: метки из решений экспертов по кандидатам. У обеих — итерация дообучения,
приёмка, ручное утверждение или отклонение версии, откат, журнал итераций (ML_Retraining_Log)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inspector.api.deps import API_PREFIX, ApiError, DbDep, Reader, audit, get_process_or_404, iso, require
from inspector.config import get_settings
from inspector.ml import page_classifier as PC
from inspector.ml import page_router as PR
from inspector.ml import registry as R
from inspector.models import File, ModelVersion, Page, PageCorrection, PageLabel, RetrainingLog, User

router = APIRouter(prefix=API_PREFIX, tags=["page-classifier"])


def _labels(db: Session, process_id: str, source: str | None = None) -> dict[int, PageLabel]:
    """Последняя метка каждой страницы проверки (source — только метки этого источника)."""
    pages = select(Page.id).join(File, File.id == Page.file_pk).where(File.process_id == process_id)
    last = select(PageLabel.page_pk, func.max(PageLabel.id).label("id")).where(PageLabel.page_pk.in_(pages))
    if source:
        last = last.where(PageLabel.source == source)
    last = last.group_by(PageLabel.page_pk).subquery()
    return {lb.page_pk: lb for lb in db.scalars(select(PageLabel).join(last, PageLabel.id == last.c.id))}


def page_row(p: Page, f: File, lb: PageLabel | None, model: PageLabel | None, corrected: bool) -> dict:
    return {"page_pk": p.id, "file_pk": f.id, "file_id": f.file_id, "file_name": f.original_name,
            "stage": f.doc_stage, "page_no": p.page_no, "page_class": p.page_class,
            "other_classes": p.other_classes or [], "is_first_page": p.is_first_page, "section": p.section,
            "quality_label": p.quality_label, "title": p.title, "text_source": p.text_source,
            "label_source": lb.source if lb else None, "confidence": lb.confidence if lb else None,
            "label_model": lb.model_version if lb else None,
            "model_class": model.page_class if model else None,
            "model_confidence": model.confidence if model else None,
            "corrected": corrected, "document_pk": p.document_pk}


@router.get("/ml/page-taxonomy", summary="Типы страниц и разделы для разметки")
def taxonomy(_: Reader) -> dict:
    from inspector.pipeline.classify import CLASSES, ID_CLASSES, SECTIONS, SHEET_CLASSES

    def group(c: str) -> str:
        return "sheet" if c in SHEET_CLASSES else "id" if c in ID_CLASSES else "other"

    return {"classes": [{"code": c, "description": d, "group": group(c)} for c, d in CLASSES.items()],
            "sections": SECTIONS}


@router.get("/processes/{process_id}/pages", summary="Страницы проверки с типом, источником метки и уверенностью")
def list_pages(process_id: str, _: Reader, db: DbDep, page_class: str | None = None,
               source: str | None = Query(default=None, description="model | qwen | inspector | none"),
               max_confidence: float | None = Query(default=None, ge=0, le=1,
                                                    description="уверенность модели не выше (страницы без метки "
                                                                "модели не попадают)"),
               corrected: bool | None = None, file_pk: int | None = None, stage: str | None = None,
               limit: int = Query(default=200, ge=1, le=5000), offset: int = Query(default=0, ge=0)) -> dict:
    get_process_or_404(db, process_id)
    q = select(Page, File).join(File, File.id == Page.file_pk).where(File.process_id == process_id,
                                                                    File.status == "ok")
    if page_class:
        q = q.where(Page.page_class == page_class)
    if file_pk:
        q = q.where(Page.file_pk == file_pk)
    if stage:
        q = q.where(File.doc_stage == stage)
    rows = db.execute(q.order_by(Page.file_pk, Page.page_no)).all()
    labels, model = _labels(db, process_id), _labels(db, process_id, "model")
    fixed = {pk for (pk,) in db.execute(select(PageCorrection.page_pk).where(
        PageCorrection.process_id == process_id).distinct())}
    items = [page_row(p, f, labels.get(p.id), model.get(p.id), p.id in fixed) for p, f in rows]
    if source:
        items = [it for it in items if (it["label_source"] or "none") == source]
    if max_confidence is not None:
        items = [it for it in items if it["model_confidence"] is not None and it["model_confidence"] <= max_confidence]
    if corrected is not None:
        items = [it for it in items if it["corrected"] == corrected]
    return {"items": items[offset:offset + limit], "total": len(items)}


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_pk: int | None = None
    file_pk: int | None = None
    page_no: int | None = Field(default=None, ge=1)
    page_class: str | None = Field(default=None, max_length=40)
    is_first_page: bool | None = None
    section: str | None = Field(default=None, max_length=20)


class CorrectionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CorrectionIn] = Field(min_length=1, max_length=5000)
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/processes/{process_id}/pages/corrections", status_code=201,
             summary="Правки разметчика: верный тип / первая страница / раздел (сразу меняют метку, идут в обучение)")
def correct(process_id: str, body: CorrectionsIn, request: Request, db: DbDep,
            user: User = Depends(require("label"))) -> dict:
    from inspector.pipeline.classify import CLASSES, SECTIONS

    process = get_process_or_404(db, process_id)
    items = [it.model_dump(exclude_none=True) for it in body.items]
    bad = [it for it in items if ("page_class" in it and it["page_class"] not in CLASSES)
           or ("section" in it and it["section"] not in SECTIONS)
           or not (it.get("page_pk") or (it.get("file_pk") and it.get("page_no")))]
    if bad:
        raise ApiError(422, "BAD_CORRECTION", "Неизвестный тип или раздел страницы, либо страница не указана",
                       details={"items": bad[:10]})
    try:
        rows = PC.correct_pages(db, process, user, items, body.comment)
    except PC.ClassifierError as exc:
        raise ApiError(404, "PAGE_NOT_FOUND", str(exc)) from exc
    audit(db, request, user, "page_correction", process_id=process_id,
          details={"n": len(rows), "pages": [r.page_pk for r in rows][:50]})
    db.commit()
    return {"created": len(rows), "items": [{"id": r.id, "page_pk": r.page_pk, "page_class": r.page_class,
                                             "is_first_page": r.is_first_page, "section": r.section,
                                             "before": r.before} for r in rows]}


def log_out(r: RetrainingLog) -> dict:
    return {"id": r.id, "model_kind": r.model_kind, "status": r.status, "base_version": r.base_version,
            "new_version": r.new_version,
            "n_corrections": r.n_corrections, "corrections_sha256": r.corrections_sha256,
            "metrics_before": r.metrics_before, "metrics_after": r.metrics_after, "rule": r.rule,
            "seconds": r.seconds, "error": r.error, "requested_by": r.requested_by, "decided_by": r.decided_by,
            "artifact": r.artifact, "created_at": iso(r.created_at), "finished_at": iso(r.finished_at)}


@router.get("/ml/page-classifier", summary="Классификатор страниц: действующая версия, версии, правки, итерации")
def status(_: Reader, db: DbDep) -> dict:
    settings = get_settings()
    model_dir = PC.active_model_dir(db, settings)
    db.commit()                                   # начальная модель могла зарегистрироваться
    cur = PC.deployed(db)
    versions = [{"model_version": m.model_version, "approval_status": m.approval_status, "approved_by": m.approved_by,
                 "deployed_at": iso(m.deployed_at), "rollback_to": m.rollback_to, "metrics": m.metrics,
                 "artifact": m.artifact, "is_current": cur is not None and m.id == cur.id}
                for m in db.scalars(select(ModelVersion).where(ModelVersion.model_version.like(f"{PC.KIND}:%"))
                                    .order_by(ModelVersion.id.desc()))]
    total = db.scalar(select(func.count()).select_from(PageCorrection)) or 0
    unused = db.scalar(select(func.count()).select_from(PageCorrection)
                       .where(PageCorrection.used_in_version.is_(None))) or 0
    log = [log_out(r) for r in R.log_rows(db, PC.REG)]
    acceptance = PC.load_acceptance(settings)
    return {"enabled": model_dir is not None, "current": cur.model_version if cur else None,
            "manifest": PC.read_manifest(model_dir) if model_dir else None, "versions": versions,
            "corrections": {"total": total, "unused": unused, "confusions": confusions(db)},
            "dataset": PC.dataset_state(db),
            "acceptance_set": acceptance is not None, "acceptance_pages": len(acceptance) if acceptance else 0,
            "threshold": PC.threshold(settings, model_dir) if model_dir else None, "log": log}


def confusions(db: Session, top: int = 12) -> list[dict]:
    """Что на что исправляют чаще всего (по последней правке страницы): где классификатор путается."""
    pairs: dict[tuple[str, str], int] = {}
    for r in PC.latest_corrections(db):
        was = (r.before or {}).get("page_class")
        if r.page_class and was and r.page_class != was:
            pairs[(was, r.page_class)] = pairs.get((was, r.page_class), 0) + 1
    return [{"from": a, "to": b, "n": n} for (a, b), n in sorted(pairs.items(), key=lambda kv: -kv[1])[:top]]


@router.post("/ml/page-classifier/retrain", status_code=202,
             summary="Итерация дообучения на правках разметчиков (выполнит worker; одна за раз)")
def retrain(request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        row = PC.request_retrain(db, user.login)
    except PC.ClassifierError as exc:
        raise ApiError(409, "RETRAIN_NOT_ALLOWED", str(exc)) from exc
    audit(db, request, user, "retrain_requested", details={"log_id": row.id})
    db.commit()
    return log_out(row)


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=300)


@router.post("/ml/page-classifier/approve", summary="Утвердить черновую версию и опубликовать её")
def approve(body: VersionIn, request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        mv = PC.approve(db, body.version, user.login)
    except PC.ClassifierError as exc:
        raise ApiError(409, "MODEL_NOT_APPROVABLE", str(exc)) from exc
    audit(db, request, user, "model_approved", details={"version": mv.model_version})
    db.commit()
    return {"current": mv.model_version, "rollback_to": mv.rollback_to}


@router.post("/ml/page-classifier/reject", summary="Отклонить версию-кандидата (публиковать её будет нельзя)")
def reject(body: VersionIn, request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        mv = PC.reject(db, body.version, user.login)
    except PC.ClassifierError as exc:
        raise ApiError(409, "MODEL_NOT_REJECTABLE", str(exc)) from exc
    audit(db, request, user, "model_rejected", details={"version": mv.model_version})
    db.commit()
    cur = PC.deployed(db)
    return {"current": cur.model_version if cur else None, "rejected": mv.model_version}


@router.post("/ml/page-classifier/rollback", summary="Откатиться к прежней утверждённой версии")
def rollback(body: VersionIn, request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        mv = PC.rollback(db, body.version, user.login)
    except PC.ClassifierError as exc:
        raise ApiError(409, "MODEL_NOT_FOUND", str(exc)) from exc
    audit(db, request, user, "model_rollback", details={"version": mv.model_version, "from": mv.rollback_to})
    db.commit()
    return {"current": mv.model_version, "rollback_to": mv.rollback_to}


@router.get("/ml/retraining-log", summary="Журнал итераций дообучения (ML_Retraining_Log)")
def retraining_log(_: Reader, db: DbDep, limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    rows = list(db.scalars(select(RetrainingLog).order_by(RetrainingLog.id.desc()).limit(limit)))
    return {"items": [log_out(r) for r in rows], "total": len(rows)}


# ------------------------------------------------------------------------------------------------ отбор страниц F40
@router.get("/ml/page-router", tags=["page-router"],
            summary="Отбор страниц: действующая версия, версии, метки из решений экспертов, итерации")
def router_status(_: Reader, db: DbDep) -> dict:
    from inspector.pipeline.router import BASE_K

    model_dir = PR.active_dir(db)
    labels = PR.collect_labels(db)
    sha = labels.sha256() if labels.pages else None
    man = PR.read_manifest(model_dir) if model_dir else None
    return {"enabled": model_dir is not None, "select_mode": get_settings().select_mode, "top_k": BASE_K,
            "current": PR.current_name(db),
            "manifest": {k: man.get(k) for k in ("name", "version", "n_pages", "n_models", "keep_per_param",
                                                 "trained_on", "labels", "scan_text", "parent_version",
                                                 "expert_labels", "adaptation", "created")} if man else None,
            "versions": [{**v, "deployed_at": iso(v["deployed_at"])} for v in PR.versions(db)],
            "labels": {**labels.counts(), "sha256": sha, "new": sha is not None and sha != PR.last_trained_sha(db)},
            "log": [log_out(r) for r in R.log_rows(db, PR.REG)]}


@router.post("/ml/page-router/retrain", status_code=202, tags=["page-router"],
             summary="Итерация дообучения отбора страниц на решениях экспертов (выполнит worker; одна за раз)")
def router_retrain(request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        row = PR.request_retrain(db, user.login)
    except PR.RouterError as exc:
        raise ApiError(409, "RETRAIN_NOT_ALLOWED", str(exc)) from exc
    audit(db, request, user, "router_retrain_requested", details={"log_id": row.id})
    db.commit()
    return log_out(row)


@router.post("/ml/page-router/approve", tags=["page-router"], summary="Утвердить версию отбора и опубликовать её")
def router_approve(body: VersionIn, request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        mv = PR.approve(db, body.version, user.login)
    except PR.RouterError as exc:
        raise ApiError(409, "MODEL_NOT_APPROVABLE", str(exc)) from exc
    audit(db, request, user, "router_approved", details={"version": mv.model_version})
    db.commit()
    return {"current": mv.model_version, "rollback_to": mv.rollback_to}


@router.post("/ml/page-router/reject", tags=["page-router"],
             summary="Отклонить версию-кандидата отбора (публиковать её будет нельзя)")
def router_reject(body: VersionIn, request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        mv = PR.reject(db, body.version, user.login)
    except PR.RouterError as exc:
        raise ApiError(409, "MODEL_NOT_REJECTABLE", str(exc)) from exc
    audit(db, request, user, "router_rejected", details={"version": mv.model_version})
    db.commit()
    return {"current": PR.current_name(db), "rejected": mv.model_version}


@router.post("/ml/page-router/rollback", tags=["page-router"], summary="Откатить отбор к прежней утверждённой версии")
def router_rollback(body: VersionIn, request: Request, db: DbDep, user: User = Depends(require("retrain"))) -> dict:
    try:
        mv = PR.rollback(db, body.version, user.login)
    except PR.RouterError as exc:
        raise ApiError(409, "MODEL_NOT_FOUND", str(exc)) from exc
    audit(db, request, user, "router_rollback", details={"version": mv.model_version, "from": mv.rollback_to})
    db.commit()
    return {"current": mv.model_version, "rollback_to": mv.rollback_to}
