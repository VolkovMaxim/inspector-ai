"""Обучаемый отбор F40 (pipeline/router.py): артефакт сохраняется и читается с проверкой SHA-256; слияние со списком
по словам — страница, которую видит только модель, попадает в кандидаты; у параметра без модели — только слова."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer

from inspector.pipeline import router as RT
from inspector.pipeline.select import PageInfo

PARAMS = [{"code": "KR-057", "parameter_name": "Класс арматуры форшахты", "unit": "—", "source_pd": "", "source_rd": "",
           "source_id": ""},
          {"code": "PZ-007", "parameter_name": "Этажность надземная", "unit": "ед.", "source_pd": "", "source_rd": "",
           "source_id": ""}]


def pages() -> list[PageInfo]:
    return [PageInfo(1, 1, "PD", "Класс арматуры форшахты А400, этажность 9"),
            PageInfo(1, 2, "PD", "Ведомость расхода стали: стержни d12 A-III"),     # слов названия нет — только модель
            PageInfo(1, 3, "PD", "Пояснительная записка. Общие сведения об объекте"),
            PageInfo(2, 1, "ID", "", "ПУСТАЯ")]


def tiny_model(tmp_path) -> RT.RouterModel:
    """Модель на одном параметре (KR-057), где стр. 2 получает наибольшую оценку — через её слова «стали», «стержни»."""
    texts = [p.text for p in pages()]
    metas = [RT.meta_tokens(p.stage, p.page_class, p.other_classes, p.section, p.text) for p in pages()]
    wv = TfidfVectorizer(**RT.WORD_PARAMS, dtype=np.float32).fit(texts)
    cv = TfidfVectorizer(**RT.CHAR_PARAMS, dtype=np.float32).fit(texts)
    mv = TfidfVectorizer(**RT.META_PARAMS, dtype=np.float32).fit(metas)
    n_w, n_c, n_m = len(wv.vocabulary_), len(cv.vocabulary_), len(mv.vocabulary_)
    coef = np.zeros((1, n_w + n_c + n_m), dtype=np.float32)
    for word in ("стали", "стержни"):
        coef[0, wv.vocabulary_[word]] = 5.0
    vocab = {name: [t for t, _ in sorted(v.vocabulary_.items(), key=lambda x: x[1])]
             for name, v in (("words", wv), ("chars", cv), ("meta", mv))}
    RT.save_router(tmp_path, vocab, {"words": wv.idf_, "chars": cv.idf_, "meta": mv.idf_}, csr_matrix(coef),
                   np.array([-1.0]), ["KR-057"], {"name": "test"})
    return RT.load_router_dir(tmp_path)


def test_artifact_roundtrip_and_version(tmp_path):
    m = tiny_model(tmp_path)
    assert m.version.startswith("router@") and m.codes == ["KR-057"]
    s = m.scores(pages())
    assert s.shape == (4, 1) and int(np.argmax(s[:, 0])) == 1


def test_tampered_artifact_is_rejected(tmp_path):
    tiny_model(tmp_path)
    (tmp_path / "coef.npz").write_bytes(b"broken")
    with pytest.raises(ValueError, match="SHA-256"):
        RT.load_router_dir(tmp_path)


def test_fused_selection_adds_page_seen_only_by_model(tmp_path):
    m = tiny_model(tmp_path)
    got = RT.select_fused(pages(), PARAMS, top_k=2, idf_pages=None, model=m)
    kr = [(c.file_pk, c.page_no) for c in got if c.param_code == "KR-057" and c.stage == "PD"]
    assert (1, 2) in kr and (1, 1) in kr and len(kr) == 2
    assert not any(c.page_no == 1 and c.file_pk == 2 for c in got)          # страница без текста не кандидат
    pz = [(c.file_pk, c.page_no) for c in got if c.param_code == "PZ-007"]
    lexical = [(c.file_pk, c.page_no) for c in RT.select_candidates(pages(), PARAMS[1:], 2)]
    assert pz == lexical                                                     # параметр без модели — только слова


def test_rrf_ties_are_deterministic():
    assert RT.rrf([["b", "a"], ["a", "b"]], 2) == RT.rrf([["a", "b"], ["b", "a"]], 2)
    assert [k for k, _ in RT.rrf([["x"], ["y"]], 2)] == ["x", "y"]


def test_missing_artifact_means_lexical(tmp_path):
    RT.load_router.cache_clear()
    assert RT.load_router(str(tmp_path / "nothing")) is None
