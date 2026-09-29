"""Вектор картинки страницы SigLIP2 (google/siglip2-base-patch16-512, Apache-2.0, закреплённая ревизия): страница
растягивается в 512×512, нормировка mean = std = 0.5, pooler_output и среднее last_hidden_state, каждое L2-нормировано
-> 1536. Битая страница — нули. Исходник — service/model_code/page_cls."""
from __future__ import annotations

import numpy as np

REPO, REVISION = "google/siglip2-base-patch16-512", "a89f5c5093f902bf39d3cd4d81d2c09867f0724b"
SIDE = 512


class SiglipImage:
    def __init__(self, device: str = "cuda", batch: int = 64):
        import torch
        from transformers import AutoModel

        self.torch, self.device, self.batch = torch, device, batch
        try:
            m = AutoModel.from_pretrained(REPO, revision=REVISION, dtype=torch.bfloat16, local_files_only=True)
        except OSError:
            m = AutoModel.from_pretrained(REPO, revision=REVISION, dtype=torch.bfloat16)
        self.model = m.vision_model.to(device).eval()

    def __call__(self, imgs: list[np.ndarray]) -> np.ndarray:
        torch = self.torch
        out = []
        for i in range(0, len(imgs), self.batch):
            x = torch.from_numpy(np.stack(imgs[i:i + self.batch])).to(self.device, non_blocking=True)
            x = (x.permute(0, 3, 1, 2).float() / 255.0 - 0.5) / 0.5
            with torch.inference_mode():
                o = self.model(pixel_values=x.to(torch.bfloat16))
            pool = torch.nn.functional.normalize(o.pooler_output.float(), dim=-1)
            mean = torch.nn.functional.normalize(o.last_hidden_state.float().mean(1), dim=-1)
            out.append(torch.cat([pool, mean], 1).cpu().numpy())
        return np.vstack(out) if out else np.zeros((0, 1536), np.float32)


def from_e(emb: dict, idx=None) -> np.ndarray:
    """Векторы, посчитанные в опыте E (emb_{split}.npz: pool, mean, ok) -> 1536, нули у битых страниц."""
    pool, mean, ok = emb["pool"].astype(np.float32), emb["mean"].astype(np.float32), emb["ok"]
    v = np.hstack([pool, mean])
    v[~ok] = 0.0
    return v if idx is None else v[idx]
