"""Проверка целостности хранилища (ТЗ 13.8: ежедневная сверка контрольных сумм файлов): SHA-256 каждого файла
на диске сравнивается с записанным при загрузке. Итог — запись в журнал аудита (STORE_INTEGRITY_OK или
STORE_INTEGRITY_FAILED); расхождение или пропажа -> код выхода 1. Раз в сутки это же делает worker
(inspector/maintenance.py); скрипт — для проверки по требованию.
usage (из service/): .venv/bin/python scripts/verify_store.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspector.db import init_engine, session_scope  # noqa: E402
from inspector.maintenance import verify_store  # noqa: E402


def main() -> int:
    init_engine()
    with session_scope() as db:
        result = verify_store(db)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
