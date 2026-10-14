"""Telling a vanished NAS from an empty folder, without hanging on it."""

from __future__ import annotations

import time

from prudify.core import reachability
from prudify.core.reachability import check_dir


def test_a_listable_folder_is_fine(tmp_path):
    assert check_dir(tmp_path) is None


def test_a_missing_folder_says_so(tmp_path):
    problem = check_dir(tmp_path / "gone")
    assert problem and "gone" in problem


def test_a_hung_mount_times_out_instead_of_blocking(tmp_path, monkeypatch):
    def hang(_path):
        time.sleep(5)

    monkeypatch.setattr(reachability.os, "scandir", hang)
    started = time.monotonic()
    problem = check_dir(tmp_path, timeout=0.2)
    assert problem and "did not respond" in problem
    assert time.monotonic() - started < 2
