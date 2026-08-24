"""Configuration model and YAML persistence for Prudify.

Configuration lives in a single ``config.yaml`` inside the data directory.
Everything is a plain pydantic model so the same schema validates the file on
disk, the ``PUT /api/v1/settings`` request body, and the defaults used on first
run. Environment variables only choose *where* the config lives -- they do not
duplicate the settings themselves, which keeps a single source of truth.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

APP_NAME = "prudify"
DEFAULT_PORT = 8317

AUDIO_EXTENSIONS = {
    ".m4b",
    ".m4a",
    ".mp3",
    ".mp4",
    ".aac",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
}

# Containers we can write while keeping chapters + cover art intact.
CHAPTER_CAPABLE_CONTAINERS = {".m4b", ".m4a", ".mp4", ".mp3", ".ogg", ".opus"}


def default_data_dir() -> Path:
    """Resolve the platform-appropriate data directory.

    Docker images set ``PRUDIFY_DATA_DIR=/config``, which is the conventional
    mount point for self-hosted services; native installs fall back to the OS
    user-data location.
    """
    env = os.environ.get("PRUDIFY_DATA_DIR")
    if env:
        return Path(env).expanduser()

    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "Prudify"
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / "Prudify"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    url_base: str = ""
    api_key: str = Field(default_factory=lambda: secrets.token_hex(16))
    # When false the API accepts unauthenticated requests from anywhere. Useful
    # behind an authenticating reverse proxy; a bad idea on the open internet.
    require_api_key: bool = True
    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("url_base")
    @classmethod
    def _normalise_url_base(cls, value: str) -> str:
        value = value.strip().strip("/")
        return f"/{value}" if value else ""


class LibrarySettings(BaseModel):
    """One watched source tree and where its cleaned copies are written."""

    id: str = Field(default_factory=lambda: secrets.token_hex(6))
    name: str = "Audiobooks"
    source_path: str
    output_path: str
    enabled: bool = True
    # Automatically queue newly detected books rather than only listing them.
    auto_process: bool = True
    # Restrict to these extensions; empty means "every supported format".
    extensions: list[str] = Field(default_factory=list)
    # Glob patterns (relative to source_path) that are never processed.
    exclude_patterns: list[str] = Field(default_factory=list)

    @field_validator("extensions")
    @classmethod
    def _normalise_extensions(cls, value: list[str]) -> list[str]:
        out = []
        for ext in value:
            ext = ext.strip().lower()
            if not ext:
                continue
            out.append(ext if ext.startswith(".") else f".{ext}")
        return out


class TranscriptionSettings(BaseModel):
    engine: Literal["faster-whisper", "whisper-cpp", "openai-whisper"] = "faster-whisper"
    model: str = "base.en"
    # "auto" picks cuda when torch/CTranslate2 report a usable GPU, else cpu.
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"
    language: str = "en"
    beam_size: int = Field(default=5, ge=1, le=10)
    # Voice-activity detection skips silence, which is a large speed win on
    # audiobooks and reduces hallucinated text in quiet passages.
    vad_filter: bool = True
    cpu_threads: int = Field(default=4, ge=1, le=64)
    num_workers: int = Field(default=1, ge=1, le=8)
    # 0 streams the whole file (faster-whisper is memory-bounded). Set a value
    # in minutes to force segmented decoding on very constrained machines.
    chunk_minutes: int = Field(default=0, ge=0, le=240)
    chunk_overlap_seconds: int = Field(default=2, ge=0, le=30)
    model_dir: str = ""
    # Path to a whisper.cpp binary when engine == "whisper-cpp".
    whisper_cpp_binary: str = ""
    initial_prompt: str = ""


class FilterSettings(BaseModel):
    # Name of a bundled list (strict/moderate) or "custom".
    wordlist: str = "strict"
    custom_words: list[str] = Field(default_factory=list)
    custom_allowlist: list[str] = Field(default_factory=list)
    # exact: whole-token equality. prefix: honours trailing '*' rules.
    # fuzzy: prefix plus a small edit-distance tolerance for mishearings.
    match_mode: Literal["exact", "prefix", "fuzzy"] = "exact"
    fuzzy_max_distance: int = Field(default=1, ge=0, le=3)
    pad_before_ms: int = Field(default=0, ge=0, le=5000)
    pad_after_ms: int = Field(default=100, ge=0, le=5000)
    # Two hits closer together than this are merged into one interval.
    merge_gap_ms: int = Field(default=250, ge=0, le=5000)
    # Padding stops this far short of the neighbouring word, so a generous
    # pad cannot clip the speech either side. 0 disables the clamp and lets
    # padding run wherever it likes.
    neighbour_guard_ms: int = Field(default=30, ge=0, le=1000)
    # Discard matches whose word-level probability is below this threshold.
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class OutputSettings(BaseModel):
    mode: Literal["mute", "beep", "cut"] = "mute"
    beep_frequency: int = Field(default=1000, ge=100, le=8000)
    # Fraction of full scale. 0.15 sits comfortably under typical narration.
    beep_volume: float = Field(default=0.15, ge=0.0, le=1.0)
    # "same" keeps the source container; otherwise force one.
    container: Literal["same", "m4b", "m4a", "mp3", "opus"] = "same"
    audio_codec: str = "auto"
    bitrate: str = "auto"
    sample_rate: int = 0  # 0 = keep source
    preserve_chapters: bool = True
    preserve_cover: bool = True
    preserve_metadata: bool = True
    # Writes a "PRUDIFY_CLEANED" tag so a re-scan can recognise its own output.
    tag_cleaned: bool = True
    # Mirror the source's Author/Book folder layout under output_path.
    mirror_structure: bool = True
    overwrite_existing: bool = False
    # When a book turns out to contain nothing to silence, copy it across
    # unchanged so the clean library stays a complete mirror of the source.
    copy_when_clean: bool = True


class ProcessingSettings(BaseModel):
    max_concurrent_jobs: int = Field(default=1, ge=1, le=8)
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    scan_interval_minutes: int = Field(default=60, ge=0, le=10080)
    # A new file must stop changing size for this long before it is queued.
    stability_seconds: int = Field(default=30, ge=0, le=3600)
    skip_if_output_exists: bool = True
    keep_transcripts: bool = True
    keep_work_files: bool = False
    dry_run: bool = False
    min_free_space_mb: int = Field(default=2048, ge=0)
    # Reject the result if the cleaned duration drifts more than this.
    duration_tolerance_seconds: float = Field(default=1.0, ge=0.0, le=60.0)


class Config(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    libraries: list[LibrarySettings] = Field(default_factory=list)
    transcription: TranscriptionSettings = Field(default_factory=TranscriptionSettings)
    filtering: FilterSettings = Field(default_factory=FilterSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- paths -----------------------------------------------------------
    # Not persisted as user-editable settings; derived from the data dir so a
    # container bind-mount is the only thing an operator has to think about.
    data_dir: str = ""
    work_dir: str = ""

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    def resolved_work_dir(self) -> Path:
        if self.work_dir:
            return Path(self.work_dir)
        return self.resolved_data_dir() / "work"

    def database_path(self) -> Path:
        return self.resolved_data_dir() / "prudify.db"

    def transcript_dir(self) -> Path:
        return self.resolved_data_dir() / "transcripts"

    def log_path(self) -> Path:
        return self.resolved_data_dir() / "logs" / "prudify.log"

    def library_by_id(self, library_id: str) -> LibrarySettings | None:
        return next((lib for lib in self.libraries if lib.id == library_id), None)


def config_path(data_dir: Path | None = None) -> Path:
    env = os.environ.get("PRUDIFY_CONFIG")
    if env:
        return Path(env).expanduser()
    return (data_dir or default_data_dir()) / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    """Load config from disk, creating a default file on first run."""
    data_dir = default_data_dir()
    path = path or config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = Config.model_validate(raw)
    else:
        config = Config()
        if os.environ.get("PRUDIFY_API_KEY"):
            config.server.api_key = os.environ["PRUDIFY_API_KEY"]

    if not config.data_dir:
        config.data_dir = str(data_dir)
    if os.environ.get("PRUDIFY_WORK_DIR"):
        config.work_dir = os.environ["PRUDIFY_WORK_DIR"]

    # Environment wins for the handful of values a container needs to control.
    if os.environ.get("PRUDIFY_PORT"):
        config.server.port = int(os.environ["PRUDIFY_PORT"])
    if os.environ.get("PRUDIFY_HOST"):
        config.server.host = os.environ["PRUDIFY_HOST"]
    if os.environ.get("PRUDIFY_LOG_LEVEL"):
        config.log_level = os.environ["PRUDIFY_LOG_LEVEL"].upper()  # type: ignore[assignment]

    if not path.exists():
        save_config(config, path)

    for directory in (
        config.resolved_data_dir(),
        config.resolved_work_dir(),
        config.transcript_dir(),
        config.log_path().parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    return config


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or config_path(config.resolved_data_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def find_binary(name: str, configured: str = "") -> str | None:
    """Locate ffmpeg/ffprobe, honouring an explicit path first."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
    env = os.environ.get(f"PRUDIFY_{name.upper()}")
    if env and Path(env).is_file():
        return env
    return shutil.which(name)
