"""Filesystem monitoring and scheduled rescans.

Two mechanisms, because neither alone is sufficient:

* **watchdog** gives near-instant reaction to files appearing in a library.
  A file is not acted on until its size has stopped changing for
  ``stability_seconds`` -- copying a 900 MB M4B over SMB takes a while, and
  transcribing a half-written file is a waste of an hour.
* **A periodic rescan** catches everything watchdog misses: network shares
  where inotify events never arrive, files added while the service was down,
  and NFS mounts generally. On a Synology or a mapped drive this is usually
  the mechanism that actually fires, which is why it defaults to on.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from sqlalchemy import select
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from ..config import AUDIO_EXTENSIONS, Config
from ..db import session_scope
from ..models import Book, BookStatus
from . import library as library_service
from .events import bus
from .queue import get_queue

log = logging.getLogger(__name__)


class _AudioEventHandler(FileSystemEventHandler):
    def __init__(self, library_id: str, on_change) -> None:
        self.library_id = library_id
        self.on_change = on_change

    def _handle(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith("."):
            self.on_change(self.library_id, path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.dest_path))


class LibraryWatcher:
    def __init__(self, config: Config, use_polling: bool = False) -> None:
        self.config = config
        self.use_polling = use_polling
        self._observer = None
        self._pending: dict[Path, tuple[str, int, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_full_scan = 0.0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        observer_cls = PollingObserver if self.use_polling else Observer
        self._observer = observer_cls()
        watched = 0
        for lib in self.config.libraries:
            if not lib.enabled:
                continue
            root = Path(lib.source_path).expanduser()
            if not root.is_dir():
                log.warning("Watcher: %s does not exist, skipping", root)
                continue
            handler = _AudioEventHandler(lib.id, self._note_change)
            try:
                self._observer.schedule(handler, str(root), recursive=True)
                watched += 1
            except OSError as exc:
                log.warning("Could not watch %s: %s", root, exc)
        if watched:
            self._observer.start()
        log.info("Watching %d librar%s", watched, "y" if watched == 1 else "ies")

        self._thread = threading.Thread(target=self._loop, name="prudify-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            try:
                self._observer.join(timeout=5)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- change tracking --------------------------------------------------

    def _note_change(self, library_id: str, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        with self._lock:
            self._pending[path] = (library_id, size, time.time())

    def _ready_paths(self) -> list[tuple[str, Path]]:
        """Paths whose size has been stable long enough to be considered done."""
        ready: list[tuple[str, Path]] = []
        stability = self.config.processing.stability_seconds
        now = time.time()
        with self._lock:
            for path, (library_id, last_size, last_time) in list(self._pending.items()):
                try:
                    size = path.stat().st_size
                except OSError:
                    self._pending.pop(path, None)
                    continue
                if size != last_size:
                    self._pending[path] = (library_id, size, now)
                    continue
                if now - last_time >= stability:
                    self._pending.pop(path, None)
                    ready.append((library_id, path))
        return ready

    # -- main loop --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ready = self._ready_paths()
                if ready:
                    library_ids = {library_id for library_id, _ in ready}
                    log.info("Detected %d settled file(s); rescanning", len(ready))
                    for library_id in library_ids:
                        self._scan_and_queue(library_id)

                interval = self.config.processing.scan_interval_minutes
                if interval and time.time() - self._last_full_scan >= interval * 60:
                    self._last_full_scan = time.time()
                    log.info("Running scheduled library scan")
                    self._scan_and_queue(None)
            except Exception:  # pragma: no cover - defensive
                log.exception("Watcher loop error")

            self._stop.wait(timeout=5.0)

    def _scan_and_queue(self, library_id: str | None) -> None:
        with session_scope() as session:
            if library_id:
                lib = self.config.library_by_id(library_id)
                if lib is None or not lib.enabled:
                    return
                library_service.scan_library(session, self.config, lib)
                libraries = [lib]
            else:
                library_service.scan_all(session, self.config)
                libraries = [lib for lib in self.config.libraries if lib.enabled]

            queue = get_queue()
            queued = 0
            for lib in libraries:
                if not lib.auto_process:
                    continue
                books = session.execute(
                    select(Book).where(
                        Book.library_id == lib.id,
                        Book.status.in_([BookStatus.NEW.value, BookStatus.PARTIAL.value]),
                        Book.monitored.is_(True),
                    )
                ).scalars().all()
                for book in books:
                    queue.enqueue_book(session, book)
                    queued += 1
            if queued:
                log.info("Auto-queued %d book(s)", queued)
                bus.publish("queue.auto_enqueued", {"count": queued})

    def _scan_and_queue_safe(self, library_id: str | None) -> None:
        """Thread entry point: never let an exception escape.

        An unhandled error here kills the thread and prints a traceback to the
        container log, while the UI shows nothing at all -- the user clicks
        "Scan libraries" and is left guessing. Catch it, log it, and publish
        it so the failure is visible in the app.
        """
        try:
            self._scan_and_queue(library_id)
        except Exception as exc:  # noqa: BLE001 - a background thread must not die
            log.exception("Library scan failed")
            bus.publish("library.scan_failed", {"error": f"{type(exc).__name__}: {exc}"})

    def trigger_scan(self) -> None:
        threading.Thread(
            target=self._scan_and_queue_safe, args=(None,), name="prudify-scan", daemon=True
        ).start()


_watcher: LibraryWatcher | None = None


def init_watcher(config: Config, use_polling: bool = False) -> LibraryWatcher:
    global _watcher
    _watcher = LibraryWatcher(config, use_polling=use_polling)
    return _watcher


def get_watcher() -> LibraryWatcher:
    if _watcher is None:
        raise RuntimeError("Watcher has not been initialised")
    return _watcher
