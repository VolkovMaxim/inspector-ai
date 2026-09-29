"""Протокол сверки (Приложение 2 ТЗ) из состояния БД + сабмит по схеме организаторов. Это контракт для API,
интерфейса и выгрузок PDF/DOCX/XML."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import Counter, defaultdict
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.config import RESOURCES, get_settings
from inspector.models import (DatasetVersion, Document, File, Finding, Page, Param, Process, ProtocolVersion)

MATRIX_VERSION = "matrix-132-r1.1"
PROMPT_VERSIONS = "classify=pc1r;extract=g2;judge=j1;verify=v1;ocr=plain1"
PARAM_ORDER = (S.CONFIRMED_VIOLATION, S.CANDIDATE, S.CLARIFICATION_REQUIRED, S.NOT_COMPARABLE, S.NEGATIVE_VERIFIED,
               S.MISSING_EVIDENCE, S.NOT_APPLICABLE)
ROLE_STAGE = {"expected": "PD", "actual_rd": "RD", "actual_id": "ID"}


def effective_status(status: str, inspector_status: str | None) -> str:
    """Статус с учётом решения инспектора: у CANDIDATE решение заменяет машинный статус."""
    if status == S.CANDIDATE and inspector_status and inspector_status != S.INSPECTOR_PENDING:
        return inspector_status
    return status


def input_manifest_hash(files: list[File]) -> str:
    lines = sorted(f"{f.file_id};{f.sha256};{f.doc_stage or ''}" for f in files if f.status == "ok")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def model_version(db: Session | None = None) -> str:
    """Строка моделей и промптов прогона; db — взять действующую версию отбора страниц (после дообучения), без неё —
    исходную."""
    from inspector.pipeline import extract

    s = get_settings()
    versions = PROMPT_VERSIONS
    if s.extract_with_image:                    # картинка страницы в извлечении — другая версия этапа
        versions = versions.replace(f"extract={extract.PROMPT_VERSION}",
                                    f"extract={extract.PROMPT_VERSION}+{extract.IMAGE_TAG}")
    return f"{s.model_version};{versions};select={select_label(s, db)}"


def select_label(s, db: Session | None = None) -> str:
    """Отбор страниц в версии модели: обучаемый (версия артефакта) или только по словам."""
    from inspector.ml.page_router import current_model
    from inspector.pipeline.router import load_router

    if db is not None:
        router = current_model(db, s)
    else:
        router = load_router() if s.select_mode == "fused" else None
    return f"f{s.top_k_pages}:{router.version}" if router is not None else f"lex{s.top_k_pages}"


def _iso(ts: dt.datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.isoformat()


def upload_statuses(files: list[File], pages_by_file: dict[int, tuple[int, int]]) -> list[dict]:
    out = []
    for stage in S.STAGES:
        mine = [f for f in files if f.doc_stage == stage and f.status not in ("duplicate", "junk", "archive")]
        ok = [f for f in mine if f.status == "ok"]
        problems = [f for f in mine if f.status in ("broken", "unsupported", "too_large")]
        pages = sum(pages_by_file.get(f.id, (0, 0))[0] for f in ok)
        ocr_pages = sum(pages_by_file.get(f.id, (0, 0))[1] for f in ok)
        status = S.upload_status(stage, len(ok), len(problems))
        comment = ("Все файлы читаются" if status.endswith("UPLOADED") else
                   "Файлы стадии не загружены" if status.endswith("MISSING") else
                   "Не читаются: " + ", ".join(f"{f.original_name} ({f.status})" for f in problems[:5]))
        out.append({"stage": stage, "status": status, "files": len(ok), "problem_files": len(problems),
                    "pages": pages, "ocr_pages": ocr_pages, "comment": comment})
    return out


def _page_docs(session: Session, file_pks: list[int]) -> dict[tuple[int, int], Document]:
    if not file_pks:
        return {}
    rows = session.execute(select(Page.file_pk, Page.page_no, Document).join(Document, Page.document_pk == Document.id)
                           .where(Page.file_pk.in_(file_pks))).all()
    return {(fp, pn): d for fp, pn, d in rows}


def finding_card(f: Finding, files: dict[int, File], docs: dict[tuple[int, int], Document], params: dict[str, Param],
                 object_code: str, users: dict[int, str] | None = None) -> dict:
    param = params.get(f.param_code)
    eff = effective_status(f.status, f.inspector_status)
    cs = S.card_status(eff)
    sources = []
    for e in f.evidence:
        fl = files.get(e.file_pk)
        d = docs.get((e.file_pk, e.page_no))
        sources.append({
            "role": e.role, "stage": e.stage, "file_pk": e.file_pk, "file_id": fl.file_id if fl else None,
            "file_name": fl.original_name if fl else None, "sha256": fl.sha256 if fl else None,
            "document_code": (d.document_code if d and d.document_code else fl.document_code if fl else None),
            "revision": (d.revision if d and d.revision else fl.revision if fl else None),
            "approval_status": (d.approval_status if d and d.approval_status else
                                (fl.approval_status if fl and fl.approval_status else "UNKNOWN")),
            "approval_date": fl.approval_date if fl else None,
            "sheet": d.sheet if d else None, "page": e.page_no, "bbox": e.bbox, "bbox_source": e.bbox_source,
            "value": e.value, "snippet": e.snippet})
    return {
        "id": f.id, "finding_id": f.finding_id, "object_id": object_code, "source": f.source, "code": f.param_code,
        "rule_version": f.rule_version, "section": param.section if param else ("АР" if f.source != "matrix" else ""),
        "parameter_name": param.parameter_name if param else _free_name(f.param_code),
        "location": f.location, "expected_value": f.expected_value,
        "actual_values": {k: v for k, v in (("RD", f.actual_rd), ("ID", f.actual_id)) if v is not None},
        "delta": f.delta, "status": eff, "machine_status": f.status, "finding_status": cs.finding_status,
        "completeness_status": cs.completeness_status, "review_priority": f.review_priority,
        "criticality": f.criticality, "rationale": f.rationale, "verification": f.verification,
        # уверенность гипотезы свободного поиска 0..1 (pipeline/freesearch.py); у находок Матрицы — None
        "confidence": f.verification.get("confidence") if isinstance(f.verification, dict) else None,
        "discovery_method": f.discovery_method, "approved_change_ref": f.approved_change_ref,
        "sources": sources,
        "inspector": {"status": f.inspector_status, "user": (users or {}).get(f.decided_by) if f.decided_by else None,
                      "decided_at": _iso(f.decided_at), "reason_code": f.reason_code, "comment": f.comment},
        "submission": {"violation_label": f.submission_label, "protocol_status": f.submission_status,
                       "include": f.submission_include},
        "created_in_version": f.created_in_version,
    }


FREE_NAMES = {
    "FREE-AR-EXPLICATION-TOTAL": "Итог площадей этажа по экспликации",
    "FREE-AR-EXPLICATION-AREA": "Площадь помещения по экспликации",
    "FREE-AR-EXPLICATION-FUNCTION": "Назначение (наименование) помещения",
    "FREE-AR-EXPLICATION-ROOMSET": "Состав помещений этажа",
    "FREE-AR-APARTMENT-AREA": "Площади квартиры (жилая / с летними)",
}


def _free_name(code: str) -> str:
    return FREE_NAMES.get(code, code)


def build_protocol(session: Session, process: Process, version: int | None = None) -> dict:
    """Живой протокол по текущему состоянию БД (включая решения инспектора)."""
    from inspector.models import User

    files = list(session.scalars(select(File).where(File.process_id == process.id).order_by(File.id)))
    file_by_pk = {f.id: f for f in files}
    ok_pks = [f.id for f in files if f.status == "ok"]
    pages_by_file: dict[int, tuple[int, int]] = {}
    if ok_pks:
        for fpk, n, n_ocr in session.execute(
                select(Page.file_pk, Page.page_no, Page.text_source).where(Page.file_pk.in_(ok_pks))).all():
            a, b = pages_by_file.get(fpk, (0, 0))
            pages_by_file[fpk] = (a + 1, b + (n_ocr in ("ocr", "rapidocr")))
    docs = _page_docs(session, ok_pks)
    params = {p.code: p for p in session.scalars(select(Param).order_by(Param.id))}
    users = {u.id: u.login for u in session.scalars(select(User))}
    findings = list(session.scalars(select(Finding).where(Finding.process_id == process.id, Finding.active.is_(True))
                                    .order_by(Finding.id)))
    cards = [finding_card(f, file_by_pk, docs, params, process.object.object_code, users) for f in findings]

    by_param: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by_param[c["code"]].append(c)
    param_rows = []
    for code, p in params.items():
        if not p.is_active:
            continue
        mine = [c for c in by_param.get(code, []) if c["status"] != S.SUSPICION]
        status = next((s for s in PARAM_ORDER if any(c["status"] == s for c in mine)), S.MISSING_EVIDENCE)
        comment = next((c["rationale"] for c in mine if c["status"] == status and not c["location"]), "") if mine else \
            "Параметр не обработан"
        param_rows.append({"code": code, "section": p.section, "name": p.parameter_name, "status": status,
                           "comment": comment, "finding_ids": [c["finding_id"] for c in mine if c["location"]]})
    counts = Counter(r["status"] for r in param_rows)
    summary = {"params_total": len(param_rows),
               "compared": sum(counts[s] for s in (S.NEGATIVE_VERIFIED, S.CANDIDATE, S.CONFIRMED_VIOLATION)),
               **{s: counts.get(s, 0) for s in PARAM_ORDER},
               "SUSPICION": sum(c["status"] == S.SUSPICION for c in cards)}
    findings_summary = dict(Counter(c["status"] for c in cards if c["location"] or c["source"] != "matrix"))
    stages_present = {f.doc_stage for f in files if f.status == "ok" and f.doc_stage}
    low_quality = [{"file_id": file_by_pk[fpk].file_id, "page": pn, "reason": q or "плохое качество"}
                   for fpk, pn, q in session.execute(
                       select(Page.file_pk, Page.page_no, Page.quality_label)
                       .where(Page.file_pk.in_(ok_pks), Page.low_quality.is_(True))).all()] if ok_pks else []
    latest = session.scalars(select(DatasetVersion).order_by(DatasetVersion.id.desc())).first()
    return {
        "protocol_id": f"PRT-{process.id[:8]}-v{version or process.protocol_version or 0}",
        "process_id": process.id, "version": version or process.protocol_version, "process_status": process.status,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "object": {"object_id": process.object.object_code, "name": process.object.name,
                   "address": process.object.address, "customer": process.object.customer,
                   "contractor": process.object.contractor, "permit_number": process.object.permit_number},
        "traceability": {"process_id": process.id, "matrix_version": MATRIX_VERSION,
                         "dataset_version": latest.version if latest else "none",
                         "model_version": (process.options or {}).get("model_version") or model_version(session),
                         "run_timestamp": _iso(process.updated_at), "input_manifest_sha256": input_manifest_hash(files)},
        "scenario": process.scenario or S.scenario(stages_present),
        "upload_status": upload_statuses(files, pages_by_file),
        "summary": summary, "findings_summary": findings_summary,
        "params": param_rows, "findings": cards,
        "files": [{"file_pk": f.id, "file_id": f.file_id, "name": f.original_name, "rel_path": f.rel_path,
                   "stage": f.doc_stage, "stage_source": f.stage_source, "sha256": f.sha256,
                   "pages": pages_by_file.get(f.id, (0, 0))[0], "ocr_pages": pages_by_file.get(f.id, (0, 0))[1],
                   "status": f.status, "error": f.error, "document_code": f.document_code, "revision": f.revision,
                   "approval_status": f.approval_status, "discipline": f.discipline} for f in files],
        "low_quality_pages": low_quality,
    }


SUBMISSION_FIELDS = ("parameter_code", "location", "pd_value", "rd_value", "id_value", "violation_label",
                     "protocol_status", "criticality", "evidence")


def submission_label(card: dict, apply_decisions: bool = True) -> tuple[str | None, str | None]:
    """Метка сабмита: машинная, либо по решению инспектора, если оно есть."""
    label, status = card["submission"]["violation_label"], card["submission"]["protocol_status"]
    if not apply_decisions:
        return label, status
    decision = card["inspector"]["status"]
    if decision == S.CONFIRMED_VIOLATION:
        return "VIOLATION_PRESENT", S.violation_protocol_status(card.get("criticality"))
    if decision == S.NEGATIVE_VERIFIED:
        return "NO_VIOLATION", "OK"
    if decision == S.CLARIFICATION_REQUIRED:
        return "COMPARISON_IMPOSSIBLE", "COMPARISON_IMPOSSIBLE"
    return label, status


def build_submission(protocol: dict, apply_decisions: bool = True) -> dict:
    checks = []
    for c in protocol["findings"]:
        if not c["submission"]["include"]:
            continue
        label, status = submission_label(c, apply_decisions)
        if label is None:
            continue
        evidence = [{"stage": s["stage"], "file_id": s["file_id"], "pdf_page_number": int(s["page"])}
                    for s in c["sources"] if s.get("file_id") and s.get("page")]
        checks.append({"parameter_code": c["code"], "location": c["location"], "pd_value": c["expected_value"],
                       "rd_value": c["actual_values"].get("RD"), "id_value": c["actual_values"].get("ID"),
                       "violation_label": label, "protocol_status": status,
                       "criticality": c["criticality"] if label == "VIOLATION_PRESENT" else None,
                       "evidence": evidence})
    return {"object_id": protocol["object"]["object_id"], "checks": checks}


@lru_cache
def submission_schema() -> dict:
    return json.loads((RESOURCES / "submission_schema.json").read_text(encoding="utf-8"))


def validate_submission(sub: dict) -> list[str]:
    import jsonschema

    v = jsonschema.Draft202012Validator(submission_schema())
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(sub)]


def save_snapshot(session: Session, process: Process, kind: str, user_pk: int | None = None) -> ProtocolVersion:
    """Новая версия протокола: JSON на диск + строка protocols. Прежние версии не трогаются."""
    settings = get_settings()
    process.protocol_version = (process.protocol_version or 0) + 1
    proto = build_protocol(session, process, version=process.protocol_version)
    out_dir = settings.data_dir / "processes" / process.id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"protocol.v{process.protocol_version}.json"
    path.write_text(json.dumps(proto, ensure_ascii=False, indent=1), encoding="utf-8")
    pv = ProtocolVersion(process_id=process.id, version=process.protocol_version, kind=kind, status=process.status,
                         matrix_version=MATRIX_VERSION, dataset_version=proto["traceability"]["dataset_version"],
                         model_version=proto["traceability"]["model_version"],
                         input_manifest_hash=proto["traceability"]["input_manifest_sha256"],
                         summary=proto["summary"], json_path=str(path), created_by=user_pk,
                         finalized_at=dt.datetime.now(dt.timezone.utc) if kind == "final" else None)
    session.add(pv)
    session.flush()
    return pv
