"""Этап 3 — OCR страниц без пригодного текстового слоя.

OCR_ENGINE=rapidocr (по умолчанию): текст всех таких страниц — слова RapidOCR (bbox.py), собранные в строки. Его
хватает для типа страницы и отбора: он читает как GLM-OCR (76,4 против 76,3). Точный текст нужен только страницам, которые пойдут в извлечение, — их после отбора перечитывает
модель (этап reocr, run_reocr): на сканах актов и сертификатов Qwen3.8 читает 94 % слов против 80 % у RapidOCR.
OCR_ENGINE=vlm — модель читает все страницы без слоя (как раньше). Конфигурация модели:
промпт PLAIN, temperature 0, без рассуждения, max_tokens 4000, плитки 200 dpi / 1800 px / ≤ 12,
обрезка петель trim_loop, сторож от мусора (повтор плитки). Кэш по SHA-256 файла и имени модели."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.llm import ChatClient, LLMError, image_part
from inspector.models import File, Page, Process
from inspector.pipeline.textutils import looks_garbage, pool_context, trim_loop

log = logging.getLogger("inspector.ocr")

PLAIN = ("Распознай весь текст на изображении и выведи его как есть, строка за строкой, в естественном порядке чтения. "
         "Таблицы — строками, ячейки через « | ». Ничего не добавляй, не пересказывай и не переводи.")
PROMPT_VERSION = "plain1"
DPI, TILE_PX, OVERLAP_PX, MAX_TILES = 200, 1800, 150, 12


def tile_boxes(width: int, height: int, tile: int = TILE_PX, overlap: int = OVERLAP_PX) -> list[tuple[int, int, int, int]]:
    """Прямоугольники плиток в пикселях. Страница до 1,35 плитки по стороне — одной плиткой (А4 при 200 dpi)."""
    def cuts(size: int) -> list[tuple[int, int]]:
        if size <= tile * 1.35:
            return [(0, size)]
        n = -(-(size - overlap) // (tile - overlap))
        step = (size - tile) / (n - 1)
        return [(round(i * step), round(i * step) + tile) for i in range(n)]
    return [(x0, y0, x1, y1) for y0, y1 in cuts(height) for x0, x1 in cuts(width)]


def render_tiles(pdf_path: str, page_no: int, rotate: int = 0) -> list[tuple[bytes, list[float]]]:
    """-> [(jpeg плитки, рамка плитки в долях видимой страницы)]. Рисуется страница как видна (с Rotate); rotate —
    ещё столько градусов по часовой (выпрямить повёрнутый скан, pipeline/orient.py; рамки тогда — в долях повёрнутой)."""
    import pymupdf

    page = pymupdf.open(pdf_path)[page_no - 1]
    if rotate % 360:
        page.set_rotation((page.rotation + rotate) % 360)
    dpi = DPI
    while True:
        w, h = page.rect.width * dpi / 72, page.rect.height * dpi / 72
        boxes = tile_boxes(round(w), round(h))
        if len(boxes) <= MAX_TILES or dpi <= 100:
            break
        dpi -= 25
    zoom = dpi / 72
    W, H = page.rect.width * zoom, page.rect.height * zoom
    out = []
    for x0, y0, x1, y1 in boxes:
        clip = pymupdf.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom) + tuple(page.rect[:2]) * 2
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY)
        out.append((pix.tobytes("jpeg", jpg_quality=85),
                    [round(x0 / W, 5), round(y0 / H, 5), round(min(x1, W) / W, 5), round(min(y1, H) / H, 5)]))
    return out


def ocr_cache_path(data_dir: Path, sha: str, model: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model)[:60]
    return data_dir / "cache" / "ocr" / f"{sha}.{slug}.{PROMPT_VERSION}.jsonl"


def load_ocr_cache(path: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out[r["page"]] = r
    return out


async def ocr_tile(client: ChatClient, jpeg: bytes, max_tokens: int) -> tuple[str, str | None]:
    messages = [{"role": "user", "content": [image_part(jpeg), {"type": "text", "text": PLAIN}]}]
    try:
        res = await client.chat(messages, max_tokens=max_tokens)
        text = trim_loop(res.content)
        if looks_garbage(text):   # редкий сбой: плитка мусором из чужих алфавитов -> ещё раз с другой выборкой
            res = await client.chat(messages, max_tokens=max_tokens, temperature=0.3)
            text = trim_loop(res.content)
            if looks_garbage(text):
                return "", "мусор в ответе OCR после повтора"
        return text, None
    except LLMError as exc:
        return "", f"OCR недоступен: {exc}"


async def ocr_pages(client: ChatClient, todo: list[tuple[int, str, int]], max_tokens: int, cache_paths: dict[int, Path],
                    on_page, page_parallel: int = 48) -> None:
    """todo: [(file_pk, pdf_path, page_no)]. Рендер плиток — в процессах, распознавание — параллельно в клиенте."""
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(page_parallel)
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    with ProcessPoolExecutor(max_workers=workers, mp_context=pool_context()) as pool:
        async def one(file_pk: int, pdf: str, page_no: int) -> None:
            async with sem:
                try:
                    tiles = await loop.run_in_executor(pool, render_tiles, pdf, page_no)
                except Exception as exc:  # noqa: BLE001
                    on_page(file_pk, page_no, {"page": page_no, "tiles": [], "text": "", "error": f"рендер: {exc}"[:200]})
                    return
                parts = await asyncio.gather(*[ocr_tile(client, jpeg, max_tokens) for jpeg, _ in tiles])
                rec = {"page": page_no, "tiles": [{"box": box, "text": text} for (_, box), (text, _) in zip(tiles, parts)],
                       "text": "\n".join(t for t, _ in parts if t),
                       "error": "; ".join(sorted({e for _, e in parts if e})) or None}
                if rec["error"] is None or rec["text"]:
                    with open(cache_paths[file_pk], "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                on_page(file_pk, page_no, rec)
        await asyncio.gather(*[one(*t) for t in todo])


def run_ocr(session: Session, process: Process, batch: int, settings: Settings, client: ChatClient | None,
            progress=None) -> dict:
    files = {f.id: f for f in session.scalars(select(File).where(File.process_id == process.id, File.batch == batch,
                                                                 File.status == "ok"))}
    pages = list(session.scalars(select(Page).where(Page.file_pk.in_(list(files) or [-1]), Page.need_ocr.is_(True),
                                                    Page.text_source.not_in(("ocr", "rapidocr")))
                                 .order_by(Page.file_pk, Page.page_no)))
    stats = {"pages": len(pages), "from_cache": 0, "recognized": 0, "errors": 0, "seconds": 0.0}
    if not pages or settings.ocr_engine == "none":
        return stats
    if settings.ocr_engine == "rapidocr":
        return rapidocr_text(session, pages, files, settings, stats, progress)
    if client is None:
        return stats
    return vlm_text(session, pages, files, settings, client, stats, progress)


def rapidocr_text(session: Session, pages: list[Page], files: dict[int, File], settings: Settings, stats: dict,
                  progress=None) -> dict:
    """Текст страниц без слоя — слова RapidOCR в порядке чтения (text_source = rapidocr). Пустой результат (чистый
    лист) — не ошибка: страница остаётся без текста."""
    from inspector.pipeline import bbox as bbox_mod

    started = time.time()
    stats["empty"] = 0
    todo = []
    for p in pages:
        f = files[p.file_pk]
        cf = bbox_mod.cache_file(settings.data_dir, f.sha256, p.page_no, settings.rapidocr_orient)
        stats["from_cache"] += cf.exists()
        todo.append((f.pdf_path, p.page_no, str(cf)))
    step = 64
    need = sum(1 for _, _, cf in todo if not Path(cf).exists())
    with bbox_mod.ocr_pool(settings.rapidocr_gpu, max(1, need), settings.rapidocr_workers,
                           settings.rapidocr_gpu_workers) as pool:
        stats["rapidocr_gpu"] = pool[1]
        for i in range(0, len(todo), step):
            part = todo[i:i + step]
            got = bbox_mod.words_for_pages(part, orient=settings.rapidocr_orient, pool=pool)
            for p, (pdf, no, cf) in zip(pages[i:i + step], part):
                text = bbox_mod.words_to_text(got.get((pdf, no), []), bbox_mod.cached_angle(cf))
                if text:
                    p.text, p.text_source = text, "rapidocr"
                    stats["recognized"] += 1
                else:
                    stats["empty"] += 1
            if progress:
                progress(min(i + step, len(todo)), len(todo))
    session.flush()
    stats["seconds"] = round(time.time() - started, 1)
    return stats


def partial_over_text(rec: dict, page: Page) -> bool:
    """Ответ модели неполный (часть плиток с ошибкой), а у страницы уже есть текст (reocr: RapidOCR всего листа) —
    неполный текст не должен его вытеснять; страница остаётся прежней и перечитается при следующем прогоне."""
    return bool(rec.get("error")) and bool(page.text)


def vlm_text(session: Session, pages: list[Page], files: dict[int, File], settings: Settings, client: ChatClient,
             stats: dict, progress=None) -> dict:
    """Текст страниц моделью (text_source = ocr). Сбой страницы оставляет прежний текст (у reocr — RapidOCR)."""
    started = time.time()
    model = settings.ocr_model_eff
    cache_paths = {pk: ocr_cache_path(settings.data_dir, files[pk].sha256, model) for pk in {p.file_pk for p in pages}}
    by_key = {(p.file_pk, p.page_no): p for p in pages}
    todo = []
    cached = {pk: load_ocr_cache(path) for pk, path in cache_paths.items()}
    for p in pages:
        rec = cached.get(p.file_pk, {}).get(p.page_no)
        if rec and rec.get("text") and not partial_over_text(rec, p):
            p.text, p.text_source = rec["text"], "ocr"
            stats["from_cache"] += 1
        else:
            todo.append((p.file_pk, files[p.file_pk].pdf_path, p.page_no))
    for path in cache_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    done = 0

    def on_page(file_pk: int, page_no: int, rec: dict) -> None:
        nonlocal done
        page = by_key[(file_pk, page_no)]
        if rec["text"] and not partial_over_text(rec, page):
            page.text, page.text_source = rec["text"], "ocr"
            stats["recognized"] += 1
        if rec.get("error"):
            page.error = rec["error"]
            stats["errors"] += 1
        done += 1
        if progress and (done % 10 == 0 or done == len(todo)):
            progress(stats["from_cache"] + done, len(pages))

    if todo:
        asyncio.run(_run(client, todo, settings.ocr_max_tokens, cache_paths, on_page))
    session.flush()
    stats["seconds"] = round(time.time() - started, 1)
    return stats


def run_reocr(session: Session, process: Process, candidates: list, settings: Settings, client: ChatClient | None,
              progress=None) -> dict:
    """Этап reocr: страницы-кандидаты извлечения с текстом RapidOCR перечитывает модель. Дальше по ним всё
    (извлечение, проверка цитаты, судья, карточка) берёт текст модели; сбой — остаётся текст RapidOCR."""
    keys = {(c.file_pk, c.page_no) for c in candidates}
    pages = [p for p in session.scalars(select(Page).where(Page.file_pk.in_({k[0] for k in keys} or {-1}),
                                                           Page.text_source == "rapidocr")
                                        .order_by(Page.file_pk, Page.page_no)) if (p.file_pk, p.page_no) in keys]
    stats = {"pages": len(pages), "from_cache": 0, "recognized": 0, "errors": 0, "seconds": 0.0}
    if not pages or client is None:
        return stats
    files = {f.id: f for f in session.scalars(select(File).where(File.id.in_({p.file_pk for p in pages})))}
    return vlm_text(session, pages, files, settings, client, stats, progress)


async def _run(client: ChatClient, todo, max_tokens, cache_paths, on_page) -> None:
    async with client:
        await ocr_pages(client, todo, max_tokens, cache_paths, on_page)
