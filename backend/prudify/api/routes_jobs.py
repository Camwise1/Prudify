"""Queue inspection, control, and the live event stream."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..db import db_session
from ..models import Book, Job, JobStatus, LogRecord
from ..schemas import JobOut, QueueState
from ..services.events import bus, format_sse
from ..services.queue import get_queue, job_mode
from .deps import get_config, require_auth_stream

router = APIRouter(prefix="/queue", tags=["queue"])

_OTHER_JOB = re.compile(r"Job \d+ ")

# How long to wait for an event before emitting a keep-alive comment. Proxies
# and load balancers commonly drop idle connections at 30-60s.
_HEARTBEAT_SECONDS = 20.0


@router.get("", response_model=QueueState)
def queue_state(session: Session = Depends(db_session)) -> QueueState:
    queue = get_queue()
    pending = session.execute(
        select(Job)
        .where(Job.status == JobStatus.PENDING.value)
        .order_by(Job.priority.asc(), Job.queued_at.asc())
        .limit(200)
    ).scalars().all()
    recent = session.execute(
        select(Job)
        .where(
            Job.status.in_(
                [
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ]
            )
        )
        .order_by(Job.finished_at.desc())
        .limit(25)
    ).scalars().all()
    durations = dict(
        session.execute(
            select(Book.id, Book.duration).where(Book.id.in_([job.book_id for job in pending]))
        ).all()
    )
    rows = []
    for job in pending:
        length = max(0.0, durations.get(job.book_id) or 0.0)
        estimate = 60.0 if job_mode(job) == "copy" else queue.estimate_seconds(length)
        rows.append(
            JobOut.model_validate(job).model_copy(
                update={"book_duration": length, "estimate_seconds": estimate}
            )
        )
    active = queue.active_jobs
    return QueueState(
        paused=queue.is_paused,
        paused_reason=queue.pause_reason,
        active=active,
        pending=rows,
        recent=[JobOut.model_validate(job) for job in recent],
        estimate=_queue_estimate(rows, active, queue.rates()),
    )


def _queue_estimate(pending: list[JobOut], active: list[dict], rates: dict) -> dict:
    """How long until the queue is empty, with what the figure rests on.

    Books not yet probed and stages not yet measured are counted and reported
    rather than silently left out, so the page can say "at least" when it
    has to.
    """
    unknown = sum(1 for job in pending if job.estimate_seconds is None)
    seconds = sum(job.estimate_seconds or 0.0 for job in pending)
    for job in active:
        if job.get("job_eta_seconds") is not None:
            seconds += job["job_eta_seconds"]
        else:
            unknown += 1
    return {
        "pending_audio_seconds": round(sum(job.book_duration for job in pending), 1),
        "seconds": round(seconds, 1),
        "unknown_jobs": unknown,
        "rates": {name: round(rate, 4) for name, rate in rates.items()},
    }


@router.get("/history")
def history(
    session: Session = Depends(db_session),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
) -> dict:
    query = select(Job)
    if status:
        query = query.where(Job.status.in_(status.split(",")))
    rows = (
        session.execute(
            query.order_by(Job.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        .scalars()
        .all()
    )
    return {
        "items": [JobOut.model_validate(job).model_dump(mode="json") for job in rows],
        "page": page,
        "page_size": page_size,
    }


@router.post("/pause")
def pause() -> dict:
    get_queue().pause()
    return {"paused": True}


@router.post("/resume")
def resume() -> dict:
    get_queue().resume()
    return {"paused": False}


@router.delete("/{job_id}")
def cancel(job_id: int, session: Session = Depends(db_session)) -> dict:
    if not get_queue().cancel_job(session, job_id):
        raise HTTPException(status_code=409, detail="Job is not cancellable")
    return {"job_id": job_id, "cancelled": True}


@router.post("/clear")
def clear(session: Session = Depends(db_session)) -> dict:
    return {"cleared": get_queue().clear_pending(session)}


@router.get("/events", dependencies=[Depends(require_auth_stream)])
async def events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of queue, job and library activity."""
    queue = bus.subscribe()

    async def generator() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"
            for payload in bus.backlog()[-25:]:
                yield format_sse(payload)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield format_sse(payload)
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(db_session)) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobOut.model_validate(job)


@router.get("/{job_id}/detail")
def job_detail(
    job_id: int,
    session: Session = Depends(db_session),
    config: Config = Depends(get_config),
) -> dict:
    """One job in full: its row, live state while running, the book, and its log.

    The log is every record written while the job ran, less lines that name a
    different job: the copier runs alongside the transcription worker, and its
    progress is noise here. Unattributed lines -- ffmpeg, the engine -- stay,
    since they are the context someone diagnosing a slow job wants.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    book = session.get(Book, job.book_id)

    logs = []
    if job.started_at:
        query = select(LogRecord).where(LogRecord.created_at >= job.started_at)
        if job.finished_at:
            query = query.where(LogRecord.created_at <= job.finished_at)
        records = session.execute(
            query.order_by(LogRecord.id.desc()).limit(200)
        ).scalars().all()
        own = f"Job {job_id} "
        logs = [
            {
                "id": r.id,
                "created_at": r.created_at,
                "level": r.level,
                "logger": r.logger,
                "message": r.message,
            }
            for r in reversed(records)
            if not _OTHER_JOB.match(r.message) or r.message.startswith(own)
        ]

    return {
        "job": JobOut.model_validate(job).model_dump(mode="json"),
        "mode": job_mode(job),
        "live": get_queue().job_snapshot(job_id),
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "folder": book.folder,
            "duration": max(0.0, book.duration or 0.0),
            "part_count": len(book.parts),
            "total_bytes": book.total_bytes,
            "formats": book.formats,
        }
        if book
        else None,
        "engine": config.transcription.engine,
        "model": config.transcription.model,
        "logs": json.loads(json.dumps(logs, default=str)),
    }


@router.get("/{job_id}/result")
def job_result(job_id: int, session: Session = Depends(db_session)) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(json.dumps(job.result or {}, default=str))
