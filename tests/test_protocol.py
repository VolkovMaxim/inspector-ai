from inspector import statuses as S
from inspector.protocol import build_protocol, build_submission, effective_status, validate_submission


def test_effective_status_applies_decision_only_to_candidates():
    assert effective_status(S.CANDIDATE, "PENDING") == S.CANDIDATE
    assert effective_status(S.CANDIDATE, S.NEGATIVE_VERIFIED) == S.NEGATIVE_VERIFIED
    assert effective_status(S.NEGATIVE_VERIFIED, S.CONFIRMED_VIOLATION) == S.NEGATIVE_VERIFIED


def test_protocol_sections_and_param_statuses(db, example_process):
    proto = build_protocol(db, example_process, version=1)
    assert proto["summary"]["params_total"] == 132
    by_code = {p["code"]: p for p in proto["params"]}
    assert by_code["KR-057"]["status"] == S.CANDIDATE
    assert by_code["KR-055"]["status"] == S.NEGATIVE_VERIFIED
    assert by_code["POD-093"]["status"] == S.NOT_APPLICABLE
    assert by_code["PZ-009"]["status"] == S.MISSING_EVIDENCE
    assert proto["summary"]["SUSPICION"] == 1
    stages = {u["stage"]: u["status"] for u in proto["upload_status"]}
    assert stages == {"PD": "PD_UPLOADED", "RD": "RD_UPLOADED", "ID": "ID_PARTIAL"}
    card = next(c for c in proto["findings"] if c["code"] == "KR-057")
    assert card["sources"][0]["sha256"] == "a" * 64 and card["sources"][0]["bbox"] == [0.412, 0.518, 0.468, 0.534]
    assert proto["low_quality_pages"] == [{"file_id": "F0068", "page": 7, "reason": "плохой скан"}]


def test_submission_follows_org_schema_and_decisions(db, example_process):
    proto = build_protocol(db, example_process, version=1)
    sub = build_submission(proto)
    assert validate_submission(sub) == []
    codes = [c["parameter_code"] for c in sub["checks"]]
    assert "PZ-009" not in codes and "POD-093" not in codes          # «не нашли» в сабмит не идёт
    card = next(c for c in proto["findings"] if c["code"] == "KR-057")
    card["inspector"]["status"] = S.NEGATIVE_VERIFIED
    sub2 = build_submission(proto)
    assert next(c for c in sub2["checks"] if c["parameter_code"] == "KR-057")["violation_label"] == "NO_VIOLATION"
    assert next(c for c in build_submission(proto, apply_decisions=False)["checks"]
                if c["parameter_code"] == "KR-057")["violation_label"] == "VIOLATION_PRESENT"


def test_machine_verdict_table():
    mv = S.machine_verdict
    kw = dict(has_pd=True, object_has_rd=True, object_has_id=True, clarification=False, criticality="Критическое (приостановка работ)")
    assert mv(judge_verdict="VIOLATION", verified=True, has_rd_value=True, has_id_value=False, **kw).status == S.CANDIDATE
    assert mv(judge_verdict="VIOLATION", verified=False, has_rd_value=True, has_id_value=False, **kw).status == S.NOT_COMPARABLE
    assert mv(judge_verdict="NO_VIOLATION", verified=None, has_rd_value=True, has_id_value=False, **kw).submission_label == "NO_VIOLATION"
    missing = mv(judge_verdict=None, verified=None, has_rd_value=False, has_id_value=False, **kw)
    assert missing.status == S.MISSING_EVIDENCE and not missing.submission_include
    kw2 = {**kw, "object_has_rd": False, "object_has_id": False}
    nodoc = mv(judge_verdict=None, verified=None, has_rd_value=False, has_id_value=False, **kw2)
    assert nodoc.submission_label == "MISSING_DOCUMENT" and nodoc.submission_include
    assert S.scenario({"PD", "RD", "ID"}) == "FULL" and S.scenario({"PD"}) == "SINGLE_ONLY"
