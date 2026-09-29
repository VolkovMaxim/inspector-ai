"""Этап 7 — извлечение значений (промпт g2): один вызов Qwen3.8 на
страницу по всем её параметрам (≤ 8 за вызов), JSON-схема; цитата обязана быть на странице (иначе значение не
участвует в сравнении). Затем рамка значения: слова слоя PDF -> точная; OCR-страница -> слова RapidOCR; иначе
рамки нет (карточка покажет страницу целиком).
Флаг EXTRACT_WITH_IMAGE (по умолчанию выключен): на страницах, чей текст получен OCR, вместе с текстом идёт картинка
страницы (как на этапе класса: 1300 px + вырезка штампа у больших листов)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.llm import ChatClient, LLMError, image_part
from inspector.models import Extraction, File, Page, Process
from inspector.pipeline import bbox as bbox_mod
from inspector.pipeline.geometry import locate
from inspector.pipeline.pages import load_words
from inspector.pipeline.select import Candidate
from inspector.pipeline.textutils import pool_context, snippet_supported

log = logging.getLogger("inspector.extract")

PROMPT_VERSION = "g2"
OCR_SOURCES = ("ocr", "rapidocr")     # текст страницы распознан (моделью или RapidOCR), а не взят из слоя
IMAGE_TAG = "img1"                    # вариант промпта с картинкой страницы (EXTRACT_WITH_IMAGE)
IMAGE_NOTE = ("К странице приложена её картинка. Текст ниже распознан с этой картинки и может содержать ошибки "
              "распознавания: значения сверяй с картинкой; snippet — цитата из текста ниже, как он там написан.\n\n")
MAX_CHARS = 16000
MAX_PARAMS_PER_CALL = 8
SYSTEM = (
    "Ты извлекаешь значения параметров из одной страницы строительной документации (проектной, рабочей или "
    "исполнительной). Отвечай только JSON по схеме. Правила:\n"
    "1. Извлекай ТОЛЬКО то, что явно написано на странице. Ничего не вычисляй и не додумывай. Нет параметра — не "
    "возвращай по нему записей.\n"
    "2. value_raw — значение как в документе, с единицей измерения (например '1250,5 м2', 'В30', 'А500С', '45 шт').\n"
    "3. location — к чему относится значение, коротко и в именительном падеже: 'Объект в целом' для показателей "
    "всего объекта/участка; иначе корпус ('Корпус 1'), конструкция ('Фундаментная плита'), помещение (номер: '012'), "
    "система ('П1'). Одно значение на нескольких локациях — отдельная запись на каждую.\n"
    "4. snippet — дословная цитата со страницы до 120 символов, содержащая значение.\n"
    "5. Значения из нормативных ссылок, примеров, оглавлений и штампов не извлекай."
)
STRUCTURES = ("Стена в грунте, Форшахта, Сваи, Ростверк, Обвязочная балка, Распределительная балка, Фундаментная плита, "
              "Бетонная подготовка, Вертикальные конструкции подземной части, Вертикальные конструкции надземной части, "
              "Плиты перекрытия подземной части, Плиты перекрытия надземной части, Плита покрытия, Лестницы")
LOCATION_HINTS = {
    "KR": f"конструкция из списка: {STRUCTURES}; если на странице чертёж/спецификация одной конструкции — все значения "
          "страницы относятся к ней (смотри заголовок листа и штамп)",
    "AR": "номер помещения ('012') или 'Корпус N' / 'Объект в целом'",
    "IOS": "обозначение системы ('П1', 'В3', 'ЩР-1') или номер помещения; иначе 'Объект в целом'",
    "PZ": "'Объект в целом' или 'Корпус N'", "SPZU": "'Объект в целом' (участок) или название площадки/проезда",
}
STAGE_RU = {"PD": "PD (проектная документация)", "RD": "RD (рабочая документация)", "ID": "ID (исполнительная документация)"}


def location_hints(codes: list[str]) -> str:
    seen = []
    for c in codes:
        prefix = "IOS" if c.startswith("IOS") else c.split("-")[0]
        hint = LOCATION_HINTS.get(prefix)
        if hint and (prefix, hint) not in seen:
            seen.append((prefix, hint))
    return "".join(f"\nЛокации для {p}-*: {h}." for p, h in seen)


def schema(codes: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
        "type": "array", "maxItems": 40, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["parameter_code", "location", "value_raw", "snippet", "confidence"],
            "properties": {"parameter_code": {"enum": codes}, "location": {"type": "string", "maxLength": 80},
                           "value_raw": {"type": "string", "maxLength": 80},
                           "snippet": {"type": "string", "maxLength": 160},
                           "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}}}}


def build_user(params: list[dict], stage: str, text: str, with_image: bool = False) -> str:
    lines = [f"- {p['code']}: {p['parameter_name']} (ед.: {p.get('unit') or '—'})" for p in params]
    return ((IMAGE_NOTE if with_image else "") + f"Стадия документа: {stage}.\nПараметры:\n" + "\n".join(lines)
            + location_hints([p["code"] for p in params]) + f"\n\nСТРАНИЦА:\n{text[:MAX_CHARS]}")


def cache_key(model: str, codes: list[str], text: str, with_image: bool = False) -> str:
    """Без картинки ключ прежний (кэш прошлых прогонов действует), с картинкой — отдельный."""
    key = f"{model}|{PROMPT_VERSION}|{','.join(codes)}|{text[:MAX_CHARS]}" + (f"|{IMAGE_TAG}" if with_image else "")
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def build_messages(params: list[dict], stage: str, text: str, images: list[bytes] | None) -> list[dict]:
    if not images:
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_user(params, stage, text)}]
    content = [image_part(i) for i in images] + [{"type": "text", "text": build_user(params, stage, text, True)}]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]


async def extract_calls(client: ChatClient, calls: list[tuple], cache_dir: Path) -> list[tuple[tuple, list[dict] | None, str | None]]:
    """calls: [(file_pk, page_no, stage, text, params, pdf_path | None)] -> [(ключ страницы, items | None, ошибка)].
    pdf_path задан — к тексту прикладывается картинка страницы (рендер в пуле процессов, как на этапе класса)."""
    from inspector.pipeline.classify import render_page

    loop = asyncio.get_running_loop()
    pool = None
    if any(c[5] for c in calls):
        pool = ProcessPoolExecutor(max_workers=max(1, min(8, (os.cpu_count() or 2) - 1)), mp_context=pool_context())

    async def one(call):
        file_pk, page_no, stage, text, params, pdf = call
        codes = [p["code"] for p in params]
        path = cache_dir / f"{cache_key(client.model, codes, text, bool(pdf))}.json"
        if path.exists():
            return (file_pk, page_no, stage), json.loads(path.read_text(encoding="utf-8")).get("items", []), None
        images = None
        if pdf:
            try:
                images = await loop.run_in_executor(pool, render_page, pdf, page_no)
            except Exception as exc:  # noqa: BLE001 — битая страница: без картинки нельзя, иначе смешаем варианты
                return (file_pk, page_no, stage), None, f"картинка страницы: {type(exc).__name__}: {exc}"[:300]
        try:
            res = await client.chat(build_messages(params, stage, text, images), max_tokens=3000,
                                    json_schema={"name": "extraction", "schema": schema(codes)})
            raw = res.json()
        except (LLMError, ValueError) as exc:
            return (file_pk, page_no, stage), None, str(exc)[:300]
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return (file_pk, page_no, stage), raw.get("items", []), None
    try:
        async with client:
            return await asyncio.gather(*[one(c) for c in calls])
    finally:
        if pool is not None:
            pool.shutdown()


def run_extract(session: Session, process: Process, candidates: list[Candidate], params: dict[str, dict],
                settings: Settings, client: ChatClient, batch: int, progress=None) -> dict:
    started = time.time()
    wanted: dict[tuple[int, int, str], set[str]] = defaultdict(set)
    for c in candidates:
        wanted[(c.file_pk, c.page_no, c.stage)].add(c.param_code)
    files = {f.id: f for f in session.scalars(select(File).where(File.process_id == process.id))}
    pages = {(p.file_pk, p.page_no): p for p in session.scalars(
        select(Page).where(Page.file_pk.in_({k[0] for k in wanted} or {-1})))}
    calls = []
    for (fpk, pno, stage), codes in sorted(wanted.items()):
        page = pages.get((fpk, pno))
        if page is None or not page.text:
            continue
        codes_sorted = sorted(codes)
        pdf = files[fpk].pdf_path if settings.extract_with_image and page.text_source in OCR_SOURCES else None
        for i in range(0, len(codes_sorted), MAX_PARAMS_PER_CALL):
            calls.append((fpk, pno, stage, page.text, [params[c] for c in codes_sorted[i:i + MAX_PARAMS_PER_CALL]],
                          pdf))
    cache_dir = settings.data_dir / "cache" / "extract"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(0, len(calls))
    results = asyncio.run(extract_calls(client, calls, cache_dir)) if calls else []
    stats = {"calls": len(calls), "with_image": sum(bool(c[5]) for c in calls), "errors": 0, "items": 0,
             "unsupported": 0, "bbox_layer": 0, "bbox_rapidocr": 0, "bbox_none": 0}
    rows: list[Extraction] = []
    for (fpk, pno, stage), items, error in results:
        if items is None:
            stats["errors"] += 1
            continue
        page = pages[(fpk, pno)]
        for it in items:
            if it.get("parameter_code") not in params:
                continue
            supported = snippet_supported(it.get("snippet", ""), page.text)
            rows.append(Extraction(process_id=process.id, param_code=it["parameter_code"], stage=stage, file_pk=fpk,
                                   page_no=pno, location=(it.get("location") or "").strip()[:300],
                                   value_raw=(it.get("value_raw") or "").strip()[:300],
                                   snippet=(it.get("snippet") or "")[:500], confidence=float(it.get("confidence") or 0),
                                   supported=supported, batch=batch))
            stats["items"] += 1
            stats["unsupported"] += not supported
    if progress:
        progress(len(calls), len(calls))
    # рамки: слой -> слова PDF; OCR -> RapidOCR (лениво, только страницы со значениями)
    need_ocr_words: dict[tuple[int, int], tuple[str, int, str]] = {}
    for r in rows:
        page = pages[(r.file_pk, r.page_no)]
        if r.supported and page.text_source in OCR_SOURCES and settings.bbox_engine == "rapidocr":
            f = files[r.file_pk]
            need_ocr_words[(r.file_pk, r.page_no)] = (f.pdf_path, r.page_no,
                                                      str(bbox_mod.cache_file(settings.data_dir, f.sha256, r.page_no,
                                                                              settings.rapidocr_orient)))
    ocr_words: dict[tuple[int, int], list] = {}
    if need_ocr_words:
        try:
            got = bbox_mod.words_for_pages(list(need_ocr_words.values()), gpu=settings.rapidocr_gpu,
                                           orient=settings.rapidocr_orient, workers=settings.rapidocr_workers,
                                           gpu_workers=settings.rapidocr_gpu_workers)
            ocr_words = {k: got.get((v[0], v[1]), []) for k, v in need_ocr_words.items()}
        except Exception as exc:  # noqa: BLE001 — нет рамок, но значения остаются
            log.warning("rapidocr failed", extra={"error": str(exc)[:300]})
    layer_cache: dict[int, dict[int, list]] = {}
    for r in rows:
        if not r.supported:
            continue
        page = pages[(r.file_pk, r.page_no)]
        words: list = []
        source = None
        if page.text_source == "layer":
            if r.file_pk not in layer_cache:
                layer_cache[r.file_pk] = load_words(settings.data_dir, files[r.file_pk].sha256)
            words, source = layer_cache[r.file_pk].get(r.page_no, []), "layer"
        elif (r.file_pk, r.page_no) in ocr_words:
            words, source = ocr_words[(r.file_pk, r.page_no)], "rapidocr"
        loc = locate(r.value_raw, r.snippet, [tuple(w) for w in words]) if words else None
        if loc and loc.bbox:
            r.bbox, r.bbox_source = loc.bbox, source
            stats[f"bbox_{source}"] += 1
        else:
            r.bbox, r.bbox_source = None, "page"
            stats["bbox_none"] += 1
    session.add_all(rows)
    session.flush()
    stats["seconds"] = round(time.time() - started, 1)
    return stats
