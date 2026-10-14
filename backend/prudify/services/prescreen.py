"""Forecast whether a book needs cleaning, from its ebook, before transcribing it.

The ebook is matched by title and author, read, and run through the same
wordlists and matcher the transcript would be. Zero matches in a trustworthy
ebook means the audiobook can be copied across unchanged; any match means it
still needs transcribing, because only the audio says *where* the words are.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Config
from ..core import ebooks
from ..core import matcher as matcher_mod
from ..core.reachability import check_dir
from ..core.transcribe import Word
from ..models import Book, Prescreen, utcnow

log = logging.getLogger(__name__)

# Narration runs at roughly 150 words a minute. An ebook with under half the
# words that implies is a sample, an excerpt or the wrong book, and saying
# "no profanity" on the strength of it would be a guess.
_WORDS_PER_HOUR = 9000
_MIN_COVERAGE = 0.5
_INDEX_TTL_SECONDS = 600.0

_index_lock = threading.Lock()
_index: dict[str, object] = {"key": None, "built": 0.0, "books": []}
_metadata: dict[tuple[str, float], ebooks.Ebook] = {}


def enabled(config: Config) -> bool:
    return bool(config.prescreen.ebook_paths)


def ebook_index(config: Config) -> list[ebooks.Ebook]:
    """Every EPUB under the configured folders, rebuilt every ten minutes.

    Metadata is kept per (path, mtime), so a rebuild only opens new or
    changed files -- a walk of the folders over the network, not a read of
    every book.
    """
    roots = [Path(p).expanduser() for p in config.prescreen.ebook_paths]
    key = "|".join(str(r) for r in roots)
    with _index_lock:
        if _index["key"] == key and time.monotonic() - _index["built"] < _INDEX_TTL_SECONDS:
            return list(_index["books"])
        reachable = [r for r in roots if check_dir(r) is None]
        found = []
        for path in ebooks.find_ebooks(reachable):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            meta = _metadata.get((str(path), mtime))
            if meta is None:
                meta = ebooks.read_metadata(path)
                _metadata[(str(path), mtime)] = meta
            found.append(meta)
        _index.update(key=key, built=time.monotonic(), books=found)
        log.info("Ebook index: %d EPUB(s) under %d folder(s)", len(found), len(reachable))
        return list(found)


def forget_index() -> None:
    with _index_lock:
        _index.update(key=None, built=0.0, books=[])


def _settings_key(config: Config) -> str:
    """Changes whenever the filter settings or a user wordlist does."""
    parts = [config.filtering.model_dump_json()]
    user_dir = config.resolved_data_dir() / "wordlists"
    if user_dir.is_dir():
        for path in sorted(user_dir.iterdir()):
            try:
                parts.append(f"{path.name}:{path.stat().st_mtime}")
            except OSError:
                continue
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def is_clean(record: Prescreen | None) -> bool:
    """True only for a usable ebook with no matches at all."""
    return bool(record and record.ebook_path and not record.caveat and record.match_count == 0)


def screen_book(
    session: Session, config: Config, book: Book, force: bool = False
) -> Prescreen | None:
    """Screen ``book`` against its ebook, reusing a still-valid earlier result.

    Returns None when screening is off or no ebook matches the book.
    """
    if not enabled(config):
        return None
    match = ebooks.best_match(book.title, book.author, ebook_index(config))
    record = session.get(Prescreen, book.id)
    if match is None:
        if record is not None:
            session.delete(record)
            session.commit()
        return None

    key = _settings_key(config)
    if (
        record is not None
        and not force
        and record.ebook_path == str(match.path)
        and record.ebook_mtime == match.mtime
        and record.settings_key == key
    ):
        return record

    if record is None:
        record = Prescreen(book_id=book.id)
        session.add(record)
    record.ebook_path = str(match.path)
    record.ebook_title = match.title
    record.ebook_mtime = match.mtime
    record.settings_key = key
    record.checked_at = utcnow()
    record.caveat = ""
    record.counts_by_word = {}
    record.match_count = 0
    record.word_count = 0

    try:
        words = ebooks.read_words(match.path)
    except ebooks.EbookUnreadable as exc:
        record.caveat = str(exc)
    except Exception as exc:  # a corrupt zip, a share that went away
        record.caveat = f"could not read the ebook: {exc}"
    else:
        matcher = matcher_mod.build_matcher_from_settings(
            config.filtering, user_dir=config.resolved_data_dir() / "wordlists"
        )
        found = matcher.find([Word(i, i + 0.5, text) for i, text in enumerate(words)])
        counts: dict[str, int] = {}
        for hit in found:
            word = matcher_mod.normalize(hit.text) or hit.text
            counts[word] = counts.get(word, 0) + 1
        record.word_count = len(words)
        record.match_count = len(found)
        record.counts_by_word = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        expected = max(0.0, book.duration or 0.0) / 3600 * _WORDS_PER_HOUR
        if not expected:
            record.caveat = "the audiobook's length is unknown, so the ebook cannot be checked"
        elif len(words) < expected * _MIN_COVERAGE:
            record.caveat = (
                f"the ebook has {len(words):,} words, far fewer than a "
                f"{book.duration / 3600:.1f}-hour reading implies"
            )

    session.commit()
    log.info(
        "Ebook check for %s: %d match(es) in %d words%s",
        book.title,
        record.match_count,
        record.word_count,
        f" ({record.caveat})" if record.caveat else "",
    )
    return record


def to_dict(record: Prescreen | None) -> dict | None:
    if record is None:
        return None
    return {
        "ebook_path": record.ebook_path,
        "ebook_title": record.ebook_title,
        "word_count": record.word_count,
        "match_count": record.match_count,
        "counts_by_word": record.counts_by_word or {},
        "caveat": record.caveat,
        "checked_at": record.checked_at,
        "clean": is_clean(record),
    }
