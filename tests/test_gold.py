"""GOLD (ТЗ 9.4): метки из решений инспектора, черновик при финализации, версии с разбиением по объектам, отчёт."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from inspector import statuses as S
from inspector.models import DatasetItem, Finding


def test_gold_label_only_from_inspector_decisions_on_candidates():
    from inspector.gold import gold_label

    assert gold_label(S.CANDIDATE, S.CONFIRMED_VIOLATION) == "POSITIVE"
    assert gold_label(S.CANDIDATE, S.NEGATIVE_VERIFIED) == "NEGATIVE"
    assert gold_label(S.CANDIDATE, S.CLARIFICATION_REQUIRED) is None     # спорное — не в GOLD (ТЗ 9.3)
    assert gold_label(S.CANDIDATE, "PENDING") is None
    assert gold_label(S.NEGATIVE_VERIFIED, "PENDING") is None           # машинный «нет нарушения» без решения
    assert gold_label(S.SUSPICION, "PENDING") is None


def _decide(db, process, code, status, reason=None):
    f = db.scalars(select(Finding).where(Finding.process_id == process.id, Finding.param_code == code)).one()
    f.inspector_status, f.reason_code = status, reason
    return f


def test_draft_items_follow_decisions_and_are_idempotent(db, example_process):
    from inspector.gold import create_draft_items

    _decide(db, example_process, "KR-057", S.CONFIRMED_VIOLATION)
    _decide(db, example_process, "KR-059", S.NEGATIVE_VERIFIED, "OCR_ERROR")
    db.flush()
    assert create_draft_items(db, example_process) == 2
    assert create_draft_items(db, example_process) == 2               # повтор не плодит дубли
    items = {i.param_code: i for i in db.scalars(select(DatasetItem))}
    assert len(items) == 2 and len(list(db.scalars(select(DatasetItem)))) == 2
    assert items["KR-057"].gold_label == "POSITIVE" and items["KR-059"].gold_label == "NEGATIVE"
    assert items["KR-059"].reason_code == "OCR_ERROR"
    assert items["KR-057"].evidence_group_id == "FND-KR-057-Форшахта"
    assert items["KR-057"].card["code"] == "KR-057" and items["KR-057"].card["status"] == S.CONFIRMED_VIOLATION


def test_published_items_are_kept_and_unchanged_decisions_not_redrafted(db, example_process):
    from inspector.gold import create_draft_items, drop_draft_items

    _decide(db, example_process, "KR-057", S.CONFIRMED_VIOLATION)
    _decide(db, example_process, "KR-059", S.NEGATIVE_VERIFIED, "OCR_ERROR")
    db.flush()
    create_draft_items(db, example_process)
    published = db.scalars(select(DatasetItem).where(DatasetItem.param_code == "KR-057")).one()
    published.dataset_version, published.split = "gold-v1", "train"
    db.flush()
    _decide(db, example_process, "KR-059", S.CONFIRMED_VIOLATION)         # решение изменили после отмены финализации
    db.flush()
    assert create_draft_items(db, example_process) == 1                  # KR-057 уже в gold-v1 с той же меткой
    rows = sorted((i.param_code, i.gold_label, i.dataset_version) for i in db.scalars(select(DatasetItem)))
    assert rows == [("KR-057", "POSITIVE", "gold-v1"), ("KR-059", "POSITIVE", None)]
    assert drop_draft_items(db, example_process) == 1
    assert [(i.param_code, i.dataset_version) for i in db.scalars(select(DatasetItem))] == [("KR-057", "gold-v1")]


# ---------------------------------------------------------------- версии: разбиение по объектам и SHA-256


@pytest.mark.parametrize("object_code, split", [
    ("OBJ-NOVOSLOBODSKAYA", "train"),          # int(sha256[:8], 16) % 100 = 39
    ("OBJ-RECHNIKOV", "train"),                # 1
    ("OBJ-KRESTOVOZDVIZHENSKIY", "validation"),  # 72
    ("OBJ-C", "validation"),                   # 71
    ("OBJ-NAGATINSKAYA", "test"),              # 94
    ("OBJ-D", "test"),                         # 96
])
def test_split_for_object_follows_hash_buckets(object_code, split):
    from inspector.gold import split_for_object

    assert split_for_object(object_code) == split


def _draft(db, process, object_code, code, label, n, reason=None):
    """n черновых элементов объекта object_code (карточка — минимальная, как у настоящих: dict)."""
    from inspector.models import DatasetItem

    f = db.scalars(select(Finding).where(Finding.process_id == process.id)).first()
    for i in range(n):
        db.add(DatasetItem(finding_pk=f.id, evidence_group_id=f"FND-{object_code}-{code}-{i}", object_code=object_code,
                           param_code=code, gold_label=label, reason_code=reason,
                           card={"code": code, "location": f"loc-{i}", "object_id": object_code}))
    db.flush()


OBJECTS = [f"OBJ-{i:03d}" for i in range(30)]


def test_publish_version_keeps_each_object_in_one_split(db, example_process):
    from inspector.gold import publish_version
    from inspector.models import DatasetItem, DatasetVersion

    for i, obj in enumerate(OBJECTS):
        _draft(db, example_process, obj, "KR-057", "POSITIVE", 2)
        _draft(db, example_process, obj, "KR-059", "NEGATIVE", 1, reason="OCR_ERROR")
    dv = publish_version(db, user_pk=None)
    assert isinstance(dv, DatasetVersion) and dv.version == "gold-v1"
    assert (dv.items, dv.positives, dv.negatives) == (90, 60, 30)
    items = list(db.scalars(select(DatasetItem)))
    assert all(i.dataset_version == "gold-v1" and i.split in ("train", "validation", "test") for i in items)
    per_object: dict[str, set] = {}
    for i in items:
        per_object.setdefault(i.object_code, set()).add(i.split)
    assert all(len(splits) == 1 for splits in per_object.values())          # объект целиком в одном наборе
    sets = {k: set(v) for k, v in dv.split_objects.items()}
    assert set(sets) == {"train", "validation", "test"}
    assert not (sets["train"] & sets["validation"]) and not (sets["train"] & sets["test"]) \
        and not (sets["validation"] & sets["test"])
    assert sets["train"] | sets["validation"] | sets["test"] == set(OBJECTS)
    assert all(per_object[o] == {s} for s, objs in sets.items() for o in objs)


def test_version_hash_is_stable_and_matches_export(db, example_process):
    import hashlib

    from inspector.gold import export_bytes, publish_version, version_lines
    from inspector.models import DatasetItem

    for obj in OBJECTS[:8]:
        _draft(db, example_process, obj, "KR-057", "POSITIVE", 1)
    dv = publish_version(db, user_pk=None)
    first, second = export_bytes(db, "gold-v1"), export_bytes(db, "gold-v1")
    assert first == second and hashlib.sha256(first).hexdigest() == dv.sha256
    items = list(db.scalars(select(DatasetItem)))
    assert hashlib.sha256("".join(l + "\n" for l in version_lines(list(reversed(items)))).encode()).hexdigest() \
        == dv.sha256                                                        # от порядка строк БД не зависит
    import json

    lines = [json.loads(l) for l in first.decode().splitlines()]
    for split, digest in dv.split_hashes.items():
        mine = "".join(json.dumps(l, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for l in lines if l["split"] == split)
        assert hashlib.sha256(mine.encode()).hexdigest() == digest
    assert {"card", "gold_label", "split", "reason_code", "object_id", "param_code", "evidence_group_id"} <= \
        set(lines[0])


def test_next_version_takes_only_new_drafts_and_old_export_is_unchanged(db, example_process):
    import hashlib

    from inspector.gold import export_bytes, publish_version

    _draft(db, example_process, "OBJ-A", "KR-057", "POSITIVE", 2)
    v1 = publish_version(db, user_pk=None)
    _draft(db, example_process, "OBJ-B", "KR-059", "NEGATIVE", 3, reason="BINDING_ERROR")
    v2 = publish_version(db, user_pk=None)
    assert (v2.version, v2.items, v2.negatives) == ("gold-v2", 3, 3)
    assert hashlib.sha256(export_bytes(db, "gold-v1")).hexdigest() == v1.sha256
    assert len(export_bytes(db, "gold-v2").decode().splitlines()) == 3


def test_publish_rules_empty_draft_bad_or_duplicate_name(db, example_process):
    from inspector.gold import publish_version
    from inspector.verification import DomainError

    with pytest.raises(DomainError) as empty:
        publish_version(db, user_pk=None)
    assert empty.value.code == "NO_DRAFT_ITEMS" and empty.value.status_code == 409
    _draft(db, example_process, "OBJ-A", "KR-057", "POSITIVE", 1)
    with pytest.raises(DomainError) as bad:
        publish_version(db, user_pk=None, version="gold v1; drop")
    assert bad.value.code == "INVALID_VERSION" and bad.value.status_code == 422
    assert publish_version(db, user_pk=None, version="gold-2026-09-25").version == "gold-2026-09-25"
    _draft(db, example_process, "OBJ-B", "KR-057", "POSITIVE", 1)
    with pytest.raises(DomainError) as dup:
        publish_version(db, user_pk=None, version="gold-2026-09-25")
    assert dup.value.code == "VERSION_EXISTS"
    assert publish_version(db, user_pk=None).version == "gold-v1"


# ---------------------------------------------------------------- отчёт по дообучению


def test_retraining_report_counts_latest_decisions_in_period(db, example_process):
    import datetime as dt

    from inspector.gold import retraining_report
    from inspector.models import Decision

    def add_finding(code, n):
        rows = []
        for i in range(n):
            f = Finding(process_id=example_process.id, finding_id=f"FND-R-{code}-{i}", param_code=code,
                        location=f"r{i}", status=S.CANDIDATE)
            db.add(f)
            rows.append(f)
        db.flush()
        return rows

    now = dt.datetime.now(dt.timezone.utc)
    kr = add_finding("KR-057", 3)
    ar = add_finding("AR-040", 1)
    ok = add_finding("KR-058", 2)
    cl = add_finding("KR-055", 1)
    old = add_finding("KR-059", 1)
    for f in kr:
        db.add(Decision(finding_pk=f.id, decision=S.CONFIRMED_VIOLATION, created_at=now - dt.timedelta(hours=2)))
        db.add(Decision(finding_pk=f.id, decision=S.NEGATIVE_VERIFIED, reason_code="OCR_ERROR",
                        created_at=now - dt.timedelta(hours=1)))            # последнее решение — отклонение
    db.add(Decision(finding_pk=ar[0].id, decision=S.NEGATIVE_VERIFIED, reason_code="BINDING_ERROR", created_at=now))
    for f in ok:
        db.add(Decision(finding_pk=f.id, decision=S.CONFIRMED_VIOLATION, created_at=now))
    db.add(Decision(finding_pk=cl[0].id, decision=S.CLARIFICATION_REQUIRED, created_at=now))
    db.add(Decision(finding_pk=cl[0].id, decision="PROMOTE", previous_status=S.SUSPICION,
                    created_at=now - dt.timedelta(days=1)))
    db.add(Decision(finding_pk=old[0].id, decision=S.NEGATIVE_VERIFIED, reason_code="ACTUAL_REVISION_WRONG",
                    created_at=now - dt.timedelta(days=30)))                 # вне окна 7 дней
    db.flush()
    rep = retraining_report(db, days=7)
    assert rep["period_days"] == 7
    assert rep["decisions"] == {"confirmed": 2, "rejected": 4, "clarification": 1}
    assert rep["rejection_by_reason"] == {"OCR_ERROR": 3, "BINDING_ERROR": 1}
    assert rep["rejection_by_section"] == {"КР": 3, "АР": 1}
    assert rep["rejection_by_param"] == [{"code": "KR-057", "count": 3}, {"code": "AR-040", "count": 1}]
    assert rep["confirm_rate"] == pytest.approx(2 / 6, abs=1e-3)
    text = " ".join(rep["recommendations"])
    assert "OCR" in text and "локаци" in text and "KR-057" in text
    assert "редакц" not in text                                              # ACTUAL_REVISION_WRONG — вне периода
    assert rep["dataset"] == {"draft_items": 0, "latest_version": None}


def test_retraining_report_without_decisions(db):
    from inspector.gold import retraining_report

    rep = retraining_report(db, days=7)
    assert rep["decisions"] == {"confirmed": 0, "rejected": 0, "clarification": 0}
    assert rep["confirm_rate"] is None and rep["recommendations"] == [] and rep["rejection_by_param"] == []
