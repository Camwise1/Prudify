"""End-to-end pipeline tests against real audio, with a seeded transcript.

Seeding the transcript cache lets these run in CI without downloading a
Whisper model, while still exercising every other stage for real: ffprobe,
matching, the filter graph, encoding, validation and publication.
"""

from __future__ import annotations

import pytest
from prudify.core import audio as audio_mod
from prudify.core.cancel import OperationCancelled
from prudify.core.pipeline import (
    PipelineCancelled,
    clean_part,
    pass_through_part,
    stage_fraction,
    transcript_cache_path,
)
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
def test_cancel_signal_reaches_render(prepared, tmp_path, monkeypatch):
    config, source = prepared
    destination = tmp_path / "out" / "cancel.m4b"
    asked_to_cancel = False

    def cancel():
        return asked_to_cancel

    def fake_render(**kwargs):
        nonlocal asked_to_cancel
        render_cancel = kwargs["cancel"]
        assert render_cancel is cancel
        asked_to_cancel = True
        assert render_cancel() is True
        raise OperationCancelled("Cancelled")

    monkeypatch.setattr(audio_mod, "render", fake_render)

    with pytest.raises(PipelineCancelled):
        clean_part(source, destination, config, tmp_path / "work", cancel=cancel)

    assert not destination.exists()


@needs_ffmpeg
def test_missing_source_is_reported(config, tmp_path):
    outcome = clean_part(
        tmp_path / "nope.m4b", tmp_path / "out.m4b", config, tmp_path / "work"
    )
    assert not outcome.ok
    assert "no longer exists" in outcome.reason


def test_drm_protected_source_fails_before_transcribing(config, tmp_path, monkeypatch):
    from prudify.core import pipeline as pipeline_mod

    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    monkeypatch.setattr(
        pipeline_mod.audio_mod,
        "probe",
        lambda path: audio_mod.MediaInfo(path=path, duration=1640.0, codec_tag="drms"),
    )

    def never(*_args, **_kwargs):
        raise AssertionError("transcription should not start")

    monkeypatch.setattr(pipeline_mod.transcribe_mod, "transcribe_file", never)
    outcome = clean_part(source, tmp_path / "out/book.m4b", config, tmp_path / "work")

    assert not outcome.ok
    assert outcome.reason.startswith("DRM-protected (Apple FairPlay)")
    assert not (tmp_path / "out").exists()


class TestDestinationPreflight:
    """An unusable output path must be caught before the CPU is spent.

    This is the read-only-mount bug: an output path pointing inside a ``:ro``
    media mount only failed at ``destination.parent.mkdir()``, which happens
    after transcription. A twenty hour book burned hours of CPU to reach a
    conclusion available in milliseconds.
    """

    def test_unwritable_destination_fails_before_transcribing(
        self, config, sample_m4b, tmp_path
    ):
        # Deliberately no seeded transcript: if the preflight regresses, this
        # falls through to a real Whisper model download instead of returning
        # at once, and the test hangs rather than quietly passing.
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where a directory needs to be")
        outcome = clean_part(
            sample_m4b, blocker / "Author" / "Book.m4b", config, tmp_path / "work"
        )
        assert not outcome.ok
        assert "Cannot create" in outcome.reason
        assert outcome.word_count == 0

    def test_a_writable_destination_still_works(self, prepared, tmp_path):
        config, source = prepared
        destination = tmp_path / "clean" / "Author" / "Book.m4b"
        outcome = clean_part(source, destination, config, tmp_path / "work")
        assert outcome.ok, outcome.reason
        assert destination.exists()

    def test_the_probe_file_is_cleaned_up(self, prepared, tmp_path):
        config, source = prepared
        destination = tmp_path / "clean" / "Book.m4b"
        clean_part(source, destination, config, tmp_path / "work")
        assert list(destination.parent.glob(".prudify-write-test-*")) == []

    def test_a_dry_run_creates_no_output_directory(self, prepared, tmp_path):
        config, source = prepared
        config.processing.dry_run = True
        destination = tmp_path / "never-created" / "Book.m4b"
        outcome = clean_part(source, destination, config, tmp_path / "work")
        assert outcome.ok and outcome.skipped
        assert not destination.parent.exists()

    def test_low_space_on_the_work_volume_is_refused(
        self, prepared, tmp_path, monkeypatch
    ):
        config, source = prepared
        config.processing.min_free_space_mb = 1024
        work = tmp_path / "work"

        import prudify.core.pipeline as pipeline_mod

        real = pipeline_mod.free_space_mb
        monkeypatch.setattr(
            pipeline_mod,
            "free_space_mb",
            lambda path: 1.0 if str(path).startswith(str(work)) else real(path),
        )
        outcome = clean_part(source, tmp_path / "clean" / "Book.m4b", config, work)
        assert not outcome.ok
        assert "work directory" in outcome.reason



def test_stage_fraction_undoes_the_part_scaling():
    # Rendering owns 0.76-0.96 of a part's bar; 0.86 is halfway through it.
    assert stage_fraction("rendering", 0.86) == pytest.approx(0.5)
    assert stage_fraction("rendering", 0.5) == 0.0
    assert stage_fraction("copying", 0.3) == pytest.approx(0.3)


class TestPassThrough:
    def test_copies_byte_for_byte(self, tmp_path):
        source = tmp_path / "in.m4b"
        source.write_bytes(bytes(range(256)) * 100)
        destination = tmp_path / "out/Book/in.m4b"
        destination.parent.mkdir(parents=True)

        result = pass_through_part(source, destination)

        assert result.ok
        assert destination.read_bytes() == source.read_bytes()
        assert not list(destination.parent.glob(".*"))

    def test_leaves_an_existing_output_alone(self, tmp_path):
        source = tmp_path / "in.m4b"
        source.write_bytes(b"new")
        destination = tmp_path / "out.m4b"
        destination.write_bytes(b"old")

        assert pass_through_part(source, destination).ok
        assert destination.read_bytes() == b"old"
        assert pass_through_part(source, destination, overwrite=True).ok
        assert destination.read_bytes() == b"new"

    def test_cancelling_leaves_nothing_behind(self, tmp_path):
        source = tmp_path / "in.m4b"
        source.write_bytes(b"x" * 1024)
        destination = tmp_path / "out.m4b"

        with pytest.raises(PipelineCancelled):
            pass_through_part(source, destination, cancel=lambda: True)
        assert list(tmp_path.iterdir()) == [source]


class TestChunkCache:
    def test_follows_the_file_and_model_not_the_job(self, config, tmp_path):
        from prudify.core.pipeline import chunk_cache_dir

        source = tmp_path / "book.m4b"
        source.write_bytes(b"audio")
        first = chunk_cache_dir(config, source)
        assert chunk_cache_dir(config, source) == first

        config.transcription.model = "some-other-model"
        assert chunk_cache_dir(config, source) != first

    def test_survives_a_failed_transcription(self, config, tmp_path, monkeypatch):
        from prudify.core import pipeline as pipeline_mod
        from prudify.core.pipeline import chunk_cache_dir

        source = tmp_path / "book.m4b"
        source.write_bytes(b"audio")
        monkeypatch.setattr(
            pipeline_mod.audio_mod,
            "probe",
            lambda path: audio_mod.MediaInfo(path=path, duration=7200.0),
        )
        seen = []

        def failing(_source, _settings, work_dir, **_kwargs):
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "chunk-0000.json").write_text("[]")
            seen.append(work_dir)
            raise OSError("Operation timed out")

        monkeypatch.setattr(pipeline_mod.transcribe_mod, "transcribe_file", failing)
        with pytest.raises(OSError):
            clean_part(source, tmp_path / "out/book.m4b", config, tmp_path / "job-1")

        assert seen == [chunk_cache_dir(config, source)]
        assert (seen[0] / "chunk-0000.json").exists()

    def test_adopts_chunks_left_in_an_old_job_directory(self, config, tmp_path, monkeypatch):
        """Upgrading mid-book must not throw away hours of transcription."""
        from prudify.core import pipeline as pipeline_mod
        from prudify.core.pipeline import chunk_cache_dir

        source = tmp_path / "book.m4b"
        source.write_bytes(b"audio")
        legacy = tmp_path / "job-1" / "transcribe"
        legacy.mkdir(parents=True)
        (legacy / "chunk-0000.json").write_text("[]")
        monkeypatch.setattr(
            pipeline_mod.audio_mod,
            "probe",
            lambda path: audio_mod.MediaInfo(path=path, duration=7200.0),
        )

        def failing(_source, _settings, work_dir, **_kwargs):
            raise OSError("stop here")

        monkeypatch.setattr(pipeline_mod.transcribe_mod, "transcribe_file", failing)
        with pytest.raises(OSError):
            clean_part(source, tmp_path / "out/book.m4b", config, tmp_path / "job-1")

        assert (chunk_cache_dir(config, source) / "chunk-0000.json").exists()
        assert not legacy.exists()


def _two_part_clip(path, quiet_seconds=0.15, loud_seconds=0.85):
    """A clip whose start is quiet and the rest loud, to prove where it begins."""
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=2000:duration={quiet_seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=2000:duration={loud_seconds}",
            "-filter_complex",
            "[0:a]volume=0.4[q];[1:a]volume=8[l];[q][l]concat=n=2:v=0:a=1[out]",
            "-map", "[out]", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@needs_ffmpeg
def test_sound_mode_starts_the_clip_on_each_word(prepared, tmp_path):
    config, source = prepared
    config.output.mode = "sound"
    config.output.beep_volume = 0.5
    config.filtering.pad_before_ms = 0
    audio_mod.prepare_sound(_two_part_clip(tmp_path / "clip.wav"), config.sound_path())
    destination = tmp_path / "out" / "sound.m4b"
    outcome = clean_part(source, destination, config, tmp_path / "work")

    assert outcome.ok, outcome.reason
    out = audio_mod.probe(destination)
    assert out.duration == pytest.approx(60.0, abs=1.0)
    assert out.chapter_count == 3
    assert out.has_cover
    # The word starts at 10.0: the clip's quiet opening, then its loud body.
    # A clip looped under the whole book would land at an arbitrary point.
    opening = peak_db(destination, 10.02, 10.12)
    body = peak_db(destination, 10.2, 10.35)
    assert body > -10
    assert opening < body - 20


@needs_ffmpeg
def test_sound_mode_without_a_clip_mutes(prepared, tmp_path):
    config, source = prepared
    config.output.mode = "sound"
    destination = tmp_path / "out" / "nosound.m4b"
    assert clean_part(source, destination, config, tmp_path / "work").ok
    assert peak_db(destination, 10.1, 10.3) < -50


def test_the_overlay_puts_the_clip_at_each_hit_to_the_sample():
    import io
    from array import array

    clip = array("h", [1000] * 100).tobytes()
    feed = audio_mod.sound_feeder(clip, [(0.010, 0.020), (0.050, 0.200)], sample_rate=1000)
    out = io.BytesIO()
    feed(out)
    samples = array("h")
    samples.frombytes(out.getvalue())

    assert list(samples[:10]) == [0] * 10  # silence until the first hit
    assert samples[10] == 1000  # the clip starts on the hit...
    assert samples[19] < 1000  # ...and fades as the hit ends
    assert list(samples[20:50]) == [0] * 30
    assert list(samples[50:150]) == [1000] * 100  # a longer hit gets the whole clip
    assert len(samples) == 150


def test_an_old_goat_setting_becomes_a_sound():
    from prudify.config import OutputSettings

    assert OutputSettings(mode="goat").mode == "sound"


class TestPublish:
    """Replacing a clean copy that another NAS user wrote."""

    def test_across_filesystems_without_touching_the_old_files_attributes(
        self, tmp_path, monkeypatch
    ):
        import errno
        import os
        import shutil

        staged = tmp_path / "scratch" / "render.m4b"
        staged.parent.mkdir()
        staged.write_bytes(b"new audio")
        destination = tmp_path / "share" / "book.m4b"
        destination.parent.mkdir()
        destination.write_bytes(b"old audio")

        real_replace = os.replace

        def cross_device(src, dst):
            if str(src) == str(staged):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst)

        def not_permitted(*_args, **_kwargs):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        # On the NAS, changing the old file's mode fails; publishing must not try.
        monkeypatch.setattr(os, "replace", cross_device)
        monkeypatch.setattr(os, "chmod", not_permitted)
        monkeypatch.setattr(shutil, "copystat", not_permitted)

        audio_mod.publish(staged, destination)

        assert destination.read_bytes() == b"new audio"
        assert not staged.exists()
        assert [p.name for p in destination.parent.iterdir()] == ["book.m4b"]

    def test_on_the_same_filesystem_it_is_a_rename(self, tmp_path):
        staged = tmp_path / "render.m4b"
        staged.write_bytes(b"new")
        destination = tmp_path / "book.m4b"
        audio_mod.publish(staged, destination)
        assert destination.read_bytes() == b"new"
        assert not staged.exists()
