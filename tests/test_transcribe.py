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
