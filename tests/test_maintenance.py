"""Периодические задачи worker'а: ежедневная сверка SHA-256 хранилища, еженедельный отчёт по дообучению; расписание
в файле DATA_DIR/maintenance.json; сбой задачи не роняет цикл и повторяется через час."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from inspector import maintenance
from inspector.models import AuditLog, File

T0 = dt.datetime(2026, 9, 28, 3, 0, tzinfo=dt.timezone.utc)


def store_file(db, process, tmp_path: Path, name: str, content: bytes, storage: Path | None = None) -> File:
    path = storage or (tmp_path / "store" / name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if storage is None:
        path.write_bytes(content)
    f = File(process_id=process.id, file_id=f"LOCAL-{name}", original_name=name, ext=".pdf", size=len(content),
             sha256=hashlib.sha256(content).hexdigest(), storage_path=str(path), doc_stage="PD", status="ok")
    db.add(f)
    db.flush()
    return f


def audit_rows(db, action: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


# ---------------------------------------------------------------- сверка хранилища


def test_verify_store_ok_checks_each_stored_file_once(db, example_process, tmp_path):
    a = store_file(db, example_process, tmp_path, "a.pdf", b"%PDF a")
    store_file(db, example_process, tmp_path, "b.pdf", b"%PDF b")
    store_file(db, example_process, tmp_path, "a-copy.pdf", b"%PDF a", storage=Path(a.storage_path))   # дубль
    result = maintenance.verify_store(db)
    db.commit()
    assert result == {"checked": 2, "problems": 0, "details": []}          # у файлов примера пути хранения нет
    row = audit_rows(db, "STORE_INTEGRITY_OK")[0]
    assert row.user_login == "system" and row.details == {"checked": 2, "problems": 0, "files": []}


def test_verify_store_reports_changed_and_missing_files(db, example_process, tmp_path):
    changed = store_file(db, example_process, tmp_path, "changed.pdf", b"%PDF original")
    gone = store_file(db, example_process, tmp_path, "gone.pdf", b"%PDF gone")
    store_file(db, example_process, tmp_path, "fine.pdf", b"%PDF fine")
    Path(changed.storage_path).write_bytes(b"%PDF tampered")
    Path(gone.storage_path).unlink()
    result = maintenance.verify_store(db)
    db.commit()
    assert result["checked"] == 3 and result["problems"] == 2
    problems = {d["name"]: d["problem"] for d in result["details"]}
    assert problems == {"changed.pdf": "контрольная сумма не совпадает", "gone.pdf": "файл отсутствует"}
    row = audit_rows(db, "STORE_INTEGRITY_FAILED")[0]
    assert row.details["problems"] == 2 and {f["file_pk"] for f in row.details["files"]} == {changed.id, gone.id}
    assert audit_rows(db, "STORE_INTEGRITY_OK") == []


def test_verify_store_script_uses_same_check_and_exit_code(db, example_process, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "verify_store_script", Path(__file__).resolve().parent.parent / "scripts" / "verify_store.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    f = store_file(db, example_process, tmp_path, "a.pdf", b"%PDF a")
    db.commit()
    assert script.main() == 0 and json.loads(capsys.readouterr().out)["checked"] == 1
    Path(f.storage_path).write_bytes(b"%PDF other")
    assert script.main() == 1 and json.loads(capsys.readouterr().out)["problems"] == 1


# ---------------------------------------------------------------- расписание


def test_run_periodic_runs_daily_and_weekly_tasks_on_schedule(db, settings):
    assert maintenance.run_periodic(db, settings, T0) == ["store_verify", "retraining_report"]
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(hours=23)) == []
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(days=1)) == ["store_verify"]
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(days=6)) == ["store_verify"]
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(days=7)) == ["store_verify", "retraining_report"]
    state = json.loads((settings.data_dir / maintenance.STATE_FILE).read_text(encoding="utf-8"))
    assert state["retraining_report"]["last_run"] == (T0 + dt.timedelta(days=7)).isoformat()
    assert state["store_verify"]["ok"] is True and state["store_verify"]["result"] == {"checked": 0, "problems": 0}
    assert len(audit_rows(db, "STORE_INTEGRITY_OK")) == 4 and len(audit_rows(db, "RETRAINING_REPORT")) == 2


def test_retraining_report_snapshot_and_audit(db, settings):
    maintenance.run_periodic(db, settings, T0)
    path = settings.data_dir / "reports" / "retraining_2026-09-28.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["generated_at"] == T0.isoformat() and report["period_days"] == 7
    assert {"decisions", "rejection_by_reason", "confirm_rate", "recommendations", "dataset"} <= set(report)
    row = audit_rows(db, "RETRAINING_REPORT")[0]
    assert row.user_login == "system" and row.details["path"] == str(path)
    assert row.details["decisions"] == {"confirmed": 0, "rejected": 0, "clarification": 0}


def test_state_survives_restart_and_corrupt_state_starts_over(db, settings):
    maintenance.run_periodic(db, settings, T0)
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(hours=1)) == []   # новый процесс — тот же файл
    (settings.data_dir / maintenance.STATE_FILE).write_text("{не json", encoding="utf-8")
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(hours=2)) == ["store_verify", "retraining_report"]
    assert json.loads((settings.data_dir / maintenance.STATE_FILE).read_text(encoding="utf-8"))
    assert not list(settings.data_dir.glob(f"{maintenance.STATE_FILE}.*.tmp"))


@pytest.mark.parametrize("entry, due", [
    (None, True), ("мусор", True), ({"ok": True}, True), ({"last_run": "вчера", "ok": True}, True),
    ({"last_run": (T0 - dt.timedelta(hours=23)).isoformat(), "ok": True}, False),
    ({"last_run": (T0 - dt.timedelta(hours=24)).isoformat(), "ok": True}, True),
    ({"last_run": (T0 - dt.timedelta(minutes=59)).isoformat(), "ok": False}, False),     # повтор после сбоя — через час
    ({"last_run": (T0 - dt.timedelta(hours=1)).isoformat(), "ok": False}, True),
    ({"last_run": (T0 + dt.timedelta(hours=5)).isoformat(), "ok": True}, True),          # часы перевели назад
    ({"last_run": "2026-09-27T20:00:00", "ok": True}, False),                             # без зоны — UTC
])
def test_is_due(entry, due):
    assert maintenance.is_due(entry, dt.timedelta(days=1), T0) is due


def test_failed_task_is_logged_audited_retried_in_an_hour_and_does_not_block_others(db, settings, monkeypatch):
    def broken(db):
        raise OSError("диск недоступен")

    monkeypatch.setattr(maintenance, "verify_store", broken)
    assert maintenance.run_periodic(db, settings, T0) == ["store_verify", "retraining_report"]
    state = json.loads((settings.data_dir / maintenance.STATE_FILE).read_text(encoding="utf-8"))
    assert state["store_verify"]["ok"] is False and "диск недоступен" in state["store_verify"]["error"]
    assert state["retraining_report"]["ok"] is True
    failed = audit_rows(db, "MAINTENANCE_FAILED")
    assert [r.details["task"] for r in failed] == ["store_verify"] and len(audit_rows(db, "RETRAINING_REPORT")) == 1
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(minutes=30)) == []
    monkeypatch.undo()
    assert maintenance.run_periodic(db, settings, T0 + dt.timedelta(hours=1)) == ["store_verify"]
    assert len(audit_rows(db, "STORE_INTEGRITY_OK")) == 1


def test_unsaved_schedule_does_not_rerun_tasks_on_every_worker_round(db, settings, monkeypatch):
    """Том данных полон (или только для чтения) — maintenance.json не записывается. Задачи не должны запускаться на
    каждом круге worker (раз в 2 с — полная сверка SHA-256 хранилища), а сбой записи — попасть в журнал."""
    def no_space(settings, state):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(maintenance, "save_state", no_space)
    rounds = [maintenance.run_periodic(db, settings, T0 + dt.timedelta(seconds=2 * i)) for i in range(5)]
    assert rounds == [["store_verify", "retraining_report"], [], [], [], []]
    assert len(audit_rows(db, "STORE_INTEGRITY_OK")) == 1 and len(audit_rows(db, "RETRAINING_REPORT")) == 1
    assert {r.details["task"] for r in audit_rows(db, "MAINTENANCE_FAILED")} == {"save_state"}


def test_tick_uses_own_session_and_never_raises(db, settings, monkeypatch):
    assert maintenance.tick(settings) == ["store_verify", "retraining_report"]
    db.expire_all()
    assert len(audit_rows(db, "STORE_INTEGRITY_OK")) == 1                  # закоммичено сессией tick

    def boom(*args):
        raise RuntimeError("state unwritable")

    monkeypatch.setattr(maintenance, "run_periodic", boom)
    assert maintenance.tick(settings) == []
