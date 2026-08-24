"""Logging: console, rotating file, database table, and the live event bus."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
from pathlib import Path

from .services.events import bus

_DB_BATCH_INTERVAL = 2.0
_MAX_LOG_ROWS = 5000


class DatabaseLogHandler(logging.Handler):
    """Buffers records and flushes them to SQLite on a background thread.

    Writing one row per log line from inside a worker would serialise the
    queue behind SQLite's write lock, so records are batched instead.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._buffer: queue.Queue = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, name="prudify-logdb", daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def stop(self) -> None:
        self._stop.set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover
            return
        entry = {
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        try:
            self._buffer.put_nowait(entry)
        except queue.Full:
            return
        if record.levelno >= logging.WARNING:
            bus.publish("log", entry)

    def _drain(self) -> None:
        from .db import session_scope
        from .models import LogRecord

        pending: list[dict] = []
        while not self._stop.is_set():
            try:
                pending.append(self._buffer.get(timeout=_DB_BATCH_INTERVAL))
            except queue.Empty:
                pass
            while len(pending) < 200:
                try:
                    pending.append(self._buffer.get_nowait())
                except queue.Empty:
                    break
            if not pending:
                continue
            try:
                with session_scope() as session:
                    session.add_all([LogRecord(**entry) for entry in pending])
            except Exception:  # database not ready yet, or shutting down
                pass
            pending.clear()


_db_handler: DatabaseLogHandler | None = None


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    global _db_handler

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)-28s  %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _db_handler = DatabaseLogHandler()
    _db_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(_db_handler)

    # These are chatty and rarely useful at INFO.
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)


def start_db_logging() -> None:
    if _db_handler is not None:
        _db_handler.start()


def stop_db_logging() -> None:
    if _db_handler is not None:
        _db_handler.stop()


def trim_logs() -> None:
    """Keep the log table bounded."""
    from sqlalchemy import delete, select

    from .db import session_scope
    from .models import LogRecord

    try:
        with session_scope() as session:
            total = session.execute(select(LogRecord.id).order_by(LogRecord.id.desc())).scalars()
            ids = list(total)
            if len(ids) > _MAX_LOG_ROWS:
                cutoff = ids[_MAX_LOG_ROWS]
                session.execute(delete(LogRecord).where(LogRecord.id <= cutoff))
    except Exception:
        pass
