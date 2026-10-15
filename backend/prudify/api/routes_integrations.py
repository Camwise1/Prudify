"""Audiobookshelf and Plex set-up helpers, and the summary Home Assistant polls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import AudiobookshelfSettings, Config, PlexSettings
from ..db import db_session
from ..models import Book, Job, JobStatus
from ..services import integrations
from ..services import library as library_service
from ..services.queue import get_queue
from .deps import get_config

router = APIRouter(tags=["integrations"])

# Stands in for a saved secret in anything sent to the browser. Sent back
# unchanged, it means "keep what is saved".
SECRET_PLACEHOLDER = "********"


def _with_saved_secret(value: str, saved: str) -> str:
    return saved if value == SECRET_PLACEHOLDER else value


@router.post("/integrations/audiobookshelf/libraries")
def audiobookshelf_libraries(
    payload: dict = Body(default_factory=dict), config: Config = Depends(get_config)
) -> dict:
    """Audiobookshelf's libraries, using the address and key being typed in."""
    saved = config.integrations.audiobookshelf
    settings = AudiobookshelfSettings(
        url=payload.get("url", saved.url),
        api_key=_with_saved_secret(payload.get("api_key", saved.api_key), saved.api_key),
    )
    try:
        return {"libraries": integrations.abs_libraries(settings)}
    except integrations.IntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/integrations/audiobookshelf/scan")
def audiobookshelf_scan(config: Config = Depends(get_config)) -> dict:
    if not get_queue().audiobookshelf.enabled():
        raise HTTPException(
            status_code=409, detail="Set Audiobookshelf's address, key and libraries first"
        )
    return get_queue().audiobookshelf.scan_now()


@router.post("/integrations/plex/test")
def plex_test(
    payload: dict = Body(default_factory=dict), config: Config = Depends(get_config)
) -> dict:
    saved = config.integrations.plex
    settings = PlexSettings(
        url=payload.get("url", saved.url),
        token=_with_saved_secret(payload.get("token", saved.token), saved.token),
    )
    try:
        return {"playing": integrations.plex_playing(settings)}
    except (integrations.IntegrationError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@router.get("/system/summary")
def summary(session: Session = Depends(db_session)) -> dict:
    """Everything a dashboard or Home Assistant shows, in one small response.

    Times are absolute (ISO 8601, UTC) rather than "seconds from now", so a
    poller can show a finish time without knowing when it asked.
    """
    queue = get_queue()
    now = datetime.now(timezone.utc)
    stats = library_service.library_stats(session)
    by_status = stats.get("by_status", {})

    active = None
    active_remaining: float | None = 0.0
    jobs = queue.active_jobs
    clean_jobs = [job for job in jobs if job.get("mode") != "copy"] or jobs
    if clean_jobs:
        job = clean_jobs[0]
        eta = job.get("job_eta_seconds") or job.get("stage_eta_seconds")
        active_remaining = job.get("job_eta_seconds")
        active = {
            "job_id": job.get("job_id"),
            "title": job.get("title"),
            "author": job.get("author"),
            "mode": job.get("mode"),
            "stage": job.get("stage"),
            "message": job.get("message"),
            "progress": round((job.get("progress") or 0) * 100, 1),
            "speed": job.get("speed"),
            "finishes_at": _iso(now + timedelta(seconds=eta)) if eta else None,
            "finish_is_estimate_of": "job" if job.get("job_eta_seconds") else "stage",
        }

    pending = session.execute(
        select(Job.id, Book.duration, Job.result)
        .join(Book, Book.id == Job.book_id)
        .where(Job.status == JobStatus.PENDING.value)
    ).all()
    pending_audio = sum(max(0.0, duration or 0.0) for _, duration, _ in pending)
    queue_seconds = 0.0
    queue_known = True
    for _, duration, result in pending:
        if (result or {}).get("mode") == "copy":
            continue
        estimate = queue.estimate_seconds(max(0.0, duration or 0.0))
        if estimate is None:
            queue_known = False
        else:
            queue_seconds += estimate

    last = session.execute(
        select(Job)
        .where(Job.status.in_([JobStatus.COMPLETED.value, JobStatus.FAILED.value]))
        .order_by(Job.finished_at.desc())
        .limit(1)
    ).scalars().first()

    if queue.pause_reason:
        state = "suspended"
    elif queue.is_paused:
        state = "paused"
    elif queue.held_reason and not any(j.get("mode") == "copy" for j in jobs):
        state = "holding"
    elif jobs:
        state = "processing"
    else:
        state = "idle"

    return {
        "version": __version__,
        "state": state,
        "paused": queue.is_paused,
        "paused_reason": queue.pause_reason,
        "held_reason": queue.held_reason,
        "active": active,
        "pending": len(pending),
        "pending_audio_seconds": round(pending_audio, 1),
        # Only when every book still to go -- the running one included -- has
        # an estimate; a finish time missing a book would be early, not late.
        "queue_finishes_at": (
            _iso(now + timedelta(seconds=queue_seconds + active_remaining))
            if (pending or active) and queue_known and active_remaining is not None
            else None
        ),
        "books": stats.get("total_books", 0),
        "cleaned": by_status.get("cleaned", 0),
        "failed": by_status.get("failed", 0),
        "waiting": by_status.get("new", 0),
        "instances_silenced": stats.get("total_matches", 0),
        "muted_seconds": round(stats.get("total_muted_seconds", 0.0), 1),
        "last_finished": (
            {
                "job_id": last.id,
                "title": last.book_title,
                "author": last.book_author,
                "status": last.status,
                "matches": (last.result or {}).get("matches", 0),
                "muted_seconds": (last.result or {}).get("muted_seconds", 0.0),
                "mode": (last.result or {}).get("mode", "clean"),
                "finished_at": _iso(last.finished_at),
            }
            if last
            else None
        ),
    }
