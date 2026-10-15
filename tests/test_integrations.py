"""Processing hours, Plex, Audiobookshelf -- against a stand-in HTTP server."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer

import pytest
from prudify.config import (
    AudiobookshelfSettings,
    Config,
    PlexSettings,
    ScheduleSettings,
    TranscriptionSettings,
)
from prudify.core import transcribe as transcribe_mod
from prudify.core.cancel import OperationPaused
from prudify.services import integrations
from prudify.services.integrations import HoldCheck, outside_window


class FakeServices:
    """Answers like Plex and Audiobookshelf, and records what it was asked."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.plex_sessions: list[dict] = []
        self.abs_status = 200
        services = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, status: int, body: dict | None = None) -> None:
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                services.requests.append(("GET", self.path, dict(self.headers)))
                if self.path == "/status/sessions":
                    if self.headers.get("X-Plex-Token") != "plex-token":
                        return self._reply(401)
                    return self._reply(
                        200, {"MediaContainer": {"Metadata": services.plex_sessions}}
                    )
                if self.path == "/api/libraries":
                    if services.abs_status != 200:
                        return self._reply(services.abs_status)
                    return self._reply(
                        200,
                        {"libraries": [{"id": "lib_1", "name": "Clean books",
                                        "folders": [{"fullPath": "/audiobooks/Clean"}]}]},
                    )
                self._reply(404)

            def do_POST(self):
                services.requests.append(("POST", self.path, dict(self.headers)))
                self._reply(services.abs_status)

        # Not HTTPServer: its bind does a reverse DNS lookup of the address,
        # which can stall for half a minute on a Mac.
        ThreadingTCPServer.daemon_threads = True
        self.server = ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def services():
    fake = FakeServices()
    yield fake
    fake.close()


# -- processing hours -------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "at", "held"),
    [
        ("", "", "12:00", False),
        ("23:00", "07:00", "23:30", False),
        ("23:00", "07:00", "03:00", False),
        ("23:00", "07:00", "07:00", True),
        ("23:00", "07:00", "15:00", True),
        ("09:00", "17:00", "12:00", False),
        ("09:00", "17:00", "18:00", True),
    ],
)
def test_processing_hours_wrap_midnight(start, end, at, held):
    now = datetime(2026, 10, 15, *map(int, at.split(":")))
    reason = outside_window(ScheduleSettings(start=start, end=end), now)
    assert (reason is not None) is held


def test_a_malformed_time_is_rejected():
    with pytest.raises(ValueError):
        ScheduleSettings(start="7pm")


# -- Plex -------------------------------------------------------------------


def test_only_playing_plex_sessions_count(services):
    services.plex_sessions = [
        {"Player": {"state": "playing"}},
        {"Player": {"state": "paused"}},
        {"Player": {"state": "buffering"}},
    ]
    assert integrations.plex_playing(PlexSettings(url=services.url, token="plex-token")) == 2


def test_a_wrong_plex_token_says_so(services):
    with pytest.raises(integrations.IntegrationError, match="not authorised"):
        integrations.plex_playing(PlexSettings(url=services.url, token="wrong"))


def _plex_config(services) -> Config:
    config = Config()
    config.schedule.pause_while_plex_playing = True
    config.integrations.plex = PlexSettings(url=services.url, token="plex-token")
    return config


def test_transcription_holds_while_plex_plays(services):
    services.plex_sessions = [{"Player": {"state": "playing"}}]
    assert HoldCheck(_plex_config(services)).reason() == "Plex is playing (1 stream)"


def test_an_unreachable_plex_never_holds_the_queue():
    config = Config()
    config.schedule.pause_while_plex_playing = True
    config.integrations.plex = PlexSettings(url="http://127.0.0.1:9", token="t")
    assert HoldCheck(config).reason() is None


# -- Audiobookshelf ---------------------------------------------------------


def _abs(services) -> AudiobookshelfSettings:
    return AudiobookshelfSettings(url=services.url, api_key="abs-key", library_ids=["lib_1"])


def test_lists_audiobookshelf_libraries_with_the_key(services):
    libraries = integrations.abs_libraries(_abs(services))
    assert libraries == [{"id": "lib_1", "name": "Clean books", "folders": ["/audiobooks/Clean"]}]
    assert services.requests[0][2]["Authorization"] == "Bearer abs-key"


def test_a_burst_of_books_causes_one_scan(services, monkeypatch):
    import time

    monkeypatch.setattr(integrations, "_SCAN_SETTLE_SECONDS", 0.2)
    config = Config()
    config.integrations.audiobookshelf = _abs(services)
    notifier = integrations.AudiobookshelfNotifier(config)
    for _ in range(5):
        notifier.book_published()
    time.sleep(0.6)
    scans = [r for r in services.requests if r[0] == "POST"]
    assert [r[1] for r in scans] == ["/api/libraries/lib_1/scan"]


def test_a_failed_scan_is_reported_not_raised(services):
    services.abs_status = 403
    config = Config()
    config.integrations.audiobookshelf = _abs(services)
    result = integrations.AudiobookshelfNotifier(config).scan_now()
    assert result["scanned"] == []
    assert "not authorised" in result["errors"][0]


# -- pausing at a chunk boundary -------------------------------------------


def test_transcription_stops_between_chunks_and_keeps_them(monkeypatch, tmp_path):
    from prudify.core.audio import MediaInfo
    from prudify.core.transcribe import Transcript, Word

    done: list[str] = []

    class Fake:
        name = "fake"

        def transcribe(self, wav_path, progress=None, cancel=None):
            done.append(Path(wav_path).name)
            return Transcript(words=[Word(0.0, 1.0, "word")], engine=self.name)

    monkeypatch.setattr(transcribe_mod, "get_transcriber", lambda _s: Fake())
    monkeypatch.setattr(
        transcribe_mod.audio_mod, "probe", lambda p: MediaInfo(path=Path(p), duration=7200)
    )
    monkeypatch.setattr(
        transcribe_mod.audio_mod, "extract_pcm", lambda _s, d, **_k: Path(d).write_bytes(b"w")
    )

    with pytest.raises(OperationPaused):
        transcribe_mod.transcribe_file(
            Path("b.m4b"), TranscriptionSettings(), tmp_path, pause=lambda: len(done) >= 2
        )
    assert done == ["chunk-0000.wav", "chunk-0001.wav"]
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [
        "chunk-0000.json",
        "chunk-0001.json",
    ]
