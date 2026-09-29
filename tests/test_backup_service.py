"""Служба backup (docker-compose.yml) без Docker: текст команды берётся из compose-файла ($$ -> $, /backups -> временный
каталог, пауза между кругами -> выход после первого круга) и выполняется в busybox sh, как в postgres:16-alpine
(нет busybox — в sh хоста). pg_dump подменён заглушкой: код выхода задаёт тест, при успехе пишет файл копии."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
SHELL = ["busybox", "sh"] if shutil.which("busybox") else ["sh"]
SLEEP = "sleep $((BACKUP_INTERVAL_MIN * 60))"


def one_round(tmp_path: Path, pg_dump_rc: int, interval: str = "15", keep_days: str = "30"):
    script = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["backup"]["command"][0].replace("$$", "$")
    assert script.count(SLEEP) == 1
    script = script.replace(SLEEP, "break").replace("/backups", str(tmp_path / "backups"))
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "pg_dump").write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in --file=*) f="${a#--file=}";; esac; done\n'
                                  f'[ {pg_dump_rc} = 0 ] && echo dump > "$f"\nexit {pg_dump_rc}\n', encoding="utf-8")
    (stub / "pg_dump").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "PGDATABASE": "inspector",
           "BACKUP_INTERVAL_MIN": interval, "BACKUP_KEEP_DAYS": keep_days}
    return subprocess.run([*SHELL, "-c", script], env=env, capture_output=True, text=True, timeout=30)


def aged(path: Path, days: int = 31) -> Path:
    path.write_text("old", encoding="utf-8")
    t = time.time() - days * 86400
    os.utime(path, (t, t))
    return path


def test_failed_pg_dump_keeps_previous_copies(tmp_path):
    """pg_dump падает (БД не поднялась, пароль в .env разошёлся с БД) — прежние копии нужны именно сейчас."""
    (tmp_path / "backups").mkdir()
    last = aged(tmp_path / "backups" / "inspector_20260828T000000Z.dump")
    run = one_round(tmp_path, pg_dump_rc=1)
    assert "ОШИБКА pg_dump" in run.stderr
    assert last.exists()


def test_successful_dump_removes_only_own_expired_copies(tmp_path):
    """BACKUP_DIR может быть общим сетевым каталогом: удаляются только старые копии этой БД."""
    (tmp_path / "backups").mkdir()
    old = aged(tmp_path / "backups" / "inspector_20260828T000000Z.dump")
    foreign = aged(tmp_path / "backups" / "other_system.dump")
    foreign_part = aged(tmp_path / "backups" / "other_system.dump.part", days=0)
    run = one_round(tmp_path, pg_dump_rc=0)
    assert run.returncode == 0, run.stderr
    assert not old.exists() and foreign.exists() and foreign_part.exists()
    assert len(list((tmp_path / "backups").glob("inspector_*.dump"))) == 1        # новая копия


@pytest.mark.parametrize("interval,keep_days", [("00", "30"), ("15", "030")])
def test_leading_zeros_in_backup_settings_are_rejected(tmp_path, interval, keep_days):
    """«00» проходило проверку (копии без паузы), а в арифметике sh число с нулём впереди — восьмеричное."""
    (tmp_path / "backups").mkdir()
    run = one_round(tmp_path, pg_dump_rc=0, interval=interval, keep_days=keep_days)
    assert run.returncode == 1 and "целые" in run.stderr
