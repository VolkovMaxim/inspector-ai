"""Этап 2 — постраничный индекс: текстовый слой, слова с рамками в долях видимой страницы, качество слоя, нужен ли
OCR. Кэш по SHA-256 файла (ТЗ 9.1 «кеширование результатов парсинга по хешу файла»): повторная загрузка того же
файла не разбирается заново."""
from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.models import File, Page, Process
from inspector.pipeline.textutils import MIN_LAYER_CHARS, fix_mojibake, layer_quality, pool_context

CACHE_VERSION = 1


def words_cache_path(data_dir: Path, sha: str) -> Path:
    return data_dir / "cache" / "pages" / f"{sha}.v{CACHE_VERSION}.jsonl"


def index_pdf(pdf_path: str, cache_path: str) -> list[dict]:
    """PDF -> строки страниц (без слов); слова пишутся в кэш-файл. Выполняется в отдельном процессе."""
    import pymupdf

    from inspector.pipeline.geometry import layer_words

    cache = Path(cache_path)
    if cache.exists():
        rows = []
        with open(cache, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                r.pop("words", None)
                rows.append(r)
        return rows
    doc = pymupdf.open(pdf_path)
    rows, full = [], []
    for number, page in enumerate(doc, start=1):
        row = {"page": number, "width": round(page.rect.width, 1), "height": round(page.rect.height, 1),
               "rotation": page.rotation, "text": "", "has_layer": False, "quality": 0.0, "need_ocr": True,
               "error": None}
        words: list = []
        try:
            words = [(*w[:4], w[4].replace("\x00", "")) for w in layer_words(page)]   # NUL: PostgreSQL его не хранит
            text = page.get_text("text").replace("\x00", "")
            fixed = fix_mojibake(text)
            if fixed is not None:
                text = fixed
                words = [(*w[:4], fix_mojibake(w[4]) or w[4].encode("latin-1", "ignore").decode("cp1251", "ignore"))
                         for w in words]
            n_chars = sum(len(w[4]) for w in words)
            has_layer = n_chars >= MIN_LAYER_CHARS
            quality = layer_quality(text) if has_layer else 0.0
            garbage = has_layer and quality < 0.5
            row.update(has_layer=has_layer and not garbage, quality=round(quality, 3),
                       need_ocr=(not has_layer) or garbage, text=text if has_layer and not garbage else "")
            if not row["has_layer"]:
                words = []
        except Exception as exc:  # noqa: BLE001 — битая страница: фиксируем, остальные читаем
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rows.append(row)
        full.append({**row, "words": [list(w) for w in words]})
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".part")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in full:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, cache)
    return rows


def load_words(data_dir: Path, sha: str) -> dict[int, list]:
    """Слова слоя по страницам из кэша: {номер страницы: [[x0,y0,x1,y1,текст], ...]}."""
    path = words_cache_path(data_dir, sha)
    out: dict[int, list] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                out[r["page"]] = r.get("words") or []
    return out


def run_pages(session: Session, process: Process, batch: int, settings: Settings, progress=None) -> dict:
    files = [f for f in session.scalars(select(File).where(File.process_id == process.id, File.batch == batch,
                                                           File.status == "ok").order_by(File.id))
             if session.scalars(select(Page.id).where(Page.file_pk == f.id).limit(1)).first() is None]
    stats = {"files": len(files), "pages": 0, "layer": 0, "need_ocr": 0}
    if not files:
        return stats
    workers = max(1, min(8, (os.cpu_count() or 2) - 1, len(files)))
    with ProcessPoolExecutor(max_workers=workers, mp_context=pool_context()) as pool:
        futures = {f.id: pool.submit(index_pdf, f.pdf_path, str(words_cache_path(settings.data_dir, f.sha256)))
                   for f in files}
        for n, f in enumerate(files, start=1):
            try:
                rows = futures[f.id].result()
            except Exception as exc:  # noqa: BLE001
                f.status, f.error = "broken", f"Страницы не читаются: {type(exc).__name__}: {exc}"[:500]
                continue
            for r in rows:
                session.add(Page(file_pk=f.id, page_no=r["page"], width=r["width"], height=r["height"],
                                 rotation=r["rotation"], has_layer=r["has_layer"], layer_quality=r["quality"],
                                 need_ocr=r["need_ocr"], text_source="layer" if r["has_layer"] else "none",
                                 text=r["text"], error=r.get("error")))
            f.pages = len(rows)
            f.ocr_pages = sum(r["need_ocr"] for r in rows)
            stats["pages"] += len(rows)
            stats["layer"] += sum(r["has_layer"] for r in rows)
            stats["need_ocr"] += f.ocr_pages
            session.flush()
            if progress:
                progress(n, len(files))
    return stats
