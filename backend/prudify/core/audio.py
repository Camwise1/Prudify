"""FFmpeg / FFprobe integration.

Everything Prudify knows about a media file comes through here: probing,
extracting analysis-grade PCM, building filter graphs, and running the single
encode pass that produces the cleaned copy.

Design notes
------------
* Filter graphs are written to a *file* rather than the command line (see
  ``filter_script_args``, which picks the spelling this ffmpeg understands).
  A book with a few hundred hits produces a filter string well past the
  Windows 32 KB command-line limit, and this sidesteps it on every platform.
* The cleaned file is produced in **one** ffmpeg pass from the original input,
  so ``-map_metadata``/``-map_chapters`` can copy tags, chapters and cover art
  straight from the source. No separate remux step, no metadata round-trip.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..config import find_binary
from .cancel import OperationCancelled

log = logging.getLogger(__name__)

# Batch size for chained volume filters. Keeping each `enable` expression
# modest avoids pathological parse times inside ffmpeg's expression evaluator.
_INTERVALS_PER_FILTER = 50

# ffmpeg's `sine` source peaks around -18 dBFS, not full scale. Normalising it
# first means the user-facing `beep_volume` really is a fraction of full scale.
_SINE_NORMALISE = 8.0


class FFmpegError(RuntimeError):
    """Raised when ffmpeg or ffprobe exits non-zero."""

    def __init__(self, message: str, stderr: str = "", returncode: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


@dataclass(slots=True)
class Chapter:
    index: int
    start: float
    end: float
    title: str = ""


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float = 0.0
    format_name: str = ""
    codec: str = ""
    bit_rate: int = 0
    sample_rate: int = 0
    channels: int = 2
    size_bytes: int = 0
    audio_stream_index: int = 0
    cover_stream_index: int | None = None
    chapters: list[Chapter] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()

    @property
    def has_cover(self) -> bool:
        return self.cover_stream_index is not None

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "duration": self.duration,
            "format_name": self.format_name,
            "codec": self.codec,
            "bit_rate": self.bit_rate,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "size_bytes": self.size_bytes,
            "chapter_count": self.chapter_count,
            "has_cover": self.has_cover,
            "tags": self.tags,
        }


def ffmpeg_path() -> str:
    path = find_binary("ffmpeg")
    if not path:
        raise FFmpegError(
            "ffmpeg was not found on PATH. Install it, or set PRUDIFY_FFMPEG "
            "to the full path of the binary."
        )
    return path


def ffprobe_path() -> str:
    path = find_binary("ffprobe")
    if not path:
        raise FFmpegError(
            "ffprobe was not found on PATH. Install it, or set PRUDIFY_FFPROBE "
            "to the full path of the binary."
        )
    return path


@lru_cache(maxsize=1)
def ffmpeg_major_version() -> int:
    """Major version of the ffmpeg binary, or 0 if it cannot be determined."""
    try:
        out = subprocess.run(
            [ffmpeg_path(), "-version"], capture_output=True, text=True, check=False
        )
    except Exception:  # noqa: BLE001 - treated the same as an unknown version
        return 0
    first = (out.stdout or "").splitlines()
    if not first:
        return 0
    # "ffmpeg version 6.1.1-3ubuntu5", "ffmpeg version n7.1", "ffmpeg version 9.0-full_build"
    match = re.search(r"ffmpeg version n?(\d+)", first[0])
    return int(match.group(1)) if match else 0


def filter_script_args(path: Path) -> list[str]:
    """Arguments for reading a filter graph from a file, across ffmpeg versions.

    Graphs go in a file because a book with a few hundred hits produces a
    filter string past the Windows 32 KB command-line limit.

    ``-filter_complex_script`` did that job for years, was deprecated in
    ffmpeg 7.0 in favour of the generic "read this option's value from a file"
    syntax, and is **gone in 9.0** -- where it fails with "Unrecognized option
    'filter_complex_script'" and no other explanation. The replacement,
    ``-/filter_complex``, does not exist before 7.0, so neither spelling works
    everywhere and the binary in front of us decides.

    Git snapshots print a date rather than a version; those are recent, so an
    unparseable version is assumed modern.
    """
    major = ffmpeg_major_version()
    if major == 0 or major >= 7:
        return ["-/filter_complex", str(path)]
    return ["-filter_complex_script", str(path)]


def ffmpeg_version() -> str:
    try:
        out = subprocess.run(
            [ffmpeg_path(), "-version"], capture_output=True, text=True, check=False
        )
        return out.stdout.splitlines()[0] if out.stdout else "unknown"
    except FFmpegError:
        return "not found"


def probe(path: Path | str) -> MediaInfo:
    """Read duration, stream layout, chapters and tags from a media file."""
    path = Path(path)
    cmd = [
        ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}", result.stderr, result.returncode)

    data = json.loads(result.stdout or "{}")
    fmt = data.get("format", {}) or {}
    info = MediaInfo(path=path)
    info.format_name = fmt.get("format_name", "")
    info.duration = _safe_float(fmt.get("duration"))
    info.size_bytes = int(_safe_float(fmt.get("size")))
    info.bit_rate = int(_safe_float(fmt.get("bit_rate")))
    info.tags = {k.lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}

    audio_found = False
    for stream in data.get("streams", []) or []:
        codec_type = stream.get("codec_type")
        if codec_type == "audio" and not audio_found:
            audio_found = True
            info.audio_stream_index = int(stream.get("index", 0))
            info.codec = stream.get("codec_name", "")
            info.sample_rate = int(_safe_float(stream.get("sample_rate")))
            info.channels = int(stream.get("channels") or 2)
            if not info.bit_rate:
                info.bit_rate = int(_safe_float(stream.get("bit_rate")))
            if not info.duration:
                info.duration = _safe_float(stream.get("duration"))
        elif codec_type == "video":
            disposition = stream.get("disposition", {}) or {}
            # Audiobook "video" streams are cover art, flagged attached_pic.
            if disposition.get("attached_pic") or stream.get("codec_name") in {
                "mjpeg",
                "png",
                "bmp",
            }:
                if info.cover_stream_index is None:
                    info.cover_stream_index = int(stream.get("index", 0))

    for idx, chapter in enumerate(data.get("chapters", []) or []):
        scale = _safe_float(chapter.get("time_base", "1/1000").split("/")[1] or 1000)
        start = _safe_float(chapter.get("start_time"))
        end = _safe_float(chapter.get("end_time"))
        if not start and chapter.get("start") is not None and scale:
            start = _safe_float(chapter.get("start")) / scale
        if not end and chapter.get("end") is not None and scale:
            end = _safe_float(chapter.get("end")) / scale
        title = (chapter.get("tags") or {}).get("title", "") or f"Chapter {idx + 1}"
        info.chapters.append(Chapter(index=idx, start=start, end=end, title=title))

    return info


def _safe_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def extract_pcm(
    source: Path,
    destination: Path,
    sample_rate: int = 16000,
    start: float | None = None,
    duration: float | None = None,
    progress: Callable[[float], None] | None = None,
    total_duration: float = 0.0,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Decode to 16-bit mono PCM WAV, the format every Whisper backend wants."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-vn",
        "-map", "0:a:0",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        str(destination),
    ]
    run_ffmpeg(
        cmd,
        progress=progress,
        total_duration=total_duration or duration or 0.0,
        cancel=cancel,
    )
    return destination


# --------------------------------------------------------------------------
# Filter graph construction
# --------------------------------------------------------------------------


def _between_expr(intervals: Sequence[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in intervals)


def build_filter_graph(
    intervals: Sequence[tuple[float, float]],
    mode: str = "mute",
    beep_frequency: int = 1000,
    beep_volume: float = 0.2,
    sample_rate: int = 44100,
    channels: int = 2,
    duration: float = 0.0,
) -> str:
    """Return the ``filter_complex`` graph for the requested edit mode.

    ``mute`` chains batched ``volume=0:enable=...`` filters -- chaining is safe
    because successive mutes union together. ``beep`` needs the *inverse* gate
    on the tone, which cannot be chained (chained inverses intersect), so its
    expression is emitted as one term; that is exactly why the graph is written
    to a script file rather than the command line.

    The tone is generated by an in-graph ``sine`` source rather than a second
    ``-i`` input, which keeps the command to a single file input.
    """
    if not intervals:
        return "[0:a]anull[aout]"

    if mode == "beep":
        expr = _between_expr(intervals)
        layout = "mono" if channels == 1 else "stereo"
        length = f":duration={duration + 1:.3f}" if duration else ""
        # normalize=0 keeps amix from halving both inputs. The two streams are
        # gated to be mutually exclusive -- narration is muted exactly where the
        # tone plays -- so a straight sum cannot clip, and `beep_volume` means
        # what it says: a fraction of full scale.
        return (
            f"sine=frequency={beep_frequency}:sample_rate={sample_rate}{length},"
            f"aformat=sample_rates={sample_rate}:channel_layouts={layout},"
            f"volume={beep_volume * _SINE_NORMALISE:.4f}[raw];"
            f"[0:a]volume=0:enable='{expr}'[voice];"
            f"[raw]volume=0:enable='not({expr})'[tone];"
            f"[voice][tone]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )

    # mute (and the audio half of "cut", which trims separately)
    chain: list[str] = []
    for i in range(0, len(intervals), _INTERVALS_PER_FILTER):
        batch = intervals[i : i + _INTERVALS_PER_FILTER]
        chain.append(f"volume=0:enable='{_between_expr(batch)}'")
    return "[0:a]" + ",".join(chain) + "[aout]"


def build_cut_graph(intervals: Sequence[tuple[float, float]]) -> str:
    """Filter graph that physically removes the matched intervals.

    Uses ``aselect`` with the inverse gate plus ``asetpts`` to close the gaps.
    This changes the running time, so chapters have to be recomputed -- see
    :func:`shift_chapters`.
    """
    if not intervals:
        return "[0:a]anull[aout]"
    return f"[0:a]aselect='not({_between_expr(intervals)})',asetpts=N/SR/TB[aout]"


def shift_chapters(
    chapters: Iterable[Chapter], intervals: Sequence[tuple[float, float]]
) -> list[Chapter]:
    """Recompute chapter boundaries after cut-mode removals."""
    cuts = sorted(intervals)

    def removed_before(t: float) -> float:
        total = 0.0
        for start, end in cuts:
            if end <= t:
                total += end - start
            elif start < t < end:
                total += t - start
            else:
                break
        return total

    shifted = []
    for chapter in chapters:
        new_start = max(0.0, chapter.start - removed_before(chapter.start))
        new_end = max(new_start, chapter.end - removed_before(chapter.end))
        shifted.append(
            Chapter(index=chapter.index, start=new_start, end=new_end, title=chapter.title)
        )
    return shifted


def write_ffmetadata(chapters: Sequence[Chapter], tags: dict[str, str], path: Path) -> Path:
    """Serialise chapters + tags into ffmpeg's ffmetadata format."""
    lines = [";FFMETADATA1"]
    for key, value in tags.items():
        if value is None:
            continue
        lines.append(f"{key}={_escape_metadata(str(value))}")
    for chapter in chapters:
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(chapter.start * 1000))}",
            f"END={int(round(chapter.end * 1000))}",
            f"title={_escape_metadata(chapter.title)}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _escape_metadata(value: str) -> str:
    """Escape a value for ffmetadata.

    The backslash must go first. Escaping it last would also escape the
    backslashes introduced by the preceding replacements, so a chapter titled
    "Chapter; One" came out as "Chapter\\\\; One" -- ffmpeg then reads the
    semicolon as a comment marker and the title is truncated. Titles
    containing "=" were worse: the key/value split moved and the tag was
    written to the wrong field.
    """
    value = value.replace("\\", "\\\\")
    for char in ("=", ";", "#", "\n"):
        value = value.replace(char, "\\" + char)
    return value


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

_CODEC_FOR_CONTAINER = {
    ".m4b": "aac",
    ".m4a": "aac",
    ".mp4": "aac",
    ".mp3": "libmp3lame",
    ".ogg": "libvorbis",
    ".opus": "libopus",
    ".oga": "libopus",
    ".flac": "flac",
    ".wav": "pcm_s16le",
}

# ffmpeg needs to be told which muxer to use for .m4b, which it does not know.
_FORMAT_FOR_CONTAINER = {".m4b": "mp4", ".m4a": "ipod", ".mp4": "mp4"}


def choose_codec(container: str, source_codec: str, configured: str = "auto") -> str:
    if configured and configured != "auto":
        return configured
    return _CODEC_FOR_CONTAINER.get(container.lower(), "aac")


def choose_bitrate(info: MediaInfo, configured: str = "auto") -> str:
    if configured and configured != "auto":
        return configured
    if info.bit_rate and info.bit_rate > 0:
        kbps = max(32, min(320, round(info.bit_rate / 1000)))
        return f"{kbps}k"
    # Typical spoken-word default; generous for mono narration.
    return "64k" if info.channels == 1 else "128k"


def render(
    source: Path,
    destination: Path,
    info: MediaInfo,
    intervals: Sequence[tuple[float, float]],
    mode: str = "mute",
    beep_frequency: int = 1000,
    beep_volume: float = 0.2,
    codec: str = "auto",
    bitrate: str = "auto",
    sample_rate: int = 0,
    preserve_chapters: bool = True,
    preserve_cover: bool = True,
    preserve_metadata: bool = True,
    extra_tags: dict[str, str] | None = None,
    work_dir: Path | None = None,
    progress: Callable[[float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Produce the cleaned file in a single ffmpeg pass."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir or Path(tempfile.mkdtemp(prefix="prudify-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    container = destination.suffix.lower()
    out_rate = sample_rate or info.sample_rate or 44100
    chosen_codec = choose_codec(container, info.codec, codec)
    chosen_bitrate = choose_bitrate(info, bitrate)

    if mode == "cut":
        graph = build_cut_graph(intervals)
    else:
        graph = build_filter_graph(
            intervals,
            mode=mode,
            beep_frequency=beep_frequency,
            beep_volume=beep_volume,
            sample_rate=out_rate,
            channels=info.channels,
            duration=info.duration,
        )

    graph_file = work_dir / "filter_graph.txt"
    graph_file.write_text(graph, encoding="utf-8")

    copy_cover = preserve_cover and info.has_cover

    # ffmpeg quirk: an `amix` graph combined with a stream-copied attached_pic
    # truncates the output to the length of the still image. Beep mode is the
    # only mode that uses amix, so it renders audio first and attaches the
    # cover in a second, stream-copy-only pass.
    two_pass = mode == "beep" and copy_cover

    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-i", str(source)]

    metadata_file: Path | None = None
    if mode == "cut" and preserve_chapters and info.chapters:
        metadata_file = write_ffmetadata(
            shift_chapters(info.chapters, intervals),
            {},
            work_dir / "chapters.ffmeta",
        )
        cmd += ["-i", str(metadata_file)]

    cmd += [*filter_script_args(graph_file), "-map", "[aout]"]

    if copy_cover and not two_pass:
        cmd += [
            "-map", f"0:{info.cover_stream_index}",
            "-c:v", "copy",
            "-disposition:v:0", "attached_pic",
        ]
    else:
        cmd += ["-vn"]

    cmd += ["-c:a", chosen_codec]
    if chosen_codec not in {"flac", "pcm_s16le"}:
        cmd += ["-b:a", chosen_bitrate]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]

    if not two_pass:
        if preserve_metadata:
            cmd += ["-map_metadata", "0"]
        if preserve_chapters:
            cmd += ["-map_chapters", "1" if metadata_file is not None else "0"]
        else:
            cmd += ["-map_chapters", "-1"]
        for key, value in (extra_tags or {}).items():
            cmd += ["-metadata", f"{key}={value}"]

    audio_container = container if not two_pass else ".m4a"
    if audio_container in _FORMAT_FOR_CONTAINER:
        cmd += ["-f", _FORMAT_FOR_CONTAINER[audio_container]]
        if not two_pass:
            cmd += ["-movflags", "+faststart"]

    stage_out = work_dir / f"render{audio_container or '.m4b'}"
    cmd += [str(stage_out)]

    run_ffmpeg(
        cmd,
        progress=progress,
        total_duration=info.duration,
        cancel=cancel,
        expected_bytes=_expected_output_bytes(info.duration, chosen_bitrate),
    )

    if two_pass:
        stage_out = _attach_cover(
            audio_path=stage_out,
            source=source,
            info=info,
            container=container,
            work_dir=work_dir,
            preserve_metadata=preserve_metadata,
            preserve_chapters=preserve_chapters,
            extra_tags=extra_tags,
            cancel=cancel,
        )

    if cancel and cancel():
        raise OperationCancelled("Cancelled")

    # Nearly always a cross-device move: the scratch volume is local and the
    # clean library is a network share, so this is a full copy of a multi-
    # gigabyte file with no progress of any kind. Say so, or the last minutes
    # of a job look like a hang.
    try:
        staged_bytes = stage_out.stat().st_size
    except OSError:
        staged_bytes = 0
    log.info("Publishing %.2f GB to %s", staged_bytes / (1024**3), destination.parent)
    shutil.move(str(stage_out), str(destination))
    return destination


def _attach_cover(
    audio_path: Path,
    source: Path,
    info: MediaInfo,
    container: str,
    work_dir: Path,
    preserve_metadata: bool,
    preserve_chapters: bool,
    extra_tags: dict[str, str] | None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Remux rendered audio with the original's cover, chapters and tags.

    Pure stream copy -- the audio is not touched a second time.
    """
    output = work_dir / f"final{container or '.m4b'}"
    cmd = [
        ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
        "-i", str(audio_path),
        "-i", str(source),
        "-map", "0:a:0",
        "-map", f"1:{info.cover_stream_index}",
        "-c:a", "copy",
        "-c:v", "copy",
        "-disposition:v:0", "attached_pic",
    ]
    if preserve_metadata:
        cmd += ["-map_metadata", "1"]
    cmd += ["-map_chapters", "1" if preserve_chapters else "-1"]
    for key, value in (extra_tags or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    if container in _FORMAT_FOR_CONTAINER:
        cmd += ["-f", _FORMAT_FOR_CONTAINER[container], "-movflags", "+faststart"]
    cmd += [str(output)]

    run_ffmpeg(cmd, cancel=cancel)
    audio_path.unlink(missing_ok=True)
    return output


def extract_cover(source: Path, destination: Path, max_edge: int = 400) -> bool:
    """Write the embedded cover art of ``source`` to ``destination`` as JPEG.

    Returns False when the file has no cover, which is not an error -- plenty
    of libraries have none. Scaled down on the way out: the artwork inside an
    M4B is routinely 2400x2400, and a library page showing sixty of those at
    full size would move a hundred megabytes to draw thumbnails.
    """
    # Best effort throughout. A thumbnail is decoration: a truncated download,
    # an exotic container or a file that is not really audio must leave the
    # page looking plain, never raise into a request handler.
    try:
        info = probe(source)
    except Exception:  # noqa: BLE001
        log.debug("Could not probe %s for cover art", source, exc_info=True)
        return False
    if not info.has_cover:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    scale = f"scale='min({max_edge},iw)':-1"
    try:
        run_ffmpeg(
            [
                ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                "-i", str(source),
                "-map", f"0:{info.cover_stream_index}",
                "-frames:v", "1",
                "-vf", scale,
                "-f", "image2", str(destination),
            ]
        )
    except Exception:  # noqa: BLE001
        log.debug("Could not extract cover art from %s", source, exc_info=True)
        return False
    return destination.exists() and destination.stat().st_size > 0


def _expected_output_bytes(duration: float, bitrate: str) -> int:
    """Roughly how large the encoded audio will be, for fallback progress.

    Only ever used to draw a progress bar, so being a few percent out is of no
    consequence -- the bar reaching 100% slightly early beats a bar that never
    moves.
    """
    if duration <= 0 or not bitrate:
        return 0
    text = bitrate.strip().lower().rstrip("bps")
    multiplier = 1000 if text.endswith("k") else 1_000_000 if text.endswith("m") else 1
    try:
        bits_per_second = float(text.rstrip("km")) * multiplier
    except ValueError:
        return 0
    return int(duration * bits_per_second / 8)


def run_ffmpeg(
    cmd: Sequence[str],
    progress: Callable[[float], None] | None = None,
    total_duration: float = 0.0,
    cancel: Callable[[], bool] | None = None,
    expected_bytes: int = 0,
) -> None:
    """Run ffmpeg, translating ``-progress`` output into a 0..1 fraction.

    ``out_time`` is the natural source of that fraction, but ffmpeg does not
    always have one to give: with an attached cover picture in the output it
    can report ``out_time=N/A`` for the entire run, which is how an encode
    ends up showing no progress at all from start to finish. ``total_size`` is
    always reported, so when a size is known up front it serves as a fallback.
    Audio at a fixed bitrate makes bytes an honest proxy for time.
    """
    full_cmd = list(cmd)
    if progress and total_duration > 0:
        # Insert progress flags right after the binary.
        full_cmd = [full_cmd[0], "-progress", "pipe:1", "-nostats", *full_cmd[1:]]

    log.debug("ffmpeg: %s", " ".join(full_cmd))
    creationflags = 0
    if os.name == "nt":  # keep console windows from flashing on Windows
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    process = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )

    # ffmpeg writes progress to stdout and its log to stderr, and it blocks
    # when either pipe's buffer fills. Reading only stdout therefore deadlocks:
    # ffmpeg stops writing progress because it is stuck writing stderr, and we
    # wait forever for progress that will never come. It takes a chatty input
    # to trigger -- an M4B carrying a stale chapter stream emits a warning per
    # packet -- and Windows pipe buffers are far smaller than Linux's, so it
    # strikes there first. Drain stderr on its own thread.
    stderr_tail: deque[str] = deque(maxlen=200)

    def _drain_stderr() -> None:
        if process.stderr is None:
            return
        try:
            for line in process.stderr:
                stderr_tail.append(line)
        except Exception:  # noqa: BLE001 - the pipe closing is not an error
            pass

    drainer = threading.Thread(target=_drain_stderr, name="ffmpeg-stderr", daemon=True)
    drainer.start()

    stdout_error: list[BaseException] = []
    saw_time: list[bool] = []
    warned_no_time: list[bool] = []

    def _drain_stdout() -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not (progress and total_duration > 0):
                    continue
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    raw = line.split("=", 1)[1]
                    if raw.isdigit():
                        seconds = int(raw) / 1_000_000
                        saw_time.append(True)
                        progress(min(1.0, seconds / total_duration))
                elif line.startswith("total_size=") and expected_bytes > 0:
                    raw = line.split("=", 1)[1]
                    # Only once ffmpeg has proved it will not give us a
                    # timestamp: a real out_time is always the better number.
                    if raw.isdigit() and not saw_time:
                        if not warned_no_time:
                            log.warning(
                                "ffmpeg is reporting out_time=N/A; estimating "
                                "progress from output size instead"
                            )
                            warned_no_time.append(True)
                        progress(min(1.0, int(raw) / expected_bytes))
        except BaseException as exc:  # noqa: BLE001 - surfaced after the process exits
            stdout_error.append(exc)

    stdout_drainer = threading.Thread(
        target=_drain_stdout, name="ffmpeg-stdout", daemon=True
    )
    stdout_drainer.start()

    cancelled = False
    try:
        while process.poll() is None:
            if cancel and cancel():
                cancelled = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise OperationCancelled("Cancelled")
            time.sleep(0.2)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        stdout_drainer.join(timeout=5)
        drainer.join(timeout=5)

    if stdout_error:
        raise stdout_error[0]
    if cancelled:
        raise OperationCancelled("Cancelled")

    if process.returncode != 0:
        # ffmpeg dying because the whole process group was signalled is not a
        # render failure, and reporting it as one is actively harmful: the
        # book is marked FAILED and its nearly-finished render thrown away,
        # when the truth is that the container was being stopped. `tini -g`
        # forwards SIGTERM to every child, so ffmpeg is hit directly and dies
        # before the poll loop above notices the shutdown. Ask again, now.
        if cancel and cancel():
            raise OperationCancelled("Cancelled")

        tail = "\n".join("".join(stderr_tail).strip().splitlines()[-25:])
        # Put the tail in the message too. It was previously only on the
        # exception's .stderr attribute, which nothing printed -- so a failed
        # render surfaced as a bare exit code and nothing to diagnose it with.
        # A negative code is POSIX for "killed by signal N"; ffmpeg also
        # exits 255 when it handles SIGTERM itself and says so on the way out.
        # Both mean something outside this process made the decision, and
        # saying which signal turns a mystery into a one-line diagnosis:
        # 15 is a stop or redeploy, 9 is usually the OOM killer.
        signalled = -process.returncode if process.returncode < 0 else None
        if signalled is None and "received signal" in "".join(stderr_tail):
            signalled = 15
        if signalled is not None:
            summary = (
                f"ffmpeg was terminated by signal {signalled} -- it did not "
                f"fail on its own. The usual causes are the container being "
                f"stopped or restarted, or the kernel running out of memory."
            )
        else:
            summary = f"ffmpeg exited with code {process.returncode}"
            if not tail:
                # Only meaningful when nothing chose to end the process. Said
                # of a signalled run it contradicts the line above it.
                summary = (
                    f"{summary} (no output captured -- the process most likely "
                    f"crashed rather than exiting with an error)\n"
                    f"command: {' '.join(full_cmd)}"
                )
        if tail:
            summary = f"{summary}\n\nffmpeg said:\n{tail}"
        raise FFmpegError(summary, tail, process.returncode)


def validate_output(
    source_info: MediaInfo,
    output_path: Path,
    duration_tolerance: float = 1.0,
    expect_chapters: bool = True,
    expect_cover: bool = True,
    mode: str = "mute",
    removed_seconds: float = 0.0,
) -> tuple[bool, list[str]]:
    """Compare the rendered file against the source. Returns (ok, problems)."""
    problems: list[str] = []
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, ["Output file is missing or empty"]

    out = probe(output_path)

    expected_duration = source_info.duration
    if mode == "cut":
        expected_duration -= removed_seconds
    drift = abs(out.duration - expected_duration)
    if drift > duration_tolerance:
        problems.append(
            f"Duration drifted {drift:.2f}s "
            f"(expected {expected_duration:.2f}s, got {out.duration:.2f}s)"
        )

    if expect_chapters and source_info.chapter_count:
        if out.chapter_count != source_info.chapter_count:
            problems.append(
                f"Chapter count changed: {source_info.chapter_count} -> {out.chapter_count}"
            )

    if expect_cover and source_info.has_cover and not out.has_cover:
        problems.append("Embedded cover art was lost")

    return (not problems), problems
