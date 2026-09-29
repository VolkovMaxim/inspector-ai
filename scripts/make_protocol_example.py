"""Пример протокола для разработки интерфейса и выгрузок: тестовая БД с правдоподобными находками -> JSON.
usage: DATA_DIR=/tmp/x .venv/bin/python scripts/make_protocol_example.py tests/data/protocol_example.json"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from inspector.config import get_settings
from inspector.db import init_engine, session_scope
from inspector.protocol import build_protocol, build_submission, validate_submission
from inspector.seed import seed_all
from inspector.testing import build_example_process


def main(out: Path) -> None:
    init_engine(f"sqlite:///{Path(tempfile.mkdtemp()) / 'x.db'}")
    with session_scope() as db:
        seed_all(db, get_settings())
        p = build_example_process(db)
        proto = build_protocol(db, p, version=1)
        sub = build_submission(proto)
        assert not validate_submission(sub)
    out.write_text(json.dumps(proto, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_name("submission_example.json").write_text(json.dumps(sub, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out, len(proto["findings"]), proto["summary"])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
