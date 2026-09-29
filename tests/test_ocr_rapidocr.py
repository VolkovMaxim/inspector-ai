"""OCR_ENGINE=rapidocr: текст сканов — слова RapidOCR в порядке чтения; страницы-кандидаты извлечения перечитывает
модель (этап reocr), и дальше по ним работает её текст."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from inspector.models import Page
from inspector.pipeline import bbox as bbox_mod
from inspector.pipeline.bbox import words_to_text
from inspector.pipeline.orient import to_upright, to_visible
from inspector.pipeline.select import Candidate


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_to_upright_inverts_to_visible(k):
    box = [0.12, 0.34, 0.56, 0.78]
    assert to_upright(to_visible(box, k), k) == pytest.approx(box)


def test_words_to_text_reading_order():
    words = [[0.50, 0.101, 0.60, 0.12, "Акт"], [0.10, 0.10, 0.20, 0.12, "Приложение"],
             [0.10, 0.30, 0.20, 0.32, "бетон"], [0.25, 0.305, 0.35, 0.325, "В25"], [0.4, 0.5, 0.5, 0.52, " "]]
    assert words_to_text(words) == "Приложение Акт\nбетон В25"


def test_words_to_text_rotated_scan_is_read_upright():
    upright = [[0.10, 0.10, 0.30, 0.12, "первая"], [0.35, 0.10, 0.50, 0.12, "строка"],
               [0.10, 0.20, 0.30, 0.22, "вторая"]]
    visible = [[*to_visible(w[:4], 1), w[4]] for w in upright]      # скан повёрнут: в видимых координатах столбцы
    assert words_to_text(visible, angle=90) == "первая строка\nвторая"
    assert words_to_text(visible, angle=0) != "первая строка\nвторая"


def test_words_to_text_keeps_one_copy_of_a_word_read_by_two_overlapping_tiles():
    """Лист А3 и больше RapidOCR читает плитками с перекрытием: слово из зоны перекрытия приходит от обеих плиток
    (рамки почти совпадают). В тексте оно должно быть один раз; то же значение в другом месте листа — настоящий повтор."""
    line = [[0.10, 0.50, 0.20, 0.52, "бетон"], [0.21, 0.50, 0.30, 0.52, "класса"], [0.31, 0.50, 0.36, 0.52, "В30"]]
    other_tile = [[x0 + 0.001, y0 + 0.001, x1 + 0.001, y1 + 0.001, t] for x0, y0, x1, y1, t in line]
    table = [[0.31, 0.60, 0.36, 0.62, "В30"], [0.41, 0.60, 0.46, 0.62, "В30"]]
    assert words_to_text(line + other_tile + table) == "бетон класса В30\nВ30 В30"


def test_words_to_text_empty():
    assert words_to_text([]) == ""


@pytest.fixture()
def rapid_settings(settings, monkeypatch):
    from inspector import config as config_module

    monkeypatch.setenv("OCR_ENGINE", "rapidocr")
    monkeypatch.setenv("BBOX_ENGINE", "rapidocr")
    config_module.get_settings.cache_clear()
    s = config_module.get_settings()
    yield s
    config_module.get_settings.cache_clear()


def fake_words(todo, gpu=False, orient=True, workers=0, pool=None, gpu_workers=4):
    """Вместо RapidOCR: у страницы 1 — слова, страница 2 — чистый лист; кэш с углом 0, как пишет page_words."""
    out = {}
    for pdf, no, cache in todo:
        words = [[0.1, 0.1, 0.3, 0.12, "арматура"], [0.35, 0.1, 0.45, 0.12, "А240С"]] if no == 1 else []
        with open(_mk(cache), "w", encoding="utf-8") as f:
            json.dump({"angle": 0, "words": words}, f)
        out[(pdf, no)] = words
    return out


def _mk(path: str) -> str:
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


def _scan_process(db):
    from inspector.models import ConstructionObject, File, Process

    obj = ConstructionObject(object_code="OBJ-T")
    db.add(obj)
    db.flush()
    proc = Process(object_pk=obj.id)
    db.add(proc)
    db.flush()
    f = File(process_id=proc.id, file_id="F1", original_name="scan.pdf", sha256="ab" * 32, pdf_path="/tmp/scan.pdf",
             status="ok", doc_stage="ID")
    db.add(f)
    db.flush()
    for no in (1, 2):
        db.add(Page(file_pk=f.id, page_no=no, need_ocr=True, text_source="none"))
    db.flush()
    return proc, f


def test_rapidocr_stage_fills_text_and_counts_blank_pages(rapid_settings, db, monkeypatch):
    from inspector.pipeline.ocr import run_ocr

    monkeypatch.setattr(bbox_mod, "words_for_pages", fake_words)
    proc, f = _scan_process(db)
    stats = run_ocr(db, proc, 1, rapid_settings, None)
    pages = {p.page_no: p for p in db.scalars(select(Page).where(Page.file_pk == f.id))}
    assert pages[1].text == "арматура А240С" and pages[1].text_source == "rapidocr"
    assert pages[2].text == "" and pages[2].text_source == "none"
    assert stats["recognized"] == 1 and stats["empty"] == 1 and stats["errors"] == 0


def test_reocr_rereads_only_selected_rapidocr_pages(rapid_settings, db, monkeypatch):
    from inspector.pipeline import ocr as ocr_mod

    monkeypatch.setattr(bbox_mod, "words_for_pages", fake_words)
    proc, f = _scan_process(db)
    ocr_mod.run_ocr(db, proc, 1, rapid_settings, None)
    seen = []

    def fake_vlm(session, pages, files, settings, client, stats, progress=None):
        for p in pages:
            seen.append(p.page_no)
            p.text, p.text_source = "Арматура класса А240С по ГОСТ 5781-82", "ocr"
            stats["recognized"] += 1
        return stats

    monkeypatch.setattr(ocr_mod, "vlm_text", fake_vlm)
    cands = [Candidate("KR-057", "ID", f.id, 1, 1.0), Candidate("KR-057", "ID", f.id, 1, 0.5)]
    stats = ocr_mod.run_reocr(db, proc, cands, rapid_settings, client=object())
    assert seen == [1] and stats["pages"] == 1
    page1 = db.scalars(select(Page).where(Page.file_pk == f.id, Page.page_no == 1)).one()
    assert page1.text_source == "ocr" and "ГОСТ 5781-82" in page1.text
    assert ocr_mod.run_reocr(db, proc, cands, rapid_settings, client=object())["pages"] == 0   # уже прочитана


def test_reocr_without_model_keeps_rapidocr_text(rapid_settings, db, monkeypatch):
    from inspector.pipeline import ocr as ocr_mod

    monkeypatch.setattr(bbox_mod, "words_for_pages", fake_words)
    proc, f = _scan_process(db)
    ocr_mod.run_ocr(db, proc, 1, rapid_settings, None)
    stats = ocr_mod.run_reocr(db, proc, [Candidate("KR-057", "ID", f.id, 1, 1.0)], rapid_settings, client=None)
    page1 = db.scalars(select(Page).where(Page.file_pk == f.id, Page.page_no == 1)).one()
    assert stats["recognized"] == 0 and page1.text_source == "rapidocr"


def test_reocr_partial_answer_keeps_full_rapidocr_text(rapid_settings, db, tmp_path):
    """Лист из нескольких плиток (А3 и больше), модель ответила не на все плитки (503). Неполный текст модели не должен
    вытеснять полный текст RapidOCR; страница остаётся rapidocr и перечитается, когда модель ответит на всё."""
    import httpx

    from inspector.llm import ocr_client
    from inspector.models import ConstructionObject, File, Process
    from inspector.pipeline.ocr import render_tiles, run_reocr
    from inspector.testing import make_scan_pdf

    settings = rapid_settings.model_copy(update={"llm_retries": 1})
    obj = ConstructionObject(object_code="OBJ-TALL")
    db.add(obj)
    db.flush()
    proc = Process(object_pk=obj.id)
    db.add(proc)
    db.flush()
    pdf = make_scan_pdf(tmp_path / "tall.pdf", ["верх листа\n" * 40 + "низ листа"], size=(595, 1400))
    f = File(process_id=proc.id, file_id="F1", original_name="tall.pdf", sha256="cd" * 32, pdf_path=str(pdf),
             status="ok", doc_stage="RD")
    db.add(f)
    db.flush()
    rapid = "Арматура А500С\nсредняя часть листа\nБетон класса В30 W8 F150"
    page = Page(file_pk=f.id, page_no=1, width=595, height=1400, need_ocr=True, text_source="rapidocr", text=rapid)
    db.add(page)
    db.flush()
    assert len(render_tiles(str(pdf), 1)) == 3
    calls = {"n": 0}

    def first_tile_only(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] > 1:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "верх листа"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    def all_tiles(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "плитка модели"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    cand = [Candidate("KR-055", "RD", f.id, 1, 1.0)]
    stats = run_reocr(db, proc, cand, settings, ocr_client(settings, httpx.MockTransport(first_tile_only)))
    assert page.text == rapid and page.text_source == "rapidocr"
    assert stats["recognized"] == 0 and stats["errors"] == 1
    again = run_reocr(db, proc, cand, settings, ocr_client(settings, httpx.MockTransport(all_tiles)))
    assert again["pages"] == 1 and again["recognized"] == 1
    assert page.text_source == "ocr" and page.text.count("плитка модели") == 3


def test_model_version_names_rapidocr_and_reocr(rapid_settings):
    assert "ocr=rapidocr+reocr:" in rapid_settings.model_version


def test_gpu_failure_on_a_page_retries_it_on_cpu(monkeypatch):
    """Большой лист не влез в предел видеопамяти onnxruntime — страницу считает CPU, а не остаётся без текста."""
    from inspector.pipeline import bbox

    calls = []

    def fake(pdf, page, cache, gpu, orient):
        calls.append(gpu)
        if gpu:
            raise RuntimeError("Available memory of 99 MB is smaller than requested 124 MB")
        return [[0.1, 0.1, 0.2, 0.2, "бетон"]]

    monkeypatch.setattr(bbox, "page_words", fake)
    assert bbox.page_words_safe("a.pdf", 1, "c.json", gpu=True) == [[0.1, 0.1, 0.2, 0.2, "бетон"]]
    assert calls == [True, False]


def test_gpu_requested_but_cuda_missing_goes_to_cpu_pool(monkeypatch):
    """RAPIDOCR_GPU=true без CUDA для onnxruntime: не одним процессом «на GPU» (фактически CPU), а обычным пулом CPU."""
    from inspector.pipeline import bbox

    monkeypatch.setattr(bbox, "gpu_ready", lambda: False)
    seen = []

    class Pool:
        def __init__(self, max_workers, mp_context):
            seen.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, *cols):
            return [[] for _ in cols[0]]

    monkeypatch.setattr(bbox, "ProcessPoolExecutor", Pool)
    out = bbox.words_for_pages([("a.pdf", 1, "c1.json"), ("a.pdf", 2, "c2.json")], gpu=True, workers=2)
    assert seen == [2] and out == {("a.pdf", 1): [], ("a.pdf", 2): []}


def test_gpu_ready_uses_a_pool_of_gpu_workers_for_the_whole_stage(monkeypatch):
    """GPU: пул RAPIDOCR_GPU_WORKERS процессов (один процесс на GPU едва быстрее пула CPU: 0,89 против 0,65 скана/с)."""
    from inspector.pipeline import bbox

    monkeypatch.setattr(bbox, "gpu_ready", lambda: True)
    seen = []

    class Pool:
        def __init__(self, max_workers, mp_context):
            seen.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, pdfs, pages, caches, gpus, orients):
            seen.append(list(gpus))
            return [[] for _ in pdfs]

    monkeypatch.setattr(bbox, "ProcessPoolExecutor", Pool)
    todo = [("a.pdf", n, f"c{n}.json") for n in range(1, 11)]
    with bbox.ocr_pool(True, len(todo), workers=7, gpu_workers=4) as pool:
        assert pool[1] is True
        bbox.words_for_pages(todo[:5], pool=pool)
        bbox.words_for_pages(todo[5:], pool=pool)
    assert seen == [4, [True] * 5, [True] * 5]              # один пул на этап, страницы — на GPU


def test_gpu_engine_skips_exhaustive_cudnn_search(monkeypatch):
    """Движок на GPU: cuDNN без перебора алгоритмов на каждую форму входа (EXHAUSTIVE — в разы медленнее на H100),
    с пределом памяти; движок на CPU этих ключей не получает."""
    import rapidocr

    from inspector.pipeline import bbox

    seen = {}

    class Fake:
        def __init__(self, params=None):
            seen[bool(params.get("EngineConfig.onnxruntime.use_cuda"))] = params

    monkeypatch.setattr(rapidocr, "RapidOCR", Fake)
    monkeypatch.setattr(bbox, "_cuda_providers", lambda eng: True)
    monkeypatch.setattr(bbox, "_ENGINES", {})
    bbox._engine(True)
    bbox._engine(False)
    gpu, cpu = seen[True], seen[False]
    assert gpu["EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search"] == "DEFAULT"
    assert gpu["EngineConfig.onnxruntime.cuda_ep_cfg.gpu_mem_limit"] > 0
    assert not any("cuda_ep_cfg" in k for k in cpu)
