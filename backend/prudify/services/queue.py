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
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..core import audio as audio_mod
from ..core.pipeline import PipelineCancelled, clean_part, pass_through_part, stage_fraction
from ..db import session_scope
from ..models import Book, BookStatus, Job, JobStatus, Part
from .events import bus

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_HEARTBEAT_SECONDS = 5.0
# Below this, a publish per ffmpeg progress line is a few hundred events a
# minute per job, all saying almost the same thing.
_MIN_PUBLISH_INTERVAL = 1.0
# After this long with the fraction unmoved, any extrapolation from it is
# fiction and gets withheld.
_STALE_PROGRESS_SECONDS = 30.0
# ...unless progress arrives in steps. Chunked transcription moves once per
# chunk, every five minutes or so, and a fixed 30 seconds called every one of
# those waits a stall. Quiet for this many typical steps is a stall.
_STALE_STEPS = 3.0
_MAX_TICKS = 500

_STAGE_ORDER = {
    "clean": ("probing", "transcribing", "matching", "rendering", "validating", "publishing"),
    "copy": ("copying",),
}
# Stages whose running time scales with the length of the audio. The rest take
# seconds and are left out of estimates rather than guessed at.
_MEASURED_STAGES = ("transcribing", "rendering")
# How many recent jobs the per-stage speed is averaged over.
_RATE_HISTORY = 10


def _stall_threshold(state: dict) -> float:
    ticks = state.get("ticks") or []
    if len(ticks) < 2:
        return _STALE_PROGRESS_SECONDS
    mean_gap = (ticks[-1][0] - ticks[0][0]) / (len(ticks) - 1)
    return max(_STALE_PROGRESS_SECONDS, mean_gap * _STALE_STEPS)


def _speed(state: dict) -> float | None:
    """Seconds of audio handled per wall-clock second in the current stage."""
    if state.get("stage") not in _MEASURED_STAGES:
        return None
    audio = _current_part_audio(state)
    started = state.get("stage_started_at")
    moved_at = state.get("fraction_changed_at")
    fraction = state.get("stage_fraction") or 0.0
    if not audio or started is None or moved_at is None:
        return None
    span = moved_at - started
    if span <= 1.0 or fraction <= 0.0:
        return None
    return fraction * audio / span


def _current_part_audio(state: dict) -> float:
    parts = state.get("part_audio") or []
    index = (state.get("part_index") or 1) - 1
    return parts[index] if 0 <= index < len(parts) else 0.0


def _remaining_after_stage(state: dict, rates: dict[str, float]) -> float | None:
    """Estimated seconds for everything after the current stage, or None.

    Built from measured seconds-per-audio-second rates, so it needs the parts'
    durations and a rate for every slow stage still to come. Missing either,
    the honest answer is "unknown" rather than a number that looks precise.
    """
    order = _STAGE_ORDER.get(state.get("mode") or "clean", ())
    parts = state.get("part_audio") or []
    current = (state.get("part_index") or 1) - 1
    stage = state.get("stage")
    if stage not in order or not parts:
        return None
    later_here = order[order.index(stage) + 1:]
    total = 0.0
    for index in range(current, len(parts)):
        stages = later_here if index == current else order
        for name in stages:
            if name not in _MEASURED_STAGES:
                continue
            rate = rates.get(name)
            if rate is None or not parts[index]:
                return None
            total += rate * parts[index]
    return total


def _progress_payload(state: dict, now: float, rates: dict[str, float] | None = None) -> dict:
    """The wire form of a running job, with the timings the UI needs.

    ``stage_elapsed`` is what makes a stalled-looking percentage readable, and
    ``stage_eta_seconds`` turns it into something a person can plan around.
    The ETA extrapolates from the moment progress last moved and then counts
    down, so step-wise progress -- one chunk at a time -- does not make the
    estimate balloon between steps. It is withheld until the stage is 2% in,
    because before that the extrapolation is numerically silly.

    ``job_eta_seconds`` adds the later stages and parts, priced from measured
    rates; it is None whenever any of those is still unmeasured.
    """
    started = state.get("stage_started_at") or now
    stage_elapsed = max(0.0, now - started)
    fraction = state.get("stage_fraction") or 0.0
    moved_at = state.get("fraction_changed_at") or started
    stalled = (now - moved_at) > _stall_threshold(state)

    # An estimate divided by a fraction that has stopped advancing does not
    # decay towards zero -- it climbs, forever, because the elapsed time keeps
    # growing while the denominator does not. A remaining time that gets
    # further away the longer you wait is worse than no estimate at all, so
    # once progress goes quiet the number is withheld and the UI says why.
    eta = None
    moved_span = moved_at - started
    if fraction > 0.02 and moved_span > 1.0 and not stalled:
        eta = max(0.0, moved_span * (1.0 - fraction) / fraction - (now - moved_at))

    job_eta = None
    if eta is not None:
        after = _remaining_after_stage(state, rates or {})
        if after is not None:
            job_eta = eta + after

    payload = {
        key: state.get(key)
        for key in (
            "job_id", "progress", "stage", "message", "part_index", "part_total", "mode",
        )
    }
    payload["stage_elapsed"] = round(stage_elapsed, 1)
    payload["elapsed"] = round(max(0.0, now - (state.get("started_at") or now)), 1)
    payload["stage_eta_seconds"] = round(eta, 1) if eta is not None else None
    payload["job_eta_seconds"] = round(job_eta, 1) if job_eta is not None else None
    speed = _speed(state)
    payload["speed"] = round(speed, 2) if speed is not None else None
    payload["stalled"] = stalled
    return payload


def _stage_timings(stages: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for entry in stages:
        if entry.get("seconds") is not None:
            totals[entry["stage"]] = totals.get(entry["stage"], 0.0) + entry["seconds"]
    return {name: round(seconds, 1) for name, seconds in totals.items()}


def rates_from_history(results: list[dict]) -> dict[str, float]:
    """Seconds of work per second of audio, per slow stage, over recent jobs.

    A stage under a second long is left out: that is a cached transcript or a
    copied file, and averaging it in would promise the next book the same.
    """
    spent: dict[str, float] = {}
    audio: dict[str, float] = {}
    for result in results:
        timings = (result or {}).get("timings") or {}
        length = timings.get("audio_seconds") or 0.0
        if length <= 0:
            continue
        for name, seconds in (timings.get("stages") or {}).items():
            if name in _MEASURED_STAGES and seconds > 1.0:
                spent[name] = spent.get(name, 0.0) + seconds
                audio[name] = audio.get(name, 0.0) + length
    return {name: spent[name] / audio[name] for name in spent if audio[name] > 0}


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


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
        # Seconds of work per second of audio, per slow stage: from finished
        # jobs, and from whatever is running now. Live wins where both exist,
        # because it reflects this machine's conditions today -- another
        # model, or a memory squeeze.
        self._history_rates: dict[str, float] = {}
        self._live_rates: dict[str, float] = {}
        self._durations_wake = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._requeue_orphans()
        self._sweep_stale_work_dirs()
        heartbeat = threading.Thread(
            target=self._heartbeat, name="prudify-heartbeat", daemon=True
        )
        heartbeat.start()
        self._threads.append(heartbeat)
        self._reload_history_rates()
        durations = threading.Thread(
            target=self._fill_durations, name="prudify-durations", daemon=True
        )
        durations.start()
        self._threads.append(durations)
        # Pass-through copies get a worker of their own. They take minutes and
        # need no GPU, so queuing one behind a three-hour transcription would
        # make "this book is clean, just copy it" wait for no reason.
        copier = threading.Thread(
            target=self._run, args=("copy",), name="prudify-copier", daemon=True
        )
        copier.start()
        self._threads.append(copier)
        count = max(1, self.config.processing.max_concurrent_jobs)
        for index in range(count):
            thread = threading.Thread(
                target=self._run, name=f"prudify-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info("Job queue started with %d worker(s)", count)

    def request_stop(self) -> None:
        """Stop claiming new work, and tell running work to wind up.

        Separate from :meth:`stop`, which also waits for the workers. This is
        what a signal handler can safely call: it must return promptly, and
        setting the event is all that is needed for the worker loop to stop
        claiming parts and for in-flight ffmpeg runs to read the cancellation.
        """
        self._stop.set()
        self._wake.set()
        self._durations_wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        self._durations_wake.set()
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
        self._durations_wake.set()

    # -- estimates --------------------------------------------------------

    def rates(self) -> dict[str, float]:
        return {**self._history_rates, **self._live_rates}

    def _reload_history_rates(self) -> None:
        with session_scope() as session:
            results = session.execute(
                select(Job.result)
                .where(Job.status == JobStatus.COMPLETED.value)
                .order_by(Job.finished_at.desc())
                .limit(_RATE_HISTORY * 3)
            ).scalars().all()
        recent = [r for r in results if (r or {}).get("timings")][:_RATE_HISTORY]
        self._history_rates = rates_from_history(recent)

    def estimate_seconds(self, audio_seconds: float) -> float | None:
        """Wall-clock time to clean this much audio, or None if not yet known."""
        rates = self.rates()
        if not audio_seconds or any(name not in rates for name in _MEASURED_STAGES):
            return None
        return sum(rates[name] for name in _MEASURED_STAGES) * audio_seconds

    def _fill_durations(self) -> None:
        """Probe the running time of queued books that the scan left at zero.

        The library scan deliberately skips ffprobe -- one call per file across
        a whole NAS is slow -- so a fresh library knows no durations, and
        without them nothing can say how long the queue will take. Only books
        that are actually queued are probed, a few at a time, in the
        background.
        """
        while not self._stop.is_set():
            self._durations_wake.wait(timeout=60.0)
            self._durations_wake.clear()
            while not self._stop.is_set():
                with session_scope() as session:
                    book_ids = session.execute(
                        select(Job.book_id)
                        .join(Book, Book.id == Job.book_id)
                        .where(Job.status == JobStatus.PENDING.value, Book.duration == 0)
                        .order_by(Job.priority.asc(), Job.queued_at.asc())
                        .limit(3)
                    ).scalars().all()
                if not book_ids:
                    break
                for book_id in book_ids:
                    if self._stop.is_set():
                        return
                    _probe_book_durations(book_id)

    def job_snapshot(self, job_id: int) -> dict | None:
        """Everything known about a running job, for the detail view."""
        state = self._active.get(job_id)
        if state is None:
            return None
        now = time.monotonic()
        snapshot = self._public_state(state, now)
        started = state.get("stage_started_at") or now
        snapshot["ticks"] = [
            [round(t - started, 1), round(f, 4)] for t, f in state.get("ticks") or []
        ]
        snapshot["stages"] = [dict(entry) for entry in state.get("stages") or []]
        snapshot["part_audio"] = list(state.get("part_audio") or [])
        snapshot["rates"] = self.rates()
        return snapshot

    def _public_state(self, state: dict, now: float) -> dict:
        payload = _progress_payload(state, now, self.rates())
        for key in ("book_id", "title", "author", "audio_seconds"):
            payload[key] = state.get(key)
        return payload

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

    def _heartbeat(self) -> None:
        """Re-publish a running job's state even when nothing has changed.

        Encoding a long book is one ffmpeg invocation that can run for an hour,
        and between its progress lines -- or while it is doing something that
        emits none at all, like the faststart rewrite -- the UI has nothing to
        say. A percentage that has not moved in ten minutes is indistinguishable
        from a hung process, and the only honest way to tell them apart is to
        keep saying how long the current stage has been running. Silence is the
        thing that makes people restart a job that was working.
        """
        while not self._stop.wait(_HEARTBEAT_SECONDS):
            now = time.monotonic()
            for state in list(self._active.values()):
                updated = state.get("updated_at") or now
                if now - updated < _HEARTBEAT_SECONDS:
                    continue  # real progress is already flowing
                state["updated_at"] = now
                bus.publish("job.progress", _progress_payload(state, now, self.rates()))

    def _sweep_stale_work_dirs(self) -> None:
        """Delete scratch directories left behind by a job that will never finish.

        ``_process`` removes its own work directory on the way out, but only
        when it gets to run to the end. A container killed mid-render -- OOM,
        ``docker restart``, a host reboot -- leaves a job directory holding a
        multi-gigabyte WAV and a partial render, and nothing ever collected
        them. On a Docker volume that is invisible growth until the disk is
        full, which is precisely when it is hardest to diagnose.

        Directories belonging to a job that is still pending or running are
        kept: those hold the cached transcription chunks that let a requeued
        job resume rather than start the book again.
        """
        if self.config.processing.keep_work_files:
            return
        work_root = self.config.resolved_work_dir()
        if not work_root.is_dir():
            return

        with session_scope() as session:
            live = {
                job_id
                for (job_id,) in session.execute(
                    select(Job.id).where(
                        Job.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value])
                    )
                ).all()
            }

        removed = 0
        freed = 0
        for entry in work_root.iterdir():
            if not entry.is_dir() or not entry.name.startswith("job-"):
                continue
            try:
                job_id = int(entry.name[4:])
            except ValueError:
                continue
            if job_id in live:
                continue
            freed += _directory_size(entry)
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

        if removed:
            log.info(
                "Removed %d abandoned work director%s, freeing %.1f GB",
                removed,
                "y" if removed == 1 else "ies",
                freed / (1024**3),
            )

    # -- enqueue ----------------------------------------------------------

    def enqueue_book(
        self, session: Session, book: Book, priority: int = 100, mode: str = "clean"
    ) -> Job | None:
        """Queue a book. ``mode="copy"`` passes it through unchanged.

        The mode rides in ``Job.result`` until the job finishes and replaces it
        with its outcome; the database is a cache, so a new column would need
        a migration for a value that only matters while the job is waiting.
        """
        existing = session.execute(
            select(Job).where(
                Job.book_id == book.id,
                Job.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
            )
        ).scalars().first()
        if existing:
            if existing.status == JobStatus.PENDING.value and job_mode(existing) != mode:
                existing.result = {"mode": mode}
                existing.priority = min(existing.priority, priority)
                session.commit()
                bus.publish("job.queued", {"job_id": existing.id, "book_id": book.id})
                self.notify()
            return existing

        job = Job(
            book_id=book.id,
            book_title=book.title,
            book_author=book.author,
            library_id=book.library_id,
            priority=priority,
            part_total=len(book.parts),
            status=JobStatus.PENDING.value,
            result={"mode": mode} if mode != "clean" else {},
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
        now = time.monotonic()
        return [self._public_state(state, now) for state in list(self._active.values())]

    # -- worker loop ------------------------------------------------------

    def _claim(self, mode: str = "clean") -> int | None:
        with self._claim_lock, session_scope() as session:
            job = session.execute(
                select(Job)
                .where(
                    Job.status == JobStatus.PENDING.value,
                    func.coalesce(func.json_extract(Job.result, "$.mode"), "clean") == mode,
                )
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

    def _run(self, mode: str = "clean") -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue

            job_id = self._claim(mode)
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
            if mode == "clean" and cooldown and not self._stop.is_set():
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
            mode = job_mode(job)
            title = book.title
            author = book.author
            library_id = book.library_id
            book_id = book.id
            part_ids = [p.id for p in parts]
            session.commit()

        library = self.config.library_by_id(library_id)
        work_root = self.config.resolved_work_dir() / f"job-{job_id}"

        # Durations turn a fraction into a speed and an end time. A copy has
        # no use for them, and probing every part over a network share would
        # add more to it than the copy itself.
        part_audio = [] if mode == "copy" else _probe_book_durations(book_id)

        now = time.monotonic()
        self._active[job_id] = {
            "job_id": job_id,
            "book_id": book_id,
            "title": title,
            "author": author,
            "mode": mode,
            "progress": 0.0,
            "stage": "starting",
            "stage_fraction": 0.0,
            "started_at": now,
            "stage_started_at": now,
            "fraction_changed_at": now,
            "updated_at": now,
            "part_audio": part_audio,
            "audio_seconds": round(sum(part_audio), 1),
            "ticks": [],
            "stages": [],
        }
        bus.publish("job.started", self._public_state(self._active[job_id], now))

        def cancel() -> bool:
            return self._stop.is_set() or job_id in self._cancelled

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
                if state is None:
                    return

                now = time.monotonic()
                local = stage_fraction(stage, fraction)
                stage_changed = (
                    state.get("stage") != stage or state.get("part_index") != _i + 1
                )
                if stage_changed:
                    # Each stage times itself. An ETA drawn from the whole job
                    # is dominated by transcription and tells you nothing about
                    # the encode you are actually waiting on.
                    state["stage_started_at"] = now
                    state["fraction_changed_at"] = now
                    state["ticks"] = [(now, local)]
                    stages = state["stages"]
                    if stages and stages[-1]["seconds"] is None:
                        stages[-1]["seconds"] = round(time.time() - stages[-1]["started"], 1)
                    stages.append(
                        {"stage": stage, "part": _i + 1, "started": time.time(), "seconds": None}
                    )
                    log.info(
                        "Job %s part %s/%s: %s", job_id, _i + 1, len(part_ids), message or stage
                    )
                elif local != state.get("stage_fraction"):
                    state["fraction_changed_at"] = now
                    ticks = state["ticks"]
                    ticks.append((now, local))
                    if len(ticks) > _MAX_TICKS:
                        # Keep the first: it anchors the stage's average pace.
                        del ticks[1:len(ticks) - _MAX_TICKS + 1]

                state.update(
                    {
                        "progress": overall,
                        "stage": stage,
                        "stage_fraction": local,
                        "message": message,
                        "part_index": _i + 1,
                        "part_total": len(part_ids),
                    }
                )

                # A stage change is news and goes out at once; everything else
                # waits its turn, so one ffmpeg does not become an event storm.
                if not stage_changed and now - (state.get("published_at") or 0.0) < (
                    _MIN_PUBLISH_INTERVAL
                ):
                    return
                state["published_at"] = now
                state["updated_at"] = now
                speed = _speed(state)
                if speed:
                    self._live_rates[stage] = 1.0 / speed
                bus.publish("job.progress", _progress_payload(state, now, self.rates()))

            try:
                if mode == "copy":
                    result = pass_through_part(
                        source=source,
                        destination=destination,
                        overwrite=self.config.output.overwrite_existing,
                        progress=progress,
                        cancel=cancel,
                    )
                else:
                    result = clean_part(
                        source=source,
                        destination=destination,
                        config=self.config,
                        work_dir=work_root / f"part-{part_id}",
                        progress=progress,
                        cancel=cancel,
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
                    f"{succeeded} {'copied unchanged' if mode == 'copy' else 'cleaned'}, "
                    f"{skipped} skipped" + (f", {failed} failed" if failed else "")
                )
            job.progress = 1.0
            job.finished_at = _utcnow()
            stages = self._active.get(job_id, {}).get("stages") or []
            if stages and stages[-1]["seconds"] is None:
                stages[-1]["seconds"] = round(time.time() - stages[-1]["started"], 1)
            job.result = {
                "mode": mode,
                "timings": {
                    "audio_seconds": round(sum(part_audio), 1),
                    "stages": _stage_timings(stages),
                },
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

        if not cancelled and mode == "clean":
            self._reload_history_rates()

        if not self.config.processing.keep_work_files:
            shutil.rmtree(work_root, ignore_errors=True)


def job_mode(job: Job) -> str:
    return (job.result or {}).get("mode") or "clean"


def _probe_book_durations(book_id: str) -> list[float]:
    """Each part's running time, probing any the scan left at zero.

    Saved to the parts and the book so the work is done once. A part that
    cannot be probed counts as zero, which leaves estimates unknown rather
    than wrong.
    """
    with session_scope() as session:
        book = session.get(Book, book_id)
        if book is None:
            return []
        parts = [(p.id, p.path, p.duration or 0.0) for p in book.parts]

    durations: list[float] = []
    probed: dict[int, float] = {}
    for part_id, path, known in parts:
        if known > 0:
            durations.append(known)
            continue
        try:
            seconds = audio_mod.probe(Path(path)).duration
        except Exception as exc:
            log.debug("Could not probe %s: %s", path, exc)
            seconds = 0.0
        probed[part_id] = seconds
        durations.append(seconds)

    with session_scope() as session:
        book = session.get(Book, book_id)
        if book is None:
            return durations
        for part in book.parts:
            if part.id in probed and probed[part.id] > 0:
                part.duration = probed[part.id]
        # Marked as tried even when probing failed, so the background filler
        # does not retry an unreadable book forever.
        book.duration = sum(durations) or -1.0
        session.commit()
    return durations


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    if _queue is None:
        raise RuntimeError("Job queue has not been initialised")
    return _queue


def init_queue(config: Config) -> JobQueue:
    global _queue
    _queue = JobQueue(config)
    return _queue
