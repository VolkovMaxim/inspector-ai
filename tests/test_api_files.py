"""A3: файлы и собранные документы проверки, страница (JSON), картинка страницы «как видна» (Rotate), скачивание."""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inspector import db as db_module
from inspector.models import AuditLog, Document, File, Page
from inspector.testing import make_pdf


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str = "inspector") -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def upload_one(client, headers, name: str, content: bytes, stage: str = "PD") -> dict:
    r = client.post("/api/v1/documents/upload", headers=headers, data={"stage": stage, "name": "Объект"},
                    files=[("files", (name, content, "application/octet-stream"))])
    assert r.status_code == 200, r.text
    return r.json()["accepted"][0] if r.json()["accepted"] else r.json()


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


# ---------------------------------------------------------------- файлы и документы


def test_process_files_list_has_passport_fields(client, example_process):
    r = client.get(f"/api/v1/processes/{example_process.id}/files", headers=login(client))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["file_id"] for i in items] == ["F0105", "F0137", "F0068", "LOCAL-0badf11e"]
    f = items[0]
    assert f["name"] == "НВС-2025.03-4.1-КР1.pdf" and f["stage"] == "PD" and f["stage_source"] == "declared"
    assert f["pages"] == 29 and f["document_code"] == "НВС-2025.03-4.1-КР1" and f["discipline"] == "КР"
    assert f["sha256"] == "a" * 64 and f["status"] == "ok" and f["batch"] == 1
    for key in ("file_pk", "rel_path", "ext", "size", "error", "ocr_pages", "revision", "approval_status",
                "uploaded_at"):
        assert key in f
    broken = items[3]
    assert broken["status"] == "broken" and "повреждён" in broken["error"]


def test_process_documents_list(client, db, example_process):
    f = db.scalars(select(File).where(File.file_id == "F0105")).one()
    db.add(Document(process_id=example_process.id, file_pk=f.id, first_page=1, last_page=29, doc_type="ТОМ",
                    stage="PD", section="КР", title="Конструктивные решения", document_code="НВС-2025.03-4.1-КР1",
                    sheet="1", revision="0", approval_status="APPROVED", is_actual=True))
    db.commit()
    r = client.get(f"/api/v1/processes/{example_process.id}/documents", headers=login(client))
    assert r.status_code == 200
    d = r.json()["items"][0]
    assert d["file_pk"] == f.id and d["file_id"] == "F0105" and d["file_name"] == "НВС-2025.03-4.1-КР1.pdf"
    assert (d["first_page"], d["last_page"], d["doc_type"], d["stage"], d["section"]) == (1, 29, "ТОМ", "PD", "КР")
    assert d["is_actual"] is True and d["clarification"] is False and d["clarification_reason"] is None
    for key in ("id", "title", "document_code", "sheet", "revision", "approval_status"):
        assert key in d


def test_files_of_unknown_process_is_404(client):
    r = client.get("/api/v1/processes/nope/files", headers=login(client))
    assert r.status_code == 404 and r.json()["error"]["code"] == "PROCESS_NOT_FOUND"


# ---------------------------------------------------------------- страница: JSON


def test_page_json_and_text_alias(client, db, example_process):
    f = db.scalars(select(File).where(File.file_id == "F0068")).one()
    page = db.scalars(select(Page).where(Page.file_pk == f.id, Page.page_no == 7)).one()
    page.text, page.page_class, page.other_classes, page.is_first_page = "АКТ ОСВИДЕТЕЛЬСТВОВАНИЯ", "АОСР", ["ТАБЛ"], True
    db.commit()
    h = login(client)
    r = client.get(f"/api/v1/files/{f.id}/pages/7", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body == {"page_no": 7, "width": 595.0, "height": 842.0, "rotation": 0, "text_source": "ocr",
                    "text": "АКТ ОСВИДЕТЕЛЬСТВОВАНИЯ", "page_class": "АОСР", "other_classes": ["ТАБЛ"],
                    "is_first_page": True, "section": None, "title": None, "quality_label": "плохой скан",
                    "low_quality": True, "document_pk": None}
    assert client.get(f"/api/v1/files/{f.id}/pages/7/text", headers=h).json() == body
    missing = client.get(f"/api/v1/files/{f.id}/pages/99", headers=h)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "PAGE_NOT_FOUND"
    nofile = client.get("/api/v1/files/999999/pages/1", headers=h)
    assert nofile.status_code == 404 and nofile.json()["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------- картинка страницы


def test_rotated_page_image_is_landscape_and_cached(client, settings, tmp_path):
    h = login(client)
    content = make_pdf(tmp_path / "rot.pdf", ["Повёрнутый лист"], size=(595, 842), rotate=90).read_bytes()
    acc = upload_one(client, h, "rot.pdf", content)
    r = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image", params={"width": 400}, headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    w, hgt = png_size(r.content)
    assert abs(w - 400) <= 1 and hgt < w                          # страница видна «лёжа»: шире, чем выше
    assert abs(hgt - round(400 * 595 / 842)) <= 1
    sha = hashlib.sha256(content).hexdigest()
    cached = settings.data_dir / "cache" / "renders" / sha / "1_400.png"
    assert cached.read_bytes() == r.content
    again = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image", params={"width": 400}, headers=h)
    assert again.content == r.content


def _ink_bbox(png: bytes) -> tuple[float, float, float, float]:
    """Рамка тёмных пикселей картинки в долях её размеров."""
    import pymupdf

    pix = pymupdf.Pixmap(png)
    n, w, h, s = pix.n, pix.width, pix.height, pix.samples
    xs, ys = [], []
    for y in range(h):
        row = y * pix.stride
        for x in range(w):
            if s[row + x * n] < 128:
                xs.append(x)
                ys.append(y)
    return min(xs) / w, min(ys) / h, (max(xs) + 1) / w, (max(ys) + 1) / h


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_layer_bbox_matches_rendered_page_for_rotation(client, tmp_path, rotate):
    """Review Focus 4: рамка слов конвейера (доли видимой страницы) совпадает с тем, что видно на картинке API."""
    geometry = pytest.importorskip("inspector.pipeline.geometry")
    import pymupdf

    path = make_pdf(tmp_path / f"r{rotate}.pdf", ["\n\n\n\n\n\n\n\n\n\n            Форшахта А400"],
                    size=(595, 842), rotate=rotate)
    with pymupdf.open(path) as doc:
        words = geometry.layer_words(doc[0])
    wx0, wy0 = min(w[0] for w in words), min(w[1] for w in words)
    wx1, wy1 = max(w[2] for w in words), max(w[3] for w in words)
    h = login(client)
    acc = upload_one(client, h, f"r{rotate}.pdf", path.read_bytes())
    png = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image", params={"width": 600}, headers=h).content
    ix0, iy0, ix1, iy1 = _ink_bbox(png)
    tol = 0.01
    assert wx0 - tol <= ix0 and ix1 <= wx1 + tol and wy0 - tol <= iy0 and iy1 <= wy1 + tol, \
        ((ix0, iy0, ix1, iy1), (wx0, wy0, wx1, wy1))
    # чернила занимают большую часть рамки вдоль строки (иначе рамка «уехала» бы, оставаясь широкой)
    along = (ix1 - ix0) / (wx1 - wx0) if rotate in (0, 180) else (iy1 - iy0) / (wy1 - wy0)
    assert along > 0.8


def test_image_width_is_bounded(client, tmp_path):
    h = login(client)
    acc = upload_one(client, h, "a.pdf", make_pdf(tmp_path / "a.pdf", ["x"]).read_bytes())
    for width in (199, 3001):
        r = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image", params={"width": width}, headers=h)
        assert r.status_code == 422
    default = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image", headers=h)
    assert abs(png_size(default.content)[0] - 1400) <= 1


def test_image_errors_page_range_unconverted_and_broken(client, tmp_path):
    h = login(client)
    one_page = upload_one(client, h, "one.pdf", make_pdf(tmp_path / "one.pdf", ["x"]).read_bytes())
    r = client.get(f"/api/v1/files/{one_page['file_pk']}/pages/2/image", headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "PAGE_NOT_FOUND"
    docx = upload_one(client, h, "письмо.docx", b"PK\x03\x04 fake docx")
    r2 = client.get(f"/api/v1/files/{docx['file_pk']}/pages/1/image", headers=h)
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "NOT_CONVERTED"
    broken = upload_one(client, h, "broken.pdf", b"%PDF-1.4 this is not a real pdf")
    r3 = client.get(f"/api/v1/files/{broken['file_pk']}/pages/1/image", headers=h)
    assert r3.status_code == 422 and r3.json()["error"]["code"] == "FILE_BROKEN"


def test_image_uses_converted_pdf_when_present(client, db, tmp_path):
    h = login(client)
    docx = upload_one(client, h, "письмо.docx", b"PK\x03\x04 fake docx")
    converted = make_pdf(tmp_path / "conv.pdf", ["из DOCX"], size=(842, 595))
    f = db.get(File, docx["file_pk"])
    f.pdf_path = str(converted)
    db.commit()
    r = client.get(f"/api/v1/files/{docx['file_pk']}/pages/1/image", params={"width": 300}, headers=h)
    assert r.status_code == 200
    w, hgt = png_size(r.content)
    assert hgt < w


# ---------------------------------------------------------------- скачивание и токен в ссылке


def test_download_returns_original_with_utf8_filename(client, tmp_path):
    h = login(client)
    content = make_pdf(tmp_path / "t.pdf", ["Том 1"]).read_bytes()
    acc = upload_one(client, h, "Том 1.pdf", content)
    r = client.get(f"/api/v1/files/{acc['file_pk']}/download", headers=h)
    assert r.status_code == 200 and r.content == content
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=utf-8''%d0%a2%d0%be%d0%bc%201.pdf" in disposition.lower()      # «Том 1.pdf» в UTF-8


def test_media_links_accept_access_token_query_but_json_does_not(client, tmp_path):
    h = login(client)
    token = h["Authorization"].split()[1]
    acc = upload_one(client, h, "a.pdf", make_pdf(tmp_path / "a.pdf", ["x"]).read_bytes())
    img = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image", params={"width": 200, "access_token": token})
    assert img.status_code == 200
    dl = client.get(f"/api/v1/files/{acc['file_pk']}/download", params={"access_token": token})
    assert dl.status_code == 200
    as_json = client.get(f"/api/v1/files/{acc['file_pk']}/pages/1", params={"access_token": token})
    assert as_json.status_code == 401
    assert client.get(f"/api/v1/files/{acc['file_pk']}/pages/1/image").status_code == 401


def test_download_is_audited_and_missing_file_is_not(client, tmp_path):
    h = login(client)
    content = make_pdf(tmp_path / "t.pdf", ["Том 1"]).read_bytes()
    acc = upload_one(client, h, "Том 1.pdf", content)
    assert client.get(f"/api/v1/files/{acc['file_pk']}/download", headers=h).status_code == 200
    with db_module.session_scope() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "file_download")).one()
        f = s.get(File, acc["file_pk"])
        assert row.user_login == "inspector" and row.ip == "testclient" and row.process_id == f.process_id
        assert row.object_code
        assert row.details == {"file_pk": f.id, "file_id": f.file_id, "name": "Том 1.pdf", "sha256": f.sha256,
                               "stage": "PD"}
        Path(f.storage_path).unlink()                                       # файл пропал из хранилища
    assert client.get(f"/api/v1/files/{acc['file_pk']}/download", headers=h).status_code == 404
    assert client.get("/api/v1/files/987654/download", headers=h).status_code == 404
    with db_module.session_scope() as s:
        assert len(s.scalars(select(AuditLog).where(AuditLog.action == "file_download")).all()) == 1
