"""XML протокола сверки: xml.etree.ElementTree, UTF-8 с объявлением XML. Только построение документа (разбора
внешнего XML здесь нет).

Структура повторяет словарь протокола (раздел 4 спеки): корень <protocol id version process_id status>, ветви
object, traceability, upload_status/stage, summary (атрибуты), findings_summary, params/param,
findings/finding (+ location, expected_value, actual/rd|id, delta, rationale, inspector, sources/source), files/file,
low_quality_pages/page. Пустое значение — пустой элемент или отсутствующий необязательный атрибут; управляющие
символы, недопустимые в XML 1.0, удаляются."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from inspector.render.common import as_dict, as_list, clean

DECLARATION = b'<?xml version="1.0" encoding="UTF-8"?>\n'
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _num(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _num(value)
    return clean(value)


def _bbox(value) -> str | None:
    """[x0, y0, x1, y1] → «x0,y0,x1,y1»; многоугольник [(x, y), …] → «x,y x,y …» (доли страницы [0;1])."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        if all(isinstance(pt, (list, tuple)) for pt in value):
            return " ".join(",".join(_num(float(v)) for v in pt) for pt in value)
        return ",".join(_num(float(v)) for v in value)
    except (TypeError, ValueError):
        return None


def _set(el: ET.Element, attrs: dict, required: tuple[str, ...] = ()) -> ET.Element:
    """Атрибуты по порядку; None пропускается, кроме обязательных (тогда пустая строка)."""
    for key, value in attrs.items():
        if value is None and key not in required:
            continue
        el.set(key, "" if value is None else _text(value))
    return el


def _sub(parent: ET.Element, tag: str, value=None, attrs: dict | None = None,
         required: tuple[str, ...] = ()) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if attrs:
        _set(el, attrs, required)
    if value is not None and _text(value) != "":
        el.text = _text(value)
    return el


def _finding(parent: ET.Element, f: dict) -> None:
    el = _sub(parent, "finding", attrs={
        "id": f.get("id"), "finding_id": f.get("finding_id"), "code": f.get("code"), "status": f.get("status"),
        "review_priority": f.get("review_priority"), "machine_status": f.get("machine_status"),
        "finding_status": f.get("finding_status"), "completeness_status": f.get("completeness_status"),
        "source": f.get("source"), "rule_version": f.get("rule_version"), "section": f.get("section"),
        "object_id": f.get("object_id"), "discovery_method": f.get("discovery_method"),
        "confidence": f.get("confidence"), "created_in_version": f.get("created_in_version")},
        required=("id", "finding_id", "code", "status", "review_priority"))
    _sub(el, "parameter_name", f.get("parameter_name"))
    _sub(el, "location", f.get("location"))
    _sub(el, "expected_value", f.get("expected_value"))
    actual = _sub(el, "actual")
    values = as_dict(f.get("actual_values"))
    for stage, tag in (("RD", "rd"), ("ID", "id")):
        if values.get(stage) is not None:
            _sub(actual, tag, values.get(stage))
    _sub(el, "delta", f.get("delta"))
    _sub(el, "criticality", f.get("criticality"))
    _sub(el, "rationale", f.get("rationale"))
    verification = as_dict(f.get("verification"))
    if verification:
        _sub(el, "verification", verification.get("analysis"),
             {"same_entity": verification.get("same_entity"), "trigger_met": verification.get("trigger_met")})
    _sub(el, "approved_change_ref", f.get("approved_change_ref"))
    insp = as_dict(f.get("inspector"))
    inspector = _sub(el, "inspector", attrs={"status": insp.get("status") or "PENDING", "user": insp.get("user"),
                                             "decided_at": insp.get("decided_at"),
                                             "reason_code": insp.get("reason_code")}, required=("status",))
    _sub(inspector, "comment", insp.get("comment"))
    sub = as_dict(f.get("submission"))
    if sub:
        _sub(el, "submission", attrs={"violation_label": sub.get("violation_label"),
                                      "protocol_status": sub.get("protocol_status"), "include": sub.get("include")})
    sources = _sub(el, "sources")
    for s in (x for x in as_list(f.get("sources")) if isinstance(x, dict)):
        src = _sub(sources, "source", attrs={
            "role": s.get("role"), "stage": s.get("stage"), "file_id": s.get("file_id"), "sha256": s.get("sha256"),
            "page": s.get("page"), "bbox": _bbox(s.get("bbox")), "bbox_source": s.get("bbox_source"),
            "sheet": s.get("sheet"), "document_code": s.get("document_code"), "revision": s.get("revision"),
            "approval_status": s.get("approval_status"), "file_pk": s.get("file_pk"),
            "file_name": s.get("file_name")}, required=("stage", "file_id", "sha256", "page", "bbox"))
        _sub(src, "value", s.get("value"))
        _sub(src, "snippet", s.get("snippet"))


def build_tree(protocol: dict) -> ET.Element:
    p = as_dict(protocol)
    root = _set(ET.Element("protocol"), {
        "id": p.get("protocol_id"), "version": p.get("version"), "process_id": p.get("process_id"),
        "status": p.get("process_status"), "generated_at": p.get("generated_at"), "scenario": p.get("scenario")},
        required=("id", "version", "process_id", "status"))
    obj = as_dict(p.get("object"))
    object_el = _sub(root, "object", attrs={"object_id": obj.get("object_id")}, required=("object_id",))
    for key in ("name", "address", "customer", "contractor", "permit_number"):
        _sub(object_el, key, obj.get(key))
    trace = as_dict(p.get("traceability"))
    _sub(root, "traceability", attrs={k: trace.get(k) for k in (
        "process_id", "matrix_version", "dataset_version", "model_version", "run_timestamp",
        "input_manifest_sha256")})
    upload = _sub(root, "upload_status", attrs={"scenario": p.get("scenario")})
    for u in (x for x in as_list(p.get("upload_status")) if isinstance(x, dict)):
        stage = _sub(upload, "stage", attrs={"code": u.get("stage"), "status": u.get("status"), "files": u.get("files"),
                                             "problem_files": u.get("problem_files"), "pages": u.get("pages"),
                                             "ocr_pages": u.get("ocr_pages")}, required=("code", "status"))
        _sub(stage, "comment", u.get("comment"))
    for tag in ("summary", "findings_summary"):
        data = as_dict(p.get(tag))
        _sub(root, tag, attrs={k: v for k, v in data.items() if isinstance(k, str) and _NAME.match(k)})
    params = as_list(p.get("params"))
    params_el = _sub(root, "params", attrs={"count": len(params)})
    for prm in (x for x in params if isinstance(x, dict)):
        el = _sub(params_el, "param", attrs={"code": prm.get("code"), "section": prm.get("section"),
                                             "status": prm.get("status")}, required=("code", "status"))
        _sub(el, "name", prm.get("name"))
        _sub(el, "comment", prm.get("comment"))
        ids = _sub(el, "finding_ids")
        for fid in as_list(prm.get("finding_ids")):
            _sub(ids, "finding_id", fid)
    findings = as_list(p.get("findings"))
    findings_el = _sub(root, "findings", attrs={"count": len(findings)})
    for f in (x for x in findings if isinstance(x, dict)):
        _finding(findings_el, f)
    files = as_list(p.get("files"))
    files_el = _sub(root, "files", attrs={"count": len(files)})
    for f in (x for x in files if isinstance(x, dict)):
        el = _sub(files_el, "file", attrs={
            "file_pk": f.get("file_pk"), "file_id": f.get("file_id"), "stage": f.get("stage"),
            "stage_source": f.get("stage_source"), "sha256": f.get("sha256"), "pages": f.get("pages"),
            "ocr_pages": f.get("ocr_pages"), "status": f.get("status"), "document_code": f.get("document_code"),
            "revision": f.get("revision"), "approval_status": f.get("approval_status"),
            "discipline": f.get("discipline")}, required=("file_id", "sha256", "status"))
        _sub(el, "name", f.get("name"))
        _sub(el, "rel_path", f.get("rel_path"))
        _sub(el, "error", f.get("error"))
    low = _sub(root, "low_quality_pages")
    for item in (x for x in as_list(p.get("low_quality_pages")) if isinstance(x, dict)):
        _sub(low, "page", item.get("reason"), {"file_id": item.get("file_id"), "page": item.get("page")})
    return root


def render_xml(protocol: dict) -> bytes:
    """Протокол (словарь build_protocol) → XML (UTF-8)."""
    root = build_tree(protocol)
    ET.indent(root, space=" ")
    return DECLARATION + ET.tostring(root, encoding="unicode").encode("utf-8") + b"\n"
