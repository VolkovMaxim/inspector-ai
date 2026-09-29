"""Головы классификатора страниц (исходник — model_code/page_cls, копируется в каталог модели).

Линейные головы (мультиномиальная логистическая регрессия) cls / first / section / quality на векторе признаков
модели: признаки стандартизуются, полный батч, L-BFGS (torch; GPU, если есть). Метка -1 — поля нет: строка в этой
голове не участвует.

Дообучение (retrain): обучающих данных в модели нет — набор приходит из сервиса: страницы загруженных проверок с их
текущими метками (вес 1) и страницы с правками разметчиков (вес CORR_WEIGHT). Головы дообучаются от текущих весов:
штраф тянет веса к исходным, а не к нулю (Heads.adapt), поэтому типы, которых нет в новых документах, модель не
забывает. Если в каталоге модели есть сохранённый набор (train_features.npy + train_labels.npz), он идёт первым;
страница определяется ключом «pdf_path|page_no», повтор ключа заменяет метку.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

HEADS = ("cls", "first", "section", "quality")
CORR_WEIGHT = 5.0


def page_key(page: dict) -> str:
    return f"{page['pdf_path']}|{int(page['page_no'])}"


def _torch():
    import torch

    return torch


class Heads:
    def __init__(self, labels: dict, mean: np.ndarray, std: np.ndarray, W: dict, b: dict, lam: float):
        self.labels = labels                  # {"cls": [...32], "first": [False, True], "section": [...19], "quality": [...3]}
        self.mean, self.std, self.W, self.b, self.lam = mean, std, W, b, lam

    # ------------------------------------------------------------------ fit
    @classmethod
    def fit(cls, X: np.ndarray, Y: dict, labels: dict, lam: float, weight: np.ndarray | None = None,
            max_iter: int = 300, device: str | None = None) -> "Heads":
        torch = _torch()
        dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
        X = np.asarray(X, np.float32)
        mean, std = X.mean(0), X.std(0) + 1e-6
        Xt = torch.from_numpy((X - mean) / std).to(dev)
        wt = torch.ones(len(X), device=dev) if weight is None else torch.as_tensor(np.asarray(weight, np.float32), device=dev)
        W, b = {}, {}
        for h in HEADS:
            y = torch.as_tensor(np.asarray(Y[h], np.int64), device=dev)
            m = y >= 0
            k = len(labels[h])
            w_h = torch.zeros(X.shape[1], k, device=dev, requires_grad=True)
            b_h = torch.zeros(k, device=dev, requires_grad=True)
            if m.sum() == 0:
                W[h], b[h] = w_h.detach().cpu().numpy(), np.full(k, -1e4, np.float32)
                continue
            xm, ym, wm = Xt[m], y[m], wt[m]
            # класс, которого нет в данных (например «рукописный»), получает сдвиг -inf -> вероятность 0
            seen = torch.bincount(ym, minlength=k) > 0
            opt = torch.optim.LBFGS([w_h, b_h], lr=1, max_iter=max_iter, history_size=20, line_search_fn="strong_wolfe",
                                    tolerance_grad=1e-6, tolerance_change=1e-9)
            wsum = wm.sum()

            def closure():
                opt.zero_grad()
                logits = xm @ w_h + b_h
                logits = logits.masked_fill(~seen, -1e4)
                loss = (torch.nn.functional.cross_entropy(logits, ym, reduction="none") * wm).sum() / wsum
                loss = loss + lam * (w_h * w_h).sum()
                loss.backward()
                return loss

            opt.step(closure)
            bb = b_h.detach().clone()
            bb[~seen] = -1e4
            W[h], b[h] = w_h.detach().cpu().numpy(), bb.cpu().numpy()
        return cls(labels, mean.astype(np.float32), std.astype(np.float32), W, b, lam)

    def adapt(self, X: np.ndarray, Y: dict, weight: np.ndarray | None = None, prior: float | None = None,
              max_iter: int = 300, device: str | None = None) -> "Heads":
        """Дообучение от текущих весов: min Σ вес·CE / Σ вес + prior·(‖W − W0‖² + ‖b − b0‖²). Стандартизация признаков —
        прежняя; класс, которого нет в данных, не выключается — его держит штраф к исходным весам (MAP-адаптация;
        Chelba, Acero, 2004). Голова без меток в данных не меняется."""
        torch = _torch()
        dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
        prior = self.lam if prior is None else prior
        Xt = torch.from_numpy((np.asarray(X, np.float32) - self.mean) / self.std).to(dev)
        wt = torch.ones(len(X), device=dev) if weight is None else torch.as_tensor(np.asarray(weight, np.float32), device=dev)
        W, b = {}, {}
        for h in HEADS:
            y = torch.as_tensor(np.asarray(Y[h], np.int64), device=dev)
            m = y >= 0
            if m.sum() == 0:
                W[h], b[h] = self.W[h].copy(), self.b[h].copy()
                continue
            b0_np = np.where(self.b[h] < -1e3, -30.0, self.b[h]).astype(np.float32)   # выключенный при обучении класс
            W0 = torch.from_numpy(np.asarray(self.W[h], np.float32)).to(dev)
            b0 = torch.from_numpy(b0_np).to(dev)
            w_h = W0.clone().requires_grad_(True)
            b_h = b0.clone().requires_grad_(True)
            xm, ym, wm = Xt[m], y[m], wt[m]
            wsum = wm.sum()
            opt = torch.optim.LBFGS([w_h, b_h], lr=1, max_iter=max_iter, history_size=20, line_search_fn="strong_wolfe",
                                    tolerance_grad=1e-6, tolerance_change=1e-9)

            def closure():
                opt.zero_grad()
                loss = (torch.nn.functional.cross_entropy(xm @ w_h + b_h, ym, reduction="none") * wm).sum() / wsum
                loss = loss + prior * (((w_h - W0) ** 2).sum() + ((b_h - b0) ** 2).sum())
                loss.backward()
                return loss

            opt.step(closure)
            W[h], b[h] = w_h.detach().cpu().numpy(), b_h.detach().cpu().numpy()
        return Heads(self.labels, self.mean, self.std, W, b, self.lam)

    # ------------------------------------------------------------------ predict
    def proba(self, X: np.ndarray) -> dict:
        Z = (np.asarray(X, np.float32) - self.mean) / self.std
        out = {}
        for h in HEADS:
            logits = Z @ self.W[h] + self.b[h]
            logits -= logits.max(1, keepdims=True)
            p = np.exp(logits)
            out[h] = p / p.sum(1, keepdims=True)
        return out

    def decode(self, X: np.ndarray) -> list[dict]:
        P = self.proba(X)
        res = []
        for i in range(len(X)):
            pc = P["cls"][i]
            top = np.argsort(-pc)[:3]
            r = {"cls": self.labels["cls"][top[0]], "cls_p": float(pc[top[0]]),
                 "cls_top3": [[self.labels["cls"][j], float(pc[j])] for j in top]}
            pf = P["first"][i]
            r["first"] = bool(self.labels["first"][int(np.argmax(pf))])
            r["first_p"] = float(pf[self.labels["first"].index(True)])
            for h in ("section", "quality"):
                ph = P[h][i]
                j = int(np.argmax(ph))
                r[h], r[f"{h}_p"] = self.labels[h][j], float(ph[j])
            res.append(r)
        return res

    # ------------------------------------------------------------------ io
    def save(self, path: Path) -> None:
        arrs = {"mean": self.mean, "std": self.std}
        for h in HEADS:
            arrs[f"W_{h}"], arrs[f"b_{h}"] = self.W[h].astype(np.float32), self.b[h].astype(np.float32)
        np.savez(path, **arrs)
        Path(str(path) + ".json").write_text(json.dumps({"labels": self.labels, "lam": self.lam}, ensure_ascii=False),
                                             encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Heads":
        meta = json.loads(Path(str(path) + ".json").read_text(encoding="utf-8"))
        if meta.get("type") == "nn":
            return NNHeads.load(path)
        z = np.load(path)
        return cls(meta["labels"], z["mean"], z["std"], {h: z[f"W_{h}"] for h in HEADS}, {h: z[f"b_{h}"] for h in HEADS},
                   meta["lam"])


LOSS_W = {"cls": 1.0, "first": 1.0, "section": 1.0, "quality": 0.2}


class NNHeads(Heads):
    """Голова обучения сети: LayerNorm(d) -> Linear на каждую задачу (как при дообучении модели; без dropout на выводе).
    Дообучение на правках — AdamW от текущих весов (refit), а не лог. регрессия с нуля."""

    def __init__(self, labels: dict, gamma: np.ndarray, beta: np.ndarray, W: dict, b: dict, epochs: int = 3, lr: float = 1e-3):
        self.labels, self.gamma, self.beta, self.W, self.b = labels, gamma, beta, W, b
        self.epochs, self.lr, self.lam = epochs, lr, 0.0

    @classmethod
    def from_state(cls, state: dict, labels: dict) -> "NNHeads":
        g = lambda k: np.asarray(state[k].detach().float().cpu().numpy() if hasattr(state[k], "float") else state[k], np.float32)  # noqa: E731
        W, b = {}, {}
        for h in HEADS:
            key = f"out.{h}" if f"out.{h}.weight" in state else h
            W[h], b[h] = g(key + ".weight").T.copy(), g(key + ".bias")
        return cls(labels, g("norm.weight"), g("norm.bias"), W, b)

    def proba(self, X: np.ndarray) -> dict:
        x = np.asarray(X, np.float32)
        z = (x - x.mean(1, keepdims=True)) / np.sqrt(x.var(1, keepdims=True) + 1e-5) * self.gamma + self.beta
        out = {}
        for h in HEADS:
            lg = z @ self.W[h] + self.b[h]
            lg -= lg.max(1, keepdims=True)
            e = np.exp(lg)
            out[h] = e / e.sum(1, keepdims=True)
        return out

    def refit(self, X: np.ndarray, Y: dict, weight: np.ndarray | None = None, seed: int = 20260928,
              device: str | None = None, bs: int = 1024) -> "NNHeads":
        torch = _torch()
        dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
        g = torch.Generator().manual_seed(seed)
        x = torch.from_numpy(np.asarray(X, np.float32)).to(dev)
        w = torch.ones(len(X)) if weight is None else torch.as_tensor(np.asarray(weight, np.float32))
        ys = {h: torch.as_tensor(np.asarray(Y[h], np.int64)) for h in HEADS}
        gamma = torch.tensor(self.gamma, device=dev, requires_grad=True)
        beta = torch.tensor(self.beta, device=dev, requires_grad=True)
        Ws = {h: torch.tensor(self.W[h], device=dev, requires_grad=True) for h in HEADS}
        bs_ = {h: torch.tensor(self.b[h], device=dev, requires_grad=True) for h in HEADS}
        opt = torch.optim.AdamW([gamma, beta, *Ws.values(), *bs_.values()], lr=self.lr, weight_decay=0.01)
        n = len(X)
        for _ in range(self.epochs):
            perm = torch.randperm(n, generator=g)
            for s in range(0, n, bs):
                idx = perm[s:s + bs]
                xb = x[idx.to(dev)]
                z = torch.nn.functional.layer_norm(xb, (xb.shape[1],), gamma, beta, 1e-5)
                wb = w[idx].to(dev)
                loss = 0.0
                for h in HEADS:
                    yb = ys[h][idx].to(dev)
                    m = yb >= 0
                    if m.any():
                        ce = torch.nn.functional.cross_entropy(z[m] @ Ws[h] + bs_[h], yb[m], reduction="none")
                        loss = loss + LOSS_W[h] * (ce * wb[m]).sum() / wb[m].sum()
                opt.zero_grad()
                loss.backward()
                opt.step()
        return NNHeads(self.labels, gamma.detach().cpu().numpy(), beta.detach().cpu().numpy(),
                       {h: Ws[h].detach().cpu().numpy() for h in HEADS}, {h: bs_[h].detach().cpu().numpy() for h in HEADS},
                       self.epochs, self.lr)

    def save(self, path: Path) -> None:
        arrs = {"gamma": self.gamma, "beta": self.beta}
        for h in HEADS:
            arrs[f"W_{h}"], arrs[f"b_{h}"] = self.W[h].astype(np.float32), self.b[h].astype(np.float32)
        np.savez(path, **arrs)
        Path(str(path) + ".json").write_text(json.dumps({"labels": self.labels, "type": "nn", "epochs": self.epochs,
                                                         "lr": self.lr, "lam": 0.0}, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "NNHeads":
        z = np.load(path)
        meta = json.loads(Path(str(path) + ".json").read_text(encoding="utf-8"))
        return cls(meta["labels"], z["gamma"], z["beta"], {h: z[f"W_{h}"] for h in HEADS}, {h: z[f"b_{h}"] for h in HEADS},
                   meta.get("epochs", 3), meta.get("lr", 1e-3))


def encode_labels(rows: list[dict], labels: dict) -> dict:
    """Строки с полями cls/first/section/quality (нет поля или None -> -1) -> индексы."""
    idx = {h: {v: i for i, v in enumerate(labels[h])} for h in HEADS}
    out = {}
    for h in HEADS:
        out[h] = np.array([idx[h].get(r.get(h), -1) if r.get(h) is not None else -1 for r in rows], np.int64)
    return out


# ------------------------------------------------------------------ обучающий набор: сохранить / дополнить правками
def save_train_set(model_dir: Path, feats: np.ndarray, keys: list[str], Y: dict, weight: np.ndarray) -> None:
    np.save(model_dir / "train_features.npy", np.asarray(feats, np.float16))
    np.savez(model_dir / "train_labels.npz", keys=np.array(keys), weight=np.asarray(weight, np.float32),
             **{h: np.asarray(Y[h], np.int64) for h in HEADS})


def load_train_set(model_dir: Path) -> tuple[np.ndarray, list[str], dict, np.ndarray] | None:
    """Сохранённый набор каталога модели; нет файлов — None (выпущенная модель обучающих данных не содержит)."""
    if not (model_dir / "train_features.npy").exists() or not (model_dir / "train_labels.npz").exists():
        return None
    X = np.load(model_dir / "train_features.npy").astype(np.float32)
    z = np.load(model_dir / "train_labels.npz")
    return X, [str(k) for k in z["keys"]], {h: z[h] for h in HEADS}, z["weight"]


def empty_train_set(dim: int) -> tuple[np.ndarray, list[str], dict, np.ndarray]:
    return np.zeros((0, dim), np.float32), [], {h: np.zeros(0, np.int64) for h in HEADS}, np.zeros(0, np.float32)


def validate_corrections(corrections: list[dict], labels: dict) -> None:
    """Правка: {"page": {...pdf_path, page_no...}, "cls"?, "first"?, "section"?}; метки — только из словарей модели.
    Ошибка — до расчёта признаков, с номером правки."""
    for i, c in enumerate(corrections):
        pg = c.get("page") or {}
        if not pg.get("pdf_path") or int(pg.get("page_no", 0)) < 1:
            raise ValueError(f"правка {i}: нужна страница с pdf_path и page_no ≥ 1, получено {pg!r:.120}")
        for h in ("cls", "section"):
            if c.get(h) is not None and c[h] not in labels[h]:
                raise ValueError(f"правка {i}: {h} = {c[h]!r} нет в словаре модели")
        if c.get("first") is not None and not isinstance(c["first"], bool):
            raise ValueError(f"правка {i}: first должен быть true/false, получено {c['first']!r}")
        if all(c.get(h) is None for h in ("cls", "first", "section")):
            raise ValueError(f"правка {i}: нет ни одного поля cls / first / section")
        if c.get("weight") is not None and not float(c["weight"]) > 0:
            raise ValueError(f"правка {i}: вес должен быть > 0, получено {c['weight']!r}")


def merge_corrections(X, keys, Y, weight, corrections: list[dict], corr_feats: np.ndarray, labels: dict):
    """Страница из сервиса заменяет метку той же страницы в своих полях и получает свой вес («weight»; нет — CORR_WEIGHT,
    как у правки разметчика); новая страница добавляется (поля без метки у неё -1)."""
    pos = {k: i for i, k in enumerate(keys)}
    X, keys = X.copy(), list(keys)
    Y = {h: Y[h].copy() for h in HEADS}
    weight = weight.copy()
    lab_idx = {h: {v: i for i, v in enumerate(labels[h])} for h in HEADS}
    add_x, n_new, n_override = [], 0, 0
    for c, f in zip(corrections, corr_feats):
        k = page_key(c["page"])
        if k in pos:
            i = pos[k]
            n_override += 1
        else:
            i = len(keys)
            keys.append(k)
            pos[k] = i
            add_x.append(f)
            for h in HEADS:
                Y[h] = np.append(Y[h], -1)
            weight = np.append(weight, 1.0)
            n_new += 1
        for h in ("cls", "first", "section"):
            if h in c and c[h] is not None:
                Y[h][i] = lab_idx[h][c[h]]
        weight[i] = float(c["weight"]) if c.get("weight") is not None else CORR_WEIGHT
    if add_x:
        X = np.vstack([X, np.asarray(add_x, np.float32)])
    return X, keys, Y, weight, {"override": n_override, "new": n_new}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip(rel: Path) -> bool:
    """Служебное: __pycache__ и скрытые каталоги (.cache — метаданные `hf download --local-dir`)."""
    return "__pycache__" in rel.parts or any(part.startswith(".") for part in rel.parts)


def write_manifest(model_dir: Path, manifest: dict) -> dict:
    """files {путь: sha256} по всем файлам папки (кроме manifest.json и служебных); version = имя@sha8 от списка
    файлов."""
    files = {}
    for p in sorted(model_dir.rglob("*")):
        if p.is_file() and p.name != "manifest.json" and not _skip(p.relative_to(model_dir)):
            files[str(p.relative_to(model_dir))] = sha256_file(p)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:8]
    manifest = dict(manifest)
    manifest["files"] = files
    manifest["version"] = f"{manifest['name']}@{digest}"
    (model_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def copy_model_dir(src: Path, dst: Path) -> None:
    """Новая модель — копия старой (большие веса — жёсткие ссылки, если та же ФС); головы и набор перезапишутся."""
    if dst.exists():
        shutil.rmtree(dst)
    for p in src.rglob("*"):
        if _skip(p.relative_to(src)):
            continue
        q = dst / p.relative_to(src)
        if p.is_dir():
            q.mkdir(parents=True, exist_ok=True)
            continue
        q.parent.mkdir(parents=True, exist_ok=True)
        if p.stat().st_size > 50_000_000 and p.name not in ("train_features.npy",):
            try:
                os.link(p, q)
                continue
            except OSError:
                pass
        shutil.copy2(p, q)


def retrain(model_dir: str, corrections: list[dict], out_dir: str, clf) -> dict:
    """Общая часть retrain_heads: clf — загруженный Classifier (признаки страниц считает он). corrections — страницы из
    сервиса: {"page", "cls"?, "first"?, "section"?, "weight"?}."""
    t0 = time.time()
    if not corrections:
        raise ValueError("нет страниц для дообучения: набор не загружен")
    validate_corrections(corrections, clf.heads.labels)
    src, dst = Path(model_dir), Path(out_dir)
    corr_feats = clf.features([c["page"] for c in corrections])
    t_feat = time.time() - t0
    base = load_train_set(src) or empty_train_set(corr_feats.shape[1])
    X2, keys2, Y2, w2, stat = merge_corrections(*base, corrections, corr_feats, clf.heads.labels)
    if isinstance(clf.heads, NNHeads):
        heads = clf.heads.refit(X2, Y2, w2)                     # голова обучения: дообучение от текущих весов
    else:
        heads = clf.heads.adapt(X2, Y2, w2)                     # линейные головы: от текущих весов, штраф к ним
    copy_model_dir(src, dst)
    for name in ("train_features.npy", "train_labels.npz"):     # набор страниц остаётся в сервисе, не в модели
        (dst / name).unlink(missing_ok=True)
    heads.save(dst / "heads.npz")
    man = dict(clf.manifest)
    corr_sha = hashlib.sha256(json.dumps([[page_key(c["page"]), c.get("cls"), c.get("first"), c.get("section"),
                                           c.get("weight")] for c in corrections], ensure_ascii=False).encode()
                              ).hexdigest()
    man["parent_version"] = clf.manifest.get("version")
    man["created"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    man.pop("train", None)
    man["adaptation"] = {"from": clf.manifest.get("version"), "pages": len(keys2), "sha256": corr_sha, **stat,
                         "corr_weight": CORR_WEIGHT, "prior": getattr(clf.heads, "lam", None)}
    man["metrics"] = {k: v for k, v in (clf.manifest.get("metrics") or {}).items() if k == "hybrid_threshold"}
    man = write_manifest(dst, man)
    return {"seconds": round(time.time() - t0, 2), "seconds_features": round(t_feat, 2),
            "n_corrections": len(corrections), "version": man["version"], "corrections_sha256": corr_sha}
