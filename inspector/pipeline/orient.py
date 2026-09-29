"""Угол страницы перед RapidOCR (0/90/180/270). Поворачиваем, только если два независимых способа назвали один угол:
  A — голоса строк: детектор и Cls RapidOCR (те же модели, что у рамок) на самых «текстовых» клетках страницы 1024 px;
      вытянутая рамка стоит вертикально -> 90/270, Cls 0/180 -> направление (идея SberOCR pagescalerotator, свой код);
  B — RapidOrientation (RapidAI, Apache-2.0; модель PaddleClas, 4 класса) на всей странице; веса — resources/orientation.
Замер: 300 сканов корпуса, угол размечен вслепую, повёрнутых 15 %; правило «A и B
согласны» нашло 82 % повёрнутых, вредных поворотов 0,7 % (A один — 4,7 %, B один — 6,3 %); +0,17 с CPU на страницу.
k — сколько раз по 90° против часовой повёрнуто содержимое видимой страницы; исправление — /Rotate + 90·k."""
from __future__ import annotations

from collections import Counter

import numpy as np

from inspector.config import RESOURCES

MODEL = RESOURCES / "orientation" / "rapid_orientation.onnx"
DET_SIDE = 2048                                        # страница для определения угла: большая сторона, px
PAGE_SIDE, CELL, STEP, MAX_CELLS = 1024, 256, 128, 25  # A: страница, клетки, шаг, сколько клеток смотреть
VOTERS, MIN_VOTERS, MIN_RATIO = 18, 6, 3               # A: голосов достаточно / меньше — не поворачивать / рамка > 3:1
LABEL_TO_K = {"0": 0, "270": 1, "180": 2, "90": 3}     # метка B — угол по часовой (калибровка вне наборов: 24/24)
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

_SESSION = None


def page_image(page, side: int = DET_SIDE) -> np.ndarray:
    """Видимая страница (с /Rotate) в оттенках серого, большая сторона — side px."""
    import pymupdf

    zoom = side / max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].copy()


def _starts(size: int) -> list[int]:
    s = list(range(0, max(size - CELL, 0) + 1, STEP))
    return s + [size - CELL] if size > CELL and s[-1] != size - CELL else s


def votes_angle(gray: np.ndarray, engine) -> int:
    """A. Рамка горизонтальная: Cls 0 -> k 0, 180 -> k 2. Вертикальную RapidOCR поворачивает на 90° против часовой, как
    при чтении: Cls 0 -> текст шёл сверху вниз -> k 3; 180 -> снизу вверх -> k 1. Меньше 6 голосов — 0."""
    import cv2
    from rapidocr.utils.process_img import get_rotate_crop_image

    h, w = gray.shape
    s = PAGE_SIDE / max(h, w)
    img = cv2.resize(gray, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else gray
    H, W = img.shape
    cells = sorted(((float(np.std(cv2.Canny(img[y:y + CELL, x:x + CELL], 50, 150))), x, y)
                    for y in _starts(H) for x in _starts(W)), reverse=True)
    votes: list[int] = []
    for _, x, y in cells[:MAX_CELLS]:
        if len(votes) > VOTERS:
            break
        crop = cv2.cvtColor(np.ascontiguousarray(img[y:y + CELL, x:x + CELL]), cv2.COLOR_GRAY2BGR)
        det = engine.text_det(crop)
        if det.boxes is None or len(det.boxes) == 0:
            continue
        items = []
        for b in det.boxes:
            bw = max(np.linalg.norm(b[0] - b[1]), np.linalg.norm(b[2] - b[3]))
            bh = max(np.linalg.norm(b[0] - b[3]), np.linalg.norm(b[1] - b[2]))
            items.append((max(bw, bh) / max(min(bw, bh), 1.0), bool(bh > bw), b))
        items.sort(key=lambda t: -t[0])
        items = [t for t in items[:VOTERS] if t[0] > MIN_RATIO]
        if not items:
            continue
        cls = engine.text_cls([get_rotate_crop_image(crop, t[2].astype(np.float32)) for t in items]).cls_res
        for (_, vertical, _), (label, _score) in zip(items, cls):
            flip = label == "180"
            votes.append((1 if flip else 3) if vertical else (2 if flip else 0))
    if len(votes) < MIN_VOTERS:
        return 0
    counts = Counter(votes)
    return max(range(4), key=lambda a: (counts[a], a == 0))


def _session():
    global _SESSION
    if _SESSION is None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        _SESSION = ort.InferenceSession(str(MODEL), sess_options=opts, providers=["CPUExecutionProvider"])
    return _SESSION


def model_angle(gray: np.ndarray) -> int:
    """B. Предобработка как в пакете rapid-orientation 0.0.11: короткая сторона 256 (Lanczos), центр 224×224, нормировка
    ImageNet; метки модели — угол по часовой."""
    import cv2

    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    h, w = img.shape[:2]
    p = 256 / min(w, h)
    img = cv2.resize(img, (int(round(w * p)), int(round(h * p))), interpolation=cv2.INTER_LANCZOS4)
    h, w = img.shape[:2]
    y0, x0 = (h - 224) // 2, (w - 224) // 2
    x = ((img[y0:y0 + 224, x0:x0 + 224].astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
    sess = _session()
    labels = sess.get_modelmeta().custom_metadata_map["character"].splitlines()
    return LABEL_TO_K[labels[int(np.argmax(sess.run(None, {sess.get_inputs()[0].name: x})[0][0]))]]


def page_angle(page, engine) -> int:
    """k страницы по правилу «A и B согласны»; B считается, только если A нашёл поворот."""
    gray = page_image(page)
    a = votes_angle(gray, engine)
    if a == 0:
        return 0
    return a if model_angle(gray) == a else 0


def to_visible(box: list[float], k: int) -> list[float]:
    """Рамка в долях страницы, повёрнутой на 90·k по часовой (/Rotate + 90·k) -> доли исходной видимой страницы."""
    u0, v0, u1, v1 = box
    if k == 1:
        return [v0, 1 - u1, v1, 1 - u0]
    if k == 2:
        return [1 - u1, 1 - v1, 1 - u0, 1 - v0]
    if k == 3:
        return [1 - v1, u0, 1 - v0, u1]
    return [u0, v0, u1, v1]


def to_upright(box: list[float], k: int) -> list[float]:
    """Обратное к to_visible: рамка в долях видимой страницы -> доли страницы, повёрнутой на 90·k по часовой
    (нужно, чтобы собирать строки текста выпрямленного скана в порядке чтения)."""
    x0, y0, x1, y1 = box
    if k == 1:
        return [1 - y1, x0, 1 - y0, x1]
    if k == 2:
        return [1 - x1, 1 - y1, 1 - x0, 1 - y0]
    if k == 3:
        return [y0, 1 - x1, y1, 1 - x0]
    return [x0, y0, x1, y1]
