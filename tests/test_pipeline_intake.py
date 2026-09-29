"""Этап приёма: архивы (cp866, zip slip), XML (DTD запрещён), картинки, дубли, мусор, подписи, манифест."""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from inspector.config import get_settings
from inspector.models import ConstructionObject, File, Process
from inspector.pipeline.intake import flatten_xml, run_intake, store_local
from inspector.testing import make_pdf


@pytest.fixture()
def process(db):
    obj = ConstructionObject(object_code="OBJ-INTAKE", name="t")
    db.add(obj)
    db.flush()
    p = Process(object_pk=obj.id)
    db.add(p)
    db.flush()
    return p


def add(db, process, path: Path, stage=None, rel=None) -> File:
    sha, size, stored = store_local(path, get_settings().data_dir, path.suffix.lower())
    f = File(process_id=process.id, file_id="LOCAL-" + sha[:8], original_name=path.name, rel_path=rel or path.name,
             ext=path.suffix.lower(), size=size, sha256=sha, storage_path=stored, declared_stage=stage, doc_stage=stage,
             status="pending", batch=1)
    db.add(f)
    db.flush()
    return f


def test_zip_with_cp866_names_nested_and_zip_slip(db, process, tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["Акт освидетельствования"])
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as z:
        z.write(pdf, "Вложенный акт.pdf")
    outer = tmp_path / "outer.zip"
    real = "Папка/АОСР №1.pdf".encode("cp866")            # как пишут архиваторы Windows: cp866 без UTF-8 флага
    placeholder = b"Q" * 5 + b"/" + b"Z" * (len(real) - 6)
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr(placeholder.decode("ascii"), pdf.read_bytes())
        z.write(inner, "inner.zip")
        z.writestr("../../evil.pdf", pdf.read_bytes())
    outer.write_bytes(outer.read_bytes().replace(placeholder, real))
    add(db, process, outer, "ID")
    stats = run_intake(db, process, 1, get_settings())
    files = {f.rel_path: f for f in db.scalars(select(File).where(File.process_id == process.id))}
    assert files["outer.zip"].status == "archive"
    assert files["outer.zip/Папка/АОСР №1.pdf"].status == "ok"
    assert files["outer.zip/Папка/АОСР №1.pdf"].doc_stage == "ID"          # стадия наследуется от архива
    assert files["outer.zip/inner.zip/Вложенный акт.pdf"].status == "duplicate"   # тот же PDF — дубль
    assert not any("evil" in k for k in files)
    assert stats["ok"] == 1


def test_xml_flatten_and_dtd_rejected(tmp_path):
    good = tmp_path / "pz.xml"
    good.write_text('<?xml version="1.0" encoding="UTF-8"?><ПЗ><ТЭП Площадь="1250.5"><Этажность>9</Этажность></ТЭП></ПЗ>',
                    encoding="utf-8")
    assert flatten_xml(good) == ["ПЗ/ТЭП@Площадь: 1250.5", "ПЗ/ТЭП/Этажность: 9"]
    evil = tmp_path / "evil.xml"
    evil.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', encoding="utf-8")
    with pytest.raises(ValueError):
        flatten_xml(evil)


def test_formats_junk_signature_manifest(db, process, tmp_path):
    from PIL import Image

    img = tmp_path / "скан.jpg"
    Image.new("RGB", (400, 300), "white").save(img)
    xml = tmp_path / "ПЗ.xml"
    xml.write_text("<ПЗ><Шифр>П-1</Шифр></ПЗ>", encoding="utf-8")
    pdf = make_pdf(tmp_path / "Том.pdf", ["Проектная документация"])
    sig = tmp_path / "Том.pdf.sig"
    sig.write_bytes(b"signature")
    thumbs = tmp_path / "Thumbs.db"
    thumbs.write_bytes(b"x")
    dwg = tmp_path / "план.dwg"
    dwg.write_bytes(b"AC1027")
    add(db, process, img, "ID")
    add(db, process, xml, "PD")
    f_pdf = add(db, process, pdf, None)
    add(db, process, sig, None)
    add(db, process, thumbs, None)
    add(db, process, dwg, "RD")
    manifest = tmp_path / "document_manifest.jsonl"
    manifest.write_text(json.dumps({"sha256": f_pdf.sha256, "file_id": "F0999", "stage": "PD"}) + "\n", encoding="utf-8")
    add(db, process, manifest, None)
    run_intake(db, process, 1, get_settings())
    by_name = {f.original_name: f for f in db.scalars(select(File).where(File.process_id == process.id))}
    assert by_name["скан.jpg"].status == "ok" and by_name["скан.jpg"].pdf_path.endswith(".pdf")
    assert by_name["ПЗ.xml"].status == "ok"
    assert by_name["Том.pdf"].file_id == "F0999" and by_name["Том.pdf"].doc_stage == "PD"
    assert by_name["Том.pdf"].stage_source == "manifest" and by_name["Том.pdf"].has_signature
    assert by_name["Thumbs.db"].status == "junk"
    assert by_name["план.dwg"].status == "unsupported" and "DWG" in by_name["план.dwg"].error
    assert by_name["document_manifest.jsonl"].status == "manifest"


SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


@pytest.mark.skipif(not SOFFICE or "/snap/" in SOFFICE or Path(SOFFICE).resolve().name == "snap",
                    reason="нет LibreOffice или он из snap (свой /tmp не видит каталог pytest); в Docker — пакет Debian")
def test_docx_converted_to_pdf(db, process, tmp_path):
    import docx

    d = docx.Document()
    d.add_paragraph("Акт освидетельствования скрытых работ № 5. Бетон В30.")
    path = tmp_path / "акт.docx"
    d.save(path)
    add(db, process, path, "ID")
    run_intake(db, process, 1, get_settings())
    f = db.scalars(select(File).where(File.process_id == process.id)).one()
    assert f.status == "ok" and f.pages == 1


def test_copy_capped_counts_real_bytes():
    import io

    from inspector.pipeline.intake import copy_capped

    out = io.BytesIO()
    assert copy_capped(io.BytesIO(b"x" * 1000), out, 0, limit=5000) == 1000
    with pytest.raises(ValueError):
        copy_capped(io.BytesIO(b"x" * 10_000), io.BytesIO(), 0, limit=5000)


def make_7z(tmp_path: Path, members: dict[str, bytes]) -> Path:
    """7z-архив самим bsdtar (libarchive пишет 7zip): имена элементов — как есть, без раскрытия шаблонов."""
    import subprocess

    src = tmp_path / "src"
    for name, data in members.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data)
    out = tmp_path / "pkg.7z"
    subprocess.run(["bsdtar", "--format", "7zip", "-cf", str(out), "-C", str(src), *members], check=True)
    return out


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="нет bsdtar")
def test_7z_member_names_with_pattern_characters_are_taken_literally(tmp_path):
    """bsdtar -x читает имя элемента как шаблон: «Лист [1].pdf» не находился — весь архив помечался битым, а элемент
    «*» получал склейку всех файлов архива. Имена в Windows со скобками обычны."""
    from inspector.pipeline.intake import extract_archive

    members = {"РД/Лист [1].pdf": b"AAAA", "*": b"STAR", "q?.pdf": b"QQ", "РД/Лист 1.pdf": b"BBBB"}
    work = tmp_path / "work"
    work.mkdir()
    got = {rel: dest.read_bytes() for rel, dest in extract_archive(make_7z(tmp_path, members), ".7z", work)}
    assert got == members


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="нет bsdtar")
def test_7z_unpack_limit_is_enforced_while_writing(tmp_path, monkeypatch):
    """У zip предел распаковки соблюдается по ходу копирования; у 7z/rar элемент писался на диск целиком и только
    потом сравнивался с пределом: 50 МБ архива нулей — сотни ГБ на томе данных до отказа."""
    from inspector.pipeline import intake

    monkeypatch.setattr(intake, "MAX_ARCHIVE_BYTES", 1 << 20)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="слишком большой"):
        intake.extract_archive(make_7z(tmp_path, {"нули.pdf": bytes(8 << 20)}), ".7z", work)
    assert sum(p.stat().st_size for p in work.iterdir()) <= 1 << 20
