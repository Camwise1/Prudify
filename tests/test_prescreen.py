"""Forecasting from an ebook whether a book needs cleaning."""

from __future__ import annotations

import pytest
from prudify.db import init_db, session_scope
from prudify.models import Book
from prudify.services import prescreen

from .test_ebooks import make_epub

# Enough words to pass for a one-hour reading.
_ONE_HOUR = "<p>" + "a quiet sentence " * 3200 + "</p>"


@pytest.fixture
def library(config, tmp_path):
    init_db(config.database_path())
    config.prescreen.ebook_paths = [str(tmp_path / "ebooks")]
    prescreen.forget_index()
    with session_scope() as session:
        session.add(Book(id="b1", library_id="lib", title="The Test Book", author="Jane Writer",
                         duration=3600.0))
        session.commit()
    yield config, tmp_path / "ebooks"
    prescreen.forget_index()


def _screen(config):
    with session_scope() as session:
        return prescreen.to_dict(prescreen.screen_book(session, config, session.get(Book, "b1")))


def test_a_clean_ebook_says_the_book_is_clean(library):
    config, shelf = library
    make_epub(shelf / "Jane Writer/book.epub", chapters=(_ONE_HOUR,))
    result = _screen(config)
    assert result["clean"] is True
    assert result["match_count"] == 0


def test_profanity_in_the_ebook_is_counted(library):
    config, shelf = library
    make_epub(shelf / "book.epub", chapters=(_ONE_HOUR + "<p>Well, fuck. Fucking hell.</p>",))
    result = _screen(config)
    assert result["clean"] is False
    assert result["match_count"] >= 2
    assert result["counts_by_word"]


def test_an_ebook_too_short_for_the_audio_is_not_trusted(library):
    """A sample chapter with no swearing says nothing about the whole book."""
    config, shelf = library
    make_epub(shelf / "book.epub", chapters=("<p>Just a sample chapter.</p>",))
    result = _screen(config)
    assert result["match_count"] == 0
    assert result["clean"] is False
    assert "fewer" in result["caveat"]


def test_a_drm_protected_ebook_is_not_trusted(library):
    config, shelf = library
    make_epub(shelf / "book.epub", chapters=(_ONE_HOUR,), encrypted=True)
    result = _screen(config)
    assert result["clean"] is False
    assert "DRM" in result["caveat"]


def test_no_matching_ebook_means_no_forecast(library):
    config, shelf = library
    make_epub(shelf / "other.epub", title="Another Book Entirely", chapters=(_ONE_HOUR,))
    assert _screen(config) is None


def test_the_result_is_reused_until_the_settings_change(library, monkeypatch):
    config, shelf = library
    make_epub(shelf / "book.epub", chapters=(_ONE_HOUR,))
    _screen(config)

    reads = []
    original = prescreen.ebooks.read_words
    monkeypatch.setattr(
        prescreen.ebooks, "read_words", lambda path: reads.append(path) or original(path)
    )
    _screen(config)
    assert reads == []

    config.filtering.pad_before_ms += 10
    _screen(config)
    assert len(reads) == 1
