"""Other software on the same network: Plex, Audiobookshelf.

Plain ``urllib`` rather than an HTTP client library: these are a handful of
small JSON requests, and the server should not grow a dependency for them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from datetime import time as clock

from ..config import AudiobookshelfSettings, Config, PlexSettings, ScheduleSettings

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
# Plex is asked at most this often; the queue checks far more frequently.
_PLEX_CACHE_SECONDS = 30.0
# Publishing a batch of copies should cause one Audiobookshelf scan, not one
# per book, so a scan waits for this long without a new book first.
_SCAN_SETTLE_SECONDS = 60.0


class IntegrationError(RuntimeError):
    pass


def _request(
    url: str, headers: dict[str, str], method: str = "GET", data: bytes | None = None
) -> bytes:
    request = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise IntegrationError(f"{url}: not authorised ({exc.code}); check the key") from exc
        raise IntegrationError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise IntegrationError(f"{url}: {reason}") from exc


# -- processing hours ------------------------------------------------------


def _parse(value: str) -> clock:
    hours, minutes = value.split(":")
    return clock(int(hours), int(minutes))


def outside_window(schedule: ScheduleSettings, now: datetime | None = None) -> str | None:
    """Why transcription should wait, if the time of day says so."""
    if not (schedule.start and schedule.end) or schedule.start == schedule.end:
        return None
    now_t = (now or datetime.now()).time()
    start, end = _parse(schedule.start), _parse(schedule.end)
    inside = start <= now_t < end if start < end else (now_t >= start or now_t < end)
    if inside:
        return None
    return f"Outside processing hours ({schedule.start}–{schedule.end})"


# -- Plex ------------------------------------------------------------------


def plex_playing(plex: PlexSettings) -> int:
    """How many Plex sessions are playing right now (paused ones do not count)."""
    url = plex.url.rstrip("/") + "/status/sessions"
    body = _request(url, {"Accept": "application/json", "X-Plex-Token": plex.token})
    container = json.loads(body or b"{}").get("MediaContainer", {})
    sessions = container.get("Metadata", []) or []
    return sum(
        1
        for item in sessions
        if (item.get("Player") or {}).get("state", "playing") in ("playing", "buffering")
    )


class HoldCheck:
    """Decides whether transcription should hold off right now, and why.

    Plex is asked at most every 30 seconds. If it cannot be reached, nothing
    is held: a Plex server that is down is not streaming anything, and an
    unreachable check must never stop the queue for good.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._plex_at = 0.0
        self._plex_reason: str | None = None
        self._lock = threading.Lock()

    def reason(self) -> str | None:
        window = outside_window(self.config.schedule)
        if window:
            return window
        if not self.config.schedule.pause_while_plex_playing:
            return None
        if not self.config.integrations.plex.token:
            return None
        with self._lock:
            if time.monotonic() - self._plex_at >= _PLEX_CACHE_SECONDS:
                self._plex_at = time.monotonic()
                try:
                    playing = plex_playing(self.config.integrations.plex)
                except (IntegrationError, ValueError) as exc:
                    log.warning("Could not ask Plex what is playing: %s", exc)
                    playing = 0
                self._plex_reason = (
                    f"Plex is playing ({playing} stream{'s' if playing != 1 else ''})"
                    if playing
                    else None
                )
            return self._plex_reason


# -- Audiobookshelf --------------------------------------------------------


def _abs_headers(settings: AudiobookshelfSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"}


def abs_libraries(settings: AudiobookshelfSettings) -> list[dict]:
    """Audiobookshelf's libraries, for choosing which to rescan."""
    if not settings.url:
        raise IntegrationError("Audiobookshelf's address is not set")
    body = _request(settings.url.rstrip("/") + "/api/libraries", _abs_headers(settings))
    libraries = json.loads(body or b"{}").get("libraries", [])
    return [
        {
            "id": library.get("id", ""),
            "name": library.get("name", ""),
            "folders": [f.get("fullPath", "") for f in library.get("folders", [])],
        }
        for library in libraries
    ]


def abs_scan(settings: AudiobookshelfSettings, library_id: str) -> None:
    url = (
        settings.url.rstrip("/")
        + "/api/libraries/"
        + urllib.parse.quote(library_id, safe="")
        + "/scan"
    )
    _request(url, _abs_headers(settings), method="POST", data=b"")


class AudiobookshelfNotifier:
    """Asks Audiobookshelf to rescan once a burst of publishing has settled."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last_scan: dict | None = None

    def enabled(self) -> bool:
        settings = self.config.integrations.audiobookshelf
        return bool(settings.url and settings.api_key and settings.library_ids)

    def book_published(self) -> None:
        if not self.enabled():
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_SCAN_SETTLE_SECONDS, self.scan_now)
            self._timer.daemon = True
            self._timer.start()

    def scan_now(self) -> dict:
        settings = self.config.integrations.audiobookshelf
        scanned, errors = [], []
        for library_id in settings.library_ids:
            try:
                abs_scan(settings, library_id)
                scanned.append(library_id)
            except IntegrationError as exc:
                errors.append(str(exc))
                log.warning("Audiobookshelf scan of %s failed: %s", library_id, exc)
        if scanned:
            log.info("Asked Audiobookshelf to rescan %d librar%s", len(scanned),
                     "y" if len(scanned) == 1 else "ies")
        self.last_scan = {"at": time.time(), "scanned": scanned, "errors": errors}
        return self.last_scan

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
