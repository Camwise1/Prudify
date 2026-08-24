"""End-to-end pipeline tests against real audio, with a seeded transcript.

Seeding the transcript cache lets these run in CI without downloading a
Whisper model, while still exercising every other stage for real: ffprobe,
matching, the filter graph, encoding, validation and publication.
"""

from __future__ import annotations

import pytest

from prudify.core import audio as audio_mod
from prudify.core.pipeline import clean_part, transcript_cache_path
from prudify.core.transcribe import Transcript, Word

from .conftest import needs_ffmpeg, peak_db

pytestmark = needs_ffmpeg

FILLER = "the quick brown fox jumped over a lazy dog and kept running through fields".split()


def seed_transcript(config, source, hits=((10.0, 10.4, "fucking"), (30.0, 30.6, "motherfucker!"))):
    """Build a plausible 60 second transcript with the given words planted."""
    words: list[Word] = []
    planted = {round(start, 3): (end, text) for start, end, text in hits}
    time = 0.0
    index = 0
    while time < 59:
        key = round(time, 3)
        if key in planted:
            end, text = planted[key]
            words.append(Word(start=time, end=end, text=text, probability=0.95))
            time = round(end + 0.1, 3)
            continue
        nxt = round(time + 0.5, 3)
        words.append(Word(start=time, end=nxt - 0.05, text=FILLER[index % len(FILLER)]))
        index += 1
        time = nxt

    transcript = Transcript(words=words, language="en", duration=60.0, engine="test", model="test")
    transcript.save(transcript_cache_path(config, source))
    return transcript


@pytest.fixture
def prepared(config, sample_m4b):
    seed_transcript(config, sample_m4b)
    return config, sample_m4b


class TestMuteMode:
    @pytest.fixture
    def result(self, prepared, tmp_path):
        config, source = prepared
        destination = tmp_path / "out" / "Test Book.m4b"
        return clean_part(source, destination, config, tmp_path / "work"), destination, source

    def test_succeeds(self, result):
        outcome, _, _ = result
        assert outcome.ok, outcome.reason
        assert outcome.problems == []

    def test_finds_both_instances(self, result):
        outcome, _, _ = result
        assert outcome.match_count == 2
        assert outcome.counts_by_word == {"fucking": 1, "motherfucker": 1}

    def test_duration_preserved(self, result):
        outcome, destination, source = result
        assert audio_mod.probe(destination).duration == pytest.approx(
            audio_mod.probe(source).duration, abs=1.0
        )

    def test_chapters_and_cover_preserved(self, result):
        _, destination, _ = result
        out = audio_mod.probe(destination)
        assert out.chapter_count == 3
        assert out.has_cover

    def test_metadata_preserved_and_tagged(self, result):
        _, destination, _ = result
        tags = audio_mod.probe(destination).tags
        assert tags.get("artist") == "Test Author"
        assert tags.get("comment", "").startswith("Cleaned by Prudify")

    def test_matched_regions_are_silent(self, result):
        _, destination, _ = result
        assert peak_db(destination, 10.05, 10.45) < -60
        assert peak_db(destination, 30.05, 30.65) < -60

    def test_surrounding_audio_untouched(self, result):
        _, destination, _ = result
        assert peak_db(destination, 5.0, 6.0) > -40
        assert peak_db(destination, 45.0, 46.0) > -40

    def test_second_run_skips(self, result, prepared, tmp_path):
        config, source = prepared
        _, destination, _ = result
        again = clean_part(source, destination, config, tmp_path / "work2")
        assert again.ok and again.skipped


@needs_ffmpeg
def test_beep_mode_keeps_length_and_cover(prepared, tmp_path):
    config, source = prepared
    config.output.mode = "beep"
    destination = tmp_path / "out" / "beep.m4b"
    outcome = clean_part(source, destination, config, tmp_path / "work")

    assert outcome.ok, outcome.reason
    out = audio_mod.probe(destination)
    assert out.duration == pytest.approx(60.0, abs=1.0)
    assert out.chapter_count == 3
    assert out.has_cover
    # The tone replaces the word rather than leaving a hole.
    assert peak_db(destination, 10.05, 10.45) > -45


@needs_ffmpeg
def test_cut_mode_shortens_and_rebuilds_chapters(prepared, tmp_path):
    config, source = prepared
    config.output.mode = "cut"
    destination = tmp_path / "out" / "cut.m4b"
    outcome = clean_part(source, destination, config, tmp_path / "work")

    assert outcome.ok, outcome.reason
    out = audio_mod.probe(destination)
    assert out.duration == pytest.approx(60.0 - outcome.muted_seconds, abs=1.0)
    assert out.chapter_count == 3


@needs_ffmpeg
def test_dry_run_writes_nothing(prepared, tmp_path):
    config, source = prepared
    config.processing.dry_run = True
    destination = tmp_path / "out" / "dry.m4b"
    outcome = clean_part(source, destination, config, tmp_path / "work")

    assert outcome.ok and outcome.skipped
    assert outcome.match_count == 2
    assert not destination.exists()


@needs_ffmpeg
def test_clean_book_is_copied_across(config, sample_m4b, tmp_path):
    """A book with nothing to silence still lands in the clean library."""
    seed_transcript(config, sample_m4b, hits=())
    destination = tmp_path / "out" / "clean.m4b"
    outcome = clean_part(sample_m4b, destination, config, tmp_path / "work")

    assert outcome.ok
    assert outcome.match_count == 0
    assert destination.exists()
    assert destination.stat().st_size == sample_m4b.stat().st_size


@needs_ffmpeg
def test_transcript_cache_is_reused(prepared, tmp_path):
    """Changing filter settings must not trigger a second transcription."""
    config, source = prepared
    cache = transcript_cache_path(config, source)
    assert cache.exists()
    before = cache.stat().st_mtime_ns

    config.filtering.wordlist = "moderate"
    clean_part(source, tmp_path / "out" / "a.m4b", config, tmp_path / "w1")
    assert cache.stat().st_mtime_ns == before


@needs_ffmpeg
def test_failed_validation_removes_broken_output(prepared, tmp_path, monkeypatch):
    config, source = prepared
    destination = tmp_path / "out" / "bad.m4b"

    def fake_validate(*args, **kwargs):
        return False, ["synthetic failure"]

    monkeypatch.setattr(audio_mod, "validate_output", fake_validate)
    outcome = clean_part(source, destination, config, tmp_path / "work")

    assert not outcome.ok
    assert "synthetic failure" in outcome.reason
    assert not destination.exists()


@needs_ffmpeg
def test_missing_source_is_reported(config, tmp_path):
    outcome = clean_part(
        tmp_path / "nope.m4b", tmp_path / "out.m4b", config, tmp_path / "work"
    )
    assert not outcome.ok
    assert "no longer exists" in outcome.reason
