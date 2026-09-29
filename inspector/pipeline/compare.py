"""Этап 8 — сопоставление ПД↔РД↔ИД.

Судья: один вызов Qwen3.8 на параметр — группы по локациям и вердикт по триггеру матрицы; ссылается на значения по
индексам, код проверяет индексы и стадии (выдумать страницу или значение нельзя); reason идёт ПЕРЕД verdict.
Верификатор: для каждого VIOLATION перечитывает ±700 символов вокруг цитат; «не та же сущность»/«не уверен» ->
NOT_COMPARABLE. Истина = ПД. Итог -> статусы ТЗ через inspector.statuses.machine_verdict."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.config import Settings
from inspector.llm import ChatClient, LLMError
from inspector.models import Decision, Document, EvidenceFragment, Extraction, File, Finding, Page, Param, Process
from inspector.pipeline.textutils import norm_location, number_of

RULE_VERSION = "matrix-132-r1.1"
MIN_CONFIDENCE = 0.5
MAX_ITEMS_PER_STAGE = 40
JUDGE_SYSTEM = (
    "Ты инспектор строительного надзора. Даны значения одного параметра, извлечённые из проектной (ПД), рабочей (РД) "
    "и исполнительной (ИД) документации одного объекта; у каждого значения есть индекс. Истина — ПД (прошла "
    "госэкспертизу). Задача: сгруппировать значения по локациям (одна и та же конструкция/помещение/корпус может "
    "называться немного по-разному) и по каждой локации решить, есть ли нарушение по ТРИГГЕРУ параметра.\n"
    "Правила:\n"
    "1. Сравнивай только значения об одном и том же. Разные корпуса, этажи, элементы — разные локации.\n"
    "2. Нарушение — только если РД или ИД отклоняется от ПД в сторону, описанную триггером. Улучшение, округление, "
    "иная форма записи того же значения, а также дополнительное значение рядом с проектным (например, А240 на хомуты "
    "при рабочей А500) — не нарушение.\n"
    "3. Если значения на стадии противоречат друг другу или сопоставить локации нельзя — verdict=UNCLEAR.\n"
    "4. В pd_index/rd_index/id_index указывай индекс значения, на которое опираешься (или null, если на стадии нет "
    "значения по этой локации). location — краткое каноническое название, как в ПД.\n"
    "5. Не создавай группы без значения ПД. Если по локации несколько разных значений ПД из разных редакций (метка "
    "«ред.год/К№/И№» у стадии: больше год, корректировка К и изменение И — свежее), опирайся на самую свежую ПД.\n"
    "6. В reason сначала сравни значения (для числового порога посчитай отклонение в процентах или единицах и сравни "
    "с порогом триггера), затем выведи verdict строго из этого сравнения. Не уверен, что значения об одном и том же — "
    "UNCLEAR, а не VIOLATION. Отвечай только JSON."
)
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["groups"], "properties": {"groups": {
    "type": "array", "maxItems": 30, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["location", "pd_index", "rd_index", "id_index", "reason", "verdict"],
        "properties": {"location": {"type": "string", "maxLength": 80},
                       "pd_index": {"type": ["integer", "null"]}, "rd_index": {"type": ["integer", "null"]},
                       "id_index": {"type": ["integer", "null"]},
                       "reason": {"type": "string", "maxLength": 400},
                       "verdict": {"enum": ["VIOLATION", "NO_VIOLATION", "UNCLEAR"]}}}}}}
VERIFY_CONTEXT = 700
VERIFY_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["pd_entity", "other_entity", "analysis", "same_entity", "trigger_met"],
                 "properties": {"pd_entity": {"type": "string", "maxLength": 200},
                                "other_entity": {"type": "string", "maxLength": 200},
                                "analysis": {"type": "string", "maxLength": 500},
                                "same_entity": {"enum": ["YES", "NO", "UNSURE"]},
                                "trigger_met": {"enum": ["YES", "NO", "UNSURE"]}}}
VERIFY_SYSTEM = (
    "Ты перепроверяешь заявленное нарушение при сверке проектной (ПД) и рабочей/исполнительной (РД/ИД) документации. "
    "Даны фрагменты страниц вокруг каждого значения. Сначала опиши, к какой именно сущности относится значение в ПД "
    "(конструкция, помещение, система, корпус, слой, этаж) и к какой — значение в РД/ИД. same_entity=YES только если "
    "это один и тот же элемент объекта; разные элементы, разные корпуса/этажи, норматив вместо проектного значения, "
    "общее требование против частного случая — NO. trigger_met=YES только если при этом отклонение соответствует "
    "триггеру. Сомневаешься — UNSURE. Отвечай только JSON."
)


def edition_tag(item: dict) -> str:
    rev = item.get("revision")
    return f" ред.И{rev}" if rev and str(rev).isdigit() and int(rev) > 0 else ""


def prepare_items(items: list[dict]) -> list[dict]:
    """Фильтр (цитата на странице, уверенность), дедуп одинаковых (стадия, локация, значение), лимит на стадию."""
    seen, by_stage = set(), defaultdict(list)
    for it in sorted(items, key=lambda x: -x.get("confidence", 0)):
        if not it.get("supported") or it.get("confidence", 0) < MIN_CONFIDENCE or it.get("stage") not in ("PD", "RD", "ID"):
            continue
        key = (it["stage"], it["location"].casefold().strip(), it["value_raw"].casefold().replace(" ", ""))
        if key in seen:
            continue
        seen.add(key)
        if len(by_stage[it["stage"]]) < MAX_ITEMS_PER_STAGE:
            by_stage[it["stage"]].append(it)
    return by_stage["PD"] + by_stage["RD"] + by_stage["ID"]


def validate_groups(items: list[dict], groups: list[dict]) -> list[dict]:
    """Ответ судьи -> группы с проверенными ссылками: индекс существует и принадлежит своей стадии; без ПД — вон."""
    out, used = [], set()
    for g in groups:
        def pick(index, stage):
            if index is None or not isinstance(index, int) or not 0 <= index < len(items):
                return None
            return items[index] if items[index]["stage"] == stage else None
        pd, rd, idd = pick(g.get("pd_index"), "PD"), pick(g.get("rd_index"), "RD"), pick(g.get("id_index"), "ID")
        location = (g.get("location") or "").strip()
        if pd is None or not location or norm_location(location) in used:
            continue
        used.add(norm_location(location))
        out.append({"location": location, "pd": pd, "rd": rd, "id": idd, "reason": g.get("reason", ""),
                    "verdict": g.get("verdict")})
    return out


def context_around(text: str, snippet: str, value: str, width: int = VERIFY_CONTEXT) -> str:
    def find(needle: str) -> int:
        probe = "".join(needle.split()).casefold()[:40]
        if len(probe) < 3:
            return -1
        squashed, index = [], []
        for i, ch in enumerate(text):
            if not ch.isspace():
                squashed.append(ch.casefold())
                index.append(i)
        pos = "".join(squashed).find(probe)
        return index[pos] if pos >= 0 else -1
    at = find(snippet)
    if at < 0:
        at = find(value)
    if at < 0:
        return text[: 2 * width]
    return text[max(0, at - width): at + width]


def delta_text(pd: str | None, other: str | None) -> str | None:
    if pd is None or other is None:
        return None
    a, b = number_of(pd), number_of(other)
    if a is not None and b is not None and a != 0 and pd.strip() != other.strip():
        diff = b - a
        return f"{pd} → {other} (Δ {diff:+g}; {diff / abs(a) * 100:+.1f} %)"
    return f"{pd} → {other}"


def finding_id(code: str, location: str) -> str:
    return f"FND-{code}-{hashlib.sha1(norm_location(location).encode()).hexdigest()[:8]}"


async def judge_and_verify(client: ChatClient, jobs: list[dict], texts: dict[tuple[int, int], str]) -> None:
    """jobs: [{param, items}] -> в каждый job добавляется groups (с verification для VIOLATION)."""
    async def judge(job):
        param, items = job["param"], job["items"]
        listing = "\n".join(f"[{i}] {it['stage']}{edition_tag(it)} | {it['location']} | {it['value_raw']} | "
                            f"«{it['snippet'][:110]}»" for i, it in enumerate(items))
        user = (f"Параметр {param['code']}: {param['parameter_name']} (ед.: {param.get('unit')}).\n"
                f"ТРИГГЕР нарушения: {param.get('trigger_logic')}\n\nЗНАЧЕНИЯ:\n{listing}")
        try:
            res = await client.chat([{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
                                    max_tokens=4000, json_schema={"name": "judge", "schema": JUDGE_SCHEMA})
            job["groups"] = validate_groups(items, res.json().get("groups", []))
        except (LLMError, ValueError) as exc:
            job["groups"], job["error"] = [], f"судья недоступен: {exc}"[:300]
            return
        await asyncio.gather(*[verify(param, g) for g in job["groups"] if g["verdict"] == "VIOLATION"
                               and (g["rd"] is not None or g["id"] is not None)])

    async def verify(param, g):
        parts = []
        for it in [g["pd"]] + [x for x in (g["rd"], g["id"]) if x is not None]:
            text = texts.get((it["file_pk"], it["page_no"]), "")
            parts.append(f"--- {it['stage']} ({it['file_id']} с.{it['page_no']}), значение «{it['value_raw']}»:\n"
                         f"{context_around(text, it['snippet'], it['value_raw'])}")
        user = (f"Параметр {param['code']}: {param['parameter_name']}. ТРИГГЕР: {param.get('trigger_logic')}\n"
                f"Заявлено: локация «{g['location']}», ПД «{g['pd']['value_raw']}» → РД "
                f"«{g['rd']['value_raw'] if g['rd'] else None}» / ИД «{g['id']['value_raw'] if g['id'] else None}».\n\n"
                + "\n\n".join(parts))
        try:
            res = await client.chat([{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}],
                                    max_tokens=1200, json_schema={"name": "verify", "schema": VERIFY_SCHEMA})
            g["verification"] = res.json()
        except (LLMError, ValueError):
            g["verification"] = {"same_entity": "UNSURE", "trigger_met": "UNSURE", "analysis": "верификатор недоступен"}

    async with client:
        await asyncio.gather(*[judge(j) for j in jobs])


def _param_dict(p: Param) -> dict:
    return {"code": p.code, "parameter_name": p.parameter_name, "unit": p.unit, "trigger_logic": p.trigger_logic,
            "criticality": p.criticality, "section": p.section, "source_pd": p.source_pd, "source_rd": p.source_rd,
            "source_id": p.source_id}


def run_compare(session: Session, process: Process, settings: Settings, client: ChatClient,
                affected: set[str] | None, version: int, progress=None) -> dict:
    """affected — коды параметров для пересчёта (None — все). Старые находки этих параметров становятся неактивными,
    решения инспектора по совпавшим (код, локация) переносятся."""
    started = time.time()
    params = {p.code: p for p in session.scalars(select(Param).where(Param.is_active.is_(True)))}
    codes = set(params) if affected is None else (affected & set(params))
    files = {f.id: f for f in session.scalars(select(File).where(File.process_id == process.id))}
    ok_files = [f for f in files.values() if f.status == "ok" and f.doc_stage]
    stages_present = {f.doc_stage for f in ok_files}
    docs = {d.id: d for d in session.scalars(select(Document).where(Document.process_id == process.id))}
    ok_pks = [f.id for f in ok_files] or [-1]
    page_doc = {(a, b): c for a, b, c in session.execute(
        select(Page.file_pk, Page.page_no, Page.document_pk).where(Page.file_pk.in_(ok_pks))).all()}
    sections_present = {s for (s,) in session.execute(select(Page.section).where(
        Page.file_pk.in_(ok_pks)).distinct()).all() if s}
    extractions = list(session.scalars(select(Extraction).where(Extraction.process_id == process.id,
                                                                Extraction.param_code.in_(codes or {"-"}))))
    by_param: dict[str, list[dict]] = defaultdict(list)
    for e in extractions:
        doc = docs.get(page_doc.get((e.file_pk, e.page_no)))
        if doc is not None and not doc.is_actual:
            continue                      # устаревшая редакция не может быть эталоном (ТЗ 9.1)
        f = files.get(e.file_pk)
        if f is None or f.status != "ok":
            continue
        by_param[e.param_code].append({
            "extraction_pk": e.id, "stage": e.stage, "file_pk": e.file_pk, "file_id": f.file_id, "page_no": e.page_no,
            "location": e.location, "value_raw": e.value_raw, "snippet": e.snippet, "confidence": e.confidence,
            "supported": e.supported, "bbox": e.bbox, "bbox_source": e.bbox_source,
            "revision": doc.revision if doc else None, "clarification": bool(doc and doc.clarification),
            "clarification_reason": doc.clarification_reason if doc else None})
    jobs, param_level = [], []
    for code in sorted(codes):
        items = prepare_items(by_param.get(code, []))
        if any(it["stage"] == "PD" for it in items):
            jobs.append({"param": _param_dict(params[code]), "items": items})
        else:
            param_level.append((code, by_param.get(code, [])))
    need_texts = {(it["file_pk"], it["page_no"]) for j in jobs for it in j["items"]}
    texts = {(p.file_pk, p.page_no): p.text for p in session.scalars(
        select(Page).where(Page.file_pk.in_({k[0] for k in need_texts} or {-1})))
        if (p.file_pk, p.page_no) in need_texts}
    if progress:
        progress(0, len(jobs))
    if jobs:
        asyncio.run(judge_and_verify(client, jobs, texts))
        failed = sum(bool(j.get("error")) for j in jobs)
        if failed == len(jobs):
            # модель недоступна: прежние находки не трогаем, этап падает (задачу перезапустит worker)
            raise RuntimeError(f"модель недоступна: этап compare, судья не ответил ни по одному из {failed} параметров")
    if progress:
        progress(len(jobs), len(jobs))

    # старые находки пересчитываемых параметров -> неактивны (история остаётся)
    old = {f.finding_id: f for f in session.scalars(select(Finding).where(
        Finding.process_id == process.id, Finding.active.is_(True), Finding.source == "matrix",
        Finding.param_code.in_(codes or {"-"})))}
    session.execute(update(Finding).where(Finding.id.in_([f.id for f in old.values()] or [-1])).values(active=False))
    stats = {"params": len(codes), "judged": len(jobs), "groups": 0, "claimed": 0, "candidates": 0, "negatives": 0,
             "not_comparable": 0, "missing": 0, "not_applicable": 0, "clarification": 0, "judge_errors": 0}
    object_has_rd, object_has_id = "RD" in stages_present, "ID" in stages_present
    for job in jobs:
        p = params[job["param"]["code"]]
        if job.get("error"):
            stats["judge_errors"] += 1
        if not job["groups"]:
            _param_row(session, process, p, S.MISSING_EVIDENCE, job.get("error") or
                       "Значения ПД найдены, но сопоставить их с РД/ИД не удалось", version, old)
            stats["missing"] += 1
            continue
        for g in job["groups"]:
            stats["groups"] += 1
            verified = None
            if g["verdict"] == "VIOLATION":
                stats["claimed"] += 1
                v = g.get("verification") or {}
                verified = v.get("same_entity") == "YES" and v.get("trigger_met") == "YES"
            evid = [x for x in (g["pd"], g["rd"], g["id"]) if x is not None]
            clar = [x for x in evid if x.get("clarification")]
            mv = S.machine_verdict(judge_verdict=g["verdict"], verified=verified, has_pd=True,
                                   has_rd_value=g["rd"] is not None, has_id_value=g["id"] is not None,
                                   object_has_rd=object_has_rd, object_has_id=object_has_id,
                                   clarification=bool(clar), criticality=p.criticality)
            rationale = g["reason"]
            if clar:
                rationale = clar[0]["clarification_reason"] + ". " + rationale
            elif verified is False:
                rationale += " | Верификатор: " + (g.get("verification") or {}).get("analysis", "не подтвердил")
            fid = finding_id(p.code, g["location"])
            f = Finding(process_id=process.id, finding_id=fid, source="matrix", param_code=p.code,
                        rule_version=RULE_VERSION, location=g["location"], expected_value=g["pd"]["value_raw"],
                        actual_rd=g["rd"]["value_raw"] if g["rd"] else None,
                        actual_id=g["id"]["value_raw"] if g["id"] else None,
                        delta=delta_text(g["pd"]["value_raw"], (g["rd"] or g["id"] or {}).get("value_raw")),
                        status=mv.status, review_priority=S.review_priority(p.criticality), criticality=p.criticality,
                        rationale=rationale, verification=g.get("verification"),
                        submission_label=mv.submission_label, submission_status=mv.submission_status,
                        submission_include=mv.submission_include, created_in_version=version)
            session.add(f)
            session.flush()
            for role, it in (("expected", g["pd"]), ("actual_rd", g["rd"]), ("actual_id", g["id"])):
                if it is not None:
                    session.add(EvidenceFragment(finding_pk=f.id, role=role, stage=it["stage"], file_pk=it["file_pk"],
                                                 page_no=it["page_no"], bbox=it["bbox"], bbox_source=it["bbox_source"],
                                                 value=it["value_raw"], snippet=it["snippet"]))
            _carry_decision(session, f, old.get(fid))
            key = {S.CANDIDATE: "candidates", S.NEGATIVE_VERIFIED: "negatives", S.NOT_COMPARABLE: "not_comparable",
                   S.MISSING_EVIDENCE: "missing", S.CLARIFICATION_REQUIRED: "clarification"}[mv.status]
            stats[key] += 1
    for code, raw in param_level:
        p = params[code]
        if "PD" not in stages_present:
            status, why = S.MISSING_EVIDENCE, "ПД не загружена — эталона для сравнения нет"
        elif p.section in S.OPTIONAL_SECTIONS and p.section not in sections_present:
            status, why = S.NOT_APPLICABLE, f"Раздел {p.section} в документации объекта не найден — параметр неприменим"
        else:
            other = sorted({x["stage"] for x in raw if x.get("supported")} - {"PD"})
            why = "Значение ПД не найдено в документах объекта" + (
                f" (есть значения в {', '.join(other)})" if other else "")
            status = S.MISSING_EVIDENCE
        _param_row(session, process, p, status, why, version, old)
        stats["missing" if status == S.MISSING_EVIDENCE else "not_applicable"] += 1
    session.flush()
    stats["seconds"] = round(time.time() - started, 1)
    return stats


def _param_row(session: Session, process: Process, p: Param, status: str, why: str, version: int, old: dict) -> None:
    """Строка протокола по параметру без сопоставленных групп (комплектность): локация пустая."""
    session.add(Finding(process_id=process.id, finding_id=f"FND-{p.code}-param", source="matrix", param_code=p.code,
                        rule_version=RULE_VERSION, location="", status=status,
                        review_priority=S.review_priority(p.criticality), criticality=p.criticality, rationale=why,
                        submission_include=False, created_in_version=version))


def _carry_decision(session: Session, new: Finding, prev: Finding | None) -> None:
    """Дозагрузка: решение инспектора по той же доказательной группе (код + локация) переносится, если машинный
    статус не изменился; история — строкой decisions с пометкой."""
    if prev is None or prev.inspector_status == S.INSPECTOR_PENDING or prev.status != new.status:
        return
    new.inspector_status, new.decided_by, new.decided_at = prev.inspector_status, prev.decided_by, prev.decided_at
    new.reason_code, new.comment = prev.reason_code, prev.comment
    session.add(Decision(finding_pk=new.id, user_pk=prev.decided_by, decision=prev.inspector_status,
                         previous_status=S.INSPECTOR_PENDING, reason_code=prev.reason_code,
                         comment=f"Перенесено из прошлой версии протокола: {prev.comment or ''}".strip()))
