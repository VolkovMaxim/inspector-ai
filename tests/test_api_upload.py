"""A2: проверки (процессы), загрузка пакетов, лимиты 50/200 МБ, дозагрузка, запуск, импорт с диска; storage.save_stream."""
from __future__ import annotations

import hashlib
import io
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from inspector import config as config_module
from inspector import db as db_module
from inspector import statuses as S
from inspector.models import AuditLog, ConstructionObject, File, Job, Process
from inspector.testing import make_pdf

MB = 1024 * 1024


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def small_client(settings, monkeypatch):
    """Лимиты 1 МБ на файл и 2 МБ на запрос — те же проверки без файлов по 50 МБ."""
    monkeypatch.setenv("MAX_FILE_MB", "1")
    monkeypatch.setenv("MAX_UPLOAD_MB", "2")
    config_module.get_settings.cache_clear()
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str = "inspector") -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def upload(client, headers, files, field="files", **form):
    return client.post("/api/v1/documents/upload", headers=headers, data=form,
                       files=[(field, (name, content, "application/octet-stream")) for name, content in files])


@pytest.fixture()
def pdf(tmp_path) -> bytes:
    return make_pdf(tmp_path / "src.pdf", ["Раздел КР. Арматура класса А400"]).read_bytes()


def count(model, *where) -> int:
    with db_module.session_scope() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def store_files(settings) -> list:
    root = settings.data_dir / "store"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def tmp_files(settings) -> list:
    root = settings.data_dir / "tmp"
    return sorted(root.rglob("*")) if root.exists() else []


# ---------------------------------------------------------------- создание и просмотр проверок


def test_create_process_generates_latin_object_code(client):
    r = client.post("/api/v1/processes", headers=login(client),
                    json={"name": "Жилой дом, Новослободская", "address": "г. Москва", "customer": "ООО «З»"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert re.fullmatch(r"OBJ-ZHILOY-DOM-NOVOSLOBODSKAYA-[0-9A-F]{4}", p["object"]["object_id"])
    assert p["object"]["name"] == "Жилой дом, Новослободская" and p["object"]["address"] == "г. Москва"
    assert p["status"] == S.PENDING and p["indicator"] == "gray"
    assert p["protocol_version"] == 0 and p["batch"] == 1
    assert p["counts"] == {"files": 0, "pages": 0, "candidates": 0, "candidates_pending": 0, "confirmed": 0,
                           "negatives": 0, "suspicions": 0, "not_comparable": 0, "missing": 0, "clarifications": 0}
    assert [u["status"] for u in p["upload_status"]] == ["PD_MISSING", "RD_MISSING", "ID_MISSING"]
    for key in ("id", "stage", "progress", "scenario", "error", "sync_status", "created_at", "updated_at",
                "finalized_at"):
        assert key in p


def test_create_process_reuses_existing_object_id(client):
    h = login(client)
    a = client.post("/api/v1/processes", headers=h, json={"object_id": "OBJ-X1", "name": "Первый"}).json()
    b = client.post("/api/v1/processes", headers=h, json={"object_id": "OBJ-X1", "name": "Другое имя"}).json()
    assert a["id"] != b["id"]
    assert a["object"]["object_id"] == b["object"]["object_id"] == "OBJ-X1"
    assert b["object"]["name"] == "Первый"
    assert count(ConstructionObject, ConstructionObject.object_code == "OBJ-X1") == 1


def test_same_name_gives_distinct_object_codes(client):
    h = login(client)
    a = client.post("/api/v1/processes", headers=h, json={"name": "Школа"}).json()["object"]["object_id"]
    b = client.post("/api/v1/processes", headers=h, json={"name": "Школа"}).json()["object"]["object_id"]
    assert a != b and a.startswith("OBJ-SHKOLA-") and b.startswith("OBJ-SHKOLA-")


def test_create_process_needs_name_or_object_id(client):
    r = client.post("/api/v1/processes", headers=login(client), json={"address": "где-то"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "OBJECT_REQUIRED"


def test_ml_role_cannot_create_or_upload(client, pdf):
    h = login(client, "ml")
    assert client.post("/api/v1/processes", headers=h, json={"name": "X"}).status_code == 403
    assert upload(client, h, [("a.pdf", pdf)], name="X").status_code == 403
    assert count(Process) == 0


def test_get_unknown_process_is_404(client):
    r = client.get("/api/v1/processes/no-such-id", headers=login(client))
    assert r.status_code == 404 and r.json()["error"]["code"] == "PROCESS_NOT_FOUND"


def test_process_counts_indicator_and_upload_status(client, example_process):
    r = client.get(f"/api/v1/processes/{example_process.id}", headers=login(client))
    assert r.status_code == 200
    p = r.json()
    assert p["object"]["object_id"] == "OBJ-NOVOSLOBODSKAYA"
    assert p["counts"] == {"files": 4, "pages": 53, "candidates": 2, "candidates_pending": 2, "confirmed": 0,
                           "negatives": 1, "suspicions": 1, "not_comparable": 1, "missing": 1, "clarifications": 0}
    assert p["indicator"] == "red"                  # нерешённый кандидат с приоритетом HIGH
    assert {u["stage"]: u["status"] for u in p["upload_status"]} == {"PD": "PD_UPLOADED", "RD": "RD_UPLOADED",
                                                                       "ID": "ID_PARTIAL"}
    assert p["scenario"] == "FULL" and p["protocol_version"] == 1


def test_list_processes_filters_and_paginates(client, example_process):
    h = login(client)
    client.post("/api/v1/processes", headers=h, json={"name": "Школа на Речников"})
    all_ = client.get("/api/v1/processes", headers=h).json()
    assert all_["total"] == 2 and len(all_["items"]) == 2
    assert all_["items"][0]["object"]["name"] == "Школа на Речников"          # новые сверху
    ready = client.get("/api/v1/processes", params={"status": "READY"}, headers=h).json()
    assert [i["id"] for i in ready["items"]] == [example_process.id]
    both = client.get("/api/v1/processes", params={"status": "READY,PENDING"}, headers=h).json()
    assert both["total"] == 2
    found = client.get("/api/v1/processes", params={"q": "NOVOSLOBOD"}, headers=h).json()
    assert [i["id"] for i in found["items"]] == [example_process.id]
    page = client.get("/api/v1/processes", params={"limit": 1, "offset": 1}, headers=h).json()
    assert page["total"] == 2 and [i["id"] for i in page["items"]] == [example_process.id]
    bad = client.get("/api/v1/processes", params={"status": "DONE"}, headers=h)
    assert bad.status_code == 422
    today = client.get("/api/v1/processes", params={"date_from": "2000-01-01", "date_to": "2999-01-01"},
                       headers=h).json()
    assert today["total"] == 2
    none = client.get("/api/v1/processes", params={"date_to": "2000-01-01"}, headers=h).json()
    assert none["total"] == 0


def test_list_date_bounds_follow_client_timezone(client, db, example_process):
    """Проверка создана 27.09 в 22:30 UTC = 28.09 01:30 по Москве: для московского клиента это 28-е, не 27-е."""
    import datetime as dt
    from inspector.models import Process
    db.get(Process, example_process.id).created_at = dt.datetime(2026, 9, 27, 22, 30, tzinfo=dt.timezone.utc)
    db.commit()
    h = login(client)
    day = lambda d, tz: [i["id"] for i in client.get("/api/v1/processes", headers=h, params={  # noqa: E731
        "date_from": d, "date_to": d, "tz_offset_min": tz}).json()["items"]]
    assert day("2026-09-27", 0) == [example_process.id] and day("2026-09-28", 0) == []
    assert day("2026-09-28", -180) == [example_process.id] and day("2026-09-27", -180) == []


@pytest.mark.parametrize("rows, expected", [
    ([], "green"),
    ([(S.NEGATIVE_VERIFIED, "PENDING", "HIGH")], "green"),
    ([(S.CANDIDATE, "PENDING", "HIGH")], "red"),
    ([(S.CANDIDATE, "PENDING", "MEDIUM")], "yellow"),
    ([(S.CANDIDATE, S.CONFIRMED_VIOLATION, "LOW")], "red"),
    ([(S.CANDIDATE, S.NEGATIVE_VERIFIED, "HIGH")], "green"),
    ([(S.CANDIDATE, S.CLARIFICATION_REQUIRED, "HIGH")], "yellow"),
    ([(S.NOT_COMPARABLE, "PENDING", "HIGH")], "yellow"),
    ([(S.SUSPICION, "PENDING", "MEDIUM"), (S.MISSING_EVIDENCE, "PENDING", "HIGH")], "green"),
])
def test_indicator_rules(rows, expected):
    from inspector.api.processes import process_indicator

    assert process_indicator(1, [(*r, 1) for r in rows]) == expected
    assert process_indicator(0, [(*r, 1) for r in rows]) == "gray"          # протокола ещё нет


# ---------------------------------------------------------------- загрузка


def test_upload_creates_process_and_stores_file_by_hash(client, settings, pdf):
    r = upload(client, login(client), [("КР1 том 4.pdf", pdf)], stage="PD", name="Объект А")
    assert r.status_code == 200, r.text
    body = r.json()
    sha = hashlib.sha256(pdf).hexdigest()
    assert body["rejected"] == []
    assert len(body["accepted"]) == 1
    acc = body["accepted"][0]
    assert acc["name"] == "КР1 том 4.pdf" and acc["size"] == len(pdf) and acc["stage"] == "PD"
    assert acc["status"] == "pending"
    with db_module.session_scope() as s:
        f = s.get(File, acc["file_pk"])
        assert f.process_id == body["process_id"]
        assert f.file_id == f"LOCAL-{sha[:8]}" and f.sha256 == sha and f.size == len(pdf)
        assert f.declared_stage == "PD" and f.doc_stage == "PD" and f.stage_source == "declared"
        assert f.status == "pending" and f.batch == 1 and f.ext == ".pdf"
        assert f.storage_path == str(settings.data_dir / "store" / sha[:2] / f"{sha}.pdf")
        p = s.get(Process, body["process_id"])
        assert p.status == S.PENDING
    assert (settings.data_dir / "store" / sha[:2] / f"{sha}.pdf").read_bytes() == pdf
    assert tmp_files(settings) == []
    assert count(AuditLog, AuditLog.action == "upload", AuditLog.process_id == body["process_id"]) == 1


def test_upload_auto_stage_leaves_stage_for_pipeline(client, pdf):
    body = upload(client, login(client), [("a.pdf", pdf)], stage="AUTO", name="Объект").json()
    assert body["accepted"][0]["stage"] == "AUTO"
    with db_module.session_scope() as s:
        f = s.get(File, body["accepted"][0]["file_pk"])
        assert f.declared_stage is None and f.doc_stage is None and f.stage_source is None


def test_upload_accepts_brackets_field_name_and_keeps_relative_path(client, pdf):
    r = upload(client, login(client), [("../../Пакет/ПД/Том 1.pdf", pdf)], field="files[]", stage="pd", name="О")
    assert r.status_code == 200, r.text
    acc = r.json()["accepted"][0]
    assert acc["name"] == "Том 1.pdf" and acc["stage"] == "PD"
    with db_module.session_scope() as s:
        f = s.get(File, acc["file_pk"])
        assert f.original_name == "Том 1.pdf" and f.rel_path == "Пакет/ПД/Том 1.pdf"


def test_upload_rejects_unsupported_format_and_empty_file(client, pdf):
    r = upload(client, login(client), [("setup.exe", b"MZ\x90\x00"), ("empty.pdf", b""), ("noext", b"abc"),
                                       ("ok.PDF", pdf)], stage="RD", name="Объект")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["name"] for a in body["accepted"]] == ["ok.PDF"]
    rej = {x["name"]: x for x in body["rejected"]}
    assert rej["setup.exe"]["reason_code"] == "UNSUPPORTED_FORMAT"
    assert rej["noext"]["reason_code"] == "UNSUPPORTED_FORMAT"
    assert rej["empty.pdf"]["reason_code"] == "EMPTY_FILE"
    assert all(x["reason"] for x in body["rejected"])
    assert count(File) == 1


def test_file_over_limit_is_rejected_others_accepted(small_client, settings, pdf):
    big = b"%PDF-1.4\n" + b"0" * MB                             # 1 МБ + 9 байт > лимита 1 МБ
    r = upload(small_client, login(small_client), [("big.pdf", big), ("ok.pdf", pdf)], stage="ID", name="О")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["name"] for a in body["accepted"]] == ["ok.pdf"]
    assert body["rejected"] == [{"name": "big.pdf", "reason_code": "FILE_TOO_LARGE",
                                 "reason": body["rejected"][0]["reason"]}]
    assert "1 МБ" in body["rejected"][0]["reason"]
    assert [p.name for p in store_files(settings)] == [f"{hashlib.sha256(pdf).hexdigest()}.pdf"]


def test_request_over_total_limit_is_413_and_nothing_saved(small_client, settings):
    part = b"%PDF-1.4\n" + b"1" * int(1.05 * MB)                 # 2 × 1,05 МБ > 2 МБ на запрос
    r = upload(small_client, login(small_client), [("a.pdf", part), ("b.pdf", part + b"x")], stage="PD",
               name="Слишком большой пакет")
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert count(File) == 0 and count(Process) == 0
    assert count(ConstructionObject) == 0
    assert store_files(settings) == [] and tmp_files(settings) == []


def test_request_rejected_by_content_length_before_reading(small_client, settings):
    part = b"2" * int(0.9 * MB)                                 # 4 × 0,9 МБ: Content-Length > 2 МБ + запас
    files = [(f"{i}.pdf", part + bytes([i])) for i in range(4)]
    r = upload(small_client, login(small_client), files, stage="PD", name="X")
    assert r.status_code == 413 and r.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert count(File) == 0 and count(Process) == 0 and store_files(settings) == []


def test_upload_needs_files_and_valid_stage(client, pdf):
    h = login(client)
    r = client.post("/api/v1/documents/upload", headers=h, data={"name": "X", "stage": "PD"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "NO_FILES"
    r2 = upload(client, h, [("a.pdf", pdf)], stage="XX", name="X")
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "INVALID_STAGE"
    r3 = upload(client, h, [("a.pdf", pdf)], stage="PD")
    assert r3.status_code == 422 and r3.json()["error"]["code"] == "OBJECT_REQUIRED"
    assert count(Process) == 0


def test_upload_to_unknown_process_is_404(client, pdf):
    r = upload(client, login(client), [("a.pdf", pdf)], process_id="nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "PROCESS_NOT_FOUND"


@pytest.mark.parametrize("status, code", [(S.FINALIZED, "PROCESS_FINALIZED"), (S.PARSING, "PROCESS_BUSY")])
def test_upload_into_finalized_or_busy_process_is_409(client, db, example_process, pdf, status, code):
    example_process.status = status
    db.commit()
    before = count(File)
    r = upload(client, login(client), [("a.pdf", pdf)], process_id=example_process.id, stage="RD")
    assert r.status_code == 409 and r.json()["error"]["code"] == code
    assert count(File) == before


def test_upload_into_ready_process_starts_incremental_batch_2(client, example_process, pdf):
    r = upload(client, login(client), [("РД изм 2.pdf", pdf)], process_id=example_process.id, stage="RD")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["process_id"] == example_process.id and body["batch"] == 2 and body["job_id"]
    with db_module.session_scope() as s:
        p = s.get(Process, example_process.id)
        assert p.batch == 2 and p.status == S.PARSING
        new = s.get(File, body["accepted"][0]["file_pk"])
        assert new.batch == 2
        job = s.get(Job, body["job_id"])
        assert (job.kind, job.batch, job.status, job.process_id) == ("incremental", 2, "queued", p.id)


def test_upload_into_pending_process_keeps_batch_and_queues_nothing(client, pdf):
    h = login(client)
    pid = client.post("/api/v1/processes", headers=h, json={"name": "О"}).json()["id"]
    body = upload(client, h, [("a.pdf", pdf)], process_id=pid, stage="PD").json()
    body2 = upload(client, h, [("b.pdf", pdf + b"\n")], process_id=pid, stage="RD").json()
    assert body["batch"] == body2["batch"] == 1 and body["job_id"] is None
    assert count(Job) == 0
    p = client.get(f"/api/v1/processes/{pid}", headers=h).json()
    assert p["counts"]["files"] == 2
    ups = {u["stage"]: u for u in p["upload_status"]}
    assert ups["PD"]["status"] == "PD_UPLOADED" and ups["PD"]["pending_files"] == 1
    assert ups["ID"]["status"] == "ID_MISSING"


def test_upload_is_in_process_journal_for_inspector_without_ip(client, pdf):
    h = login(client)
    pid = upload(client, h, [("a.pdf", pdf)], stage="PD", name="О").json()["process_id"]
    items = client.get("/api/v1/audit", params={"process_id": pid}, headers=h).json()["items"]
    assert [i["action"] for i in items] == ["upload"]
    assert items[0]["details"]["accepted"] == 1 and "ip" not in items[0]


# ---------------------------------------------------------------- запуск


def test_start_queues_full_job_and_second_start_is_busy(client, pdf):
    h = login(client)
    pid = upload(client, h, [("a.pdf", pdf)], stage="PD", name="О").json()["process_id"]
    r = client.post(f"/api/v1/processes/{pid}/start", headers=h)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    with db_module.session_scope() as s:
        job = s.get(Job, job_id)
        assert (job.kind, job.status, job.process_id) == ("full", "queued", pid)
        assert s.get(Process, pid).status == S.PARSING
    again = client.post(f"/api/v1/processes/{pid}/start", headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "PROCESS_BUSY"
    assert count(AuditLog, AuditLog.action == "start") == 1


def test_start_without_files_or_when_finalized_is_409(client, db, example_process):
    h = login(client)
    pid = client.post("/api/v1/processes", headers=h, json={"name": "Пусто"}).json()["id"]
    r = client.post(f"/api/v1/processes/{pid}/start", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_FILES"
    example_process.status = S.FINALIZED
    db.commit()
    r2 = client.post(f"/api/v1/processes/{example_process.id}/start", headers=h)
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "PROCESS_FINALIZED"
    assert count(Job) == 0


# ---------------------------------------------------------------- импорт с диска сервера


@pytest.fixture()
def import_client(settings, tmp_path, monkeypatch):
    root = tmp_path / "imports"
    root.mkdir()
    monkeypatch.setenv("IMPORT_ROOTS", f"{root}:{tmp_path / 'other-root'}")
    config_module.get_settings.cache_clear()
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c, root


def test_import_local_walks_directory_with_same_checks(import_client, pdf, tmp_path):
    client, root = import_client
    pkg = root / "Новослободская"
    (pkg / "ПД").mkdir(parents=True)
    (pkg / "РД" / "КЖ").mkdir(parents=True)
    (pkg / "ПД" / "КР1.pdf").write_bytes(pdf)
    (pkg / "РД" / "КЖ" / "СВГ.pdf").write_bytes(pdf + b"\n%rd")
    (pkg / "readme.exe").write_bytes(b"MZ")
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(pdf + b"\n%secret")
    (pkg / "link.pdf").symlink_to(outside)
    h = login(client, "admin")
    pid = client.post("/api/v1/processes", headers=h, json={"name": "Новослободская"}).json()["id"]
    r = client.post(f"/api/v1/processes/{pid}/import-local", headers=h, json={"path": str(pkg), "stage": "AUTO"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(a["name"] for a in body["accepted"]) == ["КР1.pdf", "СВГ.pdf"]
    rej = {x["name"]: x["reason_code"] for x in body["rejected"]}
    assert rej == {"readme.exe": "UNSUPPORTED_FORMAT", "link.pdf": "PATH_NOT_ALLOWED"}
    with db_module.session_scope() as s:
        rels = sorted(f.rel_path for f in s.scalars(select(File).where(File.process_id == pid)))
    assert rels == ["ПД/КР1.pdf", "РД/КЖ/СВГ.pdf"]
    assert count(AuditLog, AuditLog.action == "import_local") == 1


def test_import_local_is_admin_only_and_confined_to_roots(import_client, tmp_path, pdf):
    client, root = import_client
    (root / "a.pdf").write_bytes(pdf)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "b.pdf").write_bytes(pdf)
    admin = login(client, "admin")
    pid = client.post("/api/v1/processes", headers=admin, json={"name": "О"}).json()["id"]
    r = client.post(f"/api/v1/processes/{pid}/import-local", headers=login(client, "inspector"),
                    json={"path": str(root)})
    assert r.status_code == 403
    r2 = client.post(f"/api/v1/processes/{pid}/import-local", headers=admin, json={"path": str(elsewhere)})
    assert r2.status_code == 403 and r2.json()["error"]["code"] == "PATH_NOT_ALLOWED"
    r3 = client.post(f"/api/v1/processes/{pid}/import-local", headers=admin,
                     json={"path": str(root / ".." / "elsewhere")})
    assert r3.status_code == 403
    r4 = client.post(f"/api/v1/processes/{pid}/import-local", headers=admin, json={"path": str(root / "nope")})
    assert r4.status_code == 404 and r4.json()["error"]["code"] == "PATH_NOT_FOUND"
    assert count(File) == 0


def test_import_local_disabled_without_roots(client, tmp_path):
    h = login(client, "admin")
    pid = client.post("/api/v1/processes", headers=h, json={"name": "О"}).json()["id"]
    r = client.post(f"/api/v1/processes/{pid}/import-local", headers=h, json={"path": str(tmp_path)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "IMPORT_DISABLED"


# ---------------------------------------------------------------- storage.save_stream


def test_save_stream_is_content_addressed_and_idempotent(settings):
    from inspector.storage import save_stream

    data = b"hello, inspector" * 1000
    sha, size, path = save_stream(io.BytesIO(data), "Документ.PDF")
    assert sha == hashlib.sha256(data).hexdigest() and size == len(data)
    assert path == settings.data_dir / "store" / sha[:2] / f"{sha}.pdf"
    assert path.read_bytes() == data
    sha2, _, path2 = save_stream(io.BytesIO(data), "copy.pdf")
    assert (sha2, path2) == (sha, path)
    assert tmp_files(settings) == []


def test_save_stream_stops_at_limit_and_cleans_up(settings):
    from inspector.storage import FileTooLarge, save_stream

    with pytest.raises(FileTooLarge):
        save_stream(io.BytesIO(b"x" * 5000), "a.pdf", max_bytes=4096)
    assert store_files(settings) == [] and tmp_files(settings) == []
    sha, size, _ = save_stream(io.BytesIO(b"x" * 4096), "a.pdf", max_bytes=4096)       # ровно лимит — можно
    assert size == 4096


def test_save_stream_sanitizes_extension(settings):
    from inspector.storage import save_stream

    _, _, path = save_stream(io.BytesIO(b"abc"), "weird.p$d/../f")
    assert path.parent.parent == settings.data_dir / "store"
    assert "/" not in path.name and "$" not in path.name
