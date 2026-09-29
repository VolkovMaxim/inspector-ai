"""NUL (0x00) в строках: PostgreSQL не хранит его в текстовых полях — движок Postgres вычищает его из параметров
перед записью; текст слоя PDF — сразу при разборе страницы."""
from __future__ import annotations

from sqlalchemy import event

from inspector import db as db_module


def test_strip_nul_recurses_and_keeps_other_values():
    params = [{"text": "а\x00б", "n": 3, "raw": b"\x00"}, ("x\x00", None, ["y\x00"])]
    assert db_module.strip_nul(params) == [{"text": "аб", "n": 3, "raw": b"\x00"}, ("x", None, ["y"])]
    s = "без нуля"
    assert db_module.strip_nul(s) is s


def test_postgres_engine_strips_nul_sqlite_untouched():
    pg = db_module.make_engine("postgresql+psycopg://u:p@localhost:5432/x")        # соединения ещё нет
    assert any(event.contains(pg, "before_cursor_execute", fn) for fn in
               [f for f in pg.dispatch.before_cursor_execute])
    lite = db_module.make_engine("sqlite:///:memory:")
    assert not list(lite.dispatch.before_cursor_execute)


def test_page_layer_text_has_no_nul(tmp_path, monkeypatch):
    """Слой PDF с NUL (бывает в старых PDF): в тексте и словах страницы его нет."""
    import pymupdf

    from inspector.pipeline import geometry, pages as pages_mod

    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "nul.pdf"
    doc.save(path)
    line = "Пояснительная записка\x00 раздел 1, общие данные объекта "
    real = pymupdf.Page.get_text
    monkeypatch.setattr(pymupdf.Page, "get_text",
                        lambda self, kind="text", *a, **k: line * 10 if kind == "text" else real(self, kind, *a, **k))
    monkeypatch.setattr(geometry, "layer_words",
                        lambda page: [(10.0 * i, 10.0, 10.0 * i + 8, 20.0, "слово\x00") for i in range(60)])
    rows = pages_mod.index_pdf(str(path), str(tmp_path / "words.json"))
    assert rows[0]["has_layer"] and rows[0]["text"] and "\x00" not in rows[0]["text"]
    assert "\x00" not in (tmp_path / "words.json").read_text(encoding="utf-8").replace("\\u0000", "\x00")
