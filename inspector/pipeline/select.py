"""Этап 6 — отбор страниц на параметр × стадию.

База — отбор по словам (основы слов названия в окне 400 символов + idf; путь «значение +
ключевое слово» по рецептам recipes.json). Рецепт матрицы (param_sources.json: раздел × вид листа для ПД/РД, тип
документа для ИД) — ПРИОРИТЕТ, а не фильтр: замер показал, что в ПД значения чаще лежат в тексте, чем в
названном матрицей виде листа (совпало 44 %). Неактуальные редакции (Document.is_actual = False) в отбор не идут."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

from inspector.config import RESOURCES

WORD = re.compile(r"[а-яёa-z]+", re.I)
STOP = {"и", "в", "на", "по", "с", "к", "из", "для", "или", "от", "до", "при", "за", "не", "а", "о", "об", "их",
        "раздел", "лист", "листы", "таблица", "таблицы", "текст", "чертеж", "чертежи", "чертежей", "данные",
        "общие", "общих", "схема", "схемы", "план", "планы", "акт", "акты", "журнал", "ведомость", "тип", "типы",
        "количество", "параметры", "характеристики", "наличие", "том", "часть", "пд", "рд", "ид", "пз", "аоср"}
NON_NUMERIC_UNITS = {"—", "-", "", "Статус", "Буква", None}
WINDOW, STEP = 400, 200
RECIPE_BONUS_FULL, RECIPE_BONUS_HALF = 0.5, 0.25
# раздел матрицы (param_sources) -> раздел классификатора страниц
SECTION_ALIASES = {"ПБ": {"ПБ", "АР", "ОВ", "ЭОМ", "СС"}, "ЭЭ": {"ЭЭ", "АР", "ОВ"}, "ГП": {"ГП"}}


def stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    return w[:6] if len(w) > 7 else w[:5] if len(w) > 5 else w


def stems(text: str) -> list[str]:
    return [stem(w) for w in WORD.findall(text or "") if w.lower() not in STOP and len(w) > 2]


def windows(text: str) -> list[str]:
    if len(text) <= WINDOW:
        return [text]
    return [text[i:i + WINDOW] for i in range(0, len(text) - STEP, STEP)]


def prepare(text: str) -> dict:
    wins = windows(text) if text else []
    win_stems = [set(stems(w)) for w in wins]
    return {"windows": wins, "win_stems": win_stems, "page_stems": set().union(*win_stems) if win_stems else set(),
            "win_digit": [bool(re.search(r"\d", w)) for w in wins]}


def score_by_value(pre: dict, value_rx: re.Pattern, keyword_stems: set[str], idf: dict[str, float]) -> float:
    best = 0.0
    if not keyword_stems & pre["page_stems"]:
        return best
    for win, present in zip(pre["windows"], pre["win_stems"]):
        hit = keyword_stems & present
        if not hit:
            continue
        matches = len(value_rx.findall(win))
        if matches:
            best = max(best, 2.0 + min(sum(idf.get(s, 1.0) for s in hit), 12.0) + 0.5 * min(matches, 5))
    return best


def score_by_keywords(pre: dict, keyword_stems: set[str], idf: dict[str, float], numeric: bool) -> float:
    best = 0.0
    if len(keyword_stems & pre["page_stems"]) < 3:
        return best
    for present, has_digit in zip(pre["win_stems"], pre["win_digit"]):
        if numeric and not has_digit:
            continue
        hit = keyword_stems & present
        if len(hit) >= 3:
            best = max(best, 0.5 * min(sum(idf.get(s, 1.0) for s in hit), 16.0))
    return best


def score_by_name(pre: dict, name_stems: list[str], bonus_stems: list[str], idf: dict[str, float], numeric: bool) -> float:
    if not name_stems:
        return 0.0
    need = len(name_stems) if len(name_stems) <= 2 else math.ceil(0.6 * len(name_stems))
    if len(pre["page_stems"].intersection(name_stems)) < need:
        return 0.0
    best = 0.0
    for present, has_digit in zip(pre["win_stems"], pre["win_digit"]):
        if numeric and not has_digit:
            continue
        hit = [s for s in name_stems if s in present]
        if len(hit) < need:
            continue
        best = max(best, sum(idf.get(s, 1.0) for s in hit) * len(hit) / len(name_stems))
    if best == 0.0:
        return 0.0
    return best + 0.25 * sum(idf.get(s, 1.0) for s in set(bonus_stems) if s in pre["page_stems"])


def text_score(pre: dict, name_stems, bonus_stems, idf, numeric, recipe: dict | None) -> float:
    by_value = by_keywords = 0.0
    if recipe and pre["windows"]:
        if recipe.get("value_rx") is not None:
            by_value = score_by_value(pre, recipe["value_rx"], recipe["keyword_stems"], idf)
        by_keywords = score_by_keywords(pre, recipe["keyword_stems"], idf, numeric)
    return max(by_value, by_keywords, score_by_name(pre, name_stems, bonus_stems, idf, numeric))


@lru_cache
def load_recipes() -> dict[str, dict]:
    merged = json.loads((RESOURCES / "recipes.json").read_text(encoding="utf-8"))
    merged.update(json.loads((RESOURCES / "recipes_override.json").read_text(encoding="utf-8")))
    out = {}
    for code, r in merged.items():
        rx = None
        if r.get("value_regex"):
            try:
                rx = re.compile(r["value_regex"], re.I)
            except re.error:
                rx = None
        out[code] = {"value_rx": rx, "keyword_stems": {s for k in r.get("keywords", []) for s in stems(k)}}
    return out


@lru_cache
def load_param_sources() -> dict[str, dict]:
    return json.loads((RESOURCES / "param_sources.json").read_text(encoding="utf-8"))


def recipe_bonus(code: str, stage: str, page: "PageInfo") -> float:
    """Совпадение страницы с рецептом матрицы: ПД/РД — раздел и вид листа; ИД — тип документа."""
    src = load_param_sources().get(code, {}).get(stage) or {}
    if stage == "ID":
        types = {s.get("doc_type") for s in src.get("sources", []) if s.get("doc_type")}
        return RECIPE_BONUS_FULL if types & ({page.page_class, page.doc_type} - {None}) else 0.0
    kinds = {s.get("kind") for s in src.get("sources", []) if s.get("kind")}
    sections: set[str] = set()
    for s in src.get("sections", []):
        sections |= SECTION_ALIASES.get(s, {s})
    kind_ok = bool(kinds & ({page.page_class, *page.other_classes} - {None}))
    section_ok = bool(page.section and page.section in sections)
    if kind_ok and section_ok:
        return RECIPE_BONUS_FULL
    return RECIPE_BONUS_HALF if (kind_ok or section_ok) else 0.0


@dataclass
class PageInfo:
    file_pk: int
    page_no: int
    stage: str
    text: str
    page_class: str | None = None
    other_classes: list = field(default_factory=list)
    section: str | None = None
    doc_type: str | None = None


@dataclass
class Candidate:
    param_code: str
    stage: str
    file_pk: int
    page_no: int
    score: float


def select_candidates(pages: list[PageInfo], params: list[dict], top_k: int,
                      idf_pages: list[PageInfo] | None = None) -> list[Candidate]:
    """params: [{code, parameter_name, unit, source_pd, source_rd, source_id}] -> top_k страниц на (параметр, стадия).
    idf_pages — страницы, по которым считается idf (для инкрементального режима — весь объект)."""
    prepared = [(p, prepare(p.text)) for p in pages if p.text and p.stage in ("PD", "RD", "ID")]
    df: Counter = Counter()
    base = [(p, pre) for p, pre in prepared] if idf_pages is None else [(p, prepare(p.text)) for p in idf_pages if p.text]
    for _, pre in base:
        df.update(pre["page_stems"])
    n = max(1, len(base))
    idf = {s: math.log(1 + n / c) for s, c in df.items()}
    recipes = load_recipes()
    out: list[Candidate] = []
    for param in params:
        code = param["code"]
        name_stems = list(dict.fromkeys(stems(param["parameter_name"])))
        numeric = param.get("unit") not in NON_NUMERIC_UNITS
        bonus = {"PD": stems(param.get("source_pd", "")), "RD": stems(param.get("source_rd", "")),
                 "ID": stems(param.get("source_id", ""))}
        by_stage: dict[str, list[tuple[float, PageInfo]]] = defaultdict(list)
        for p, pre in prepared:
            s = text_score(pre, name_stems, bonus[p.stage], idf, numeric, recipes.get(code))
            if s > 0:
                by_stage[p.stage].append((s * (1 + recipe_bonus(code, p.stage, p)), p))
        for stage, scored in by_stage.items():
            for s, p in sorted(scored, key=lambda x: (-x[0], x[1].file_pk, x[1].page_no))[:top_k]:
                out.append(Candidate(code, stage, p.file_pk, p.page_no, round(s, 3)))
    return out
