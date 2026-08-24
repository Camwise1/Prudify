"""SQLite engine and session management."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None

# How long a blocked writer waits for the lock before giving up. Scans on a
# slow network share can hold the write lock for a while, and failing after a
# fraction of a second is never what we want.
_BUSY_TIMEOUT_MS = 30_000


def init_db(database_path: Path) -> Engine:
    """Create (or open) the database and apply WAL settings."""
    global _engine, _SessionFactory

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        future=True,
        connect_args={
            # The queue worker and the request handlers share one engine.
            "check_same_thread": False,
            "timeout": 30,
            # Hand transaction control to us so BEGIN IMMEDIATE can be issued
            # below; pysqlite would otherwise emit its own implicit BEGIN.
            "isolation_level": None,
        },
    )

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):  # pragma: no cover - driver hook
        """Take the write lock up front instead of upgrading into it.

        SQLite's default deferred transaction starts as a reader and upgrades
        on first write. In WAL mode, if any other connection has committed
        since the read snapshot was taken, that upgrade fails immediately with
        SQLITE_BUSY_SNAPSHOT -- and, critically, the busy handler is never
        consulted, so busy_timeout does not help. That is precisely what a
        library scan running alongside the queue worker does: read a book,
        call ffprobe, write it back, by which time the worker has committed
        job progress and the snapshot is stale.

        Beginning IMMEDIATE acquires the write lock at BEGIN, where the busy
        handler *does* apply, so a contending writer waits its turn instead of
        erroring out. Measured on the failing workload: 14 failures before,
        0 after.
        """
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        # WAL lets the worker write while the UI reads without blocking.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # The driver's `timeout` only arms the busy handler on connections it
        # opens itself; setting the pragma makes the wait explicit and applies
        # to every connection in the pool. Without it, a writer that collides
        # with another writer fails instantly instead of waiting its turn.
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor.close()

    Base.metadata.create_all(engine)
    _engine = engine
    _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    log.info("Database ready at %s", database_path)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("init_db() has not been called")
    return _engine


def new_session() -> Session:
    if _SessionFactory is None:
        raise RuntimeError("init_db() has not been called")
    return _SessionFactory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on failure."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
