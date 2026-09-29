"""Классификатор страниц: LiLT (текст + рамки слов, дообучен целиком) ⊕ вектор картинки SigLIP2 -> линейные головы.
Исходник — model_code/page_cls (копируется в каталог модели); настройки — manifest.json (config).

Вход LiLT: слова страницы в порядке строк (сверху вниз, внутри строки по x0 — как words_to_text), токены слова получают
рамку слова в долях страницы × 1000; [CLS] — рамка 0,0,0,0, [SEP] — 1000,1000,1000,1000; до 512 токенов.
features = [CLS] LiLT (768) ⊕ SigLIP2 pool ⊕ mean (1536) = 2304. Интерфейс — inspector/ml/page_classifier.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import heads as H  # noqa: E402
import page_render as R  # noqa: E402
import siglip_img as S  # noqa: E402

MAX_TOKENS = 512


def _lines(words: list) -> list[list]:
    """Как common._lines (words_to_text): строка — центры по y в пределах половины высоты слова."""
    items = [w for w in words or () if len(w) >= 5 and str(w[4]).strip()]
    items.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    centers: list[float] = []
    for w in items:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1e-6)
        if lines and abs(yc - centers[-1]) <= h / 2:
            lines[-1].append(w)
            centers[-1] = sum((v[1] + v[3]) / 2 for v in lines[-1]) / len(lines[-1])
        else:
            lines.append([w])
            centers.append(yc)
    return [sorted(line, key=lambda v: v[0]) for line in lines]


def reading_order(words: list) -> list:
    return [v for line in _lines(words) for v in line]


def _box(w) -> list[int]:
    x0, y0, x1, y1 = (min(max(float(c), 0.0), 1.0) for c in w[:4])
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return [int(round(x0 * 1000)), int(round(y0 * 1000)), int(round(x1 * 1000)), int(round(y1 * 1000))]


def encode_page(tok, words: list, max_tokens: int = MAX_TOKENS) -> tuple[list[int], list[list[int]]]:
    """Слова -> (input_ids, bbox) с [CLS]/[SEP], не длиннее max_tokens."""
    ws = reading_order(words)
    cls_id, sep_id = tok.cls_token_id, tok.sep_token_id
    if not ws:
        return [cls_id, sep_id], [[0, 0, 0, 0], [1000, 1000, 1000, 1000]]
    texts = [str(w[4]) for w in ws]
    if "LayoutXLM" in type(tok).__name__:
        # токенизатор lilt-infoxlm-base (класс transformers LayoutXLMTokenizer, словарь InfoXLM) сам раздаёт рамки
        # слов токенам и ставит [CLS] 0,0,0,0 / [SEP] 1000,1000,1000,1000
        enc = tok(texts, boxes=[_box(w) for w in ws], truncation=True, max_length=max_tokens)
        return list(enc["input_ids"]), [list(b) for b in enc["bbox"]]
    enc = tok(texts, is_split_into_words=True, add_special_tokens=False, truncation=True, max_length=max_tokens - 2)
    ids = enc["input_ids"]
    wid = enc.word_ids()
    boxes = [_box(ws[j]) for j in wid]
    return [cls_id] + ids + [sep_id], [[0, 0, 0, 0]] + boxes + [[1000, 1000, 1000, 1000]]


def encode_input(tok, page: dict) -> tuple[list[int], list[list[int]]]:
    return encode_page(tok, page.get("words") or [])


def load_backbone(path, dtype=None):
    import torch
    from transformers import LiltModel

    return LiltModel.from_pretrained(path, dtype=dtype or torch.float32)


def backbone_cls(model, ids, box, att):
    """[CLS] последнего слоя."""
    return model(input_ids=ids, bbox=box, attention_mask=att).last_hidden_state[:, 0]


def collate(batch_ids: list[list[int]], batch_boxes: list[list[list[int]]], pad_id: int):
    import torch

    n = max(len(x) for x in batch_ids)
    ids = torch.full((len(batch_ids), n), pad_id, dtype=torch.long)
    box = torch.zeros((len(batch_ids), n, 4), dtype=torch.long)
    att = torch.zeros((len(batch_ids), n), dtype=torch.long)
    for i, (a, b) in enumerate(zip(batch_ids, batch_boxes)):
        ids[i, :len(a)] = torch.tensor(a)
        box[i, :len(b)] = torch.tensor(b)
        att[i, :len(a)] = 1
    return ids, box, att


class Classifier:
    def __init__(self, model_dir: str, device: str = "cuda"):
        import torch
        from transformers import AutoTokenizer

        if device.startswith("cuda") and not torch.cuda.is_available():
            device = "cpu"
        self.torch, self.device = torch, device
        self.model_dir = Path(model_dir)
        self.manifest = json.loads((self.model_dir / "manifest.json").read_text(encoding="utf-8"))
        cfg = self.manifest["config"]
        self.batch, self.render_workers = cfg.get("batch", 64), cfg.get("render_workers", 8)
        self.tok = AutoTokenizer.from_pretrained(self.model_dir / "tokenizer")
        self.backbone = load_backbone(self.model_dir / "backbone").to(device).eval()
        self.heads = H.Heads.load(self.model_dir / "heads.npz")
        self._img = None

    @property
    def img(self) -> S.SiglipImage:
        if self._img is None:
            self._img = S.SiglipImage(self.device)
        return self._img

    def text_features(self, pages: list[dict]) -> np.ndarray:
        torch = self.torch
        enc = [encode_input(self.tok, p) for p in pages]
        order = sorted(range(len(enc)), key=lambda i: len(enc[i][0]))
        out = np.zeros((len(pages), self.backbone.config.hidden_size), np.float32)
        for s in range(0, len(order), self.batch):
            idx = order[s:s + self.batch]
            ids, box, att = collate([enc[i][0] for i in idx], [enc[i][1] for i in idx], self.tok.pad_token_id)
            with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16, enabled=self.device.startswith("cuda")):
                o = backbone_cls(self.backbone, ids.to(self.device), box.to(self.device), att.to(self.device))
            out[idx] = o.float().cpu().numpy()
        return out

    def image_features(self, pages: list[dict]) -> np.ndarray:
        imgs = R.render_many([(p["pdf_path"], int(p["page_no"]), S.SIDE, "square") for p in pages], self.render_workers)
        return self.img(imgs)

    def features(self, pages: list[dict]) -> np.ndarray:
        if not pages:
            return np.zeros((0, self.backbone.config.hidden_size + 1536), np.float32)
        return np.hstack([self.text_features(pages), self.image_features(pages)]).astype(np.float32)

    def predict_from_features(self, feats: np.ndarray) -> list[dict]:
        return self.heads.decode(np.asarray(feats, np.float32))

    def predict(self, pages: list[dict]) -> list[dict]:
        return self.predict_from_features(self.features(pages))


def load(model_dir: str, device: str = "cuda") -> Classifier:
    return Classifier(model_dir, device)


def retrain_heads(model_dir: str, corrections: list[dict], out_dir: str, clf: Classifier | None = None) -> dict:
    """Быстрый путь: головы дообучаются от текущих весов на страницах из сервиса (признаки считает эта же модель).
    clf — уже загруженная модель этого каталога (вторая копия на GPU не нужна)."""
    return H.retrain(model_dir, corrections, out_dir, clf if clf is not None else load(model_dir))
