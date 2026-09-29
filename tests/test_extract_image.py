"""Флаг EXTRACT_WITH_IMAGE (pipeline/extract.py): на OCR-страницах в извлечение идёт картинка страницы, на страницах со
слоем — только текст; без флага запросы и ключи кэша прежние; версия этапа в протоколе помечает вариант."""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from inspector.llm import ChatClient
from inspector.pipeline import extract as X
from tests.test_e2e_fake import pipeline_env  # noqa: F401 — фикстура конвейера (объект с ПД, РД и сканом ИД)

PARAM = {"code": "KR-057", "parameter_name": "Класс арматуры", "unit": None}
OCR_TEXT = "Арматура класса А500С по ГОСТ 34028-2016"


def recording_client(seen: list[dict]) -> ChatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"items": []}'}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    return ChatClient("http://fake/v1", "qwen", transport=httpx.MockTransport(handler), retries=1)


@pytest.fixture()
def scan_pdf(tmp_path):
    from inspector.testing import make_scan_pdf

    path = tmp_path / "scan.pdf"
    make_scan_pdf(path, [OCR_TEXT])
    return str(path)


def test_cache_key_without_image_is_unchanged():
    old = hashlib.sha256(f"qwen|{X.PROMPT_VERSION}|KR-057|{OCR_TEXT}".encode()).hexdigest()[:32]
    assert X.cache_key("qwen", ["KR-057"], OCR_TEXT) == old
    assert X.cache_key("qwen", ["KR-057"], OCR_TEXT, with_image=True) != old


def test_image_goes_only_with_ocr_pages(tmp_path, scan_pdf):
    seen: list[dict] = []
    calls = [(1, 1, "RD", OCR_TEXT, [PARAM], scan_pdf),          # OCR-страница, флаг включён
             (2, 1, "RD", "Бетон В30", [PARAM], None)]              # страница со слоем
    res = asyncio.run(X.extract_calls(recording_client(seen), calls, tmp_path))
    assert [r[2] for r in res] == [None, None] and len(seen) == 2
    by_text = {json.dumps(b["messages"][-1]["content"], ensure_ascii=False): b for b in seen}
    image_msgs = [b for b in seen if isinstance(b["messages"][-1]["content"], list)]
    assert len(image_msgs) == 1
    parts = image_msgs[0]["messages"][-1]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[-1]["type"] == "text" and parts[-1]["text"].startswith(X.IMAGE_NOTE) and OCR_TEXT in parts[-1]["text"]
    text_msgs = [b for b in by_text.values() if isinstance(b["messages"][-1]["content"], str)]
    assert len(text_msgs) == 1 and X.IMAGE_NOTE not in text_msgs[0]["messages"][-1]["content"]
    # кэш: вариант с картинкой — отдельный файл, текстовый — прежний ключ
    assert (tmp_path / f"{X.cache_key('qwen', ['KR-057'], OCR_TEXT, True)}.json").exists()
    assert (tmp_path / f"{X.cache_key('qwen', ['KR-057'], 'Бетон В30')}.json").exists()


def test_same_page_with_and_without_image_do_not_share_cache(tmp_path, scan_pdf):
    seen: list[dict] = []
    asyncio.run(X.extract_calls(recording_client(seen), [(1, 1, "RD", OCR_TEXT, [PARAM], None)], tmp_path))
    asyncio.run(X.extract_calls(recording_client(seen), [(1, 1, "RD", OCR_TEXT, [PARAM], scan_pdf)], tmp_path))
    assert len(seen) == 2                                     # второй вызов не взят из кэша первого


def test_broken_page_image_is_an_error_not_silent_text(tmp_path):
    seen: list[dict] = []
    res = asyncio.run(X.extract_calls(recording_client(seen), [(1, 1, "RD", OCR_TEXT, [PARAM],
                                                               str(tmp_path / "нет.pdf"))], tmp_path))
    assert res[0][1] is None and res[0][2].startswith("картинка страницы") and seen == []


def test_protocol_model_version_marks_image_variant(settings, monkeypatch):
    from inspector import config
    from inspector.protocol import model_version

    assert ";extract=g2;" in model_version()
    monkeypatch.setenv("EXTRACT_WITH_IMAGE", "true")
    config.get_settings.cache_clear()
    assert f";extract=g2+{X.IMAGE_TAG};" in model_version()
    config.get_settings.cache_clear()


def test_pipeline_with_flag_sends_image_only_for_scanned_page(db, pipeline_env, monkeypatch):  # noqa: F811
    """Весь конвейер на поддельной модели: картинка уходит только со скана (АОСР, текст из OCR), находка остаётся."""
    from sqlalchemy import select

    from inspector import config
    from inspector.models import Finding
    from tests.fake_llm import FakeLLM
    from tests.test_e2e_fake import ID_TEXT, run

    class Recording(FakeLLM):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.with_image: list[bool] = []

        def extract(self, body: dict) -> str:
            self.with_image.append(isinstance(body["messages"][-1]["content"], list))
            return super().extract(body)

    monkeypatch.setenv("EXTRACT_WITH_IMAGE", "true")
    config.get_settings.cache_clear()
    fake = Recording(ocr_text=ID_TEXT)
    assert run(db, pipeline_env, fake) is None
    assert fake.with_image.count(True) >= 1 and fake.with_image.count(False) >= 1
    f = db.scalars(select(Finding).where(Finding.param_code == "KR-057", Finding.active.is_(True),
                                         Finding.location != "")).one()
    assert f.actual_id and f.actual_id.startswith("А240")

