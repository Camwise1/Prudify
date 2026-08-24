"""Library discovery: folder layouts, multi-part books, exclusions."""

from __future__ import annotations

from pathlib import Path

import pytest

from prudify.config import LibrarySettings
from prudify.core import scanner


@pytest.fixture
def library(tmp_path) -> LibrarySettings:
    source = tmp_path / "audiobooks"
    for relative in [
        "Craig Alanson/Dead World/Dead World.m4b",
        "Craig Alanson/Columbus Day/01 - Columbus Day.mp3",
        "Craig Alanson/Columbus Day/02 - Columbus Day.mp3",
        "Craig Alanson/Columbus Day/10 - Columbus Day.mp3",
        "Matt Dinniman/Dungeon Crawler Carl/Book 1/Carl.m4b",
        "Matt Dinniman/Dungeon Crawler Carl/cover.jpg",
        "Samples/Preview/sample.mp3",
        "Loose Book.m4b",
    ]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 2048)
    return LibrarySettings(
        name="test", source_path=str(source), output_path=str(tmp_path / "clean")
    )


def test_natural_sort_orders_numerically():
    names = ["10 - part.mp3", "2 - part.mp3", "1 - part.mp3"]
    assert sorted(names, key=scanner.natural_key) == [
        "1 - part.mp3", "2 - part.mp3", "10 - part.mp3"
    ]


def test_finds_every_folder_containing_audio(library):
    books = {book.title: book for book in scanner.scan_library(library)}
    assert set(books) == {"Dead World", "Columbus Day", "Book 1", "Preview", "Loose Book"}


def test_multipart_books_stay_together(library):
    books = {book.title: book for book in scanner.scan_library(library)}
    columbus = books["Columbus Day"]
    assert columbus.part_count == 3
    assert [Path(part.path).name for part in columbus.parts] == [
        "01 - Columbus Day.mp3", "02 - Columbus Day.mp3", "10 - Columbus Day.mp3"
    ]


def test_non_audio_files_are_ignored(library):
    books = {book.title: book for book in scanner.scan_library(library)}
    assert all(not part.path.endswith(".jpg") for part in books["Book 1"].parts)


def test_author_inferred_from_path(library):
    books = {book.title: book for book in scanner.scan_library(library)}
    assert books["Dead World"].author == "Craig Alanson"
    assert books["Book 1"].relative_folder == "Matt Dinniman/Dungeon Crawler Carl/Book 1"


def test_exclude_patterns(library):
    library.exclude_patterns = ["Samples/*"]
    titles = {book.title for book in scanner.scan_library(library)}
    assert "Preview" not in titles


def test_extension_filter(library):
    library.extensions = ["m4b"]  # deliberately un-normalised

    titles = {book.title for book in scanner.scan_library(library)}
    assert "Columbus Day" not in titles
    assert "Dead World" in titles


def test_keys_are_stable_across_scans(library):
    first = {book.key for book in scanner.scan_library(library)}
    second = {book.key for book in scanner.scan_library(library)}
    assert first == second


def test_output_tree_is_never_scanned(tmp_path):
    """A clean folder nested inside the source must not be picked up as input."""
    source = tmp_path / "books"
    (source / "Author/Book").mkdir(parents=True)
    (source / "Author/Book/Book.m4b").write_bytes(b"\0" * 10)
    output = source / "clean"
    (output / "Author/Book").mkdir(parents=True)
    (output / "Author/Book/Book.m4b").write_bytes(b"\0" * 10)

    library = LibrarySettings(
        name="nested", source_path=str(source), output_path=str(output)
    )
    folders = {book.relative_folder for book in scanner.scan_library(library)}
    assert folders == {"Author/Book"}


def test_output_path_mirrors_source(library):
    destination = scanner.output_path_for(library, "Craig Alanson/Dead World/Dead World.m4b")
    assert destination == Path(library.output_path) / "Craig Alanson/Dead World/Dead World.m4b"


def test_output_path_can_change_container(library):
    destination = scanner.output_path_for(
        library, "Craig Alanson/Dead World/Dead World.m4b", container="m4a"
    )
    assert destination.suffix == ".m4a"


def test_missing_source_yields_nothing(tmp_path):
    library = LibrarySettings(
        name="gone", source_path=str(tmp_path / "nope"), output_path=str(tmp_path / "out")
    )
    assert scanner.scan_library(library) == []
