"""Этап 9 — свободный поиск гипотез (SUSPICION, модуль 5 ТЗ), без моделей:
сверка экспликаций помещений и квартирографии ПД↔РД на листах раздела АР. Раздел берётся из
класса страницы (содержимое), а не из имени файла. Находки — SUSPICION: не нарушения, не GOLD до решения инспектора.
В сабмит организаторов идут только надёжные пары (сумма площадей таблицы сходится с итогом) — публичный gold
организаторов содержит находки вне матрицы (FREE-…) как VIOLATION_PRESENT. Уверенность гипотезы (0..1) и из чего
она сложилась — в Finding.verification {confidence, basis}: порядок рассмотрения для инспектора, не вероятность
нарушения."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.models import Decision, EvidenceFragment, File, Finding, Page, Process

TITLE = re.compile(r"(экспликаци|спецификаци|ведомост)\w*\s+помещени", re.I)
ROW = re.compile(
    r"^[ \t]*(?P<num>\d{1,3}(?:[.\-]\d{1,3}){0,3}[а-яА-Я]?|[А-Я])[ \t]*\n"
    r"(?P<name>[^\n\d][^\n]{1,90}(?:\n[^\n\d][^\n]{1,60})?)\n"
    r"[ \t]*(?P<area>\d{1,5}[.,]\d{1,2})(?:[ \t]*м[²2])?[ \t]*$", re.M)
TOTAL = re.compile(r"(?:общий\s+)?итог[оа]?[^\n\d]{0,40}\n?[ \t]*(\d{1,6}[.,]\d{1,2})", re.I)
QUAD = re.compile(r"(?<![\d,])(\d{1,3},\d{2})\n(\d{1,3},\d{2})\n(\d{1,3},\d{2})\n((?:Ст\.?\s?)?\d(?:\.\d){1,2})[ \t]*\n")
AREA_TOLERANCE = 0.05
NAME_STOP = {"помещение", "помещения", "пом"}
MAX_ROOMS_PER_PAIR = 40
CRITICALITY = "Существенное (предписание)"
RULE = "freesearch-{}-v1"
CODES = {"TOTAL_CHANGED": "FREE-AR-EXPLICATION-TOTAL", "AREA_CHANGED": "FREE-AR-EXPLICATION-AREA",
         "RENAMED": "FREE-AR-EXPLICATION-FUNCTION", "ADDED_IN_RD": "FREE-AR-EXPLICATION-ROOMSET",
         "MISSING_IN_RD": "FREE-AR-EXPLICATION-ROOMSET"}
METHOD = {"TOTAL_CHANGED": "LOGICAL_ANALYSIS", "AREA_CHANGED": "LOGICAL_ANALYSIS", "RENAMED": "SEMANTIC_DISSONANCE",
          "ADDED_IN_RD": "SEMANTIC_DISSONANCE", "MISSING_IN_RD": "SEMANTIC_DISSONANCE"}


def parse_tables(text: str) -> list[dict]:
    tables: list[dict] = []
    current: dict[str, dict] = {}
    for m in ROW.finditer(text):
        name = re.sub(r"\s+", " ", m.group("name")).strip(" .")
        if re.search(r"итог|^(номер|имя|площадь|наименование|кат\.?|категория)\b", name, re.I):
            continue
        if m.group("num") in current:
            tables.append({"rooms": current})
            current = {}
        current[m.group("num")] = {"name": name, "area": float(m.group("area").replace(",", "."))}
    if current:
        tables.append({"rooms": current})
    page_totals = [float(x.replace(",", ".")) for x in TOTAL.findall(text)]
    for table in tables:
        area_sum = sum(r["area"] for r in table["rooms"].values())
        close = [t for t in page_totals if area_sum and abs(t - area_sum) / area_sum < 0.02]
        table["total"] = min(close, key=lambda t: abs(t - area_sum)) if close else None
        table["parsed_ok"] = bool(close)
    return [t for t in tables if len(t["rooms"]) >= 3]


def name_key(name: str) -> str:
    words = [w for w in re.findall(r"[а-яёa-z]+", name.lower().replace("ё", "е")) if w not in NAME_STOP]
    return " ".join(w[:5] for w in words)


def compare_rooms(pd: dict[str, dict], rd: dict[str, dict]) -> list[dict]:
    findings = []
    for num in sorted(set(pd) | set(rd), key=lambda s: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", s)]):
        a, b = pd.get(num), rd.get(num)
        if a and b:
            if name_key(a["name"]) != name_key(b["name"]):
                findings.append({"room": num, "type": "RENAMED", "pd": a["name"], "rd": b["name"]})
            if abs(a["area"] - b["area"]) > AREA_TOLERANCE:
                findings.append({"room": num, "type": "AREA_CHANGED", "pd": a["area"], "rd": b["area"],
                                 "delta": round(b["area"] - a["area"], 2)})
        elif b and not a:
            findings.append({"room": num, "type": "ADDED_IN_RD", "pd": None, "rd": f"{b['name']} ({b['area']} м²)"})
        elif a and not b:
            findings.append({"room": num, "type": "MISSING_IN_RD", "pd": f"{a['name']} ({a['area']} м²)", "rd": None})
    return findings


def best_pairs(pd_tables: list[dict], rd_tables: list[dict]) -> list[tuple[dict, dict, float]]:
    pairs = []
    for rd in rd_tables:
        best, best_score = None, 0.0
        for pd in pd_tables:
            common = set(pd["rooms"]) & set(rd["rooms"])
            if len(common) < 3:
                continue
            same = sum(name_key(pd["rooms"][n]["name"]) == name_key(rd["rooms"][n]["name"]) for n in common)
            score = same / max(len(pd["rooms"]), len(rd["rooms"]))
            if score > best_score:
                best, best_score = pd, score
        if best is not None and best_score >= 0.4:
            pairs.append((best, rd, round(best_score, 2)))
    return pairs


def _ratio(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def explication_confidence(similarity: float, pd_ok: bool, rd_ok: bool) -> tuple[float, str]:
    """Уверенность гипотезы по экспликации: доля помещений пары листов, совпавших по номеру и названию (similarity из
    best_pairs, от 0,4), умноженная на надёжность разбора таблиц: обе суммы площадей сошлись с итогом — ×1, одна —
    ×0,75, ни одной — ×0,5. -> (0..1, пояснение)"""
    confidence = round(min(1.0, max(0.0, similarity * (0.5 + 0.25 * (bool(pd_ok) + bool(rd_ok))))), 2)
    yes = {True: "да", False: "нет"}
    return confidence, (f"доля совпавших помещений {_ratio(similarity)}; сумма площадей сошлась с итогом: "
                        f"ПД — {yes[bool(pd_ok)]}, РД — {yes[bool(rd_ok)]}")


def flat_confidence(pd_variants: int, rd_variants: int) -> tuple[float, str]:
    """Уверенность гипотезы по квартирографии: квартира сопоставлена по типу и общей площади. Один набор площадей
    с каждой стороны — 1; однотипных квартир с разными площадями несколько — пара неоднозначна: 1 / большее из чисел.
    -> (0..1, пояснение)"""
    confidence = round(1 / max(pd_variants, rd_variants, 1), 2)
    return confidence, (f"квартира сопоставлена по типу и общей площади; вариантов площадей: ПД — {pd_variants}, "
                        f"РД — {rd_variants}")


def parse_quads(text: str) -> list[dict]:
    out = []
    for living, total, summer, flat in QUAD.findall(text):
        a, b, c = (float(x.replace(",", ".")) for x in (living, total, summer))
        if a <= b <= c:
            out.append({"type": re.sub(r"^Ст\.?\s?", "", flat), "living": a, "total": b, "summer": c})
    return out


def compare_flats(pd: list[dict], rd: list[dict]) -> list[dict]:
    def index(flats):
        by_key = defaultdict(list)
        for f in flats:
            by_key[(f["type"], f["total"])].append(f)
        return by_key
    pd_idx, rd_idx = index(pd), index(rd)
    out = []
    for key in sorted(set(pd_idx) & set(rd_idx)):
        pd_vals = {(f["living"], f["summer"]) for f in pd_idx[key]}
        rd_vals = {(f["living"], f["summer"]) for f in rd_idx[key]}
        if pd_vals & rd_vals:
            continue
        out.append({"flat_type": key[0], "total": key[1], "pd": sorted(pd_vals), "rd": sorted(rd_vals),
                    "pd_ref": pd_idx[key][0], "rd_ref": rd_idx[key][0],
                    "changed": [n for n, i in (("living", 0), ("summer", 1))
                                if {v[i] for v in pd_vals} != {v[i] for v in rd_vals}]})
    return out


def carry_over(session: Session, new: Finding, prev: Finding | None) -> None:
    """Повторный прогон (дозагрузка): гипотеза с тем же finding_id сохраняет «привязку» (SUSPICION -> CANDIDATE) и
    решение инспектора; в историю — строка decisions с пометкой."""
    if prev is None:
        return
    if prev.status == S.CANDIDATE:                        # инспектор привязал гипотезу к доказательствам
        new.status = S.CANDIDATE
    if prev.inspector_status == S.INSPECTOR_PENDING:
        return
    new.inspector_status, new.decided_by, new.decided_at = prev.inspector_status, prev.decided_by, prev.decided_at
    new.reason_code, new.comment = prev.reason_code, prev.comment
    session.add(Decision(finding_pk=new.id, user_pk=prev.decided_by, decision=prev.inspector_status,
                         previous_status=S.INSPECTOR_PENDING, reason_code=prev.reason_code,
                         comment=f"Перенесено из прошлой версии протокола: {prev.comment or ''}".strip()))


def run_freesearch(session: Session, process: Process, version: int) -> dict:
    files = {f.id: f for f in session.scalars(select(File).where(File.process_id == process.id, File.status == "ok",
                                                                 File.doc_stage.in_(("PD", "RD"))))}
    pages = list(session.scalars(select(Page).where(Page.file_pk.in_(list(files) or [-1]), Page.text != "")))
    ar_pages = [p for p in pages if p.section == "АР" or files[p.file_pk].discipline == "АР"]
    tables: dict[str, list[dict]] = defaultdict(list)
    flats: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for p in ar_pages:
        stage = files[p.file_pk].doc_stage
        if TITLE.search(p.text):
            for t in parse_tables(p.text):
                sig = (stage, p.file_pk, tuple(sorted((n, r["area"]) for n, r in t["rooms"].items())))
                if sig not in seen:
                    seen.add(sig)
                    tables[stage].append({**t, "file_pk": p.file_pk, "page_no": p.page_no})
        for q in parse_quads(p.text):
            flats[stage].append({**q, "file_pk": p.file_pk, "page_no": p.page_no})
    previous = {f.finding_id: f for f in session.scalars(select(Finding).where(
        Finding.process_id == process.id, Finding.source != "matrix", Finding.active.is_(True)))}
    session.execute(update(Finding).where(Finding.process_id == process.id, Finding.source != "matrix",
                                          Finding.active.is_(True)).values(active=False))
    stats = {"ar_pages": len(ar_pages), "pairs": 0, "reliable_pairs": 0, "suspicions": 0, "flats": 0}

    def add(code: str, location: str, pd_val, rd_val, rationale: str, method: str, reliable: bool,
            pd_ref: tuple[int, int], rd_ref: tuple[int, int], rule: str, confidence: tuple[float, str]) -> None:
        f = Finding(process_id=process.id, finding_id=f"FND-{code}-" + hashlib.sha1(repr((location, pd_ref, rd_ref)).encode()).hexdigest()[:8],
                    source=rule.split("-")[1], param_code=code, rule_version=rule, location=location,
                    expected_value=None if pd_val is None else str(pd_val),
                    actual_rd=None if rd_val is None else str(rd_val), delta=None, status=S.SUSPICION,
                    review_priority="MEDIUM", criticality=CRITICALITY, rationale=rationale, discovery_method=method,
                    verification={"confidence": confidence[0], "basis": confidence[1]},
                    submission_label="VIOLATION_PRESENT" if reliable else None,
                    submission_status="WARNING" if reliable else None, submission_include=reliable,
                    created_in_version=version)
        session.add(f)
        session.flush()
        carry_over(session, f, previous.get(f.finding_id))
        for role, stage, (fpk, pno), val in (("expected", "PD", pd_ref, pd_val), ("actual_rd", "RD", rd_ref, rd_val)):
            session.add(EvidenceFragment(finding_pk=f.id, role=role, stage=stage, file_pk=fpk, page_no=pno, bbox=None,
                                         bbox_source="page", value=None if val is None else str(val)))
        stats["suspicions"] += 1

    for pd, rd, similarity in best_pairs(tables.get("PD", []), tables.get("RD", [])):
        stats["pairs"] += 1
        if pd["total"] and rd["total"] and not 0.8 <= rd["total"] / pd["total"] <= 1.25:
            continue                 # итоги различаются больше чем на четверть — это разные этажи, а не изменение
        reliable = bool(pd["parsed_ok"] and rd["parsed_ok"])
        stats["reliable_pairs"] += reliable
        confidence = explication_confidence(similarity, pd["parsed_ok"], rd["parsed_ok"])
        found = compare_rooms(pd["rooms"], rd["rooms"])
        if pd["total"] and rd["total"] and abs(pd["total"] - rd["total"]) > AREA_TOLERANCE:
            found.insert(0, {"room": "ИТОГ", "type": "TOTAL_CHANGED", "pd": pd["total"], "rd": rd["total"],
                             "delta": round(rd["total"] - pd["total"], 2)})
        if len(found) > MAX_ROOMS_PER_PAIR:
            continue                 # лавина находок = почти наверняка неверно сопоставленные этажи
        for x in found:
            location = f"Итог этажа (ПД с.{pd['page_no']})" if x["room"] == "ИТОГ" else f"Помещение {x['room']}"
            add(CODES[x["type"]], location, x["pd"], x["rd"],
                f"Экспликация помещений ПД↔РД: {x['type']}" + (f", Δ={x['delta']} м²" if "delta" in x else "")
                + ("" if reliable else " (таблица разобрана не полностью: сумма площадей не сошлась с итогом)"),
                METHOD[x["type"]], reliable, (pd["file_pk"], pd["page_no"]), (rd["file_pk"], rd["page_no"]),
                RULE.format("explication"), confidence)
    for x in compare_flats(flats.get("PD", []), flats.get("RD", [])):
        stats["flats"] += 1
        add("FREE-AR-APARTMENT-AREA", f"Квартира типа {x['flat_type']} ({x['total']} м²)",
            "; ".join(f"жилая {a} / с летними {b}" for a, b in x["pd"]),
            "; ".join(f"жилая {a} / с летними {b}" for a, b in x["rd"]),
            "Квартирография: у той же квартиры (тип и общая площадь совпадают) изменены " + ", ".join(
                {"living": "жилая площадь", "summer": "площадь с летними помещениями"}[c] for c in x["changed"]),
            "LOGICAL_ANALYSIS", True, (x["pd_ref"]["file_pk"], x["pd_ref"]["page_no"]),
            (x["rd_ref"]["file_pk"], x["rd_ref"]["page_no"]), RULE.format("apartments"),
            flat_confidence(len(x["pd"]), len(x["rd"])))
    session.flush()
    return stats
