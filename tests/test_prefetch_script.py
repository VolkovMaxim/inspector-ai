"""scripts/prefetch_models.sh: какие веса качает каждый профиль. `hf` подменён заглушкой, которая только записывает
свои аргументы, — сеть не нужна."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prefetch_models.sh"
# вектор картинки классификатора типа страницы — всем профилям, с закреплённой ревизией
SIGLIP = "download org/siglip --revision abc123 --dry-run"


def classifier(tmp_path: Path) -> str:
    """Сам классификатор — каталогом PAGE_CLASSIFIER_HOST_DIR (не в кэш HF), с закреплённой ревизией."""
    return f"download org/cls --revision r1 --local-dir {tmp_path / 'cls'} --dry-run"


def prefetch(profile: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir, log = tmp_path / "bin", tmp_path / "hf_calls.log"
    bin_dir.mkdir()
    hf = bin_dir / "hf"
    hf.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n', encoding="utf-8")
    hf.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HF_HOME": str(tmp_path / "hf"),
           "LLM_MODEL_REPO": "org/llm", "SINGLE_MODEL_REPO": "org/single", "OCR_MODEL_REPO": "org/ocr", "SIGLIP_MODEL_REPO": "org/siglip",
           "SIGLIP_MODEL_REVISION": "abc123", "PAGE_CLS_MODEL_REPO": "org/cls", "PAGE_CLS_MODEL_REVISION": "r1",
           "PAGE_CLASSIFIER_HOST_DIR": str(tmp_path / "cls")}
    run = subprocess.run(["bash", str(SCRIPT), profile, "--dry-run"], env=env, capture_output=True, text=True,
                         timeout=60)
    return run, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_h100_profile_prefetches_only_the_llm(tmp_path):
    """Профиль h100 запускает одну Qwen3.8 (docker-compose.yml); Qwen3.6 для OCR — отдельный профиль qwen36-ocr."""
    run, calls = prefetch("h100", tmp_path)
    assert run.returncode == 0, run.stderr
    assert calls == ["download org/llm --dry-run", SIGLIP, classifier(tmp_path)]


def test_qwen36_ocr_profile_prefetches_llm_and_ocr(tmp_path):
    run, calls = prefetch("qwen36-ocr", tmp_path)
    assert run.returncode == 0, run.stderr
    assert calls == ["download org/llm --dry-run", "download org/ocr --dry-run", SIGLIP, classifier(tmp_path)]


def test_single_profile_prefetches_only_its_own_llm(tmp_path):
    """Профиль single (GPU 32 ГБ) — свои веса SINGLE_MODEL_REPO (NVFP4): FP8 профиля h100 в 32 ГБ не помещается."""
    run, calls = prefetch("single", tmp_path)
    assert run.returncode == 0, run.stderr
    assert calls == ["download org/single --dry-run", SIGLIP, classifier(tmp_path)]
