"""Configuration loading, persistence and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from prudify.config import (
    Config,
    FilterSettings,
    LibrarySettings,
    OutputSettings,
    config_path,
    load_config,
    save_config,
)
from pydantic import ValidationError


def test_first_run_creates_a_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    assert config_path(config.resolved_data_dir()).exists()
    assert config.server.api_key


def test_api_key_is_stable_across_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    first = load_config().server.api_key
    assert load_config().server.api_key == first


def test_directories_are_created(tmp_path, monkeypatch):
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    assert config.resolved_work_dir().is_dir()
    assert config.transcript_dir().is_dir()


def test_environment_overrides_port(tmp_path, monkeypatch):
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRUDIFY_PORT", "9999")
    assert load_config().server.port == 9999


def test_round_trip_preserves_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    config.filtering.pad_after_ms = 321
    config.libraries.append(
        LibrarySettings(name="X", source_path="/a", output_path="/b")
    )
    save_config(config)

    reloaded = load_config()
    assert reloaded.filtering.pad_after_ms == 321
    assert len(reloaded.libraries) == 1
    assert reloaded.libraries[0].name == "X"


def test_saved_yaml_is_human_editable(tmp_path, monkeypatch):
    monkeypatch.setenv("PRUDIFY_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    save_config(config)
    raw = yaml.safe_load(config_path(config.resolved_data_dir()).read_text())
    assert raw["filtering"]["wordlist"] == "strict"
    assert raw["output"]["mode"] == "mute"


class TestValidation:
    def test_padding_must_be_positive(self):
        with pytest.raises(ValidationError):
            FilterSettings(pad_after_ms=-1)

    def test_mode_is_constrained(self):
        with pytest.raises(ValidationError):
            OutputSettings(mode="obliterate")

    def test_url_base_is_normalised(self):
        assert Config().server.url_base == ""
        from prudify.config import ServerSettings

        assert ServerSettings(url_base="prudify/").url_base == "/prudify"
        assert ServerSettings(url_base="/prudify").url_base == "/prudify"

    def test_extensions_get_a_leading_dot(self):
        library = LibrarySettings(
            source_path="/a", output_path="/b", extensions=["m4b", ".MP3"]
        )
        assert library.extensions == [".m4b", ".mp3"]

    def test_libraries_get_unique_ids(self):
        first = LibrarySettings(source_path="/a", output_path="/b")
        second = LibrarySettings(source_path="/c", output_path="/d")
        assert first.id != second.id


def test_library_lookup_by_id():
    library = LibrarySettings(source_path="/a", output_path="/b")
    config = Config(libraries=[library])
    assert config.library_by_id(library.id) is library
    assert config.library_by_id("nope") is None


class TestAvailableCpus:
    """Four threads was a guess. On a 12 core machine it wastes two thirds."""

    def test_leaves_a_core_free_on_a_large_machine(self, monkeypatch):
        from prudify import config as config_mod

        monkeypatch.setattr(config_mod.os, "cpu_count", lambda: 12)
        monkeypatch.setattr(Path, "read_text", _raise_oserror)
        assert config_mod.available_cpus() == 11

    def test_uses_everything_on_a_small_one(self, monkeypatch):
        from prudify import config as config_mod

        monkeypatch.setattr(config_mod.os, "cpu_count", lambda: 2)
        monkeypatch.setattr(Path, "read_text", _raise_oserror)
        assert config_mod.available_cpus() == 2

    def test_a_container_quota_wins_over_the_host_core_count(self, monkeypatch):
        """os.cpu_count() reports the host's cores, which under a limit is a lie."""
        from prudify import config as config_mod

        monkeypatch.setattr(config_mod.os, "cpu_count", lambda: 64)
        monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "400000 100000")
        assert config_mod.available_cpus() == 4

    def test_an_unlimited_quota_falls_back_to_the_core_count(self, monkeypatch):
        from prudify import config as config_mod

        monkeypatch.setattr(config_mod.os, "cpu_count", lambda: 8)
        monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "max 100000")
        assert config_mod.available_cpus() == 7

    def test_the_setting_still_wins_when_it_is_set(self):
        from prudify.config import TranscriptionSettings

        assert TranscriptionSettings().cpu_threads == 0  # 0 means "decide for me"
        assert TranscriptionSettings(cpu_threads=3).cpu_threads == 3


def _raise_oserror(*_args, **_kwargs):
    raise OSError("no cgroup here")

