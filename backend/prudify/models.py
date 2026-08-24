"""SQLAlchemy models. SQLite is the only supported store -- one file, no server.

The database is a *cache and journal*, never the source of truth for audio.
It can be deleted at any time; a rescan rebuilds it from the filesystem.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class BookStatus(str, enum.Enum):
    NEW = "new"
    QUEUED = "queued"
    PROCESSING = "processing"
    CLEANED = "cleaned"
    PARTIAL = "partial"
    FAILED = "failed"
    IGNORED = "ignored"
    MISSING = "missing"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Book(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    library_id: Mapped[str] = mapped_column(String(32), index=True)
    relative_folder: Mapped[str] = mapped_column(String(1024), default="")
    folder: Mapped[str] = mapped_column(String(1024), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    author: Mapped[str] = mapped_column(String(512), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default=BookStatus.NEW.value, index=True)
    part_count: Mapped[int] = mapped_column(Integer, default=0)
    formats: Mapped[list] = mapped_column(JSON, default=list)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    muted_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    word_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    parts: Mapped[list[Part]] = relationship(
        back_populates="book", cascade="all, delete-orphan", lazy="selectin"
    )


class Part(Base):
    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    path: Mapped[str] = mapped_column(String(1024))
    destination: Mapped[str] = mapped_column(String(1024), default="")
    extension: Mapped[str] = mapped_column(String(16), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default=BookStatus.NEW.value)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    muted_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    matches: Mapped[list] = mapped_column(JSON, default=list)
    transcript_path: Mapped[str] = mapped_column(String(1024), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    book: Mapped[Book] = relationship(back_populates="parts")


Index("ix_parts_book_relative", Part.book_id, Part.relative_path, unique=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(String(32), index=True)
    book_title: Mapped[str] = mapped_column(String(512), default="")
    book_author: Mapped[str] = mapped_column(String(512), default="")
    library_id: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value, index=True)
    stage: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(String(512), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    part_total: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or utcnow()
        start = self.started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - start).total_seconds()


class LogRecord(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO", index=True)
    logger: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
