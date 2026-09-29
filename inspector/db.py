"""Подключение к БД: Postgres в compose, SQLite в разработке и тестах."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from inspector.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker | None = None


def strip_nul(value):
    """Символ NUL (0x00) из строк параметров — PostgreSQL не хранит его в текстовых полях (текст PDF, имена файлов,
    ответы модели могут его содержать). Словари, списки и кортежи — рекурсивно; остальное без изменений."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(strip_nul(v) for v in value)
    return value


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _record):  # WAL: api и worker пишут одновременно
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
        return engine
    engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _no_nul(conn, cursor, statement, parameters, context, executemany):
        return statement, strip_nul(parameters)
    return engine


def init_engine(url: str | None = None) -> Engine:
    """Создать движок и таблицы. Повторный вызов с другим url (тесты) пересоздаёт движок."""
    global _engine, _factory
    from inspector.models import Base

    _engine = make_engine(url or get_settings().db_url)
    Base.metadata.create_all(_engine)
    _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


def session_factory() -> sessionmaker:
    if _factory is None:
        init_engine()
    return _factory  # type: ignore[return-value]


@contextmanager
def session_scope() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """Зависимость FastAPI."""
    s = session_factory()()
    try:
        yield s
    finally:
        s.close()
