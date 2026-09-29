"""Общие фикстуры: изолированный DATA_DIR и SQLite на тест, засеянная матрица и демо-учётки."""
from __future__ import annotations

import pytest

from inspector import config as config_module
from inspector import db as db_module


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LLM_URL", "http://fake-llm/v1")
    monkeypatch.setenv("BBOX_ENGINE", "none")
    # сквозные тесты написаны на OCR моделью и лексический отбор K = 12; RapidOCR + reocr и отбор F40 —
    # в test_ocr_rapidocr.py и test_router.py
    monkeypatch.setenv("OCR_ENGINE", "vlm")
    monkeypatch.setenv("SELECT_MODE", "lexical")
    monkeypatch.setenv("TOP_K_PAGES", "12")
    config_module.get_settings.cache_clear()
    s = config_module.get_settings()
    yield s
    config_module.get_settings.cache_clear()


@pytest.fixture()
def db(settings):
    from inspector.seed import seed_all

    db_module.init_engine(settings.db_url)
    session = db_module.session_factory()()
    seed_all(session, settings)
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def example_process(db):
    from inspector.testing import build_example_process

    p = build_example_process(db)
    db.commit()
    return p
