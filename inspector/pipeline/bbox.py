"""Слова и рамки на страницах без текстового слоя: RapidOCR PP-OCRv5 (детектор mobile, распознаватель eslav) по тем же
плиткам, что у OCR моделью. Две роли: (1) текст скана для типа страницы и отбора (OCR_ENGINE=rapidocr, этап 3; точный
текст страниц, идущих в извлечение, перечитывает Qwen — этап reocr); (2) рамка значения на странице (верное место у
90,6 % значений на А4 и 78,4 % на листах).
Модели лежат в resources/rapidocr (сеть не нужна).
Повёрнутый скан сначала выпрямляется (pipeline/orient.py; без этого RapidOCR на повёрнутых сканах находил 46 % слов,
после — 78 %), рамки возвращаются в доли исходной видимой страницы — как у слоя и на картинке в интерфейсе."""
from __future__ import annotations

import io
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from inspector.config import RESOURCES
from inspector.pipeline.textutils import pool_context

log = logging.getLogger("inspector.bbox")
_ENGINES: dict = {}             # False/True — движок CPU/GPU этого процесса; gpu_ok — итог пробы
MODELS = RESOURCES / "rapidocr"


def _gpu_mem_bytes() -> int:
    from inspector.config import get_settings

    return int(get_settings().rapidocr_gpu_mem_gb * 2**30)


def _cuda_providers(eng) -> bool:
    """Все три модели RapidOCR (детектор, поворот строк, распознавание) действительно на CUDA."""
    try:
        return all("CUDAExecutionProvider" in getattr(eng, n).session.session.get_providers()
                   for n in ("text_det", "text_cls", "text_rec"))
    except Exception:  # noqa: BLE001 — другая версия rapidocr: проверить нельзя
        return False


def _preload_cuda() -> None:
    """Библиотеки CUDA 12 / cuDNN 9 из пакетов nvidia-* окружения (их ставит torch): без этого onnxruntime-gpu не находит
    libcudnn.so.9 и молча считает на CPU."""
    import onnxruntime as ort

    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls(cuda=True, cudnn=True, msvc=False)


def gpu_ready() -> bool:
    """RapidOCR на GPU возможен: onnxruntime-gpu + библиотеки CUDA. Проба — маленькая модель поворота строк на
    CUDAExecutionProvider (полный движок в основном процессе не грузим: он занял бы ~5 ГБ видеопамяти).
    Нет — явная ошибка в журнале, распознавание идёт пулом CPU."""
    if "gpu_ok" not in _ENGINES:
        ok = False
        try:
            import onnxruntime as ort

            _preload_cuda()
            if "CUDAExecutionProvider" in ort.get_available_providers():
                probe = ort.InferenceSession(str(MODELS / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
                                             providers=[("CUDAExecutionProvider", {"gpu_mem_limit": 256 * 2**20}),
                                                        "CPUExecutionProvider"])
                ok = "CUDAExecutionProvider" in probe.get_providers()
                del probe
        except Exception as exc:  # noqa: BLE001
            log.error("rapidocr gpu probe failed", extra={"error": str(exc)[:300]})
        if not ok:
            log.error("RAPIDOCR_GPU=true, но CUDA для onnxruntime недоступна — RapidOCR считает на CPU")
        _ENGINES["gpu_ok"] = ok
    return bool(_ENGINES["gpu_ok"])


def _engine(gpu: bool = False):
    """RapidOCR (свой на CPU и на GPU). На GPU — предел памяти onnxruntime (RAPIDOCR_GPU_MEM_GB): без него он забирает
    всю свободную видеопамять рядом с vLLM."""
    if gpu not in _ENGINES:
        if gpu:
            _preload_cuda()
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

        params = {"Global.log_level": "error", "Global.max_side_len": 4000,
                  "Det.model_path": str(MODELS / "ch_PP-OCRv5_det_mobile.onnx"), "Det.ocr_version": OCRVersion.PPOCRV5,
                  "Det.model_type": ModelType.MOBILE,
                  "Rec.model_path": str(MODELS / "eslav_PP-OCRv5_rec_mobile.onnx"), "Rec.ocr_version": OCRVersion.PPOCRV5,
                  "Rec.model_type": ModelType.MOBILE, "Rec.lang_type": LangRec("eslav"),
                  "Cls.model_path": str(MODELS / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
                  "EngineConfig.onnxruntime.use_cuda": gpu}
        if gpu:
            params["EngineConfig.onnxruntime.cuda_ep_cfg.gpu_mem_limit"] = _gpu_mem_bytes()
            params["EngineConfig.onnxruntime.cuda_ep_cfg.arena_extend_strategy"] = "kSameAsRequested"
            # у каждой клетки и плитки своя форма входа; EXHAUSTIVE (умолчание RapidOCR) перебирает алгоритмы cuDNN на
            # каждую новую форму. H100, 4 страницы: EXHAUSTIVE 56 с, HEURISTIC 17 с, DEFAULT 10 с; слова те же (8
            # расхождений в наборах на 3 332 слова)
            params["EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search"] = "DEFAULT"
        else:
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = 4
        eng = RapidOCR(params=params)
        if gpu and not _cuda_providers(eng):       # этот процесс CUDA не получил — считаем на CPU, но не молча
            log.error("RapidOCR: сессии не на CUDA в процессе распознавания — этот процесс считает на CPU")
            eng = _engine(False)
        _ENGINES[gpu] = eng
    return _ENGINES[gpu]


def page_words(pdf_path: str, page_no: int, cache_file: str, gpu: bool = False, orient: bool = True) -> list[list]:
    """Слова страницы [[x0,y0,x1,y1,текст], ...] в долях видимой страницы. Кэш — json-файл ({angle, words}; старый
    формат без поворота — просто список слов)."""
    cache = Path(cache_file)
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data["words"] if isinstance(data, dict) else data
    from PIL import Image

    from inspector.pipeline.ocr import render_tiles
    from inspector.pipeline.orient import page_angle, to_visible

    eng = _engine(gpu)
    k = 0
    if orient:
        import pymupdf

        k = page_angle(pymupdf.open(pdf_path)[page_no - 1], eng)
    out: list[list] = []
    for jpeg, (tx0, ty0, tx1, ty1) in render_tiles(pdf_path, page_no, rotate=90 * k):
        w_px, h_px = Image.open(io.BytesIO(jpeg)).size
        res = eng(jpeg, return_word_box=True)
        for words in res.word_results or ():
            for item in words:
                if not (isinstance(item, (tuple, list)) and len(item) == 3 and item[2] is not None):
                    continue                  # пустая плитка: RapidOCR 3.9.2 отдаёт (('', 1.0, None),)
                text, _score, pts = item
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                box = [tx0 + min(xs) / w_px * (tx1 - tx0), ty0 + min(ys) / h_px * (ty1 - ty0),
                       tx0 + max(xs) / w_px * (tx1 - tx0), ty0 + max(ys) / h_px * (ty1 - ty0)]
                out.append([*(round(c, 5) for c in to_visible(box, k)), text])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"angle": 90 * k, "words": out}, ensure_ascii=False), encoding="utf-8")
    return out


def cached_angle(cache_file: str | Path) -> int:
    """Угол выпрямления (градусы по часовой) из кэша слов страницы; старый формат кэша — 0."""
    try:
        data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(data.get("angle") or 0) if isinstance(data, dict) else 0


def words_to_text(words: list[list], angle: int = 0) -> str:
    """Слова RapidOCR -> текст страницы в порядке чтения. Рамки — в долях видимой страницы; у выпрямленного скана
    строки собираются в координатах выпрямленной страницы. Строка: центр слова по y в пределах половины высоты слова
    от центра строки; внутри строки — по x0; строки через «\\n»."""
    from inspector.pipeline.orient import to_upright

    k = (angle // 90) % 4
    items = []
    seen: dict[str, list[tuple]] = {}
    for w in words:
        x0, y0, x1, y1 = to_upright([float(c) for c in w[:4]], k)
        text = str(w[4]).strip()
        # плитки листа перекрываются: слово из зоны перекрытия приходит от обеих плиток — в текст берём один раз
        if text and not any(_iou((x0, y0, x1, y1), b) > 0.5 for b in seen.get(text, ())):
            seen.setdefault(text, []).append((x0, y0, x1, y1))
            items.append((x0, y0, x1, y1, text))
    items.sort(key=lambda it: ((it[1] + it[3]) / 2, it[0]))
    lines: list[dict] = []
    for x0, y0, x1, y1, text in items:
        cy, half = (y0 + y1) / 2, max(y1 - y0, 1e-6) / 2
        if lines and abs(cy - lines[-1]["cy"]) <= half:
            line = lines[-1]
            line["words"].append((x0, text))
            line["cy"] += (cy - line["cy"]) / len(line["words"])
        else:
            lines.append({"cy": cy, "words": [(x0, text)]})
    return "\n".join(" ".join(t for _, t in sorted(line["words"])) for line in lines)


def _iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / union if union > 0 else 0.0


def cache_file(data_dir: Path, sha: str, page_no: int, orient: bool = True) -> Path:
    """С выпрямлением и без — разные кэши: рамки без него на повёрнутых сканах неполные."""
    return data_dir / "cache" / ("boxes_orient" if orient else "boxes") / sha / f"{page_no}.json"


def page_words_safe(pdf_path: str, page_no: int, cache_file: str, gpu: bool = False, orient: bool = True) -> list:
    """page_words, но сбой одной страницы даёт ей пустой список, а не роняет рамки всех страниц этапа."""
    try:
        return page_words(pdf_path, page_no, cache_file, gpu, orient)
    except Exception as exc:  # noqa: BLE001 — битая страница: без рамок только она
        if gpu:                 # не хватило видеопамяти на большой лист и т. п. — эту страницу считает CPU
            log.warning("rapidocr gpu page failed, retry on cpu", extra={"pdf": pdf_path, "page": page_no,
                                                                         "error": str(exc)[:300]})
            return page_words_safe(pdf_path, page_no, cache_file, False, orient)
        log.warning("rapidocr page failed", extra={"pdf": pdf_path, "page": page_no, "error": str(exc)[:300]})
        return []


@contextmanager
def ocr_pool(gpu: bool, n: int, workers: int = 0, gpu_workers: int = 2):
    """Пул процессов RapidOCR на весь этап (на GPU каждый процесс грузит модели и подбирает свёртки — пересоздавать
    пул на каждую порцию дорого). -> (пул, на GPU ли). CPU — workers процессов (0 — ядра / 4, по 4 потока ONNX),
    GPU — gpu_workers процессов, у каждого свой предел памяти RAPIDOCR_GPU_MEM_GB."""
    use_gpu = bool(gpu) and gpu_ready()
    k = gpu_workers if use_gpu else (workers or (os.cpu_count() or 2) // 4)
    with ProcessPoolExecutor(max_workers=max(1, min(k, n)), mp_context=pool_context()) as pool:
        yield pool, use_gpu


def words_for_pages(todo: list[tuple[str, int, str]], gpu: bool = False, orient: bool = True, workers: int = 0,
                    pool: tuple | None = None, gpu_workers: int = 2) -> dict[tuple[str, int], list]:
    """todo: [(pdf_path, page_no, cache_file)] -> {(pdf_path, page_no): слова}. pool — (пул, на GPU) из ocr_pool;
    без него пул создаётся на этот вызов."""
    if not todo:
        return {}
    if pool is None:
        with ocr_pool(gpu, len(todo), workers, gpu_workers) as own:
            return words_for_pages(todo, gpu, orient, workers, own, gpu_workers)
    executor, use_gpu = pool
    results = executor.map(page_words_safe, [t[0] for t in todo], [t[1] for t in todo], [t[2] for t in todo],
                           [use_gpu] * len(todo), [orient] * len(todo))
    return {(p, n): r for (p, n, _), r in zip(todo, results)}
