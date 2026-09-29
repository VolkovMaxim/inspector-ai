"""Этап 1 — приём партии файлов: распаковка архивов (рекурсивно), мусор, дубли по SHA-256, подписи .sig, манифест
организатора, приведение DOCX/DOC/XLS(X)/RTF/ODT, картинок и XML к PDF. Ошибка файла -> статус и понятное сообщение,
процесс не падает (модуль 12 ТЗ)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.models import File, Process

log = logging.getLogger("inspector.intake")

ARCHIVES = {".zip", ".7z", ".rar"}
OFFICE = {".docx", ".doc", ".rtf", ".odt", ".xls", ".xlsx", ".ods"}
IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
JUNK_EXT = {".bak", ".log", ".tmp", ".dwl", ".dwl2", ".lnk"}
UNSUPPORTED = {".dwg": "DWG не рассматривается (условия задачи): нужен PDF чертежа",
               ".mge": "Сметный формат Гранд-Смета не поддерживается", ".gsfx": "Сметный формат не поддерживается"}
MAX_DEPTH = 5
MAX_ARCHIVE_FILES = 20000
MAX_ARCHIVE_BYTES = 30 * 1024 ** 3
STAGE_VALUES = {"PD", "RD", "ID"}


# ------------------------------------------------------------------------------------------------ хранилище
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def store_local(src: Path, data_dir: Path, ext: str) -> tuple[str, int, str]:
    """Файл -> хранилище по хешу (та же схема, что у загрузки через API): store/<sha[:2]>/<sha><ext>."""
    sha = sha256_file(src)
    dest = data_dir / "store" / sha[:2] / f"{sha}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    if not dest.exists():
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    return sha, size, str(dest)


def zip_name(info: zipfile.ZipInfo) -> str:
    """Кириллица в zip без UTF-8 флага приходит в cp866 (архиваторы Windows)."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp866", errors="replace")


def safe_rel(name: str) -> str | None:
    """Путь внутри архива без выхода наружу (zip slip) и без абсолютных путей."""
    p = PurePosixPath(name.replace("\\", "/"))
    parts = [x for x in p.parts if x not in ("", ".", "/")]
    if not parts or any(x == ".." for x in parts):
        return None
    return "/".join(parts)


def copy_capped(src, dst, total: int, limit: int | None = None) -> int:
    """Копирование с потолком общего объёма распаковки по фактическим байтам (защита от zip-бомб). -> новый итог."""
    limit = MAX_ARCHIVE_BYTES if limit is None else limit
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            return total
        total += len(chunk)
        if total > limit:
            raise ValueError("Архив слишком большой (защита от zip-бомбы)")
        dst.write(chunk)


def extract_archive(path: Path, ext: str, work: Path) -> list[tuple[str, Path]]:
    """-> [(путь внутри архива, извлечённый файл)]. zip — модулем zipfile (имена cp866), 7z/rar — bsdtar поштучно
    (имя на диск не пишется: длинные кириллические имена > 255 байт не ломают распаковку)."""
    out: list[tuple[str, Path]] = []
    total = 0
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            for n, info in enumerate(i for i in z.infolist() if not i.is_dir()):
                rel = safe_rel(zip_name(info))
                if rel is None:
                    continue
                if n >= MAX_ARCHIVE_FILES or total + info.file_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Архив слишком большой (защита от zip-бомбы)")
                dest = work / f"{n:06d}"
                with z.open(info) as src, open(dest, "wb") as dst:
                    total = copy_capped(src, dst, total)      # заявленному размеру в заголовке не верим
                out.append((rel, dest))
        return out
    if shutil.which("bsdtar") is None:
        raise RuntimeError("Нет распаковщика bsdtar (пакет libarchive-tools) для 7z/rar")
    names = subprocess.run(["bsdtar", "-tf", str(path)], capture_output=True, timeout=300, check=True)
    for n, name in enumerate(x for x in names.stdout.decode("utf-8", "replace").splitlines() if x and not x.endswith("/")):
        rel = safe_rel(name)
        if rel is None:
            continue
        if n >= MAX_ARCHIVE_FILES:
            raise ValueError("Архив слишком большой (защита от zip-бомбы)")
        dest = work / f"{n:06d}"
        literal = re.sub(r"([\\*?\[])", r"\\\1", name)    # bsdtar читает имя как шаблон: «[1]», «*», «?» — буквально
        cmd = ["bsdtar", "-xOf", str(path), "--", literal]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        watchdog = threading.Timer(600, proc.kill)         # как прежний timeout=600: зависший bsdtar снимается
        watchdog.start()
        try:                                               # потолок распаковки — по мере записи, как у zip
            with open(dest, "wb") as dst:
                total = copy_capped(proc.stdout, dst, total)
            if proc.wait() != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd)
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        out.append((rel, dest))
    return out


# ------------------------------------------------------------------------------------------------ конвертация
def convert_office(src: Path, out_dir: Path, work_root: Path, timeout: int = 240) -> Path:
    """LibreOffice headless -> PDF. Отдельный профиль на вызов: параллельные конвертации не мешают друг другу.
    Временный каталог — внутри DATA_DIR: у LibreOffice из snap свой /tmp, файлы из системного /tmp он не видит."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice") or "/snap/bin/libreoffice"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root) as tmp:
        work = Path(tmp) / ("input" + src.suffix.lower())
        shutil.copyfile(src, work)
        profile = Path(tmp) / "profile"
        subprocess.run([soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
                        "--convert-to", "pdf", "--outdir", tmp, str(work)],
                       capture_output=True, timeout=timeout, check=False)
        produced = Path(tmp) / "input.pdf"
        if not produced.exists():
            raise RuntimeError("LibreOffice не смог сконвертировать файл в PDF")
        dest = out_dir / (src.stem + ".pdf")
        shutil.move(str(produced), dest)
        return dest


def convert_image(src: Path, dest: Path) -> Path:
    import pymupdf

    img = pymupdf.open(src)
    pdf = pymupdf.open("pdf", img.convert_to_pdf())
    dest.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(dest)
    return dest


def flatten_xml(src: Path) -> list[str]:
    """XML -> строки «путь: значение» (ПЗ по схеме Минстроя и т.п.): модели проще читать, чем теги."""
    import xml.etree.ElementTree as ET

    raw = src.read_bytes()
    head = raw[:1 << 16].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head or b"<!ENTITY" in raw.upper():
        # загружаемый XML — недоверенный: DTD и сущности запрещены (XXE, «billion laughs»); данным они не нужны
        raise ValueError("XML с DTD/сущностями отклонён из соображений безопасности")
    root = ET.fromstring(raw)
    lines: list[str] = []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def walk(el, path: str) -> None:
        here = f"{path}/{local(el.tag)}" if path else local(el.tag)
        for k, v in el.attrib.items():
            lines.append(f"{here}@{local(k)}: {v}")
        if el.text and el.text.strip():
            lines.append(f"{here}: {el.text.strip()}")
        for child in el:
            walk(child, here)
    walk(root, "")
    return lines


def convert_xml(src: Path, dest: Path, lines_per_page: int = 60) -> Path:
    import pymupdf

    from inspector.testing import font_path

    lines = flatten_xml(src)
    doc = pymupdf.open()
    for start in range(0, max(1, len(lines)), lines_per_page):
        page = doc.new_page(width=595, height=842)
        y = 40.0
        for line in lines[start:start + lines_per_page] or ["(пустой XML)"]:
            page.insert_text((30, y), line[:140], fontsize=8, fontfile=font_path(), fontname="dejavu")
            y += 12.5
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dest)
    return dest


def check_pdf(path: Path) -> tuple[int, str | None]:
    """-> (число страниц, ошибка). Битый или запароленный PDF — ошибка с понятным текстом."""
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 — любая ошибка разбора = повреждённый файл
        return 0, f"PDF повреждён, загрузите файл повторно ({type(exc).__name__})"
    if doc.needs_pass:
        return 0, "PDF защищён паролем"
    if doc.page_count == 0:
        return 0, "PDF без страниц"
    try:
        doc[0].get_text("words")
    except Exception as exc:  # noqa: BLE001
        return 0, f"PDF повреждён: страница 1 не читается ({type(exc).__name__})"
    return doc.page_count, None


# ------------------------------------------------------------------------------------------------ манифест
def read_manifest(path: Path) -> dict[str, dict]:
    """JSONL организатора: sha256 -> {file_id, stage}."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("sha256") and m.get("file_id"):
                out[m["sha256"]] = {"file_id": m["file_id"], "stage": m.get("stage")}
    return out


def is_manifest(f: File) -> bool:
    return f.ext == ".jsonl" and "manifest" in f.original_name.lower()


# ------------------------------------------------------------------------------------------------ этап
def _classify_name(f: File) -> str | None:
    name = f.original_name.lower()
    if name in JUNK_NAMES or f.ext in JUNK_EXT or name.startswith("~$"):
        return "junk"
    if f.size == 0:
        return "junk"
    return None


def process_file(f: File, settings: Settings, session: Session, depth: int) -> list[File]:
    """Обработать один pending-файл. -> новые файлы (из архива), которые тоже надо обработать."""
    src = Path(f.storage_path)
    junk = _classify_name(f)
    if junk:
        f.status, f.error = "junk", "Служебный или пустой файл"
        return []
    if f.ext == ".sig":
        f.status = "signature"
        return []
    if is_manifest(f):
        f.status = "manifest"
        return []
    if f.ext in UNSUPPORTED:
        f.status, f.error = "unsupported", UNSUPPORTED[f.ext]
        return []
    if f.ext in ARCHIVES:
        if depth >= MAX_DEPTH:
            f.status, f.error = "unsupported", "Слишком глубокая вложенность архивов"
            return []
        children: list[File] = []
        with tempfile.TemporaryDirectory(dir=settings.data_dir) as tmp:
            try:
                items = extract_archive(src, f.ext, Path(tmp))
            except Exception as exc:  # noqa: BLE001
                f.status, f.error = "broken", f"Архив не распаковывается: {exc}"[:500]
                return []
            for rel, path in items:
                ext = Path(rel).suffix.lower()
                sha, size, stored = store_local(path, settings.data_dir, ext)
                child = File(process_id=f.process_id, file_id="LOCAL-" + sha[:8], original_name=Path(rel).name,
                             rel_path=f"{f.rel_path or f.original_name}/{rel}", ext=ext, size=size, sha256=sha,
                             storage_path=stored, declared_stage=f.declared_stage, doc_stage=f.declared_stage,
                             stage_source="declared" if f.declared_stage else None, status="pending",
                             parent_pk=f.id, batch=f.batch)
                session.add(child)
                children.append(child)
        f.status = "archive"
        session.flush()
        return children
    converted = settings.data_dir / "converted" / f"{f.sha256}.pdf"
    try:
        if f.ext == ".pdf":
            pdf = src
        elif converted.exists():
            pdf = converted
        elif f.ext in OFFICE:
            pdf = convert_office(src, converted.parent / f.sha256, settings.data_dir / "tmp")
            converted.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf), converted)
            pdf = converted
        elif f.ext in IMAGES:
            pdf = convert_image(src, converted)
        elif f.ext == ".xml":
            pdf = convert_xml(src, converted)
        else:
            f.status, f.error = "unsupported", f"Формат {f.ext or '(без расширения)'} не поддерживается: PDF, DOCX, XML"
            return []
    except Exception as exc:  # noqa: BLE001
        f.status, f.error = "broken", f"Файл не читается: {exc}"[:500]
        return []
    pages, error = check_pdf(Path(pdf))
    if error:
        f.status, f.error = "broken", error
        return []
    f.pdf_path, f.pages, f.status = str(pdf), pages, "ok"
    return []


def run_intake(session: Session, process: Process, batch: int, settings: Settings, progress=None) -> dict:
    """Обработать все pending-файлы партии (включая извлечённые из архивов), затем дубли, подписи, манифест."""
    queue = list(session.scalars(select(File).where(File.process_id == process.id, File.batch == batch,
                                                    File.status == "pending").order_by(File.id)))
    depth = {f.id: 0 for f in queue}
    done = 0
    while queue:
        f = queue.pop(0)
        children = process_file(f, settings, session, depth.get(f.id, 0))
        for c in children:
            depth[c.id] = depth.get(f.id, 0) + 1
        queue.extend(children)
        done += 1
        if progress:
            progress(done, done + len(queue))
    session.flush()
    all_files = list(session.scalars(select(File).where(File.process_id == process.id).order_by(File.id)))
    # манифест: партии процесса + глобальный из настроек
    manifest: dict[str, dict] = {}
    if settings.manifest_path and Path(settings.manifest_path).exists():
        manifest.update(read_manifest(Path(settings.manifest_path)))
    for f in all_files:
        if f.status == "manifest":
            manifest.update(read_manifest(Path(f.storage_path)))
    first_by_sha: dict[str, File] = {}
    for f in all_files:
        if f.status == "ok" and f.sha256 not in first_by_sha:
            first_by_sha[f.sha256] = f
    stats = {"ok": 0, "duplicate": 0, "broken": 0, "unsupported": 0, "junk": 0, "archive": 0}
    for f in all_files:
        if f.batch != batch:
            continue
        if f.status == "ok":
            first = first_by_sha[f.sha256]
            if first.id != f.id:
                f.status, f.duplicate_of_pk = "duplicate", first.id
                f.error = f"Дубль файла {first.original_name}"
        m = manifest.get(f.sha256)
        if m:
            f.file_id = m["file_id"]
            if not f.declared_stage and m.get("stage") in STAGE_VALUES:
                f.doc_stage, f.stage_source = m["stage"], "manifest"
        elif not f.file_id:
            f.file_id = "LOCAL-" + f.sha256[:8]
        stats[f.status] = stats.get(f.status, 0) + 1
    # подписи: «имя.pdf.sig» или «имя.sig» рядом с файлом -> признак подписанного документа (ИД в электронном виде)
    by_path = {(f.rel_path or f.original_name).lower(): f for f in all_files if f.status == "ok"}
    by_stem = {}
    for f in all_files:
        if f.status == "ok":
            by_stem.setdefault(str(PurePosixPath((f.rel_path or f.original_name).lower()).with_suffix("")), f)
    for f in all_files:
        if f.status != "signature":
            continue
        key = (f.rel_path or f.original_name).lower()[:-4]
        target = by_path.get(key) or by_stem.get(str(PurePosixPath(key).with_suffix(""))) or by_stem.get(key)
        if target is not None:
            target.has_signature = True
            target.approval_status = "SIGNED"
    session.flush()
    return stats
