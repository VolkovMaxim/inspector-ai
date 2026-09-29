"""A4: живой протокол и снимки версий, история версий, выгрузки PDF/DOCX/XML, submission.json, находки и карточка."""
from __future__ import annotations

import io
import json
import sys
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from inspector import db as db_module
from inspector import statuses as S
from inspector.models import AuditLog, Decision, Finding, User
from inspector.protocol import save_snapshot, validate_submission


@pytest.fixture()
def client(settings):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str = "inspector") -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def finding(db, code: str) -> Finding:
    return db.scalars(select(Finding).where(Finding.param_code == code)).one()


# ---------------------------------------------------------------- протокол


def test_live_protocol_follows_contract(client, example_process):
    r = client.get(f"/api/v1/processes/{example_process.id}/protocol", headers=login(client))
    assert r.status_code == 200
    p = r.json()
    assert p["process_id"] == example_process.id and p["version"] == 1 and p["process_status"] == S.READY
    assert p["object"]["object_id"] == "OBJ-NOVOSLOBODSKAYA" and p["scenario"] == "FULL"
    assert p["summary"]["params_total"] == 132 and len(p["params"]) == 132
    assert len(p["findings"]) == 7 and len(p["files"]) == 4
    kr = next(c for c in p["findings"] if c["code"] == "KR-057")
    assert kr["sources"][0]["bbox"] == [0.412, 0.518, 0.468, 0.534] and kr["inspector"]["status"] == "PENDING"
    assert set(p["traceability"]) >= {"matrix_version", "model_version", "input_manifest_sha256", "dataset_version"}


def test_live_protocol_reflects_inspector_decisions(client, db, example_process):
    f = finding(db, "KR-057")
    f.inspector_status = S.CONFIRMED_VIOLATION
    db.commit()
    p = client.get(f"/api/v1/processes/{example_process.id}/protocol", headers=login(client)).json()
    kr = next(c for c in p["findings"] if c["code"] == "KR-057")
    assert kr["status"] == S.CONFIRMED_VIOLATION and kr["machine_status"] == S.CANDIDATE
    assert p["summary"]["CONFIRMED_VIOLATION"] == 1


def test_protocol_snapshot_by_version_and_versions_list(client, db, example_process):
    save_snapshot(db, example_process, "machine")                      # v2
    db.commit()
    f = finding(db, "KR-057")
    f.inspector_status = S.NEGATIVE_VERIFIED                            # живой протокол меняется, снимок — нет
    db.commit()
    example_process.status = S.FINALIZED
    save_snapshot(db, example_process, "final")                         # v3
    db.commit()
    h = login(client)
    v2 = client.get(f"/api/v1/processes/{example_process.id}/protocol", params={"version": 2}, headers=h).json()
    assert v2["version"] == 2 and v2["protocol_id"].endswith("-v2")
    assert next(c for c in v2["findings"] if c["code"] == "KR-057")["status"] == S.CANDIDATE
    live = client.get(f"/api/v1/processes/{example_process.id}/protocol", headers=h).json()
    assert next(c for c in live["findings"] if c["code"] == "KR-057")["status"] == S.NEGATIVE_VERIFIED
    missing = client.get(f"/api/v1/processes/{example_process.id}/protocol", params={"version": 9}, headers=h)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "PROTOCOL_VERSION_NOT_FOUND"
    items = client.get(f"/api/v1/processes/{example_process.id}/protocol/versions", headers=h).json()["items"]
    assert [(i["version"], i["kind"], i["status"]) for i in items] == [(3, "final", S.FINALIZED),
                                                                         (2, "machine", S.READY)]
    assert items[0]["finalized_at"] is not None and items[1]["finalized_at"] is None
    assert items[1]["summary"]["params_total"] == 132
    for key in ("created_at", "model_version", "dataset_version", "input_manifest_hash"):
        assert key in items[0]


def test_protocol_of_unknown_process_is_404(client):
    r = client.get("/api/v1/processes/nope/protocol", headers=login(client))
    assert r.status_code == 404 and r.json()["error"]["code"] == "PROCESS_NOT_FOUND"


# ---------------------------------------------------------------- сабмит


def test_submission_is_schema_valid_and_applies_decisions(client, db, example_process):
    h = login(client)
    r = client.get(f"/api/v1/processes/{example_process.id}/submission.json", headers=h)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    sub = r.json()
    assert validate_submission(sub) == []
    assert sub["object_id"] == "OBJ-NOVOSLOBODSKAYA"
    by_code = {c["parameter_code"]: c for c in sub["checks"]}
    assert by_code["KR-057"]["violation_label"] == "VIOLATION_PRESENT"
    assert "PZ-009" not in by_code and "POD-093" not in by_code
    f = finding(db, "KR-057")
    f.inspector_status = S.NEGATIVE_VERIFIED
    db.commit()
    decided = client.get(f"/api/v1/processes/{example_process.id}/submission.json", headers=h).json()
    machine = client.get(f"/api/v1/processes/{example_process.id}/submission.json", params={"machine": 1},
                         headers=h).json()
    assert {c["parameter_code"]: c for c in decided["checks"]}["KR-057"]["violation_label"] == "NO_VIOLATION"
    assert {c["parameter_code"]: c for c in machine["checks"]}["KR-057"]["violation_label"] == "VIOLATION_PRESENT"


def test_submission_schema_violation_is_500_with_error_list(client, db, example_process):
    f = finding(db, "KR-058")
    f.submission_label = "SOMETHING_ELSE"                   # вне перечня схемы организаторов
    db.commit()
    r = client.get(f"/api/v1/processes/{example_process.id}/submission.json", headers=login(client))
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "SUBMISSION_INVALID"
    assert any("violation_label" in e for e in err["details"])


# ---------------------------------------------------------------- выгрузки PDF/DOCX/XML


@pytest.fixture()
def fake_render(monkeypatch):
    """Подменный модуль inspector.render: проверяем ручку (имя файла, тип, версия), а не вёрстку потока D."""
    seen: list[dict] = []
    mod = types.ModuleType("inspector.render")

    def make(fmt):
        def render(protocol: dict) -> bytes:
            seen.append({"fmt": fmt, "version": protocol["version"]})
            return f"{fmt}:{protocol['protocol_id']}".encode()
        return render

    mod.render_pdf, mod.render_docx, mod.render_xml = make("pdf"), make("docx"), make("xml")
    monkeypatch.setitem(sys.modules, "inspector.render", mod)
    return seen


@pytest.mark.parametrize("fmt, media", [("pdf", "application/pdf"),
                                        ("docx", "application/vnd.openxmlformats-officedocument."
                                                 "wordprocessingml.document"),
                                        ("xml", "application/xml")])
def test_exports_have_type_and_file_name(client, example_process, fake_render, fmt, media):
    r = client.get(f"/api/v1/processes/{example_process.id}/protocol.{fmt}", headers=login(client))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(media)
    assert f'filename="protocol_OBJ-NOVOSLOBODSKAYA_v1.{fmt}"' in r.headers["content-disposition"]
    assert r.content == f"{fmt}:PRT-{example_process.id[:8]}-v1".encode()


def test_export_of_snapshot_version_renders_that_snapshot(client, db, example_process, fake_render):
    save_snapshot(db, example_process, "machine")               # v2
    db.commit()
    token = login(client)["Authorization"].split()[1]
    r = client.get(f"/api/v1/processes/{example_process.id}/protocol.pdf",
                   params={"version": 2, "access_token": token})
    assert r.status_code == 200 and fake_render[-1] == {"fmt": "pdf", "version": 2}
    assert "protocol_OBJ-NOVOSLOBODSKAYA_v2.pdf" in r.headers["content-disposition"]


def test_export_without_render_module_is_503(client, example_process, monkeypatch):
    monkeypatch.setitem(sys.modules, "inspector.render", None)          # import → ImportError
    r = client.get(f"/api/v1/processes/{example_process.id}/protocol.pdf", headers=login(client))
    assert r.status_code == 503 and r.json()["error"]["code"] == "RENDER_UNAVAILABLE"


def test_real_render_module_produces_readable_files(client, example_process):
    pytest.importorskip("inspector.render")
    import pymupdf
    from docx import Document as Docx

    h = login(client)
    pdf = client.get(f"/api/v1/processes/{example_process.id}/protocol.pdf", headers=h)
    assert pdf.status_code == 200
    with pymupdf.open(stream=pdf.content, filetype="pdf") as doc:
        assert "KR-057" in "".join(page.get_text() for page in doc)
    docx = client.get(f"/api/v1/processes/{example_process.id}/protocol.docx", headers=h)
    assert docx.status_code == 200
    body = "\n".join(p.text for p in Docx(io.BytesIO(docx.content)).paragraphs)
    tables = "\n".join(c.text for t in Docx(io.BytesIO(docx.content)).tables for row in t.rows for c in row.cells)
    assert "KR-057" in body + tables
    xml = client.get(f"/api/v1/processes/{example_process.id}/protocol.xml", headers=h)
    assert xml.status_code == 200 and ET.fromstring(xml.content).tag == "protocol"


# ---------------------------------------------------------------- журнал аудита: просмотр и скачивание


def access_rows(process_id: str) -> list[tuple]:
    with db_module.session_scope() as s:
        return [(r.action, r.user_login, r.ip, r.details) for r in s.scalars(
            select(AuditLog).where(AuditLog.process_id == process_id,
                                   AuditLog.action.in_(("protocol_view", "protocol_download"))).order_by(AuditLog.id))]


def test_protocol_views_and_downloads_are_audited(client, db, example_process, fake_render):
    save_snapshot(db, example_process, "machine")                          # v2
    db.commit()
    h = login(client, "supervisor")
    url = f"/api/v1/processes/{example_process.id}"
    assert client.get(f"{url}/protocol", headers=h).status_code == 200
    assert client.get(f"{url}/protocol", params={"version": 1}, headers=h).status_code == 404  # снимка v1 нет
    assert client.get(f"{url}/protocol", params={"version": 2}, headers=h).status_code == 200
    assert client.get(f"{url}/protocol.pdf", headers=h).status_code == 200
    token = h["Authorization"].split()[1]
    assert client.get(f"{url}/protocol.docx", params={"version": 2, "access_token": token}).status_code == 200
    assert client.get(f"{url}/submission.json", params={"machine": 1}, headers=h).status_code == 200
    assert client.get(f"{url}/protocol/versions", headers=h).status_code == 200    # оглавление — не журналируется
    rows = access_rows(example_process.id)
    assert [(a, u, ip) for a, u, ip, _ in rows] == [("protocol_view", "supervisor", "testclient")] * 2 + \
        [("protocol_download", "supervisor", "testclient")] * 3
    details = [d for *_, d in rows]
    assert details[0] == {"protocol_version": 2, "live": True}
    assert details[1] == {"protocol_version": 2, "live": False}
    assert details[2]["format"] == "pdf" and details[2]["live"] is True
    assert details[2]["file"] == "protocol_OBJ-NOVOSLOBODSKAYA_v2.pdf"
    assert details[3]["format"] == "docx" and details[3]["live"] is False and details[3]["protocol_version"] == 2
    assert details[4]["format"] == "submission" and details[4]["machine"] is True
    assert all(token not in str(d) for d in details)                       # токен из ссылки в журнал не пишется


def test_failed_or_unauthorized_reads_are_not_audited_as_access(client, example_process, monkeypatch):
    import inspector.render as render_module

    def broken(protocol: dict) -> bytes:
        raise ValueError("вёрстка")

    monkeypatch.setattr(render_module, "render_pdf", broken)
    url = f"/api/v1/processes/{example_process.id}"
    assert client.get(f"{url}/protocol").status_code == 401
    assert client.get(f"{url}/protocol.pdf").status_code == 401
    r = client.get(f"{url}/protocol.pdf", headers=login(client))
    assert r.status_code == 500 and r.json()["error"]["code"] == "RENDER_FAILED"
    assert access_rows(example_process.id) == []


# ---------------------------------------------------------------- находки и карточка


def test_findings_list_filters_and_priority_order(client, example_process):
    h = login(client)
    url = f"/api/v1/processes/{example_process.id}/findings"
    everything = client.get(url, headers=h).json()
    assert everything["total"] == 7
    prios = [c["review_priority"] for c in everything["items"]]
    assert prios == sorted(prios, key=["HIGH", "MEDIUM", "LOW"].index)
    assert {c["code"] for c in client.get(url, params={"status": "CANDIDATE"}, headers=h).json()["items"]} == \
        {"KR-057", "KR-059"}
    assert client.get(url, params={"status": "CANDIDATE,SUSPICION"}, headers=h).json()["total"] == 3
    assert {c["code"] for c in client.get(url, params={"source": "explication"}, headers=h).json()["items"]} == \
        {"FREE-AR-EXPLICATION-AREA"}
    kr = client.get(url, params={"section": "КР"}, headers=h).json()["items"]
    assert kr and all(c["code"].startswith("KR-") for c in kr)
    assert [c["code"] for c in client.get(url, params={"q": "форшахта"}, headers=h).json()["items"]] == ["KR-057"]
    page = client.get(url, params={"limit": 2, "offset": 1}, headers=h).json()
    assert page["total"] == 7 and len(page["items"]) == 2
    bad = client.get(url, params={"status": "WHATEVER"}, headers=h)
    assert bad.status_code == 422


def test_finding_card_with_history(client, db, example_process):
    f = finding(db, "KR-057")
    inspector = db.scalars(select(User).where(User.login == "inspector")).one()
    db.add_all([Decision(finding_pk=f.id, user_pk=inspector.id, decision=S.CONFIRMED_VIOLATION,
                         previous_status=S.CANDIDATE, comment="совпадает с АОСР"),
                Decision(finding_pk=f.id, user_pk=inspector.id, decision=S.NEGATIVE_VERIFIED,
                         previous_status=S.CONFIRMED_VIOLATION, reason_code="OCR_ERROR", comment="ошибка OCR")])
    db.commit()
    r = client.get(f"/api/v1/findings/{f.id}", headers=login(client))
    assert r.status_code == 200
    card = r.json()
    assert card["id"] == f.id and card["finding_id"] == f.finding_id and card["process_id"] == example_process.id
    assert card["code"] == "KR-057" and card["location"] == "Форшахта" and len(card["sources"]) == 3
    assert [(h["decision"], h["previous_status"], h["reason_code"], h["user"]) for h in card["history"]] == [
        (S.CONFIRMED_VIOLATION, S.CANDIDATE, None, "inspector"),
        (S.NEGATIVE_VERIFIED, S.CONFIRMED_VIOLATION, "OCR_ERROR", "inspector")]
    assert all(h["created_at"] for h in card["history"])
    by_string = client.get(f"/api/v1/findings/{f.finding_id}", headers=login(client))
    assert by_string.status_code == 200 and by_string.json()["id"] == f.id


def test_unknown_finding_is_404(client):
    r = client.get("/api/v1/findings/987654", headers=login(client))
    assert r.status_code == 404 and r.json()["error"]["code"] == "FINDING_NOT_FOUND"
