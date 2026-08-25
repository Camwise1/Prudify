"""Speech-to-text with word-level timestamps.

Prudify only ever needs one thing from a transcriber: a flat list of words,
each with a start time, an end time and a confidence. Everything else -- model
loading, chunking, resume -- is arranged around producing that list reliably on
machines with modest memory.

Backends are pluggable. ``faster-whisper`` is the default because CTranslate2
decodes in bounded memory regardless of book length, which is what makes
whole-file transcription safe on a 32 GB box that previously OOMed trying to
allocate 7.8 GB in one tensor.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import TranscriptionSettings
from . import audio as audio_mod
from .cancel import OperationCancelled

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

# Whole-file faster-whisper has one unavoidable blind spot: its initial audio
# preparation/VAD step cannot observe Prudify's cancel flag. Long books use
# automatic chunks so cancel, restart and memory pressure have bounded windows.
_AUTO_CHUNK_ABOVE_SECONDS = 60 * 60
_AUTO_CHUNK_MINUTES = 30


@dataclass(slots=True)
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Word:
        return Word(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            text=str(data.get("text", data.get("word", ""))),
            probability=float(data.get("probability", 1.0)),
        )


@dataclass(slots=True)
class Transcript:
    words: list[Word] = field(default_factory=list)
    language: str = ""
    duration: float = 0.0
    engine: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": self.duration,
            "engine": self.engine,
            "model": self.model,
            "words": [w.to_dict() for w in self.words],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Transcript:
        return Transcript(
            words=[Word.from_dict(w) for w in data.get("words", [])],
            language=data.get("language", ""),
            duration=float(data.get("duration", 0.0)),
            engine=data.get("engine", ""),
            model=data.get("model", ""),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        return path

    @staticmethod
    def load(path: Path) -> Transcript:
        return Transcript.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def plain_text(self) -> str:
        return " ".join(w.text.strip() for w in self.words if w.text.strip())


class TranscriptionError(RuntimeError):
    pass


class Transcriber:
    """Base class for speech-to-text backends."""

    name = "base"

    def __init__(self, settings: TranscriptionSettings) -> None:
        self.settings = settings

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Transcript:
        raise NotImplementedError

    def unload(self) -> None:
        """Release model memory. Called between queue items."""


# --------------------------------------------------------------------------
# faster-whisper (default)
# --------------------------------------------------------------------------


class FasterWhisperTranscriber(Transcriber):
    name = "faster-whisper"

    _model = None
    _model_key: tuple | None = None

    def _resolve_device(self) -> tuple[str, str]:
        device = self.settings.device
        compute = self.settings.compute_type

        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def _load(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise TranscriptionError(
                "faster-whisper is not installed. Install it with "
                "`pip install 'prudify[whisper]'` or switch the transcription "
                "engine in Settings."
            ) from exc

        device, compute = self._resolve_device()
        key = (
            self.settings.model,
            device,
            compute,
            self.settings.cpu_threads,
            self.settings.num_workers,
            self.settings.model_dir,
        )
        if FasterWhisperTranscriber._model is not None and (
            FasterWhisperTranscriber._model_key == key
        ):
            return FasterWhisperTranscriber._model

        self.unload()
        log.info(
            "Loading faster-whisper model=%s device=%s compute=%s threads=%s",
            self.settings.model, device, compute, self.settings.cpu_threads,
        )
        model = WhisperModel(
            self.settings.model,
            device=device,
            compute_type=compute,
            cpu_threads=self.settings.cpu_threads,
            num_workers=self.settings.num_workers,
            download_root=self.settings.model_dir or None,
        )
        FasterWhisperTranscriber._model = model
        FasterWhisperTranscriber._model_key = key
        return model

    def unload(self) -> None:
        if FasterWhisperTranscriber._model is not None:
            FasterWhisperTranscriber._model = None
            FasterWhisperTranscriber._model_key = None
            import gc

            gc.collect()

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Transcript:
        if cancel and cancel():
            raise OperationCancelled("Cancelled")
        model = self._load()
        info_probe = audio_mod.probe(wav_path)
        total = info_probe.duration or 0.0

        segments, info = model.transcribe(
            str(wav_path),
            language=self.settings.language or None,
            beam_size=self.settings.beam_size,
            word_timestamps=True,
            vad_filter=self.settings.vad_filter,
            initial_prompt=self.settings.initial_prompt or None,
            condition_on_previous_text=False,
        )

        words: list[Word] = []
        for segment in segments:
            if cancel and cancel():
                raise OperationCancelled("Cancelled")
            for word in getattr(segment, "words", None) or []:
                words.append(
                    Word(
                        start=float(word.start),
                        end=float(word.end),
                        text=word.word,
                        probability=float(getattr(word, "probability", 1.0) or 1.0),
                    )
                )
            if progress and total:
                progress(min(1.0, float(segment.end) / total), "transcribing")

        return Transcript(
            words=words,
            language=getattr(info, "language", self.settings.language),
            duration=total or getattr(info, "duration", 0.0),
            engine=self.name,
            model=self.settings.model,
        )


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# whisper.cpp
# --------------------------------------------------------------------------


class WhisperCppTranscriber(Transcriber):
    name = "whisper-cpp"

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Transcript:
        if cancel and cancel():
            raise OperationCancelled("Cancelled")
        binary = self.settings.whisper_cpp_binary or shutil.which("whisper-cli") or shutil.which(
            "main"
        )
        if not binary or not Path(binary).exists():
            raise TranscriptionError(
                "whisper.cpp binary not found. Set it in Settings -> Transcription."
            )

        model_path = Path(self.settings.model_dir or ".") / self.settings.model
        if not model_path.exists():
            model_path = Path(self.settings.model)
        if not model_path.exists():
            raise TranscriptionError(f"whisper.cpp model not found: {model_path}")

        with tempfile.TemporaryDirectory(prefix="prudify-wcpp-") as tmp:
            out_prefix = Path(tmp) / "out"
            cmd = [
                str(binary),
                "-m", str(model_path),
                "-f", str(wav_path),
                "-t", str(self.settings.cpu_threads),
                "-l", self.settings.language or "auto",
                "-ml", "1",           # one word per segment -> word timestamps
                "-oj",                # JSON output
                "-of", str(out_prefix),
                "-np",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise TranscriptionError(
                    f"whisper.cpp failed ({result.returncode}): {result.stderr[-2000:]}"
                )
            if cancel and cancel():
                raise OperationCancelled("Cancelled")

            json_path = out_prefix.with_suffix(".json")
            if not json_path.exists():
                raise TranscriptionError("whisper.cpp produced no JSON output")
            data = json.loads(json_path.read_text(encoding="utf-8"))

        words: list[Word] = []
        for item in data.get("transcription", []):
            offsets = item.get("offsets", {})
            text = (item.get("text") or "").strip()
            if not text:
                continue
            words.append(
                Word(
                    start=float(offsets.get("from", 0)) / 1000.0,
                    end=float(offsets.get("to", 0)) / 1000.0,
                    text=text,
                )
            )
        if progress:
            progress(1.0, "transcribing")

        return Transcript(
            words=words,
            language=self.settings.language,
            duration=audio_mod.probe(wav_path).duration,
            engine=self.name,
            model=str(self.settings.model),
        )


# --------------------------------------------------------------------------
# openai-whisper (reference implementation; heaviest on memory)
# --------------------------------------------------------------------------


class OpenAIWhisperTranscriber(Transcriber):
    name = "openai-whisper"

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Transcript:
        if cancel and cancel():
            raise OperationCancelled("Cancelled")
        try:
            import whisper  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise TranscriptionError("openai-whisper is not installed.") from exc

        try:
            import torch

            torch.set_num_threads(self.settings.cpu_threads)
        except Exception:
            pass

        model = whisper.load_model(
            self.settings.model, download_root=self.settings.model_dir or None
        )
        result = model.transcribe(
            str(wav_path),
            language=self.settings.language or None,
            word_timestamps=True,
            verbose=False,
        )
        if cancel and cancel():
            raise OperationCancelled("Cancelled")
        words: list[Word] = []
        for segment in result.get("segments", []):
            for word in segment.get("words", []):
                words.append(
                    Word(
                        start=float(word["start"]),
                        end=float(word["end"]),
                        text=str(word["word"]),
                        probability=float(word.get("probability", 1.0)),
                    )
                )
        if progress:
            progress(1.0, "transcribing")
        return Transcript(
            words=words,
            language=result.get("language", ""),
            duration=audio_mod.probe(wav_path).duration,
            engine=self.name,
            model=self.settings.model,
        )


ENGINES: dict[str, type[Transcriber]] = {
    "faster-whisper": FasterWhisperTranscriber,
    "whisper-cpp": WhisperCppTranscriber,
    "openai-whisper": OpenAIWhisperTranscriber,
}


def get_transcriber(settings: TranscriptionSettings) -> Transcriber:
    engine = ENGINES.get(settings.engine)
    if engine is None:
        raise TranscriptionError(f"Unknown transcription engine: {settings.engine}")
    return engine(settings)


# --------------------------------------------------------------------------
# Chunked transcription (for very constrained machines)
# --------------------------------------------------------------------------


def _usable_wav(path: Path, expected_duration: float) -> bool:
    """Is a cached WAV complete enough to reuse instead of re-extracting?

    Extraction output is cached so an interrupted run resumes rather than
    starting over. Testing only ``exists()`` was a trap: a run killed partway
    through extraction leaves a truncated -- often zero-byte -- file behind,
    extraction is then skipped forever, and every later attempt dies in
    ffprobe with an error that says nothing about the real cause. Verify the
    file actually decodes, and that it covers the expected running time.
    """
    if not path.exists() or path.stat().st_size < 1024:
        return False
    try:
        found = audio_mod.probe(path).duration
    except Exception:  # noqa: BLE001 - anything unreadable means re-extract
        return False
    if found <= 0:
        return False
    # Allow a little slack: container rounding, not a truncated file.
    return expected_duration <= 0 or found >= expected_duration * 0.98


def transcribe_file(
    source: Path,
    settings: TranscriptionSettings,
    work_dir: Path,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Transcript:
    """Transcribe a media file, chunking only if the user asked for it.

    Chunk results are cached on disk so an interrupted run resumes where it
    stopped instead of starting the book over.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    transcriber = get_transcriber(settings)
    info = audio_mod.probe(source)

    chunk_minutes = settings.chunk_minutes
    if chunk_minutes <= 0 and info.duration > _AUTO_CHUNK_ABOVE_SECONDS:
        chunk_minutes = _AUTO_CHUNK_MINUTES
        log.info(
            "Transcription duration %.1f hours; using automatic %s minute chunks",
            info.duration / 3600,
            chunk_minutes,
        )

    if chunk_minutes <= 0:
        wav = work_dir / "audio.wav"
        if not _usable_wav(wav, info.duration):
            wav.unlink(missing_ok=True)
            audio_mod.extract_pcm(
                source,
                wav,
                progress=(lambda f: progress(f * 0.15, "extracting")) if progress else None,
                total_duration=info.duration,
                cancel=cancel,
            )
        if cancel and cancel():
            raise OperationCancelled("Cancelled")
        transcript = transcriber.transcribe(
            wav,
            progress=(lambda f, s: progress(0.15 + f * 0.85, s)) if progress else None,
            cancel=cancel,
        )
        transcript.duration = info.duration
        if not os.environ.get("PRUDIFY_KEEP_WAV"):
            wav.unlink(missing_ok=True)
        return transcript

    chunk_seconds = chunk_minutes * 60
    overlap = settings.chunk_overlap_seconds
    total = info.duration
    chunk_count = max(1, int(total // chunk_seconds) + (1 if total % chunk_seconds else 0))

    all_words: list[Word] = []
    for index in range(chunk_count):
        if cancel and cancel():
            raise TranscriptionError("Cancelled")

        core_start = index * chunk_seconds
        core_end = min(total, core_start + chunk_seconds)
        read_start = max(0.0, core_start - overlap)
        read_end = min(total, core_end + overlap)

        cache = work_dir / f"chunk-{index:04d}.json"
        if cache.exists():
            chunk_words = [Word.from_dict(w) for w in json.loads(cache.read_text())]
        else:
            wav = work_dir / f"chunk-{index:04d}.wav"
            audio_mod.extract_pcm(
                source,
                wav,
                start=read_start,
                duration=read_end - read_start,
                cancel=cancel,
            )
            chunk = transcriber.transcribe(wav, cancel=cancel)
            wav.unlink(missing_ok=True)
            chunk_words = [
                Word(
                    start=w.start + read_start,
                    end=w.end + read_start,
                    text=w.text,
                    probability=w.probability,
                )
                for w in chunk.words
            ]
            cache.write_text(json.dumps([w.to_dict() for w in chunk_words]))

        # Drop words that fell inside the overlap padding; the neighbouring
        # chunk owns them. Words are kept if their midpoint is in the core.
        for word in chunk_words:
            midpoint = (word.start + word.end) / 2
            if core_start <= midpoint < core_end or (
                index == chunk_count - 1 and midpoint >= core_start
            ):
                all_words.append(word)

        if progress:
            progress((index + 1) / chunk_count, f"transcribing chunk {index + 1}/{chunk_count}")

    all_words.sort(key=lambda w: w.start)
    return Transcript(
        words=all_words,
        language=settings.language,
        duration=total,
        engine=transcriber.name,
        model=settings.model,
    )
