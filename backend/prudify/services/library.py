"""Reconciles what is on disk with what is in the database."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config, LibrarySettings
from ..core import scanner
from ..models import Book, BookStatus, Part
from .events import bus

log = logging.getLogger(__name__)

# How many books to process between commits during a scan. Small enough
# that SQLite's single write lock is released regularly, large enough that
# we are not paying a commit per book.
_SCAN_COMMIT_EVERY = 25  # books per transaction; each is now pure DB work


def destination_for(library: LibrarySettings, config: Config, relative_path: str) -> Path:
    container = config.output.container
    return scanner.output_path_for(library, relative_path, container=container)


def scan_library(session: Session, config: Config, library: LibrarySettings) -> dict:
    """Walk one library and upsert its books. Returns a summary dict."""
    root = Path(library.source_path).expanduser()
    if not root.is_dir():
        log.warning("Library %s: source path does not exist: %s", library.name, root)
        return {"library": library.name, "error": f"Source path not found: {root}"}

    started = datetime.now(timezone.utc)
    seen_keys: set[str] = set()
    added = 0
    updated = 0
    scanned = 0

    # Do every filesystem touch before opening a transaction. Walking the
    # library calls ffprobe per book and stats each output file, all of it
    # over the network share; doing that while holding SQLite's write lock is
    # what starved the queue worker into "database is locked".
    discovered_books = list(scanner.iter_library(library))
    destinations: dict[str, tuple[Path, bool, int]] = {}
    for found in discovered_books:
        for found_part in found.parts:
            destination = destination_for(library, config, found_part.relative_path)
            try:
                stat = destination.stat()
                destinations[found_part.relative_path] = (destination, True, stat.st_size)
            except OSError:
                destinations[found_part.relative_path] = (destination, False, 0)

    log.info(
        "%s: %d book(s) on disk, writing to the database",
        library.name,
        len(discovered_books),
    )

    for discovered in discovered_books:
        seen_keys.add(discovered.key)
        book = session.get(Book, discovered.key)
        is_new = book is None
        if book is None:
            book = Book(id=discovered.key, library_id=library.id, first_seen=started)
            session.add(book)
            added += 1
        else:
            updated += 1

        book.library_id = library.id
        book.relative_folder = discovered.relative_folder
        book.folder = str(discovered.folder)
        book.title = discovered.title
        book.author = discovered.author
        book.part_count = discovered.part_count
        book.formats = discovered.formats
        book.total_bytes = discovered.total_bytes
        book.last_seen = started
        if book.status == BookStatus.MISSING.value:
            book.status = BookStatus.NEW.value

        existing_parts = {p.relative_path: p for p in book.parts}
        current_paths: set[str] = set()
        all_clean = True
        any_clean = False

        for discovered_part in discovered.parts:
            current_paths.add(discovered_part.relative_path)
            part = existing_parts.get(discovered_part.relative_path)
            if part is None:
                part = Part(book_id=book.id, relative_path=discovered_part.relative_path)
                book.parts.append(part)
            part.path = str(discovered_part.path)
            part.extension = discovered_part.extension
            part.size_bytes = discovered_part.size_bytes
            destination, dest_exists, dest_size = destinations[discovered_part.relative_path]
            part.destination = str(destination)

            if dest_exists and dest_size > 0:
                part.status = BookStatus.CLEANED.value
                any_clean = True
            else:
                if part.status == BookStatus.CLEANED.value:
                    part.status = BookStatus.NEW.value
                if part.status not in (
                    BookStatus.QUEUED.value,
                    BookStatus.PROCESSING.value,
                    BookStatus.FAILED.value,
                ):
                    part.status = BookStatus.NEW.value
                all_clean = False

        for relative_path, part in existing_parts.items():
            if relative_path not in current_paths:
                session.delete(part)

        if book.status not in (BookStatus.QUEUED.value, BookStatus.PROCESSING.value):
            if all_clean and discovered.parts:
                book.status = BookStatus.CLEANED.value
            elif any_clean:
                book.status = BookStatus.PARTIAL.value
            elif book.status != BookStatus.FAILED.value:
                book.status = BookStatus.NEW.value

        if is_new:
            bus.publish(
                "library.book_added",
                {"id": book.id, "title": book.title, "author": book.author},
            )

        # Commit in batches. Scanning a large library means one ffprobe per
        # book over the network, so a single transaction would hold SQLite's
        # write lock for minutes and the queue worker would fail with
        # "database is locked". Committing often keeps each write brief.
        scanned += 1
        if scanned % _SCAN_COMMIT_EVERY == 0:
            session.commit()
            log.debug("Scan checkpoint at %d book(s) in %s", scanned, library.name)

    # Anything not seen this pass has left the filesystem.
    stale = session.execute(
        select(Book).where(Book.library_id == library.id)
    ).scalars().all()
    missing = 0
    for book in stale:
        if book.id not in seen_keys and book.status != BookStatus.MISSING.value:
            book.status = BookStatus.MISSING.value
            missing += 1

    session.commit()
    summary = {
        "library": library.name,
        "library_id": library.id,
        "added": added,
        "updated": updated,
        "missing": missing,
        "total": len(seen_keys),
    }
    log.info("Scanned %s: %s", library.name, summary)
    return summary


def scan_all(session: Session, config: Config) -> list[dict]:
    """Scan every enabled library, isolating failures to one library.

    A library on an unreachable share, or one that trips a bug, must not stop
    the others from being scanned -- and must not take down the caller's
    thread. Failures are recorded in the returned summary so the UI can show
    which library is unhappy rather than silently reporting nothing.
    """
    results = []
    for library in config.libraries:
        if not library.enabled:
            continue
        try:
            results.append(scan_library(session, config, library))
        except Exception as exc:  # noqa: BLE001 - one bad library must not stop the rest
            log.exception("Scan failed for library %s", library.name)
            session.rollback()
            results.append(
                {
                    "library": library.name,
                    "library_id": library.id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "total": 0,
                }
            )
    bus.publish("library.scan_complete", {"results": results})
    return results


def pending_books(
    session: Session,
    library_id: str | None = None,
    author: str | None = None,
) -> list[Book]:
    query = select(Book).where(
        Book.status.in_([BookStatus.NEW.value, BookStatus.PARTIAL.value]),
        Book.monitored.is_(True),
    )
    if library_id:
        query = query.where(Book.library_id == library_id)
    if author is not None:
        # Exact match, not a search. This feeds "clean everything by this
        # author", where matching loosely would quietly queue somebody else's
        # books -- hours of CPU spent on a library the person did not choose.
        query = query.where(Book.author == author)
    return list(session.execute(query.order_by(Book.author, Book.title)).scalars())


def library_stats(session: Session) -> dict:
    from sqlalchemy import func

    rows = session.execute(
        select(Book.status, func.count(Book.id)).group_by(Book.status)
    ).all()
    by_status = {status: count for status, count in rows}
    totals = session.execute(
        select(
            func.count(Book.id),
            func.coalesce(func.sum(Book.total_bytes), 0),
            func.coalesce(func.sum(Book.match_count), 0),
            func.coalesce(func.sum(Book.muted_seconds), 0.0),
        )
    ).one()
    return {
        "by_status": by_status,
        "total_books": totals[0] or 0,
        "total_bytes": int(totals[1] or 0),
        "total_matches": int(totals[2] or 0),
        "total_muted_seconds": float(totals[3] or 0.0),
    }
