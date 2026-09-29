"""Координаты: слова страницы в долях видимой страницы [0;1] (CropBox + Rotate, ТЗ 9.1) и поиск рамки значения
по цитате. Слова берутся из текстового слоя PDF или из RapidOCR (для OCR-страниц)."""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process

from inspector.pipeline.textutils import norm_token

Word = tuple[float, float, float, float, str]      # x0, y0, x1, y1 (доли видимой страницы), текст


def layer_words(page) -> list[Word]:
    """Слова текстового слоя страницы PyMuPDF в долях видимой страницы: координаты слоя заданы в неповёрнутой
    системе, rotation_matrix переводит их в то, что видно на картинке (get_pixmap рисует с поворотом)."""
    import pymupdf

    rect = page.rect                     # видимая страница (после Rotate), начало — левый верх CropBox
    W, H = rect.width or 1.0, rect.height or 1.0
    m = page.rotation_matrix
    out: list[Word] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        r = pymupdf.Rect(x0, y0, x1, y1) * m if page.rotation else pymupdf.Rect(x0, y0, x1, y1)
        r.normalize()
        out.append((round((r.x0 - rect.x0) / W, 5), round((r.y0 - rect.y0) / H, 5),
                    round((r.x1 - rect.x0) / W, 5), round((r.y1 - rect.y0) / H, 5), text))
    return out


def union(boxes: list[tuple[float, float, float, float]]) -> list[float]:
    return [round(min(b[0] for b in boxes), 4), round(min(b[1] for b in boxes), 4),
            round(max(b[2] for b in boxes), 4), round(max(b[3] for b in boxes), 4)]


def _tokens(text: str) -> list[str]:
    return [t for t in (norm_token(x) for x in (text or "").split()) if t]


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return fuzz.ratio(a, b) / 100.0


@dataclass
class Located:
    bbox: list[float] | None
    score: float
    how: str            # value | snippet | value_only | none


def locate(value: str, snippet: str, words: list[Word], min_token_sim: float = 0.7) -> Located:
    """Рамка значения на странице. Сначала окно слов, лучше всего совпадающее с цитатой (≥ половины её токенов);
    внутри окна — слова значения. Без цитаты — одиночный поиск значения (берётся, только если совпадение однозначно)."""
    if not words:
        return Located(None, 0.0, "none")
    wt = [norm_token(w[4]) for w in words]
    vt = _tokens(value)
    key = [t for t in vt if any(ch.isdigit() for ch in t)] or vt      # «1250,5 м2» -> ищем прежде всего число
    st = _tokens(snippet)[:30]
    best = (0.0, -1)
    if st:
        # похожесть считаем один раз на уникальный токен страницы, затем скользящее окно — O(число слов)
        uniq = list(set(wt))
        scores = process.cdist(list(set(st)), uniq, scorer=fuzz.ratio, workers=1).max(axis=0) / 100.0
        sim = dict(zip(uniq, scores))
        hit = [int(sim[w] >= min_token_sim) for w in wt]
        n = len(st)
        run = sum(hit[:n])
        for i in range(0, max(1, len(wt) - n + 1)):
            if i:
                run += hit[i + n - 1] - hit[i - 1] if i + n - 1 < len(hit) else -hit[i - 1]
            score = run / n
            if score > best[0]:
                best = (score, i)
                if score >= 1.0:
                    break
    if best[0] >= 0.5:
        i = best[1]
        lo, hi = max(0, i - 2), min(len(words), i + len(st) + 2)
        hits = [j for j in range(lo, hi) for k in key if _sim(k, wt[j]) >= 0.85 or (len(k) >= 3 and k in wt[j])]
        if hits:
            return Located(union([words[j][:4] for j in sorted(set(hits))]), best[0], "value")
        return Located(union([words[j][:4] for j in range(i, min(len(words), i + len(st)))]), best[0], "snippet")
    if key:
        hits = [j for j, w in enumerate(wt) if any(_sim(k, w) >= 0.9 for k in key)]
        if len(hits) == 1 or (hits and len({wt[j] for j in hits}) == 1 and len(hits) <= 2):
            return Located(union([words[hits[0]][:4]]), 0.5, "value_only")
    return Located(None, 0.0, "none")
