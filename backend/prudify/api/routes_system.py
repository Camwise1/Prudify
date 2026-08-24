"""System status, logs, and a filesystem browser for the path pickers."""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import AUDIO_EXTENSIONS, Config, find_binary
from ..core import audio as audio_mod
from ..core import matcher as matcher_mod
from ..db import db_session
from ..models import LogRecord
from ..schemas import LogOut, SystemStatus
from ..services import library as library_service
from ..services.queue import get_queue
from ..services.watcher import get_watcher
from .deps import get_config

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/status", response_model=SystemStatus)
def status(
    session: Session = Depends(db_session), config: Config = Depends(get_config)
) -> SystemStatus:
    available, detail, gpu = _transcription_status(config)
    work_dir = config.resolved_work_dir()
    try:
        free_mb = shutil.disk_usage(work_dir).free / (1024 * 1024)
    except OSError:
        free_mb = 0.0

    return SystemStatus(
        version=__version__,
        ffmpeg=audio_mod.ffmpeg_version(),
        ffprobe_available=find_binary("ffprobe") is not None,
        transcription_engine=config.transcription.engine,
        transcription_available=available,
        transcription_detail=detail,
        gpu=gpu,
        data_dir=str(config.resolved_data_dir()),
        work_dir=str(work_dir),
        free_space_mb=round(free_mb, 1),
        libraries=len(config.libraries),
        paused=get_queue().is_paused,
        stats=library_service.library_stats(session),
    )


def _transcription_status(config: Config) -> tuple[bool, str, bool]:
    engine = config.transcription.engine
    if engine == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return (
                False,
                "faster-whisper is not installed. Run: pip install 'prudify[whisper]'",
                False,
            )
        try:
            import ctranslate2

            gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            gpu = False
        return True, f"faster-whisper ready ({config.transcription.model})", gpu
    if engine == "whisper-cpp":
        binary = config.transcription.whisper_cpp_binary or shutil.which("whisper-cli")
        ok = bool(binary and Path(binary).exists())
        return ok, binary or "whisper.cpp binary not configured", False
    try:
        import whisper  # noqa: F401

        return True, "openai-whisper ready", False
    except ImportError:
        return False, "openai-whisper is not installed", False


@router.get("/about")
def about(config: Config = Depends(get_config)) -> dict:
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "ffmpeg": audio_mod.ffmpeg_version(),
        "data_dir": str(config.resolved_data_dir()),
        "config_file": str(config.resolved_data_dir() / "config.yaml"),
        "wordlists": matcher_mod.available_wordlists(),
        "supported_formats": sorted(AUDIO_EXTENSIONS),
    }


@router.get("/logs", response_model=list[LogOut])
def logs(
    session: Session = Depends(db_session),
    limit: int = Query(default=200, ge=1, le=2000),
    level: str | None = Query(default=None),
) -> list[LogOut]:
    query = select(LogRecord).order_by(LogRecord.id.desc()).limit(limit)
    if level:
        query = query.where(LogRecord.level.in_(level.upper().split(",")))
    rows = session.execute(query).scalars().all()
    return [LogOut.model_validate(row) for row in rows]


@router.delete("/logs")
def clear_logs(session: Session = Depends(db_session)) -> dict:
    result = session.execute(delete(LogRecord))
    session.commit()
    return {"deleted": result.rowcount or 0}


@router.post("/scan")
def scan_now() -> dict:
    get_watcher().trigger_scan()
    return {"started": True}


@router.get("/browse")
def browse(path: str | None = Query(default=None)) -> dict:
    """List directories so the UI can offer a path picker.

    Returns directories plus a count of audio files, which makes it obvious
    when you have landed on the right folder. Files themselves are not listed.
    """
    if not path:
        return {"path": "", "parent": None, "roots": _roots(), "entries": []}

    target = Path(path).expanduser()
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {target}")

    entries = []
    audio_here = 0
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child), "type": "dir"})
            elif child.suffix.lower() in AUDIO_EXTENSIONS:
                audio_here += 1
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    parent = str(target.parent) if target.parent != target else None
    return {
        "path": str(target),
        "parent": parent,
        "roots": _roots(),
        "audio_file_count": audio_here,
        "entries": entries,
    }


def _roots() -> list[dict]:
    """Sensible starting points, per platform."""
    roots: list[dict] = []
    if sys.platform == "win32":
        import string

        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                roots.append({"name": f"{letter}:", "path": str(drive)})
    else:
        roots.append({"name": "/", "path": "/"})
        for candidate in ("/mnt", "/media", "/volumes", "/Volumes", "/audiobooks", "/data"):
            path = Path(candidate)
            if path.is_dir():
                roots.append({"name": candidate, "path": candidate})
    home = Path.home()
    if home.is_dir():
        roots.append({"name": "Home", "path": str(home)})
    return roots
