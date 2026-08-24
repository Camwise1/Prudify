"""Shared fixtures.

Tests that touch audio build their own fixtures with ffmpeg rather than
committing binary files to the repository.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


@pytest.fixture(scope="session")
def cover_png(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("assets") / "cover.png"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=#2b3a67:s=240x240",
         "-frames:v", "1", str(path)],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def sample_m4b(tmp_path_factory, cover_png) -> Path:
    """A 60 second M4B with three chapters, a cover, and tags."""
    directory = tmp_path_factory.mktemp("book")
    meta = directory / "chapters.ffmeta"
    meta.write_text(
        ";FFMETADATA1\n"
        "title=Test Book\nartist=Test Author\nalbum=Test Book\n\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=20000\ntitle=Chapter One\n\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=20000\nEND=40000\ntitle=Chapter Two\n\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=40000\nEND=60000\ntitle=Chapter Three\n",
        encoding="utf-8",
    )
    path = directory / "Test Book.m4b"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=60:sample_rate=44100",
            "-i", str(cover_png), "-i", str(meta),
            "-map", "0:a", "-map", "1:v", "-map_metadata", "2", "-map_chapters", "2",
            "-c:a", "aac", "-b:a", "96k", "-ac", "2",
            "-c:v", "copy", "-disposition:v", "attached_pic",
            "-f", "mp4", "-movflags", "+faststart", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A Config rooted entirely inside tmp_path."""
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    from prudify.config import load_config

    cfg = load_config()
    cfg.processing.keep_transcripts = True
    cfg.processing.min_free_space_mb = 0
    return cfg


def peak_db(path: Path, start: float, end: float) -> float:
    """Peak level over a window, measured without seeking (seek is imprecise)."""
    result = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(path),
         "-af", f"atrim={start}:{end},volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in result.stderr.splitlines():
        if "max_volume" in line:
            return float(line.split("max_volume:")[1].strip().split()[0])
    raise AssertionError("volumedetect produced no reading")
