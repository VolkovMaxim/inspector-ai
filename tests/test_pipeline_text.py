import pymupdf

from inspector.pipeline.geometry import layer_words, locate
from inspector.pipeline.textutils import (fix_mojibake, layer_quality, looks_garbage, snippet_supported, trim_loop)
from inspector.testing import make_pdf


def test_trim_loop_cuts_periodic_tail_and_keeps_normal_text():
    looped = "Акт № 5 " + "бетон В30 " * 40 + "бет"
    out = trim_loop(looped)
    assert out.count("бетон В30") == 1 and out.startswith("Акт № 5")
    normal = "Таблица 1\nВ30 | В30 | В25\nИтого 3"
    assert trim_loop(normal) == normal
    assert trim_loop("х" * 5 + "未被确定或" * 20).count("未被确定或") == 1


def test_layer_quality_and_mojibake():
    good = "Пояснительная записка. Технико-экономические показатели объекта строительства, площадь застройки 1250 м2"
    assert layer_quality(good) > 0.8
    assert layer_quality("Вɡаɦ. ɢɧв. ɩɨɞɩ ɢ ɞɚɬɚ ɢɧɜ ɧɨɦɟɪ") == 0.0
    assert fix_mojibake("Äîêóìåíò î êà÷åñòâå ïðîäóêöèè") == "Документ о качестве продукции"
    assert fix_mojibake(good) is None


def test_garbage_guard_and_snippet():
    assert looks_garbage("未被确定或" * 20 + " abc")
    assert not looks_garbage("Арматура класса А240С, бетон В30 W8 F150, толщина плиты 600 мм")
    assert snippet_supported("класса  А240С", "Арматура класса А240С диаметр 12")
    assert not snippet_supported("класса А500С", "Арматура класса А240С диаметр 12")


def test_locate_value_inside_snippet_on_rotated_page(tmp_path):
    p = make_pdf(tmp_path / "r.pdf", ["Шапка документа\nАрматура класса А240С диаметр 12 мм\nБетон В30"], rotate=90)
    page = pymupdf.open(p)[0]
    words = layer_words(page)
    assert all(0 <= w[0] <= w[2] <= 1 and 0 <= w[1] <= w[3] <= 1 for w in words)
    loc = locate("А240С", "Арматура класса А240С диаметр 12 мм", words)
    assert loc.how == "value" and loc.bbox is not None
    x0, y0, x1, y1 = loc.bbox
    # поворот 90°: строки идут вертикально — рамка значения узкая по x и вытянута по y
    assert (x1 - x0) < (y1 - y0)
    # проверка по картинке: в прямоугольнике рамки на отрисованной странице есть тёмные пиксели
    pix = page.get_pixmap(dpi=100)
    X0, Y0, X1, Y1 = int(x0 * pix.width), int(y0 * pix.height), int(x1 * pix.width), int(y1 * pix.height)
    dark = sum(1 for x in range(X0, X1) for y in range(Y0, Y1) if pix.pixel(x, y)[0] < 128)
    assert dark > 10


def test_locate_without_snippet_needs_unique_value(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", ["Бетон В30 для плиты\nБетон В25 для стен\nВ30 повторно"])
    words = layer_words(pymupdf.open(p)[0])
    assert locate("В25", "", words).how == "value_only"
    assert locate("В40", "", words).bbox is None


def test_project_key_and_superseded_editions():
    from inspector.models import File
    from inspector.pipeline.documents import project_key, superseded_editions

    assert project_key("П-2025-04.266-АР") == "2025-04-266"
    assert project_key("ЖС-РД-270121-П-АР") == "270121"
    assert project_key("АР") is None
    files = [File(id=i, doc_stage=st, document_code=c) for i, (st, c) in enumerate([
        ("RD", "П-2025-04.266-АР2"), ("RD", "П-2025-04.266-КЖ"),
        ("PD", "П-2025-04.266-АР"), ("PD", "П-2025-04.266-КР"), ("PD", "П-2025-04.266-ПЗ"),
        ("PD", "ЖС-РД-270121-П-АР"), ("PD", "ЖС-РД-270121-П-КР"), ("PD", "ЖС-РД-270121-П-ПЗ"),
        ("PD", "ЭКС-461-23")])]
    base, old = superseded_editions(files)
    assert base == "2025-04-266" and old == [5, 6, 7]       # акт экспертизы (одиночный ключ) — не редакция


def test_number_of_and_delta_keep_designation_hyphens():
    from inspector.pipeline.compare import delta_text
    from inspector.pipeline.textutils import number_of

    assert number_of("EI-15") == 15 and number_of("-5 %") == -5 and number_of("1250,5 м2") == 1250.5
    assert number_of("отметка −0,150") == -0.15 and number_of("В30") == 30
    assert delta_text("EI30", "EI-15") == "EI30 → EI-15 (Δ -15; -50.0 %)"
