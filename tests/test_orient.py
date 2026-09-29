"""Выпрямление повёрнутых сканов перед RapidOCR (pipeline/orient.py): перевод рамок обратно в видимую страницу, правило
«A и B согласны», оба способа на странице в 4 положениях, сквозной page_words на скане, повёрнутом на 90°."""
from __future__ import annotations

import json

import numpy as np
import pytest

from inspector.pipeline import orient

LINES = [f"Акт освидетельствования скрытых работ № {n} от {10 + n}.03.2026, корпус {n % 3 + 1}" if n % 4 == 0 else
         f"Арматура класса А500С по ГОСТ 34028-2016 диаметр {10 + 2 * (n % 5)} мм шаг {100 + 50 * (n % 3)}"
         if n % 4 == 1 else
         f"Бетон класса В{25 + 5 * (n % 2)} W8 F150, защитный слой {30 + 5 * (n % 4)} мм, захватка {n}" if n % 4 == 2 else
         f"Работы выполнены в осях {n % 9 + 1}-{n % 9 + 3} / А-Ж по проекту НВС-2025.03-КЖ{n % 4}"
         for n in range(36)]


def forward(box: list[float], k: int) -> list[float]:
    """Доли видимой страницы -> доли страницы, повёрнутой на 90·k по часовой (независимо от orient.to_visible)."""
    x0, y0, x1, y1 = box
    for _ in range(k):                  # 90° по часовой: (x, y) -> (1 − y, x)
        x0, y0, x1, y1 = 1 - y1, x0, 1 - y0, x1
    return [x0, y0, x1, y1]


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_to_visible_inverts_rotation(k):
    box = [0.1, 0.2, 0.3, 0.25]
    assert orient.to_visible(forward(box, k), k) == pytest.approx(box)


def test_forward_matches_pdf_rotate(tmp_path):
    """forward() — то же, что /Rotate + 90 у PyMuPDF: слова слоя страницы до и после set_rotation(90)."""
    import pymupdf

    from inspector.pipeline.geometry import layer_words
    from inspector.testing import make_pdf

    page = pymupdf.open(make_pdf(tmp_path / "a.pdf", ["ГОСТ 34028-2016 А500С"]))[0]
    before = {w[4]: list(w[:4]) for w in layer_words(page)}
    page.set_rotation(90)
    after = {w[4]: list(w[:4]) for w in layer_words(page)}
    for word, box in before.items():
        assert forward(box, 1) == pytest.approx(after[word], abs=1e-4)


@pytest.mark.parametrize("a,b,expected,b_called", [(0, 1, 0, False), (1, 1, 1, True), (1, 3, 0, True),
                                                   (2, 2, 2, True), (3, 0, 0, True)])
def test_rule_rotates_only_when_both_agree(monkeypatch, a, b, expected, b_called):
    calls = []
    monkeypatch.setattr(orient, "page_image", lambda page: np.zeros((8, 8), np.uint8))
    monkeypatch.setattr(orient, "votes_angle", lambda gray, engine: a)
    monkeypatch.setattr(orient, "model_angle", lambda gray: calls.append(1) or b)
    assert orient.page_angle(object(), engine=None) == expected
    assert bool(calls) == b_called


@pytest.fixture(scope="module")
def text_page(tmp_path_factory):
    import pymupdf

    from inspector.testing import make_pdf

    path = make_pdf(tmp_path_factory.mktemp("orient") / "page.pdf", ["\n".join(LINES)])
    return orient.page_image(pymupdf.open(path)[0])


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_both_methods_find_angle(text_page, k):
    pytest.importorskip("rapidocr")
    from inspector.pipeline.bbox import _engine

    img = np.ascontiguousarray(np.rot90(text_page, k))
    assert orient.votes_angle(img, _engine()) == k
    assert orient.model_angle(img) == k


def test_page_words_straightens_rotated_scan(tmp_path):
    """Скан листа, повёрнутого на 90° по часовой: угол 270 (k 3), слова находятся, рамки — на месте слов в видимой
    странице (сверка со словами слоя того же повёрнутого листа)."""
    pytest.importorskip("rapidocr")
    import pymupdf

    from inspector.pipeline import bbox
    from inspector.pipeline.geometry import layer_words
    from inspector.testing import make_pdf, make_scan_pdf

    text = "\n".join(LINES)
    scan = make_scan_pdf(tmp_path / "scan.pdf", [text], rotate=90)
    ref = layer_words(pymupdf.open(make_pdf(tmp_path / "ref.pdf", [text], rotate=90))[0])
    words = bbox.page_words(str(scan), 1, str(tmp_path / "boxes.json"), orient=True)
    assert json.loads((tmp_path / "boxes.json").read_text(encoding="utf-8"))["angle"] == 270
    hits = [w for w in words if "34028" in w[4]]
    assert len(hits) >= 5                      # 9 строк с номером ГОСТ; без выпрямления рамки не те
    expected = [w for w in ref if "34028" in w[4]]
    for x0, y0, x1, y1, _ in hits:
        assert x1 - x0 < y1 - y0               # слово на повёрнутом листе стоит вертикально
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        assert any(e[0] - 0.01 <= cx <= e[2] + 0.01 and e[1] - 0.01 <= cy <= e[3] + 0.01 for e in expected)


def test_page_words_upright_scan_unchanged(tmp_path):
    """Прямой скан: угол 0, слова те же, что без выпрямления."""
    pytest.importorskip("rapidocr")
    from inspector.pipeline import bbox
    from inspector.testing import make_scan_pdf

    scan = make_scan_pdf(tmp_path / "scan.pdf", ["\n".join(LINES)])
    with_orient = bbox.page_words(str(scan), 1, str(tmp_path / "o.json"), orient=True)
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["angle"] == 0
    assert with_orient == bbox.page_words(str(scan), 1, str(tmp_path / "plain.json"), orient=False)
