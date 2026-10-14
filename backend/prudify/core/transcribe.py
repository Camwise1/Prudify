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

from ..config import TranscriptionSettings, available_cpus
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
        threads = self.settings.cpu_threads or available_cpus()
        key = (
            self.settings.model,
            device,
            compute,
            threads,
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
            self.settings.model, device, compute, threads,
        )
        model = WhisperModel(
            self.settings.model,
            device=device,
            compute_type=compute,
            cpu_threads=threads,
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
# mlx-whisper (Apple Silicon GPU)
# --------------------------------------------------------------------------

# CTranslate2 has no Metal backend, so on a Mac faster-whisper is CPU-only.
# MLX runs Whisper on the Apple GPU through unified memory instead. The
# mlx-community repos use their own names, so map Prudify's short model names
# onto them; anything containing a "/" is passed through as a repo or path.
MLX_MODELS: dict[str, str] = {
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
}

# Which decoder heads track word timing, per model -- the dumps OpenAI ships in
# whisper/__init__.py (MIT). mlx-whisper never sets them and falls back to
# every head in the last half of the decoder, which smears word boundaries.
# Prudify mutes on those boundaries, so loose timing means clipped syllables.
_MLX_ALIGNMENT_HEADS: dict[str, bytes] = {
    "tiny.en": b"ABzY8J1N>@0{>%R00Bk>$p{7v037`oCl~+#00",
    "tiny": b"ABzY8bu8Lr0{>%RKn9Fp%m@SkK7Kt=7ytkO",
    "base.en": b"ABzY8;40c<0{>%RzzG;p*o+Vo09|#PsxSZm00",
    "base": b"ABzY8KQ!870{>%RzyTQH3`Q^yNP!>##QT-<FaQ7m",
    "small.en": b"ABzY8>?_)10{>%RpeA61k&I|OI3I$65C{;;pbCHh0B{qLQ;+}v00",
    "small": b"ABzY8DmU6=0{>%Rpa?J`kvJ6qF(V^F86#Xh7JUGMK}P<N0000",
    "medium.en": b"ABzY8usPae0{>%R7<zz_OvQ{)4kMa0BMw6u5rT}kRKX;$NfYBv00*Hl@qhsU00",
    "medium": b"ABzY8B0Jh+0{>%R7}kK1fFL7w6%<-Pf*t^=N)Qr&0RR9",
    "large-v3": b"ABzY8gWO1E0{>%R7(9S+Kn!D~%ngiGaR?*L!iJG9p-nab0JQ=-{D1-g00",
    "large-v3-turbo": b"ABzY8j^C+e0{>%RARaKHP%t(lGR*)0g!tONPyhe`",
}

# mlx-whisper has no VAD. Its hallucination guard -- skip silence longer than
# this when the words around it look invented -- stands in for vad_filter.
_MLX_HALLUCINATION_SILENCE_SECONDS = 2.0


def mlx_model_repo(model: str) -> str:
    if "/" in model or Path(model).exists():
        return model
    try:
        return MLX_MODELS[model]
    except KeyError:
        raise TranscriptionError(
            f"No MLX build of Whisper model {model!r}. Choose one of "
            f"{', '.join(MLX_MODELS)}, or give a Hugging Face repo id."
        ) from None


class MlxWhisperTranscriber(Transcriber):
    name = "mlx-whisper"

    def _module(self):
        try:
            import mlx_whisper
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise TranscriptionError(
                "mlx-whisper is not installed. It runs only on Apple Silicon Macs; "
                "install it with `pip install 'prudify[mlx]'`."
            ) from exc
        return mlx_whisper

    def _prepare_model(self, repo: str, fp16: bool) -> None:
        """Load through mlx-whisper's own cache, then fix its alignment heads.

        ``mlx_whisper.transcribe`` fetches the model from ``ModelHolder``, so
        loading it there first and adjusting it in place is what the
        transcribe call then uses.
        """
        heads = _MLX_ALIGNMENT_HEADS.get(self.settings.model)
        if heads is None:
            return
        try:
            import mlx.core as mx
            from mlx_whisper.transcribe import ModelHolder
        except ImportError:  # pragma: no cover - only reachable on a Mac
            return
        model = ModelHolder.get_model(repo, mx.float16 if fp16 else mx.float32)
        model.set_alignment_heads(heads)

    def unload(self) -> None:
        try:
            from mlx_whisper.transcribe import ModelHolder
        except ImportError:
            return
        if ModelHolder.model is None:
            return
        ModelHolder.model = None
        ModelHolder.model_path = None
        import gc

        gc.collect()
        try:
            import mlx.core as mx

            mx.clear_cache()
        except Exception:  # noqa: BLE001 - older mlx spells it mx.metal.clear_cache
            pass

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Transcript:
        if cancel and cancel():
            raise OperationCancelled("Cancelled")
        mlx_whisper = self._module()
        repo = mlx_model_repo(self.settings.model)
        if self.settings.model_dir and not Path(repo).exists():
            # mlx-whisper downloads into the default Hugging Face cache; honour
            # the configured directory by fetching there and passing the path.
            from huggingface_hub import snapshot_download

            repo = snapshot_download(repo_id=repo, cache_dir=self.settings.model_dir)

        fp16 = self.settings.compute_type != "float32"
        self._prepare_model(repo, fp16)

        log.info("Transcribing with mlx-whisper model=%s", repo)
        # No beam_size: mlx-whisper implements greedy decoding only and raises
        # on a beam search request.
        result = mlx_whisper.transcribe(
            str(wav_path),
            path_or_hf_repo=repo,
            language=self.settings.language or None,
            word_timestamps=True,
            initial_prompt=self.settings.initial_prompt or None,
            condition_on_previous_text=False,
            hallucination_silence_threshold=(
                _MLX_HALLUCINATION_SILENCE_SECONDS if self.settings.vad_filter else None
            ),
            fp16=fp16,
            verbose=None,
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
            language=result.get("language", self.settings.language),
            duration=audio_mod.probe(wav_path).duration,
            engine=self.name,
            model=self.settings.model,
        )


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
                "-t", str(self.settings.cpu_threads or available_cpus()),
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

            torch.set_num_threads(self.settings.cpu_threads or available_cpus())
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
    "mlx-whisper": MlxWhisperTranscriber,
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
        # Announced as each chunk starts, so the label names the chunk being
        # worked on. Reported on completion it read "chunk 2/50" for the
        # whole of chunk 3, and the fraction is the same either way.
        if progress:
            progress(index / chunk_count, f"transcribing chunk {index + 1}/{chunk_count}")

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
        progress(1.0, f"transcribed {chunk_count} chunks")

    all_words.sort(key=lambda w: w.start)
    return Transcript(
        words=all_words,
        language=settings.language,
        duration=total,
        engine=transcriber.name,
        model=settings.model,
    )
