"""Is a library folder actually there right now?

Libraries usually live on a NAS, and a NAS that drops off the network does not
make its mount point disappear: ``Path.is_dir()`` keeps answering True from
cached attributes while every real read times out. Asking for a directory
entry is the cheapest operation that has to reach the server.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

DEFAULT_TIMEOUT = 15.0


def check_dir(path: Path, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """None if ``path`` can be listed within ``timeout``; otherwise why not.

    The listing runs on a daemon thread because a hard-mounted share whose
    server has gone does not time out at all -- it blocks forever, and the
    caller must not block with it. A thread stuck that way is abandoned.
    """
    outcome: dict[str, OSError] = {}

    def probe() -> None:
        try:
            with os.scandir(path) as entries:
                next(entries, None)
        except OSError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=probe, name="prudify-reachability", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return f"{path} did not respond within {timeout:.0f}s"
    error = outcome.get("error")
    if error is not None:
        return f"{path}: {error.strerror or error}"
    return None
