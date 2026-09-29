"""Хранилище файлов по хешу содержимого: DATA_DIR/store/<sha[:2]>/<sha><ext>.

Запись потоком: временный файл в DATA_DIR/tmp (та же файловая система — rename атомарен), SHA-256 и размер считаются
на лету, при превышении лимита запись прекращается (FileTooLarge) и временный файл удаляется."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from inspector.config import Settings, get_settings

CHUNK = 1024 * 1024
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


class FileTooLarge(Exception):
    def __init__(self, limit_bytes: int, read_bytes: int):
        super().__init__(f"Файл больше лимита {limit_bytes} байт")
        self.limit_bytes = limit_bytes
        self.read_bytes = read_bytes


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    size: int
    path: Path
    created: bool          # False — такой файл уже был в хранилище (тот же хеш)


def safe_ext(name: str) -> str:
    ext = Path(name.replace("\\", "/")).suffix.lower()
    return ext if _EXT_RE.match(ext) else ""


def store_path(sha256: str, ext: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.data_dir / "store" / sha256[:2] / f"{sha256}{ext}"


def store_stream(fileobj: BinaryIO, name: str, max_bytes: int | None = None,
                 settings: Settings | None = None) -> StoredFile:
    """Как save_stream, но сообщает, создан ли файл этим вызовом (нужно, чтобы убрать его при откате загрузки)."""
    settings = settings or get_settings()
    tmp_dir = settings.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="upload-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = fileobj.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise FileTooLarge(max_bytes, size)
                digest.update(chunk)
                out.write(chunk)
        sha = digest.hexdigest()
        dest = store_path(sha, safe_ext(name), settings)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            os.unlink(tmp_name)
            return StoredFile(sha, size, dest, created=False)
        os.replace(tmp_name, dest)
        return StoredFile(sha, size, dest, created=True)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def save_stream(fileobj: BinaryIO, name: str, max_bytes: int | None = None,
                settings: Settings | None = None) -> tuple[str, int, Path]:
    """Сохранить поток в хранилище. max_bytes по умолчанию — MAX_FILE_MB. -> (sha256, размер, путь)."""
    settings = settings or get_settings()
    if max_bytes is None:
        max_bytes = settings.max_file_mb * 1024 * 1024
    stored = store_stream(fileobj, name, max_bytes, settings)
    return stored.sha256, stored.size, stored.path


# ---------------------------------------------------------------- картинки страниц

class PageOutOfRange(Exception):
    pass


class PdfUnreadable(Exception):
    pass


# PyMuPDF не поддерживает работу из нескольких потоков (FAQ PyMuPDF: «does not support multithreaded use»), а API
# выполняет синхронные ручки в пуле потоков. Поэтому весь PyMuPDF в процессе api — в одном выделенном потоке.
_PDF_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pymupdf")
_KEY_RE = re.compile(r"^[0-9A-Za-z_-]{1,128}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render(pdf_path: Path, page_no: int, width: int) -> bytes:
    import pymupdf

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 — любой отказ MuPDF = «файл не читается»
        raise PdfUnreadable(str(exc)) from exc
    try:
        if doc.needs_pass or doc.page_count == 0:
            raise PdfUnreadable("PDF зашифрован или не содержит страниц")
        if not 1 <= page_no <= doc.page_count:
            raise PageOutOfRange(f"В файле {doc.page_count} стр.")
        page = doc[page_no - 1]
        zoom = width / page.rect.width               # page.rect — видимая страница (CropBox с учётом Rotate)
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    except (PageOutOfRange, PdfUnreadable):
        raise
    except Exception as exc:  # noqa: BLE001
        raise PdfUnreadable(str(exc)) from exc
    finally:
        doc.close()


def render_page_png(pdf_path: str | Path, page_no: int, width: int, cache_key: str | None = None,
                    settings: Settings | None = None) -> bytes:
    """PNG страницы так, как она видна (с учётом Rotate и CropBox), шириной width px. Кэш на диске:
    DATA_DIR/cache/renders/<sha256 файла>/<страница>_<ширина>.png. page_no — с 1."""
    settings = settings or get_settings()
    pdf_path = Path(pdf_path)
    key = cache_key if cache_key and _KEY_RE.match(cache_key) else sha256_file(pdf_path)
    cache = settings.data_dir / "cache" / "renders" / key / f"{page_no}_{width}.png"
    if cache.is_file():
        return cache.read_bytes()
    png = _PDF_EXECUTOR.submit(_render, pdf_path, page_no, width).result()
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f".{cache.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(png)
    os.replace(tmp, cache)
    return png
