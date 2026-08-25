"""Configuration model and YAML persistence for Prudify.

Configuration lives in a single ``config.yaml`` inside the data directory.
Everything is a plain pydantic model so the same schema validates the file on
disk, the ``PUT /api/v1/settings`` request body, and the defaults used on first
run. Environment variables only choose *where* the config lives -- they do not
duplicate the settings themselves, which keeps a single source of truth.
"""

from __future__ import annotations

import errno
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


class AuthSettings(BaseModel):
    """How browsers authenticate. The API key is a separate, parallel credential.

    The shape follows the *arr applications, because their users are this
    project's users and the vocabulary is already familiar:

    ``none``      no authentication at all
    ``apikey``    the API key only -- what Prudify did before login existed
    ``basic``     the browser's own username/password prompt
    ``forms``     a login page and a session cookie (the default)
    ``external``  identity comes from a reverse proxy header (Authelia et al)

    Whatever is set here, a valid ``X-Api-Key`` is always accepted on the API
    so scripts, the CLI and Home Assistant keep working -- again matching the
    *arr behaviour.
    """

    method: Literal["none", "apikey", "basic", "forms", "external"] = "forms"

    # The *arr "Authentication Required" setting. Skipping auth for private
    # addresses is convenient on a trusted LAN and wrong behind a reverse
    # proxy, where every request appears to come from the proxy itself.
    required: Literal["always", "disabled_for_local"] = "always"

    username: str = ""
    # scrypt, from prudify.security. Never a plaintext password.
    password_hash: str = ""

    # Signing key for session cookies. Rotating it invalidates every session.
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))
    # Bumped on password change or "sign out everywhere"; tokens carry the
    # epoch they were issued under, which is what lets stateless cookies be
    # revoked without a session table.
    session_epoch: int = 1
    session_lifetime_hours: int = Field(default=720, ge=1, le=8760)

    # For method="external" only. The header is trusted *only* when the
    # request arrives from one of these networks -- otherwise anyone could
    # simply send the header themselves.
    trusted_proxies: list[str] = Field(default_factory=list)
    proxy_user_header: str = "X-Forwarded-User"

    @property
    def configured(self) -> bool:
        """True when a login account exists."""
        return bool(self.username and self.password_hash)

    @property
    def needs_setup(self) -> bool:
        """True when a password-based method is selected but not set up yet."""
        return self.method in ("basic", "forms") and not self.configured


class LibrarySettings(BaseModel):
    """One watched source tree and where its cleaned copies are written."""

    id: str = Field(default_factory=lambda: secrets.token_hex(6))
    name: str = "Audiobooks"
    source_path: str
    output_path: str
    enabled: bool = True
    # Automatically queue newly detected books rather than only listing them.
    auto_process: bool = True
    # How the tree is shaped. "books" is the audiobook convention: a folder of
    # files is one work split into parts. "episodes" suits podcasts and other
    # serial audio, where a folder is a *show* and each file is a separate
    # thing someone listens to on its own. The difference is not cosmetic --
    # it decides whether one bad file fails 300 episodes or just itself.
    layout: Literal["books", "episodes"] = "books"
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
    # 0 means "as many as this machine has". Four was a safe guess on the
    # box this was written on and a poor one everywhere else: on a 12 core
    # laptop it leaves two thirds of the machine idle through the longest
    # stage of the job, and the setting is buried enough that nobody thinks
    # to raise it.
    cpu_threads: int = Field(default=0, ge=0, le=64)
    num_workers: int = Field(default=1, ge=1, le=8)
    # 0 streams short files whole and automatically chunks long files. Set a
    # value in minutes to force a specific segment length.
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
    auth: AuthSettings = Field(default_factory=AuthSettings)
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

    def cover_dir(self) -> Path:
        return self.resolved_data_dir() / "covers"

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
        raw = _migrate_auth(raw)
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


def _migrate_auth(raw: dict) -> dict:
    """Give configs written before login existed an auth section.

    An existing installation has no ``auth`` block, and defaulting it to
    ``forms`` would lock the owner out of their own server on upgrade --
    there is no account yet, and the credential they have is an API key.
    So carry forward exactly what they had: API-key auth, or none if they
    had turned it off. New installations get the ``forms`` default instead,
    because they have no key to carry forward and a login page is a better
    first-run experience than pasting a hex string.
    """
    if not isinstance(raw, dict) or "auth" in raw:
        return raw
    server = raw.get("server") or {}
    raw = dict(raw)
    raw["auth"] = {
        "method": "apikey" if server.get("require_api_key", True) else "none"
    }
    return raw


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
    # Contains the API key and the password hash. On a bind-mounted volume the
    # default 0644 makes both readable by every user on the host.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows, or a filesystem without POSIX modes
    return path


def available_cpus() -> int:
    """How many cores this process may actually use.

    ``os.cpu_count()`` reports the *host's* cores, which inside a container
    with a CPU limit is a fiction -- ask for twelve threads under a four-core
    quota and the scheduler spends its time context-switching rather than
    transcribing. cgroup v2 publishes the real quota, so prefer it when it is
    set to anything other than "max".
    """
    limit = None
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            limit = max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass

    detected = os.cpu_count() or 1
    if limit is not None:
        detected = min(detected, limit)
    # Leave a core for the rest of the machine once there are enough to spare.
    return max(1, detected - 1) if detected > 4 else detected


def writable_dir_error(path: Path) -> str | None:
    """Return why ``path`` cannot be written to, or ``None`` when it can.

    ``Path.mkdir(parents=True, exist_ok=True)`` is not a writability test.
    On a read-only mount it succeeds for any directory that already exists,
    which is exactly the shape of the mistake this catches: an output path
    aimed inside a ``:ro`` media mount whose top-level folder happens to
    exist. Nothing fails until a book finishes transcribing and the
    per-title subdirectory cannot be created -- twenty minutes of CPU after
    the point where the answer was already knowable. Writing a real file is
    the only check that tells the truth.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return f"{path} is on a read-only filesystem"
        return f"Cannot create {path}: {exc.strerror or exc}"

    probe = path / f".prudify-write-test-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        probe.touch()
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return f"{path} is on a read-only filesystem"
        if exc.errno == errno.EACCES:
            return f"No permission to write to {path} (check PUID/PGID)"
        return f"Cannot write to {path}: {exc.strerror or exc}"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return None


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
