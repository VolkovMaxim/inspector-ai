"""Отрисовка страницы PDF для классификатора страниц (исходник — service/model_code/page_cls).

mode="fit32": большая сторона ≈ side, обе стороны кратны 32 (Qwen3-VL / Qwen3.5: патч 16 × слияние 2), растяжение по
осям ≤ 16 px. mode="square": side × side с растяжением (как resize у SigLIP; так считались векторы опыта E).
Пул процессов (spawn) — для скорости «от PDF до ответа»; воркеры импортируют этот модуль по имени (папка модели
добавляется в PYTHONPATH дочерних процессов).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FACTOR = 32
_LOCAL = threading.local()       # открытый документ — свой у каждого потока (PyMuPDF не потокобезопасен)
_POOL = None
_POOL_LOCK = threading.Lock()


def _open(pdf_path: str):
    import pymupdf

    if getattr(_LOCAL, "path", None) != pdf_path:
        if getattr(_LOCAL, "doc", None) is not None:
            _LOCAL.doc.close()
        _LOCAL.doc, _LOCAL.path = pymupdf.open(pdf_path), pdf_path
    return _LOCAL.doc


def render(pdf_path: str, page_no: int, side: int, mode: str) -> np.ndarray:
    import pymupdf

    page = _open(pdf_path)[page_no - 1]
    w, h = page.rect.width or 1.0, page.rect.height or 1.0
    if mode == "square":
        W = Hh = side
    else:
        z = side / max(w, h)
        W, Hh = max(FACTOR, round(w * z / FACTOR) * FACTOR), max(FACTOR, round(h * z / FACTOR) * FACTOR)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(W / w, Hh / h), colorspace=pymupdf.csRGB, alpha=False)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3]
    a = a.reshape(pix.height, pix.width, 3)[:Hh, :W]
    if a.shape[:2] != (Hh, W):
        a = np.pad(a, ((0, Hh - a.shape[0]), (0, W - a.shape[1]), (0, 0)), constant_values=255)
    return np.ascontiguousarray(a)


def render_safe(task: tuple) -> np.ndarray:
    pdf_path, page_no, side, mode = task
    try:
        return render(pdf_path, int(page_no), side, mode)
    except Exception:  # noqa: BLE001 — битая страница: белый лист
        return np.full((side, side if mode == "square" else side * 3 // 4 // FACTOR * FACTOR, 3), 255, np.uint8)


def pool(workers: int):
    global _POOL
    with _POOL_LOCK:
        if _POOL is None and workers > 1:
            import multiprocessing as mp

            pp = os.environ.get("PYTHONPATH", "")
            if str(HERE) not in pp.split(os.pathsep):
                os.environ["PYTHONPATH"] = str(HERE) + (os.pathsep + pp if pp else "")
            _POOL = mp.get_context("spawn").Pool(workers)
    return _POOL


def render_many(tasks: list[tuple], workers: int = 0) -> list[np.ndarray]:
    """Страницы по порядку; workers > 1 — пул процессов (сортировка по файлу для кэша документа в воркере).
    page_no — с 1: 0 и отрицательные PyMuPDF молча превращает в страницы с конца, поэтому они отвергаются."""
    bad = [t for t in tasks if int(t[1]) < 1]
    if bad:
        raise ValueError(f"page_no должен быть ≥ 1 (страницы с 1): {bad[:3]}")
    if workers <= 1 or len(tasks) < 8:
        return [render_safe(t) for t in tasks]
    p = pool(workers)
    order = sorted(range(len(tasks)), key=lambda i: (tasks[i][0], tasks[i][1]))
    out: list = [None] * len(tasks)
    for i, img in zip(order, p.imap(render_safe, [tasks[i] for i in order], chunksize=4)):
        out[i] = img
    return out
