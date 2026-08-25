"""Filter-graph construction and ffmpeg integration."""

from __future__ import annotations

from pathlib import Path

import pytest
from prudify.core import audio as audio_mod
from prudify.core.audio import Chapter
from prudify.core.cancel import OperationCancelled

from .conftest import FFMPEG, needs_ffmpeg

INTERVALS = [(10.0, 10.5), (30.0, 30.7)]


class TestFilterGraph:
    def test_empty_intervals_pass_through(self):
        assert audio_mod.build_filter_graph([]) == "[0:a]anull[aout]"

    def test_mute_graph_gates_each_interval(self):
        graph = audio_mod.build_filter_graph(INTERVALS)
        assert "between(t,10.000,10.500)" in graph
        assert "between(t,30.000,30.700)" in graph
        assert graph.startswith("[0:a]") and graph.endswith("[aout]")

    def test_mute_batches_large_interval_counts(self):
        many = [(i * 0.1, i * 0.1 + 0.05) for i in range(600)]
        graph = audio_mod.build_filter_graph(many)
        # Chaining mutes is safe: successive gates union together.
        assert graph.count("volume=0:enable=") == 12

    def test_beep_graph_needs_no_extra_input(self):
        graph = audio_mod.build_filter_graph(INTERVALS, mode="beep")
        assert "sine=frequency=" in graph
        assert "[1:a]" not in graph

    def test_beep_gate_is_inverted_for_the_tone(self):
        graph = audio_mod.build_filter_graph(INTERVALS, mode="beep")
        assert "volume=0:enable='not(" in graph
        # amix must not halve the narration.
        assert "normalize=0" in graph

    def test_cut_graph_selects_the_complement(self):
        graph = audio_mod.build_cut_graph(INTERVALS)
        assert graph.startswith("[0:a]aselect='not(")
        assert "asetpts" in graph


class TestChapterShifting:
    def test_chapters_move_back_by_removed_time(self):
        chapters = [Chapter(0, 0.0, 20.0, "One"), Chapter(1, 20.0, 40.0, "Two")]
        shifted = audio_mod.shift_chapters(chapters, [(5.0, 6.0)])
        assert shifted[0].start == 0.0
        assert shifted[0].end == pytest.approx(19.0)
        assert shifted[1].start == pytest.approx(19.0)

    def test_cut_inside_a_chapter_is_accounted_for(self):
        chapters = [Chapter(0, 0.0, 20.0, "One")]
        shifted = audio_mod.shift_chapters(chapters, [(10.0, 12.0), (15.0, 16.0)])
        assert shifted[0].end == pytest.approx(17.0)

    def test_no_cuts_leaves_chapters_alone(self):
        chapters = [Chapter(0, 0.0, 20.0, "One")]
        assert audio_mod.shift_chapters(chapters, [])[0].end == 20.0


class TestCodecSelection:
    @pytest.mark.parametrize(
        "container,expected",
        [(".m4b", "aac"), (".m4a", "aac"), (".mp3", "libmp3lame"), (".opus", "libopus")],
    )
    def test_codec_follows_container(self, container, expected):
        assert audio_mod.choose_codec(container, "aac") == expected

    def test_explicit_codec_wins(self):
        assert audio_mod.choose_codec(".m4b", "aac", "libfdk_aac") == "libfdk_aac"

    def test_bitrate_tracks_the_source(self):
        info = audio_mod.MediaInfo(path=None, bit_rate=64000, channels=1)  # type: ignore[arg-type]
        assert audio_mod.choose_bitrate(info) == "64k"


class TestCancellation:
    def test_extract_pcm_passes_cancel_to_ffmpeg(self, monkeypatch, tmp_path):
        marker = object()
        seen = {}

        monkeypatch.setattr(audio_mod, "ffmpeg_path", lambda: "ffmpeg")

        def fake_run_ffmpeg(cmd, progress=None, total_duration=0.0, cancel=None):
            seen["cancel"] = cancel

        monkeypatch.setattr(audio_mod, "run_ffmpeg", fake_run_ffmpeg)

        audio_mod.extract_pcm(Path("input.m4b"), tmp_path / "out.wav", cancel=lambda: marker)

        assert seen["cancel"]() is marker

    @needs_ffmpeg
    def test_run_ffmpeg_can_cancel_without_progress(self, tmp_path):
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 2

        with pytest.raises(OperationCancelled):
            audio_mod.run_ffmpeg(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100",
                    "-t",
                    "20",
                    str(tmp_path / "cancel.wav"),
                ],
                cancel=cancel,
            )

    def test_bitrate_has_a_sane_default(self):
        info = audio_mod.MediaInfo(path=None, bit_rate=0, channels=1)  # type: ignore[arg-type]
        assert audio_mod.choose_bitrate(info) == "64k"


class TestMetadata:
    def test_ffmetadata_round_trip(self, tmp_path):
        path = audio_mod.write_ffmetadata(
            [Chapter(0, 0.0, 12.5, "Chapter; One")],
            {"title": "A=B"},
            tmp_path / "meta.ffmeta",
        )
        text = path.read_text(encoding="utf-8")
        assert text.startswith(";FFMETADATA1")
        assert "START=0" in text and "END=12500" in text
        assert r"Chapter\; One" in text
        assert r"A\=B" in text


@needs_ffmpeg
class TestProbe:
    def test_reads_chapters_cover_and_tags(self, sample_m4b):
        info = audio_mod.probe(sample_m4b)
        assert info.duration == pytest.approx(60.0, abs=0.3)
        assert info.chapter_count == 3
        assert info.has_cover
        assert info.tags["artist"] == "Test Author"
        assert info.codec == "aac"
        assert info.channels == 2

    def test_chapter_titles_survive(self, sample_m4b):
        titles = [chapter.title for chapter in audio_mod.probe(sample_m4b).chapters]
        assert titles == ["Chapter One", "Chapter Two", "Chapter Three"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(audio_mod.FFmpegError):
            audio_mod.probe(tmp_path / "nope.m4b")


@needs_ffmpeg
class TestValidation:
    def test_accepts_a_faithful_copy(self, sample_m4b, tmp_path):
        info = audio_mod.probe(sample_m4b)
        destination = tmp_path / "copy.m4b"
        audio_mod.render(sample_m4b, destination, info, [(10.0, 10.5)], work_dir=tmp_path / "w")
        ok, problems = audio_mod.validate_output(info, destination)
        assert ok, problems

    def test_rejects_a_missing_output(self, sample_m4b, tmp_path):
        info = audio_mod.probe(sample_m4b)
        ok, problems = audio_mod.validate_output(info, tmp_path / "absent.m4b")
        assert not ok and problems

    def test_flags_lost_chapters(self, sample_m4b, tmp_path):
        info = audio_mod.probe(sample_m4b)
        destination = tmp_path / "nochap.m4b"
        audio_mod.render(
            sample_m4b, destination, info, [(10.0, 10.5)],
            preserve_chapters=False, work_dir=tmp_path / "w",
        )
        ok, problems = audio_mod.validate_output(info, destination, expect_chapters=True)
        assert not ok
        assert any("Chapter" in problem for problem in problems)


class TestFilterScriptArgs:
    """ffmpeg changed how a filter graph is read from a file."""

    def test_modern_ffmpeg_uses_the_slash_form(self, monkeypatch):
        monkeypatch.setattr(audio_mod, "ffmpeg_major_version", lambda: 9)
        assert audio_mod.filter_script_args(Path("g.txt")) == ["-/filter_complex", "g.txt"]

    def test_ffmpeg_7_uses_the_slash_form(self, monkeypatch):
        monkeypatch.setattr(audio_mod, "ffmpeg_major_version", lambda: 7)
        assert audio_mod.filter_script_args(Path("g.txt")) == ["-/filter_complex", "g.txt"]

    def test_older_ffmpeg_uses_the_removed_option(self, monkeypatch):
        monkeypatch.setattr(audio_mod, "ffmpeg_major_version", lambda: 6)
        assert audio_mod.filter_script_args(Path("g.txt")) == ["-filter_complex_script", "g.txt"]

    def test_unknown_version_assumes_modern(self, monkeypatch):
        """Git snapshots print a date, not a version -- and they are recent."""
        monkeypatch.setattr(audio_mod, "ffmpeg_major_version", lambda: 0)
        assert audio_mod.filter_script_args(Path("g.txt")) == ["-/filter_complex", "g.txt"]


class TestSignalledFfmpegIsNotAFailure:
    """A redeploy killed ffmpeg seconds from done and the book was marked FAILED.

    `tini -g` forwards SIGTERM to every child, so ffmpeg dies before the poll
    loop notices the shutdown. Reported as a render failure, that discards a
    finished encode and leaves a traceback where "we stopped the container"
    belongs.
    """

    def _sleeper(self):
        return [
            FFMPEG, "-v", "error", "-y", "-f", "lavfi",
            "-i", "sine=frequency=300:duration=30", "-f", "null", "-",
        ]

    def test_a_cancelled_run_raises_cancellation_not_ffmpeg_error(self):
        from prudify.core.audio import run_ffmpeg
        from prudify.core.cancel import OperationCancelled

        with pytest.raises(OperationCancelled):
            run_ffmpeg(self._sleeper(), cancel=lambda: True)

    def test_a_run_killed_from_outside_is_read_as_cancellation(self, monkeypatch):
        """The race this actually hit: ffmpeg is already dead when we look."""
        import subprocess

        from prudify.core.audio import run_ffmpeg
        from prudify.core.cancel import OperationCancelled

        started = {}

        def cancel():
            # False while ffmpeg is alive, True once it is not -- exactly the
            # ordering a group signal produces, where the process is already
            # gone by the time the exit code is examined.
            process = started.get("process")
            return process is not None and process.poll() is not None

        real_popen = subprocess.Popen

        def popen(cmd, **kwargs):
            process = real_popen(cmd, **kwargs)
            process.terminate()  # stand in for tini forwarding the signal
            started["process"] = process
            return process

        monkeypatch.setattr(subprocess, "Popen", popen)
        with pytest.raises(OperationCancelled):
            run_ffmpeg(self._sleeper(), cancel=cancel)

    def test_a_genuine_failure_still_reports_as_one(self):
        from prudify.core.audio import FFmpegError, run_ffmpeg

        with pytest.raises(FFmpegError):
            run_ffmpeg([FFMPEG, "-v", "error", "-i", "/nonexistent.m4b", "-f", "null", "-"])

