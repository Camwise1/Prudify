"""Queue inspection, control, and the live event stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import db_session
from ..models import Job, JobStatus
from ..schemas import JobOut, QueueState
from ..services.events import bus, format_sse
from ..services.queue import get_queue
from .deps import require_auth_stream

router = APIRouter(prefix="/queue", tags=["queue"])

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
    return QueueState(
        paused=queue.is_paused,
        active=queue.active_jobs,
        pending=[JobOut.model_validate(job) for job in pending],
        recent=[JobOut.model_validate(job) for job in recent],
    )


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


@router.get("/{job_id}/result")
def job_result(job_id: int, session: Session = Depends(db_session)) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(json.dumps(job.result or {}, default=str))
