"""Transcription orchestration without downloading a Whisper model."""

from __future__ import annotations

from pathlib import Path

from prudify.config import TranscriptionSettings
from prudify.core import transcribe as transcribe_mod
from prudify.core.audio import MediaInfo
from prudify.core.transcribe import Transcript, Word


def test_default_auto_chunks_long_files(monkeypatch, tmp_path):
    calls: list[str] = []

    class FakeTranscriber:
        name = "fake"

        def transcribe(self, wav_path, progress=None, cancel=None):
            calls.append(Path(wav_path).name)
            return Transcript(words=[Word(0.0, 1.0, "word")], engine=self.name)

    def fake_extract(_source, destination, **_kwargs):
        Path(destination).write_bytes(b"wav")

    monkeypatch.setattr(transcribe_mod, "get_transcriber", lambda _settings: FakeTranscriber())
    monkeypatch.setattr(
        transcribe_mod.audio_mod,
        "probe",
        lambda path: MediaInfo(path=Path(path), duration=2 * 60 * 60),
    )
    monkeypatch.setattr(transcribe_mod.audio_mod, "extract_pcm", fake_extract)

    transcript = transcribe_mod.transcribe_file(
        Path("book.m4b"),
        TranscriptionSettings(chunk_minutes=0),
        tmp_path,
    )

    assert transcript.duration == 2 * 60 * 60
    assert calls == [
        "chunk-0000.wav",
        "chunk-0001.wav",
        "chunk-0002.wav",
        "chunk-0003.wav",
    ]


def test_mlx_model_names_map_to_mlx_community_repos():
    assert transcribe_mod.mlx_model_repo("base.en") == "mlx-community/whisper-base.en-mlx"
    assert (
        transcribe_mod.mlx_model_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    )
    assert transcribe_mod.mlx_model_repo("someone/custom-whisper") == "someone/custom-whisper"


def test_mlx_unknown_model_is_a_clear_error():
    import pytest

    with pytest.raises(transcribe_mod.TranscriptionError, match="No MLX build"):
        transcribe_mod.mlx_model_repo("large-v9")


def test_mlx_transcriber_returns_words_without_beam_search(monkeypatch, tmp_path):
    import sys
    import types

    calls: list[dict] = []

    def fake_transcribe(audio, **kwargs):
        calls.append({"audio": audio, **kwargs})
        return {
            "language": "en",
            "segments": [
                {"words": [{"start": 0.5, "end": 0.9, "word": " hello", "probability": 0.8}]},
                {"words": [{"start": 1.0, "end": 1.4, "word": " world"}]},
            ],
        }

    fake_module = types.SimpleNamespace(transcribe=fake_transcribe)
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_module)
    monkeypatch.setattr(
        transcribe_mod.audio_mod,
        "probe",
        lambda path: MediaInfo(path=Path(path), duration=2.0),
    )

    settings = TranscriptionSettings(engine="mlx-whisper", model="small.en")
    transcriber = transcribe_mod.get_transcriber(settings)
    transcript = transcriber.transcribe(tmp_path / "audio.wav")

    assert [w.text for w in transcript.words] == [" hello", " world"]
    assert transcript.words[0].probability == 0.8
    assert transcript.engine == "mlx-whisper"
    (call,) = calls
    assert call["path_or_hf_repo"] == "mlx-community/whisper-small.en-mlx"
    assert call["word_timestamps"] is True
    assert "beam_size" not in call
