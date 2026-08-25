"""Library browsing and per-book actions."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..config import Config
from ..core import audio as audio_mod
from ..db import db_session, session_scope
from ..models import Book, BookStatus, Part
from ..schemas import BookDetail, BookOut, BookPage, PartOut
from ..services import library as library_service
from ..services.queue import get_queue
from .deps import get_config

router = APIRouter(prefix="/books", tags=["books"])


@router.get("", response_model=BookPage)
def list_books(
    session: Session = Depends(db_session),
    q: str | None = Query(default=None, description="Search title or author"),
    status: str | None = Query(default=None),
    author: str | None = Query(default=None, description="Exact author match"),
    library_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    sort: str = Query(default="author"),
    order: str = Query(default="asc"),
) -> BookPage:
    query = select(Book)
    count_query = select(func.count(Book.id))

    filters = []
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(or_(Book.title.ilike(pattern), Book.author.ilike(pattern)))
    if status:
        filters.append(Book.status.in_(status.split(",")))
    if author:
        filters.append(Book.author == author)
    if library_id:
        filters.append(Book.library_id == library_id)
    for condition in filters:
        query = query.where(condition)
        count_query = count_query.where(condition)

    sort_column = {
        "author": Book.author,
        "title": Book.title,
        "status": Book.status,
        "size": Book.total_bytes,
        "matches": Book.match_count,
        "added": Book.first_seen,
        "cleaned": Book.cleaned_at,
    }.get(sort, Book.author)
    query = query.order_by(
        sort_column.desc() if order == "desc" else sort_column.asc(), Book.title.asc()
    )

    total = session.execute(count_query).scalar_one()
    rows = session.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return BookPage(
        items=[BookOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/authors")
def list_authors(session: Session = Depends(db_session)) -> list[dict]:
    """Every author with enough detail to decide what to do about them.

    A library is not a flat list of books to its owner; it is a list of
    authors, some of whom need cleaning and some of whom do not. The counts
    are what make that decision without opening anything: how many books,
    how many are done, and how many are still waiting.
    """
    rows = session.execute(
        select(
            Book.author,
            func.count(Book.id),
            func.sum(case((Book.status == BookStatus.CLEANED.value, 1), else_=0)),
            func.sum(
                case(
                    (
                        and_(
                            Book.status.in_(
                                [BookStatus.NEW.value, BookStatus.PARTIAL.value]
                            ),
                            Book.monitored.is_(True),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.coalesce(func.sum(Book.total_bytes), 0),
            func.coalesce(func.sum(Book.match_count), 0),
        )
        .group_by(Book.author)
        .order_by(Book.author)
    ).all()
    return [
        {
            "author": author or "Unknown",
            "count": count,
            "cleaned": int(cleaned or 0),
            "pending": int(pending or 0),
            "total_bytes": int(total_bytes or 0),
            "match_count": int(matches or 0),
        }
        for author, count, cleaned, pending, total_bytes, matches in rows
    ]


@router.get("/{book_id}", response_model=BookDetail)
def get_book(book_id: str, session: Session = Depends(db_session)) -> BookDetail:
    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    detail = BookDetail.model_validate(book)
    detail.parts = [PartOut.model_validate(part) for part in book.parts]
    return detail


# A 1x1 transparent GIF. Returned instead of a 404 for a book with no
# artwork, so the browser caches the "nothing here" answer rather than
# re-asking on every scroll, and the console stays free of red.
_BLANK_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000"
    "010001000002024401003b"
)

# A library page asks for every cover on screen at once, and FastAPI runs sync
# endpoints on a forty-thread pool -- so without a cap, opening the page starts
# forty ffmpeg processes against a network share simultaneously. Two at a time
# fills the cache just as quickly in practice and leaves the machine usable.
_EXTRACTION_SLOTS = threading.Semaphore(2)


@router.get("/{book_id}/cover")
def get_book_cover(
    book_id: str,
    session: Session = Depends(db_session),
    config: Config = Depends(get_config),
) -> Response:
    """The book's embedded artwork, extracted once and then served from cache.

    Extraction is lazy rather than part of scanning. A scan touches every book
    in the library, and one ffmpeg per book across a network share turns a
    fast operation into a slow one -- to produce thumbnails for the handful of
    books actually on screen. Doing it on first request spends that cost only
    where someone is looking.
    """
    cached = config.cover_dir() / f"{book_id}.jpg"
    if cached.exists():
        return FileResponse(cached, media_type="image/jpeg")

    # Both cache answers are checked before the database is touched at all, so
    # a settled library serves its artwork without a single query.
    missing = config.cover_dir() / f"{book_id}.none"
    if missing.exists():
        return Response(content=_BLANK_GIF, media_type="image/gif")

    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    sources = [Path(part.path) for part in book.parts]

    # Let go of the transaction before running ffmpeg. Every session here
    # opens with BEGIN IMMEDIATE, which takes SQLite's *write* lock even for
    # a read -- so holding one across a subprocess that probes a multi-
    # gigabyte file on a network share stops every other query in the
    # process, including the other fifty cover requests the same page just
    # issued. That is what turned opening the library into a wall of
    # "database is locked".
    session.rollback()

    with _EXTRACTION_SLOTS:
        # Another request may have extracted it while we waited for a slot.
        if cached.exists():
            return FileResponse(cached, media_type="image/jpeg")
        for source in sources:
            if source.exists() and audio_mod.extract_cover(source, cached):
                return FileResponse(cached, media_type="image/jpeg")

    # Remember the absence too, or every page view re-probes a file that has
    # no artwork and never will.
    missing.parent.mkdir(parents=True, exist_ok=True)
    missing.touch()
    return Response(content=_BLANK_GIF, media_type="image/gif")


@router.get("/{book_id}/matches")
def get_book_matches(
    book_id: str,
    session: Session = Depends(db_session),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    """Every detected instance, in timeline order, for the book detail view."""
    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    entries: list[dict] = []
    offset = 0.0
    for part in sorted(book.parts, key=lambda p: p.relative_path):
        for match in (part.matches or [])[:limit]:
            entries.append(
                {
                    **match,
                    "part": part.relative_path,
                    "absolute_start": match.get("start", 0.0) + offset,
                }
            )
        offset += part.duration or 0.0

    entries.sort(key=lambda entry: entry["absolute_start"])
    return {
        "book_id": book_id,
        "total": len(entries),
        "duration": offset,
        "counts_by_word": book.word_counts or {},
        "matches": entries[:limit],
    }


@router.post("/{book_id}/queue")
def queue_book(
    book_id: str,
    priority: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(db_session),
) -> dict:
    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    job = get_queue().enqueue_book(session, book, priority=priority)
    return {"queued": True, "job_id": job.id if job else None}


@router.post("/queue-all")
def queue_all(
    library_id: str | None = Query(default=None),
    author: str | None = Query(default=None, description="Only this author's books"),
    session: Session = Depends(db_session),
) -> dict:
    books = library_service.pending_books(session, library_id, author)
    queue = get_queue()
    count = 0
    for book in books:
        if queue.enqueue_book(session, book):
            count += 1
    return {"queued": count}


@router.post("/{book_id}/monitor")
def set_monitored(book_id: str, monitored: bool, session: Session = Depends(db_session)) -> dict:
    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    book.monitored = monitored
    if not monitored and book.status in (BookStatus.NEW.value, BookStatus.PARTIAL.value):
        book.status = BookStatus.IGNORED.value
    elif monitored and book.status == BookStatus.IGNORED.value:
        book.status = BookStatus.NEW.value
    session.commit()
    return {"id": book_id, "monitored": monitored, "status": book.status}


@router.post("/{book_id}/reset")
def reset_book(
    book_id: str,
    delete_output: bool = Query(default=False),
    delete_transcript: bool = Query(default=False),
    session: Session = Depends(db_session),
    config: Config = Depends(get_config),
) -> dict:
    """Forget results for a book so it can be processed again.

    Only ever deletes files inside the *clean output* tree and the transcript
    cache. Source audio is never touched.
    """
    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    library = config.library_by_id(book.library_id)
    output_root = Path(library.output_path).expanduser().resolve() if library else None
    removed = 0

    for part in book.parts:
        if delete_output and part.destination and output_root:
            destination = Path(part.destination)
            try:
                resolved = destination.resolve()
            except OSError:
                continue
            # Guard rail: refuse to unlink anything outside the output tree.
            if output_root in resolved.parents and resolved.exists():
                resolved.unlink()
                removed += 1
        if delete_transcript and part.transcript_path:
            Path(part.transcript_path).unlink(missing_ok=True)
        part.status = BookStatus.NEW.value
        part.match_count = 0
        part.muted_seconds = 0.0
        part.matches = []
        part.error = ""
        part.cleaned_at = None

    book.status = BookStatus.NEW.value
    book.match_count = 0
    book.muted_seconds = 0.0
    book.word_counts = {}
    book.error = ""
    book.cleaned_at = None
    session.commit()
    return {"id": book_id, "reset": True, "files_removed": removed}


@router.delete("/{book_id}")
def forget_book(book_id: str, session: Session = Depends(db_session)) -> dict:
    """Remove a book from the database only. Files on disk are untouched."""
    book = session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    session.delete(book)
    session.commit()
    return {"id": book_id, "deleted": True}


@router.get("/{book_id}/parts/{part_id}", response_model=PartOut)
def get_part(book_id: str, part_id: int, session: Session = Depends(db_session)) -> PartOut:
    part = session.get(Part, part_id)
    if part is None or part.book_id != book_id:
        raise HTTPException(status_code=404, detail="Part not found")
    return PartOut.model_validate(part)


@router.get("/{book_id}/parts/{part_id}/transcript")
def get_transcript(book_id: str, part_id: int, session: Session = Depends(db_session)) -> dict:
    part = session.get(Part, part_id)
    if part is None or part.book_id != book_id:
        raise HTTPException(status_code=404, detail="Part not found")
    path = Path(part.transcript_path) if part.transcript_path else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="No cached transcript for this part")
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    return {"part_id": part_id, "word_count": len(data.get("words", [])), **data}


@router.post("/scan")
def trigger_scan(config: Config = Depends(get_config)) -> dict:
    with session_scope() as session:
        results = library_service.scan_all(session, config)
    return {"results": results}


@router.get("/stats/summary")
def stats_summary(session: Session = Depends(db_session)) -> dict:
    return library_service.library_stats(session)
