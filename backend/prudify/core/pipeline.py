"""The cleaning pipeline: one source file in, one cleaned file out.

Stages, in order:

    probe -> transcribe -> match -> render -> validate -> publish

Every stage reports normalised progress so the UI can show a single bar per
part. Transcripts are cached on disk keyed by (file, size, mtime, model), which
means re-running with different *filter* settings is nearly instant -- you only
pay for Whisper once per book per model.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from . import audio as audio_mod
from . import matcher as matcher_mod
from . import transcribe as transcribe_mod

log = logging.getLogger(__name__)

# Fractions of the overall progress bar owned by each stage.
_STAGE_WEIGHTS = {
    "probing": (0.00, 0.02),
    "transcribing": (0.02, 0.72),
    "matching": (0.72, 0.76),
    "rendering": (0.76, 0.96),
    "validating": (0.96, 0.99),
    "publishing": (0.99, 1.00),
}

ProgressFn = Callable[[str, float, str], None]  # stage, 0..1 overall, message


class PipelineCancelled(RuntimeError):
    pass


@dataclass
class PartResult:
    source: Path
    destination: Path
    ok: bool = False
    skipped: bool = False
    reason: str = ""
    match_count: int = 0
    muted_seconds: float = 0.0
    word_count: int = 0
    counts_by_word: dict[str, int] = field(default_factory=dict)
    matches: list[dict] = field(default_factory=list)
    source_duration: float = 0.0
    output_duration: float = 0.0
    chapters: int = 0
    had_cover: bool = False
    transcript_path: str = ""
    elapsed_seconds: float = 0.0
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "match_count": self.match_count,
            "muted_seconds": round(self.muted_seconds, 3),
            "word_count": self.word_count,
            "counts_by_word": self.counts_by_word,
            "source_duration": self.source_duration,
            "output_duration": self.output_duration,
            "chapters": self.chapters,
            "had_cover": self.had_cover,
            "transcript_path": self.transcript_path,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "problems": self.problems,
        }


def transcript_cache_path(config: Config, source: Path) -> Path:
    """Cache key covers the file identity *and* the model that produced it."""
    try:
        stat = source.stat()
        stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        stamp = "0:0"
    key = ":".join(
        [
            str(source.resolve()),
            stamp,
            config.transcription.engine,
            config.transcription.model,
            config.transcription.language,
            str(config.transcription.vad_filter),
        ]
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    safe_name = "".join(c for c in source.stem if c.isalnum() or c in "-_ ")[:60].strip()
    return config.transcript_dir() / f"{safe_name or 'book'}-{digest}.json"


def _scaled(stage: str, fraction: float) -> float:
    lo, hi = _STAGE_WEIGHTS.get(stage, (0.0, 1.0))
    return lo + (hi - lo) * max(0.0, min(1.0, fraction))


def free_space_mb(path: Path) -> float:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(target).free / (1024 * 1024)
    except OSError:
        return float("inf")


def clean_part(
    source: Path,
    destination: Path,
    config: Config,
    work_dir: Path,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    extra_words: list[str] | None = None,
) -> PartResult:
    """Clean one audio file. Safe to call repeatedly; never touches the source."""
    started = time.monotonic()
    result = PartResult(source=source, destination=destination)

    def report(stage: str, fraction: float, message: str = "") -> None:
        if progress:
            progress(stage, _scaled(stage, fraction), message or stage)

    def check_cancel() -> None:
        if cancel and cancel():
            raise PipelineCancelled("Cancelled by user")

    check_cancel()
    if not source.exists():
        result.reason = "Source file no longer exists"
        return result

    if destination.exists() and config.processing.skip_if_output_exists and not (
        config.output.overwrite_existing
    ):
        result.skipped = True
        result.ok = True
        result.reason = "Cleaned output already exists"
        return result

    # ---- probe ---------------------------------------------------------
    report("probing", 0.0, "Reading media info")
    info = audio_mod.probe(source)
    result.source_duration = info.duration
    result.chapters = info.chapter_count
    result.had_cover = info.has_cover
    if info.duration <= 0:
        result.reason = "Could not determine duration; file may be corrupt"
        return result

    needed = config.processing.min_free_space_mb
    if needed and free_space_mb(destination.parent) < needed:
        result.reason = (
            f"Less than {needed} MB free at the destination; refusing to start"
        )
        return result
    report("probing", 1.0)

    work_dir.mkdir(parents=True, exist_ok=True)

    # ---- transcribe ----------------------------------------------------
    check_cancel()
    cache_path = transcript_cache_path(config, source)
    transcript: transcribe_mod.Transcript | None = None
    if cache_path.exists():
        try:
            transcript = transcribe_mod.Transcript.load(cache_path)
            log.info("Reusing cached transcript for %s", source.name)
            report("transcribing", 1.0, "Using cached transcript")
        except Exception:
            transcript = None

    if transcript is None:
        def t_progress(fraction: float, message: str) -> None:
            report("transcribing", fraction, message)

        transcript = transcribe_mod.transcribe_file(
            source,
            config.transcription,
            work_dir=work_dir / "transcribe",
            progress=t_progress,
            cancel=cancel,
        )
        if config.processing.keep_transcripts:
            transcript.save(cache_path)
            result.transcript_path = str(cache_path)
    else:
        result.transcript_path = str(cache_path)

    result.word_count = len(transcript.words)
    if not transcript.words:
        result.reason = "Transcription produced no words"
        return result

    # ---- match ---------------------------------------------------------
    check_cancel()
    report("matching", 0.0, "Matching wordlist")
    matcher = matcher_mod.build_matcher_from_settings(
        config.filtering,
        extra_words=extra_words or [],
        user_dir=config.resolved_data_dir() / "wordlists",
    )
    report_data = matcher_mod.analyse(
        transcript.words,
        matcher,
        pad_before_ms=config.filtering.pad_before_ms,
        pad_after_ms=config.filtering.pad_after_ms,
        merge_gap_ms=config.filtering.merge_gap_ms,
        neighbour_guard_ms=config.filtering.neighbour_guard_ms,
        duration=info.duration,
    )
    result.match_count = len(report_data.matches)
    result.muted_seconds = report_data.total_muted_seconds
    result.counts_by_word = report_data.counts_by_word
    result.matches = [m.to_dict() for m in report_data.matches]
    report("matching", 1.0, f"{result.match_count} matches")

    if config.processing.dry_run:
        result.ok = True
        result.skipped = True
        result.reason = f"Dry run: {result.match_count} matches, nothing written"
        result.elapsed_seconds = time.monotonic() - started
        return result

    # ---- render --------------------------------------------------------
    check_cancel()
    destination.parent.mkdir(parents=True, exist_ok=True)

    if not report_data.matches and config.output.copy_when_clean:
        report("rendering", 0.5, "No matches; copying original")
        shutil.copy2(source, destination)
        result.ok = True
        result.reason = "No profanity found; copied unchanged"
        result.output_duration = info.duration
        result.elapsed_seconds = time.monotonic() - started
        report("publishing", 1.0, "Done")
        return result

    extra_tags = {}
    if config.output.tag_cleaned:
        extra_tags["comment"] = (
            f"Cleaned by Prudify: {result.match_count} instances silenced"
        )
        extra_tags["PRUDIFY_CLEANED"] = "1"

    def r_progress(fraction: float) -> None:
        report("rendering", fraction, "Encoding cleaned audio")

    audio_mod.render(
        source=source,
        destination=destination,
        info=info,
        intervals=report_data.intervals,
        mode=config.output.mode,
        beep_frequency=config.output.beep_frequency,
        beep_volume=config.output.beep_volume,
        codec=config.output.audio_codec,
        bitrate=config.output.bitrate,
        sample_rate=config.output.sample_rate,
        preserve_chapters=config.output.preserve_chapters,
        preserve_cover=config.output.preserve_cover,
        preserve_metadata=config.output.preserve_metadata,
        extra_tags=extra_tags,
        work_dir=work_dir / "render",
        progress=r_progress,
    )

    # ---- validate ------------------------------------------------------
    report("validating", 0.0, "Validating output")
    ok, problems = audio_mod.validate_output(
        info,
        destination,
        duration_tolerance=config.processing.duration_tolerance_seconds,
        expect_chapters=config.output.preserve_chapters,
        expect_cover=config.output.preserve_cover,
        mode=config.output.mode,
        removed_seconds=report_data.total_muted_seconds,
    )
    result.problems = problems
    try:
        result.output_duration = audio_mod.probe(destination).duration
    except Exception:
        result.output_duration = 0.0

    if not ok:
        # A failed validation must not leave a broken file in the clean library.
        destination.unlink(missing_ok=True)
        result.reason = "; ".join(problems)
        result.elapsed_seconds = time.monotonic() - started
        return result

    report("publishing", 1.0, "Done")
    result.ok = True
    result.reason = f"{result.match_count} instances silenced"
    result.elapsed_seconds = time.monotonic() - started

    if not config.processing.keep_work_files:
        shutil.rmtree(work_dir / "render", ignore_errors=True)
        shutil.rmtree(work_dir / "transcribe", ignore_errors=True)

    return result


def preview_part(
    source: Path,
    config: Config,
    work_dir: Path,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> matcher_mod.MatchReport:
    """Transcribe (or reuse a cached transcript) and report matches only."""
    cache_path = transcript_cache_path(config, source)
    if cache_path.exists():
        transcript = transcribe_mod.Transcript.load(cache_path)
    else:
        transcript = transcribe_mod.transcribe_file(
            source,
            config.transcription,
            work_dir=work_dir / "transcribe",
            progress=(lambda f, m: progress("transcribing", f, m)) if progress else None,
            cancel=cancel,
        )
        if config.processing.keep_transcripts:
            transcript.save(cache_path)

    matcher = matcher_mod.build_matcher_from_settings(
        config.filtering, user_dir=config.resolved_data_dir() / "wordlists"
    )
    return matcher_mod.analyse(
        transcript.words,
        matcher,
        pad_before_ms=config.filtering.pad_before_ms,
        pad_after_ms=config.filtering.pad_after_ms,
        merge_gap_ms=config.filtering.merge_gap_ms,
        neighbour_guard_ms=config.filtering.neighbour_guard_ms,
        duration=transcript.duration,
    )
