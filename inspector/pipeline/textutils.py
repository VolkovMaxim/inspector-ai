"""Текстовые утилиты конвейера. Чистые функции, без I/O."""
from __future__ import annotations

import re
import unicodedata

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{3,}")
VOWELS = set("аеёиоуыэюяaeiouy")
MIN_LAYER_CHARS = 40
HOMOGLYPHS = str.maketrans("ABCEHKMOPTXYaceopxy", "АВСЕНКМОРТХУасеорху")


def fix_mojibake(text: str) -> str | None:
    """cp1251, прочитанная как latin-1 ('Äîêóìåíò') -> нормальный текст; None, если это не тот случай."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 10 or sum("À" <= c <= "ÿ" for c in letters) / len(letters) < 0.5:
        return None
    for enc in ("cp1252", "latin-1"):
        try:
            return text.encode(enc, errors="strict").decode("cp1251", errors="strict")
        except UnicodeError:
            continue
    return None


def layer_quality(text: str) -> float:
    """0..1, ниже 0.5 — текстовый слой мусорный (сломанная таблица шрифта), страницу надо распознавать заново."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    letters = [c for c in chars if c.isalpha()]
    broken_font = sum("ƀ" <= c <= "ͯ" for c in chars)
    if letters and broken_font / len(letters) > 0.05:
        return 0.0
    ok_chars = sum(c.isdigit() or c in ".,;:-–—()/\\№%+=*\"'«»×°±<>[]_|!?#~^&@$" or "а" <= c.lower() <= "я"
                   or c in "ёЁ" or "a" <= c.lower() <= "z" for c in chars)
    char_score = ok_chars / len(chars)
    words = WORD_RE.findall(text)
    if len(letters) < 0.2 * len(chars) or len(words) < 5:
        return char_score
    good = 0
    for w in words:
        low = w.lower()
        cyr = sum("а" <= c <= "я" or c == "ё" for c in low)
        if not 0 < cyr < len(low) and any(c in VOWELS for c in low):
            good += 1
    return char_score * (good / len(words))


def trim_loop(text: str, max_period: int = 300, min_repeats: int = 5) -> str:
    """Обрезка зацикливания VLM: периодичный хвост (кусок из 1..max_period слов подряд ≥ min_repeats раз) -> одно
    вхождение. Последнее слово (может быть оборвано лимитом) в проверке не участвует. Плюс петля без пробелов
    (период ≤ 30 символов, ≥ 10 повторов). Измерено на 334 стр.: без обрезки — 62,6 F1, с ней — 92,2."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    body = [text[a:b] for a, b in spans][:-1]
    n, best_cut = len(body), None
    for p in range(1, max_period + 1):
        if n < p * min_repeats:
            break
        i = n - p - 1
        while i >= 0 and body[i] == body[i + p]:
            i -= 1
        start = i + 1
        if n - start >= p * min_repeats:
            cut = spans[start + p][0]
            best_cut = cut if best_cut is None else min(best_cut, cut)
    text = text if best_cut is None else text[:best_cut].rstrip()
    for p in range(1, 31):
        unit, reps, end = text[-p:], 1, len(text) - p
        if not unit.strip():
            continue
        while end - p >= 0 and text[end - p:end] == unit:
            reps += 1
            end -= p
        if reps >= 10:
            return text[:end + p].rstrip()
    return text


FOREIGN = re.compile(r"[؀-ۿ぀-ヿ㐀-䶿一-鿿가-힯฀-๿]")


def looks_garbage(text: str) -> bool:
    """Сторож OCR: плитка, выданная мусором из китайских/арабских/корейских символов (редкий сбой VLM)."""
    chars = [c for c in text if not c.isspace()]
    if len(chars) < 40:
        return False
    return len(FOREIGN.findall(text)) / len(chars) > 0.2


def squash(s: str) -> str:
    return "".join(s.split()).casefold().replace("ё", "е")


def snippet_supported(snippet: str, text: str) -> bool:
    """Защита от галлюцинаций: цитата должна быть на странице (сравнение без пробелов и регистра)."""
    probe = squash(snippet or "")
    return len(probe) >= 4 and probe[:60] in squash(text)


def norm_token(t: str) -> str:
    """Токен для сопоставления со словами страницы: NFKC, латинские двойники -> кириллица, регистр, края."""
    t = unicodedata.normalize("NFKC", t).translate(HOMOGLYPHS).casefold().replace("ё", "е")
    return re.sub(r"^[^\w]+|[^\w%°]+$", "", t)


def norm_location(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold().replace("ё", "е")


def number_of(value: str) -> float | None:
    """Первое число значения. Дефис после буквы — часть обозначения, а не знак: «EI-15» -> 15, «−5 %» -> −5."""
    s = value or ""
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    x = float(m.group(0).replace(",", "."))
    i = m.start()
    if i >= 1 and s[i - 1] in "-−" and (i == 1 or not s[i - 2].isalnum()):
        x = -x
    return x


def pool_context():
    """Контекст пулов процессов конвейера: forkserver, а не fork — fork из многопоточного процесса (httpx, asyncio)
    Python 3.12 помечает как небезопасный (возможна взаимоблокировка в дочернем процессе)."""
    import multiprocessing

    return multiprocessing.get_context("forkserver")
