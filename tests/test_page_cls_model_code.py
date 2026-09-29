"""Код модели классификатора страниц (model_code/page_cls/heads.py): дообучение голов от текущих весов не выключает тип,
которого нет в новых данных; вес страницы из сервиса; модель без сохранённого набора дообучается на страницах сервиса,
и в новой версии обучающих данных нет. Модель-заглушка: головы настоящие, признаки — готовые векторы."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("torch")
CODE = Path(__file__).resolve().parents[1] / "model_code" / "page_cls"


def load_heads():
    spec = importlib.util.spec_from_file_location("page_cls_heads_under_test", CODE / "heads.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = load_heads()
LABELS = {"cls": ["А", "Б", "В"], "first": [False, True], "section": ["ПЗ", "НЕТ"], "quality": ["хорошее"]}
CENTERS = np.array([[4, 0, 0, 0], [0, 4, 0, 0], [0, 0, 4, 0]], np.float32)


def blobs(classes, n=60, seed=0):
    rng = np.random.default_rng(seed)
    y = np.repeat(classes, n)
    return (CENTERS[y] + rng.normal(0, 1, (len(y), 4))).astype(np.float32), y


def Y_of(y):
    n = len(y)
    return {"cls": np.asarray(y, np.int64), "first": np.full(n, -1), "section": np.full(n, -1), "quality": np.full(n, -1)}


def base_heads():
    X, y = blobs([0, 1, 2])
    return H.Heads.fit(X, Y_of(y), LABELS, lam=0.03, device="cpu")


def test_adapt_keeps_class_absent_from_new_data():
    base = base_heads()
    Xn, yn = blobs([0, 1], seed=1)                            # в новых документах типа «В» нет
    Xv, _ = blobs([2], n=20, seed=2)
    adapted = base.adapt(Xn, Y_of(yn), device="cpu")
    assert (adapted.proba(Xv)["cls"].argmax(1) == 2).mean() > 0.9
    scratch = H.Heads.fit(Xn, Y_of(yn), LABELS, lam=0.03, device="cpu")   # обучение с нуля тип «В» выключает
    assert (scratch.proba(Xv)["cls"].argmax(1) == 2).mean() == 0.0


def test_adapt_learns_corrections():
    base = base_heads()
    Xn, yn = blobs([0, 1], seed=3)
    X = np.vstack([Xn, CENTERS[[0]].repeat(30, 0)])           # правки: страницы у центра «А» на самом деле «Б»
    y = np.concatenate([yn, np.full(30, 1)])
    w = np.concatenate([np.ones(len(yn)), np.full(30, H.CORR_WEIGHT)]).astype(np.float32)
    adapted = base.adapt(X, Y_of(y), w, prior=0.001, device="cpu")
    assert adapted.proba(CENTERS[[0]])["cls"].argmax() == 1 and base.proba(CENTERS[[0]])["cls"].argmax() == 0


def test_merge_uses_page_weight_default_is_correction_weight():
    X, keys, Y, w = H.empty_train_set(4)
    pages = [{"page": {"pdf_path": "/a.pdf", "page_no": 1}, "cls": "А", "weight": 1.0},
             {"page": {"pdf_path": "/a.pdf", "page_no": 2}, "cls": "Б"}]
    X2, keys2, Y2, w2, stat = H.merge_corrections(X, keys, Y, w, pages, np.ones((2, 4), np.float32), LABELS)
    assert keys2 == ["/a.pdf|1", "/a.pdf|2"] and w2.tolist() == [1.0, H.CORR_WEIGHT] and stat == {"override": 0, "new": 2}
    assert Y2["cls"].tolist() == [0, 1] and Y2["first"].tolist() == [-1, -1]


class FakeClf:
    def __init__(self, model_dir: Path):
        self.manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
        self.heads = H.Heads.load(model_dir / "heads.npz")

    def features(self, pages):
        return np.stack([CENTERS[p["c"]] for p in pages]).astype(np.float32)


def test_retrain_without_stored_set_writes_no_training_data(tmp_path):
    src = tmp_path / "model"
    src.mkdir()
    base_heads().save(src / "heads.npz")
    H.write_manifest(src, {"name": "t", "labels": LABELS, "train": {"sources": ["наши данные"]},
                           "metrics": {"hybrid_threshold": 0.86, "eval": {"x": 1}}})
    assert H.load_train_set(src) is None
    pages = [{"page": {"pdf_path": "/a.pdf", "page_no": i, "c": i % 2}, "cls": "АБ"[i % 2], "weight": 1.0}
             for i in range(1, 7)] + [{"page": {"pdf_path": "/a.pdf", "page_no": 9, "c": 0}, "cls": "Б"}]
    res = H.retrain(str(src), pages, str(tmp_path / "v1"), FakeClf(src))
    out = tmp_path / "v1"
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert not (out / "train_features.npy").exists() and not (out / "train_labels.npz").exists()
    assert "train" not in man and man["metrics"] == {"hybrid_threshold": 0.86}
    assert man["adaptation"]["pages"] == 7 and man["adaptation"]["new"] == 7 and man["version"] == res["version"]
    assert set(man["files"]) == {"heads.npz", "heads.npz.json"}
    with pytest.raises(ValueError, match="набор не загружен"):
        H.retrain(str(src), [], str(tmp_path / "v2"), FakeClf(src))
