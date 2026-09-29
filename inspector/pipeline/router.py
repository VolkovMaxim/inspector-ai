"""Этап 6, обучаемая часть — отбор страниц F40.

Модель: TF-IDF текста страницы (слова 1–2 + символы 3–5, до 20 000 символов) + стадия, тип и раздел страницы ->
логистическая регрессия на каждый параметр («на странице есть значение»).
Список модели сливается с лексическим отбором сервиса (select.py) по рангам (RRF, k = 60), на пару (параметр, стадия)
идут 40 первых страниц. Параметр без модели (в обучении меньше 3 страниц со значением) — только лексический список.
На объекте, которого не было в обучении: найдено значений 42,5 % против 24,7 % у лексического
отбора K = 12 при той же точности (64,5 против 65,7 %), вызовов извлечения в 2,7 раза больше.

Артефакт — каталог resources/router: словари и idf векторизаторов, коэффициенты (разреженные, после отсечения малых),
manifest.json (версия, SHA-256 файлов)."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from inspector.config import RESOURCES
from inspector.pipeline.select import Candidate, PageInfo, select_candidates

ROUTER_DIR = RESOURCES / "router"
RRF_K, TEXT_CHARS, BASE_K, META_WEIGHT = 60, 20000, 40, 2.0
WORD_PARAMS = {"ngram_range": (1, 2), "sublinear_tf": True, "lowercase": True}
CHAR_PARAMS = {"analyzer": "char_wb", "ngram_range": (3, 5), "sublinear_tf": True}
META_PARAMS = {"token_pattern": r"\S+", "lowercase": False}
FILES = ("vocab.json", "idf.npz", "coef.npz")


def meta_tokens(stage: str, page_class: str | None, other_classes, section: str | None, text: str) -> str:
    toks = [f"stage_{stage}", f"cls_{page_class}", f"sec_{section}"]
    toks += [f"oc_{c}" for c in other_classes or []]
    toks.append("notext" if not (text or "").strip() else "hastext")
    return " ".join(toks)


def _vectorizer(params: dict, vocab: list[str], idf: np.ndarray):
    from sklearn.feature_extraction.text import TfidfVectorizer

    v = TfidfVectorizer(**params, vocabulary={t: i for i, t in enumerate(vocab)}, dtype=np.float32)
    v.idf_ = idf.astype(np.float64)
    return v


@dataclass
class RouterModel:
    words: object
    chars: object
    meta: object
    coef: object                 # scipy.sparse.csr_matrix [параметры × признаки]
    intercept: np.ndarray
    codes: list[str]
    manifest: dict

    @property
    def version(self) -> str:
        return self.manifest.get("version", "?")

    def features(self, texts: list[str], metas: list[str]):
        from scipy.sparse import hstack

        texts = [(t or "")[:TEXT_CHARS] for t in texts]
        return hstack([self.words.transform(texts), self.chars.transform(texts),
                       self.meta.transform(metas) * META_WEIGHT]).tocsr()

    def scores(self, pages: list[PageInfo]) -> np.ndarray:
        """-> [страницы × параметры] — значение решающей функции (больше — вероятнее значение параметра)."""
        if not pages:
            return np.zeros((0, len(self.codes)), dtype=np.float32)
        X = self.features([p.text for p in pages],
                          [meta_tokens(p.stage, p.page_class, p.other_classes, p.section, p.text) for p in pages])
        return np.asarray((X @ self.coef.T).todense(), dtype=np.float32) + self.intercept[None, :]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_router(path: Path, vocab: dict[str, list[str]], idf: dict[str, np.ndarray], coef, intercept: np.ndarray,
                codes: list[str], manifest: dict) -> dict:
    """Записать артефакт; manifest дополняется SHA-256 файлов и версией (router@<sha8 коэффициентов>)."""
    from scipy.sparse import save_npz

    path.mkdir(parents=True, exist_ok=True)
    (path / "vocab.json").write_text(json.dumps({**vocab, "codes": codes}, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(path / "idf.npz", **{k: v.astype(np.float32) for k, v in idf.items()},
                        intercept=intercept.astype(np.float32))
    save_npz(path / "coef.npz", coef.astype(np.float32).tocsr(), compressed=True)
    files = {name: file_sha256(path / name) for name in FILES}
    manifest = {**manifest, "version": f"router@{files['coef.npz'][:8]}", "files": files}
    (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def load_router_dir(path: Path) -> RouterModel:
    from scipy.sparse import load_npz

    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, sha in (manifest.get("files") or {}).items():
        if file_sha256(path / name) != sha:
            raise ValueError(f"модель отбора: не совпал SHA-256 файла {name} — артефакт повреждён")
    vocab = json.loads((path / "vocab.json").read_text(encoding="utf-8"))
    idf = np.load(path / "idf.npz")
    return RouterModel(words=_vectorizer(WORD_PARAMS, vocab["words"], idf["words"]),
                       chars=_vectorizer(CHAR_PARAMS, vocab["chars"], idf["chars"]),
                       meta=_vectorizer(META_PARAMS, vocab["meta"], idf["meta"]),
                       coef=load_npz(path / "coef.npz").tocsr(), intercept=idf["intercept"],
                       codes=list(vocab["codes"]), manifest=manifest)


@lru_cache(maxsize=4)
def load_router(path: str | None = None) -> RouterModel | None:
    """Модель из каталога (по умолчанию — исходная, resources/router); нет manifest.json — None (отбор по словам).
    Действующую версию после дообучения выбирает inspector/ml/page_router.py."""
    p = Path(path) if path else ROUTER_DIR
    return load_router_dir(p) if (p / "manifest.json").exists() else None


def rrf(lists: list[list], k: int) -> list[tuple[object, float]]:
    """Слияние ранжированных списков по рангам: сумма 1 / (RRF_K + ранг); при равенстве — по ключу."""
    sc: dict = defaultdict(float)
    for lst in lists:
        for r, item in enumerate(lst):
            sc[item] += 1.0 / (RRF_K + r + 1)
    return sorted(sc.items(), key=lambda x: (-x[1], x[0]))[:k]


def select_fused(pages: list[PageInfo], params: list[dict], top_k: int, idf_pages: list[PageInfo] | None,
                 model: RouterModel) -> list[Candidate]:
    """Лексический отбор (BASE_K страниц) + список модели -> RRF -> top_k страниц на (параметр, стадия)."""
    base: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for c in select_candidates(pages, params, BASE_K, idf_pages):       # уже по убыванию оценки внутри пары
        base[(c.param_code, c.stage)].append((c.file_pk, c.page_no))
    text_pages = [p for p in pages if p.text and p.stage in ("PD", "RD", "ID")]
    by_stage: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(text_pages):
        by_stage[p.stage].append(i)
    S = model.scores(text_pages)
    col = {c: j for j, c in enumerate(model.codes)}
    out: list[Candidate] = []
    for param in params:
        code = param["code"]
        for stage, idx in by_stage.items():
            b_list = base.get((code, stage), [])
            if code in col:
                s = S[:, col[code]]
                order = sorted(idx, key=lambda i: (-s[i], text_pages[i].file_pk, text_pages[i].page_no))[:BASE_K]
                r_list = [(text_pages[i].file_pk, text_pages[i].page_no) for i in order]
            else:
                r_list = b_list
            for (fpk, pno), score in rrf([r_list, b_list], top_k):
                out.append(Candidate(code, stage, fpk, pno, round(score, 6)))
    return out
