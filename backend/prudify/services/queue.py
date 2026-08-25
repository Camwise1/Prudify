"""The job queue: one book at a time (by default), resumable, cancellable.

Jobs live in SQLite, so a crash or restart loses nothing -- pending work is
still pending, and a job that was mid-flight is returned to the queue on the
next start. Within a job, each part is cleaned independently and transcripts
are cached, so resuming a 40-part MP3 book re-does at most one part.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..core.pipeline import PipelineCancelled, clean_part
from ..db import session_scope
from ..models import Book, BookStatus, Job, JobStatus, Part
from .events import bus

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobQueue:
    """Thread-pool style worker over a SQLite-backed job table."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._claim_lock = threading.Lock()
        self._cancelled: set[int] = set()
        self._active: dict[int, dict] = {}
        self._wake = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._requeue_orphans()
        count = max(1, self.config.processing.max_concurrent_jobs)
        for index in range(count):
            thread = threading.Thread(
                target=self._run, name=f"prudify-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info("Job queue started with %d worker(s)", count)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        log.info("Job queue stopped")

    def pause(self) -> None:
        self._paused.set()
        bus.publish("queue.paused", {"paused": True})

    def resume(self) -> None:
        self._paused.clear()
        self._wake.set()
        bus.publish("queue.paused", {"paused": False})

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def notify(self) -> None:
        """Wake idle workers after new jobs are enqueued."""
        self._wake.set()

    def _requeue_orphans(self) -> None:
        with session_scope() as session:
            orphans = session.execute(
                select(Job).where(Job.status == JobStatus.RUNNING.value)
            ).scalars().all()
            for job in orphans:
                job.status = JobStatus.PENDING.value
                job.stage = ""
                job.progress = 0.0
                job.message = "Requeued after restart"
                job.started_at = None
            if orphans:
                log.info("Requeued %d interrupted job(s)", len(orphans))

    # -- enqueue ----------------------------------------------------------

    def enqueue_book(self, session: Session, book: Book, priority: int = 100) -> Job | None:
        existing = session.execute(
            select(Job).where(
                Job.book_id == book.id,
                Job.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
            )
        ).scalars().first()
        if existing:
            return existing

        job = Job(
            book_id=book.id,
            book_title=book.title,
            book_author=book.author,
            library_id=book.library_id,
            priority=priority,
            part_total=len(book.parts),
            status=JobStatus.PENDING.value,
        )
        session.add(job)
        book.status = BookStatus.QUEUED.value
        for part in book.parts:
            if part.status != BookStatus.CLEANED.value:
                part.status = BookStatus.QUEUED.value
        session.commit()
        bus.publish(
            "job.queued",
            {"job_id": job.id, "book_id": book.id, "title": book.title, "author": book.author},
        )
        self.notify()
        return job

    def cancel_job(self, session: Session, job_id: int) -> bool:
        job = session.get(Job, job_id)
        if job is None:
            return False
        if job.status == JobStatus.PENDING.value:
            job.status = JobStatus.CANCELLED.value
            job.finished_at = _utcnow()
            book = session.get(Book, job.book_id)
            if book and book.status == BookStatus.QUEUED.value:
                book.status = BookStatus.NEW.value
            session.commit()
            bus.publish("job.cancelled", {"job_id": job_id})
            return True
        if job.status == JobStatus.RUNNING.value:
            self._cancelled.add(job_id)
            state = self._active.get(job_id)
            if state is not None:
                state.update({"stage": "cancelling", "message": "Cancelling"})
                bus.publish(
                    "job.progress",
                    {
                        "job_id": job_id,
                        "progress": state.get("progress", job.progress),
                        "stage": "cancelling",
                        "message": "Cancelling",
                        "part_index": state.get("part_index"),
                        "part_total": state.get("part_total"),
                    },
                )
            bus.publish("job.cancelling", {"job_id": job_id})
            return True
        return False

    def clear_pending(self, session: Session) -> int:
        pending = session.execute(
            select(Job).where(Job.status == JobStatus.PENDING.value)
        ).scalars().all()
        for job in pending:
            job.status = JobStatus.CANCELLED.value
            job.finished_at = _utcnow()
            book = session.get(Book, job.book_id)
            if book and book.status == BookStatus.QUEUED.value:
                book.status = BookStatus.NEW.value
        session.commit()
        bus.publish("queue.cleared", {"count": len(pending)})
        return len(pending)

    @property
    def active_jobs(self) -> list[dict]:
        return list(self._active.values())

    # -- worker loop ------------------------------------------------------

    def _claim(self) -> int | None:
        with self._claim_lock, session_scope() as session:
            job = session.execute(
                select(Job)
                .where(Job.status == JobStatus.PENDING.value)
                .order_by(Job.priority.asc(), Job.queued_at.asc(), Job.id.asc())
                .limit(1)
            ).scalars().first()
            if job is None:
                return None
            job.status = JobStatus.RUNNING.value
            job.started_at = _utcnow()
            job.message = "Starting"
            session.commit()
            return job.id

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue

            job_id = self._claim()
            if job_id is None:
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                continue

            try:
                self._process(job_id)
            except Exception:  # pragma: no cover - defensive
                log.exception("Worker crashed handling job %s", job_id)
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    if job:
                        job.status = JobStatus.FAILED.value
                        job.error = "Internal error; see logs"
                        job.finished_at = _utcnow()
            finally:
                self._active.pop(job_id, None)
                self._cancelled.discard(job_id)

            cooldown = self.config.processing.cooldown_seconds
            if cooldown and not self._stop.is_set():
                log.info("Cooling down for %ss", cooldown)
                bus.publish("queue.cooldown", {"seconds": cooldown})
                self._stop.wait(timeout=cooldown)

    def _process(self, job_id: int) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            book = session.get(Book, job.book_id)
            if book is None:
                job.status = JobStatus.FAILED.value
                job.error = "Book no longer exists"
                job.finished_at = _utcnow()
                return
            parts = list(book.parts)
            book.status = BookStatus.PROCESSING.value
            job.part_total = len(parts)
            title = book.title
            author = book.author
            library_id = book.library_id
            book_id = book.id
            part_ids = [p.id for p in parts]
            session.commit()

        library = self.config.library_by_id(library_id)
        work_root = self.config.resolved_work_dir() / f"job-{job_id}"

        self._active[job_id] = {
            "job_id": job_id,
            "book_id": book_id,
            "title": title,
            "author": author,
            "progress": 0.0,
            "stage": "starting",
        }
        bus.publish("job.started", self._active[job_id])

        succeeded = 0
        failed = 0
        skipped = 0
        total_matches = 0
        total_muted = 0.0
        word_counts: dict[str, int] = {}
        errors: list[str] = []

        for index, part_id in enumerate(part_ids):
            if self._stop.is_set() or job_id in self._cancelled:
                break

            with session_scope() as session:
                part = session.get(Part, part_id)
                if part is None:
                    continue
                source = Path(part.path)
                destination = Path(part.destination)
                part.status = BookStatus.PROCESSING.value
                session.commit()

            if library is None:
                errors.append("Library configuration is missing")
                failed += 1
                break

            def progress(stage: str, fraction: float, message: str, _i=index) -> None:
                overall = (_i + fraction) / max(1, len(part_ids))
                state = self._active.get(job_id)
                if state is not None:
                    state.update(
                        {"progress": overall, "stage": stage, "message": message,
                         "part_index": _i + 1, "part_total": len(part_ids)}
                    )
                bus.publish(
                    "job.progress",
                    {
                        "job_id": job_id,
                        "progress": overall,
                        "stage": stage,
                        "message": message,
                        "part_index": _i + 1,
                        "part_total": len(part_ids),
                    },
                )

            try:
                result = clean_part(
                    source=source,
                    destination=destination,
                    config=self.config,
                    work_dir=work_root / f"part-{part_id}",
                    progress=progress,
                    cancel=lambda: self._stop.is_set() or job_id in self._cancelled,
                )
            except PipelineCancelled:
                log.info("Job %s cancelled during part %s", job_id, part_id)
                break
            except Exception as exc:
                log.exception("Part failed: %s", source)
                result = None
                errors.append(f"{source.name}: {exc}")
                failed += 1

            with session_scope() as session:
                part = session.get(Part, part_id)
                job = session.get(Job, job_id)
                if part is None:
                    continue
                if result is None:
                    part.status = BookStatus.FAILED.value
                    part.error = errors[-1] if errors else "Unknown error"
                elif result.ok:
                    # A dry run writes nothing, so it must not claim the part
                    # is cleaned. Both branches of this ternary used to say
                    # CLEANED, which turned a cautious "preview first" into a
                    # library that reported itself fully processed with an
                    # empty output folder.
                    part.status = (
                        BookStatus.NEW.value if result.skipped else BookStatus.CLEANED.value
                    )
                    part.error = ""
                    part.match_count = result.match_count
                    part.muted_seconds = result.muted_seconds
                    part.word_count = result.word_count
                    part.matches = result.matches[:2000]
                    part.transcript_path = result.transcript_path
                    part.duration = result.source_duration
                    part.cleaned_at = _utcnow()
                    if result.skipped:
                        skipped += 1
                    else:
                        succeeded += 1
                    total_matches += result.match_count
                    total_muted += result.muted_seconds
                    for word, count in result.counts_by_word.items():
                        word_counts[word] = word_counts.get(word, 0) + count
                else:
                    part.status = BookStatus.FAILED.value
                    part.error = result.reason
                    errors.append(f"{source.name}: {result.reason}")
                    failed += 1
                if job is not None:
                    job.part_index = index + 1
                    job.progress = (index + 1) / max(1, len(part_ids))
                session.commit()

            bus.publish(
                "job.part_finished",
                {
                    "job_id": job_id,
                    "part_index": index + 1,
                    "part_total": len(part_ids),
                    "ok": bool(result and result.ok),
                },
            )

        cancelled = job_id in self._cancelled or self._stop.is_set()

        with session_scope() as session:
            job = session.get(Job, job_id)
            book = session.get(Book, job.book_id) if job else None
            if job is None:
                return

            if cancelled:
                job.status = JobStatus.CANCELLED.value
                job.message = "Cancelled"
            elif failed and not succeeded:
                job.status = JobStatus.FAILED.value
                job.error = "; ".join(errors[:5])
            else:
                job.status = JobStatus.COMPLETED.value
                job.message = (
                    f"{succeeded} cleaned, {skipped} skipped"
                    + (f", {failed} failed" if failed else "")
                )
            job.progress = 1.0
            job.finished_at = _utcnow()
            job.result = {
                "succeeded": succeeded,
                "skipped": skipped,
                "failed": failed,
                "matches": total_matches,
                "muted_seconds": round(total_muted, 2),
                "errors": errors[:20],
            }

            if book is not None:
                book.match_count = total_matches
                book.muted_seconds = total_muted
                book.word_counts = word_counts
                book.error = "; ".join(errors[:3])
                if cancelled:
                    book.status = BookStatus.NEW.value
                elif failed and not succeeded and not skipped:
                    book.status = BookStatus.FAILED.value
                elif failed:
                    book.status = BookStatus.PARTIAL.value
                elif skipped and not succeeded:
                    # Every part was a dry run: matches were counted but
                    # nothing was written, so the book is not cleaned.
                    book.status = BookStatus.NEW.value
                else:
                    book.status = BookStatus.CLEANED.value
                    book.cleaned_at = _utcnow()
            session.commit()

            bus.publish(
                "job.finished",
                {
                    "job_id": job_id,
                    "book_id": job.book_id,
                    "status": job.status,
                    "title": title,
                    "result": job.result,
                },
            )

        if not self.config.processing.keep_work_files:
            shutil.rmtree(work_root, ignore_errors=True)


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    if _queue is None:
        raise RuntimeError("Job queue has not been initialised")
    return _queue


def init_queue(config: Config) -> JobQueue:
    global _queue
    _queue = JobQueue(config)
    return _queue
