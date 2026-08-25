"""Pydantic response/request models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import (
    FilterSettings,
    LibrarySettings,
    OutputSettings,
    ProcessingSettings,
    ServerSettings,
    TranscriptionSettings,
)


class PartOut(BaseModel):
    id: int
    relative_path: str
    path: str
    destination: str
    extension: str
    size_bytes: int
    duration: float
    status: str
    match_count: int
    muted_seconds: float
    word_count: int
    error: str = ""
    cleaned_at: datetime | None = None

    model_config = {"from_attributes": True}


class BookOut(BaseModel):
    id: str
    library_id: str
    title: str
    author: str
    relative_folder: str
    folder: str
    status: str
    part_count: int
    formats: list[str] = Field(default_factory=list)
    total_bytes: int
    duration: float
    match_count: int
    muted_seconds: float
    word_counts: dict[str, int] = Field(default_factory=dict)
    monitored: bool = True
    error: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    cleaned_at: datetime | None = None

    model_config = {"from_attributes": True}


class BookDetail(BookOut):
    parts: list[PartOut] = Field(default_factory=list)


class BookPage(BaseModel):
    items: list[BookOut]
    total: int
    page: int
    page_size: int


class JobOut(BaseModel):
    id: int
    book_id: str
    book_title: str
    book_author: str
    library_id: str
    status: str
    stage: str
    message: str
    progress: float
    part_index: int
    part_total: int
    priority: int
    error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class QueueState(BaseModel):
    paused: bool
    active: list[dict[str, Any]] = Field(default_factory=list)
    pending: list[JobOut] = Field(default_factory=list)
    recent: list[JobOut] = Field(default_factory=list)


class SettingsOut(BaseModel):
    server: ServerSettings
    libraries: list[LibrarySettings]
    transcription: TranscriptionSettings
    filtering: FilterSettings
    output: OutputSettings
    processing: ProcessingSettings
    log_level: str


class SettingsIn(BaseModel):
    server: ServerSettings | None = None
    transcription: TranscriptionSettings | None = None
    filtering: FilterSettings | None = None
    output: OutputSettings | None = None
    processing: ProcessingSettings | None = None
    log_level: str | None = None


class LibraryIn(BaseModel):
    name: str = "Audiobooks"
    source_path: str
    output_path: str
    enabled: bool = True
    auto_process: bool = True
    layout: Literal["books", "episodes"] = "books"
    extensions: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)


class WordlistOut(BaseModel):
    name: str
    builtin: bool
    content: str
    rule_count: int


class MatchTestIn(BaseModel):
    text: str
    wordlist: str | None = None
    custom_words: list[str] = Field(default_factory=list)
    match_mode: str | None = None


class MatchTestOut(BaseModel):
    tokens: list[dict[str, Any]]
    match_count: int


class SystemStatus(BaseModel):
    version: str
    ffmpeg: str
    ffprobe_available: bool
    transcription_engine: str
    transcription_available: bool
    transcription_detail: str
    gpu: bool
    data_dir: str
    work_dir: str
    free_space_mb: float
    libraries: int
    paused: bool
    stats: dict[str, Any] = Field(default_factory=dict)


class LogOut(BaseModel):
    id: int
    created_at: datetime
    level: str
    logger: str
    message: str

    model_config = {"from_attributes": True}
