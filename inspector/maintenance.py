"""Периодические задачи worker'а: ежедневная сверка SHA-256 хранилища (ТЗ 13.8) и еженедельный отчёт по дообучению
(ТЗ 9.4). Итог каждой — строка журнала аудита от system и строка лога; отчёт ещё и снимком JSON в DATA_DIR/reports.

run_periodic(db, settings, now) сам решает, пора ли: время прошлых запусков — в DATA_DIR/maintenance.json
(переживает перезапуск worker). Сбой задачи не роняет цикл worker: он в логе и в журнале (MAINTENANCE_FAILED),
повтор — через час, а не на каждом круге цикла. Worker вызывает tick() — со своей сессией БД."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import Settings, get_settings
from inspector.models import AuditLog, File, utcnow

log = logging.getLogger("inspector.maintenance")

STATE_FILE = "maintenance.json"
REPORTS_DIR = "reports"
RETRY_AFTER_FAILURE = dt.timedelta(hours=1)
REPORT_DAYS = 7                  # период отчёта по дообучению — неделя, как и период запуска


# ---------------------------------------------------------------- задачи

def verify_store(db: Session) -> dict:
    """Сверка хранилища: SHA-256 каждого файла на диске против записанного при загрузке; файл с одним путём
    хранения проверяется один раз. Итог — строка журнала STORE_INTEGRITY_OK или STORE_INTEGRITY_FAILED (со списком
    файлов); коммитит вызывающий. -> {checked, problems, details}"""
    from inspector.pipeline.intake import sha256_file

    checked, bad = 0, []
    seen: set[str] = set()
    for f in db.scalars(select(File).where(File.storage_path != "").order_by(File.id)):
        if f.storage_path in seen:
            continue
        seen.add(f.storage_path)
        path = Path(f.storage_path)
        checked += 1
        if not path.exists():
            bad.append({"file_pk": f.id, "name": f.original_name, "problem": "файл отсутствует"})
        elif sha256_file(path) != f.sha256:
            bad.append({"file_pk": f.id, "name": f.original_name, "problem": "контрольная сумма не совпадает"})
    db.add(AuditLog(user_login="system", action="STORE_INTEGRITY_FAILED" if bad else "STORE_INTEGRITY_OK",
                    details={"checked": checked, "problems": len(bad), "files": bad[:200]}))
    return {"checked": checked, "problems": len(bad), "details": bad[:20]}


def _store_verify(db: Session, settings: Settings, now: dt.datetime) -> dict:
    result = verify_store(db)
    level = logging.ERROR if result["problems"] else logging.INFO
    log.log(level, "Сверка SHA-256 хранилища", extra={"checked": result["checked"], "problems": result["problems"]})
    return {"checked": result["checked"], "problems": result["problems"]}


def _retraining_report(db: Session, settings: Settings, now: dt.datetime) -> dict:
    """Отчёт по дообучению за неделю: снимок JSON в DATA_DIR/reports (один файл на дату) + строка журнала."""
    from inspector.gold import retraining_report

    report = retraining_report(db, REPORT_DAYS)
    out_dir = settings.data_dir / REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"retraining_{now.astimezone(dt.timezone.utc):%Y-%m-%d}.json"
    path.write_text(json.dumps({"generated_at": now.isoformat(), **report}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    db.add(AuditLog(user_login="system", action="RETRAINING_REPORT",
                    details={"path": str(path), "period_days": REPORT_DAYS, "decisions": report["decisions"],
                             "confirm_rate": report["confirm_rate"], "recommendations": report["recommendations"]}))
    log.info("Отчёт по дообучению сохранён", extra={"path": str(path), "decisions": report["decisions"],
                                                     "recommendations": len(report["recommendations"])})
    return {"path": str(path), "recommendations": len(report["recommendations"])}


@dataclass(frozen=True)
class Task:
    name: str
    every: dt.timedelta
    run: Callable[[Session, Settings, dt.datetime], dict]


TASKS = (Task("store_verify", dt.timedelta(days=1), _store_verify),
         Task("retraining_report", dt.timedelta(days=7), _retraining_report))


# ---------------------------------------------------------------- расписание

def load_state(settings: Settings) -> dict:
    """Время и итог прошлых запусков; нет файла или он испорчен — как будто задачи не запускались."""
    path = settings.data_dir / STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Файл расписания периодических задач не читается — начинаю заново",
                    extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"[:200]})
        return {}
    return state if isinstance(state, dict) else {}


def save_state(settings: Settings, state: dict) -> None:
    """Запись через временный файл и rename: оборванная запись не портит расписание."""
    path = settings.data_dir / STATE_FILE
    tmp = path.with_name(f"{STATE_FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def is_due(entry, every: dt.timedelta, now: dt.datetime) -> bool:
    """Пора ли: с прошлого запуска прошло every (после сбоя — RETRY_AFTER_FAILURE). Запуск «в будущем» (часы
    переводили назад) или нечитаемая запись — пора."""
    if not isinstance(entry, dict):
        return True
    try:
        last = dt.datetime.fromisoformat(str(entry["last_run"]))
    except (KeyError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    wait = every if entry.get("ok") else RETRY_AFTER_FAILURE
    return not (last <= now < last + wait)


_UNSAVED: dict[str, dict] = {}      # расписание, которое не удалось записать (том полон или только для чтения)


def _failed(db: Session, task: str, error: str) -> None:
    try:
        db.add(AuditLog(user_login="system", action="MAINTENANCE_FAILED",
                        details={"task": task, "error": error, "note": "уведомление администратора: повтор через час"}))
        db.commit()
    except Exception:  # noqa: BLE001 — БД недоступна: остаётся строка лога
        db.rollback()


def run_periodic(db: Session, settings: Settings, now: dt.datetime) -> list[str]:
    """Выполнить задачи, срок которых подошёл. Каждая — в своей транзакции (коммитит сама): сбой одной не отменяет
    итог другой. -> имена запущенных задач (и успешных, и со сбоем). Если файл расписания не записывается, расписание
    живёт в памяти процесса: иначе каждый круг worker заново запускал бы все задачи (полную сверку хранилища)."""
    key = str(settings.data_dir)
    state = _UNSAVED.get(key) or load_state(settings)
    ran = []
    for task in TASKS:
        if not is_due(state.get(task.name), task.every, now):
            continue
        entry: dict = {"last_run": now.isoformat(), "ok": True}
        try:
            entry["result"] = task.run(db, settings, now)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — периодическая задача не должна ронять worker
            db.rollback()
            error = f"{type(exc).__name__}: {exc}"[:300]
            entry.update(ok=False, error=error)
            log.exception("Периодическая задача не выполнена", extra={"task": task.name})
            _failed(db, task.name, error)
        state[task.name] = entry
        try:
            save_state(settings, state)
            _UNSAVED.pop(key, None)
        except OSError as exc:
            _UNSAVED[key] = state
            log.error("Расписание периодических задач не записано", extra={"error": f"{exc}"[:200]})
            _failed(db, "save_state", f"{type(exc).__name__}: {exc}"[:300])
        ran.append(task.name)
    return ran


def tick(settings: Settings | None = None) -> list[str]:
    """Вызов из цикла worker: своя сессия БД; любая ошибка (в том числе недоступность БД) остаётся в логе."""
    from inspector.db import session_factory

    settings = settings or get_settings()
    db = session_factory()()
    try:
        return run_periodic(db, settings, utcnow())
    except Exception:  # noqa: BLE001
        log.exception("Сбой расписания периодических задач")
        return []
    finally:
        db.close()
