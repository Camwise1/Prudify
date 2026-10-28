"""Folder watcher lifecycle."""

from __future__ import annotations

import threading
import time

import pytest
from prudify.config import LibrarySettings
from prudify.services import watcher as watcher_mod


class _StuckObserver:
    """Stands in for an observer whose first snapshot never finishes.

    That is what a stalled NFS mount looks like to watchdog: ``start()``
    lists the whole tree before returning, and the listing just hangs.
    """

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.stopped = False

    def schedule(self, handler, path, recursive=False):
        return None

    def start(self) -> None:
        self.started.set()
        self.release.wait(timeout=10)

    def stop(self) -> None:
        self.stopped = True
        self.release.set()

    def join(self, timeout=None) -> None:
        return None


@pytest.fixture
def stuck(monkeypatch):
    observer = _StuckObserver()
    monkeypatch.setattr(watcher_mod, "PollingObserver", lambda: observer)
    monkeypatch.setattr(watcher_mod, "Observer", lambda: observer)
    yield observer
    observer.release.set()


@pytest.fixture
def watched_config(config, tmp_path):
    source = tmp_path / "library"
    source.mkdir()
    config.libraries = [
        LibrarySettings(source_path=str(source), output_path=str(tmp_path / "clean"))
    ]
    config.processing.scan_interval_minutes = 0
    return config


def test_a_hung_share_does_not_block_start(watched_config, stuck):
    watcher = watcher_mod.LibraryWatcher(watched_config, use_polling=True)

    began = time.monotonic()
    watcher.start()
    assert time.monotonic() - began < 1.0

    assert stuck.started.wait(timeout=5), "the observer should still be set up"
    watcher.stop()
    assert stuck.stopped


def test_stopping_during_setup_stops_the_observer(watched_config, stuck):
    watcher = watcher_mod.LibraryWatcher(watched_config, use_polling=True)
    watcher.start()
    assert stuck.started.wait(timeout=5)

    watcher.stop()
    assert stuck.stopped


def test_slow_setup_is_logged(watched_config, stuck, monkeypatch, caplog):
    monkeypatch.setattr(watcher_mod, "WATCH_SETUP_WARN_SECONDS", 0.05)
    watcher = watcher_mod.LibraryWatcher(watched_config, use_polling=True)
    with caplog.at_level("WARNING", logger=watcher_mod.log.name):
        watcher.start()
        assert stuck.started.wait(timeout=5)
        deadline = time.monotonic() + 5
        while "may not be responding" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.02)
    watcher.stop()
    assert "may not be responding" in caplog.text
