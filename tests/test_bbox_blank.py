"""Рамки RapidOCR (pipeline/bbox.py): пустая плитка не роняет страницу, а сбой одной страницы не оставляет без рамок
остальные. У пустой плитки RapidOCR 3.9.2 отдаёт word_results = (('', 1.0, None),) — раньше это давало ValueError, и
words_for_pages (pool.map) терял рамки всех OCR-страниц процесса."""
from __future__ import annotations

from inspector.pipeline import bbox
from inspector.testing import make_pdf, make_scan_pdf


def test_blank_page_gives_no_words(tmp_path):
    pdf = make_pdf(tmp_path / "blank.pdf", [""])
    assert bbox.page_words(str(pdf), 1, str(tmp_path / "c.json"), orient=False) == []


def test_tall_scan_with_blank_lower_tile_keeps_words(tmp_path):
    """Лист в две плитки: текст сверху, нижняя плитка пустая."""
    pdf = make_scan_pdf(tmp_path / "tall.pdf", ["Бетон класса В30 W8 F150"], size=(595, 1400))
    words = bbox.page_words(str(pdf), 1, str(tmp_path / "c.json"), orient=False)
    assert any("В30" in w[4] or "B30" in w[4] for w in words)


def test_one_broken_page_does_not_drop_the_others(tmp_path):
    good = make_scan_pdf(tmp_path / "good.pdf", ["Арматура класса А500С"])
    todo = [(str(good), 1, str(tmp_path / "g.json")), (str(tmp_path / "нет.pdf"), 1, str(tmp_path / "b.json"))]
    got = bbox.words_for_pages(todo, gpu=False, orient=False)
    assert got[(str(tmp_path / "нет.pdf"), 1)] == []
    assert any("А500" in w[4] or "A500" in w[4] for w in got[(str(good), 1)])
