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
        "Samples/Preview/Preview.m4b",
        # Files named "sample" are skipped wherever they appear.
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


def test_natural_sort_mixes_numeric_and_text_names():
    """Regression: a folder holding both digit-led and letter-led names.

    The first implementation returned a bare int for digit runs and a bare
    str otherwise, so this raised
    "TypeError: '<' not supported between instances of 'int' and 'str'".
    Real libraries hit it constantly -- an "Intro.mp3" beside "01 - ...".
    """
    names = [
        "Intro.mp3",
        "01 - Chapter.mp3",
        "10 - Chapter.mp3",
        "2 - Chapter.mp3",
        "Outro.mp3",
        "Prologue.mp3",
    ]
    assert sorted(names, key=scanner.natural_key) == [
        "01 - Chapter.mp3",
        "2 - Chapter.mp3",
        "10 - Chapter.mp3",
        "Intro.mp3",
        "Outro.mp3",
        "Prologue.mp3",
    ]


def test_natural_sort_handles_awkward_names():
    """No comparison should ever raise, whatever the names look like."""
    names = [
        "", "1", "a", "1a", "a1", "1.mp3", ".mp3", "007 Bond.m4b",
        "Book - 2 - Part 10.m4b", "Book - 2 - Part 9.m4b", "ÜBER.mp3",
    ]
    assert len(sorted(names, key=scanner.natural_key)) == len(names)
    ordered = sorted(["Book - 2 - Part 10.m4b", "Book - 2 - Part 9.m4b"], key=scanner.natural_key)
    assert ordered == ["Book - 2 - Part 9.m4b", "Book - 2 - Part 10.m4b"]


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
    # part.path is a Path, so compare suffixes rather than calling endswith.
    assert all(part.path.suffix != ".jpg" for part in books["Book 1"].parts)


def test_sample_files_are_ignored(library):
    books = {book.title: book for book in scanner.scan_library(library)}
    names = {part.path.name for part in books["Preview"].parts}
    assert "sample.mp3" not in names
    assert names == {"Preview.m4b"}


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


def test_flat_author_shelf_splits_into_separate_books(tmp_path):
    """Regression: Author/*.m4b is many books, not one many-part book.

    A real library had 32 loose M4Bs in one author folder. The scanner
    reported a single 30 GB "book" with 32 parts, and cleaning that would
    have concatenated unrelated novels into one output file.
    """
    source = tmp_path / "src"
    for name in ["Aftermath.m4b", "Columbus Day.m4b", "SpecOps.m4b"]:
        path = source / "Craig Alanson" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 2048)

    library = LibrarySettings(
        name="t", source_path=str(source), output_path=str(tmp_path / "clean")
    )
    books = list(scanner.scan_library(library))

    assert len(books) == 3
    assert {b.title for b in books} == {"Aftermath", "Columbus Day", "SpecOps"}
    assert all(b.part_count == 1 for b in books)
    assert all(b.author == "Craig Alanson" for b in books)
    # Distinct keys, or they overwrite one another in the database.
    assert len({b.key for b in books}) == 3


def test_genuine_multipart_book_stays_whole(tmp_path):
    source = tmp_path / "src"
    for name in ["Kings - Part 1.m4b", "Kings - Part 2.m4b", "Kings - Part 3.m4b"]:
        path = source / "Brandon Sanderson" / "The Way of Kings" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 2048)

    library = LibrarySettings(
        name="t", source_path=str(source), output_path=str(tmp_path / "clean")
    )
    books = list(scanner.scan_library(library))
    assert len(books) == 1
    assert books[0].part_count == 3


def test_multipart_detection():
    assert scanner._looks_multipart(["Book - Part 1", "Book - Part 2"])
    assert scanner._looks_multipart(["Snow Crash CD01", "Snow Crash CD02"])
    assert scanner._looks_multipart(["01", "02", "03"])
    assert scanner._looks_multipart(["Title 1 of 3", "Title 2 of 3"])
    # Distinct titles are distinct books.
    assert not scanner._looks_multipart(["Aftermath", "Columbus Day"])
    assert not scanner._looks_multipart(["Book One", "Something Else"])


class TestEpisodicLayout:
    """Podcasts: a folder is a show, and every file stands on its own."""

    @pytest.fixture
    def show(self, tmp_path):
        folder = tmp_path / "src" / "Podcasts" / "Hardcore History"
        folder.mkdir(parents=True)
        for name in (
            "Blueprint for Armageddon I.mp3",
            "Blueprint for Armageddon II.mp3",
            "Prophets of Doom.mp3",
        ):
            (folder / name).write_bytes(b"\0" * 128)
        return tmp_path / "src"

    def _library(self, source, tmp_path, layout):
        return LibrarySettings(
            name="pods",
            source_path=str(source),
            output_path=str(tmp_path / "clean"),
            layout=layout,
        )

    def test_every_episode_is_its_own_item(self, show, tmp_path):
        books = list(scanner.scan_library(self._library(show, tmp_path, "episodes")))
        assert len(books) == 3
        assert all(book.part_count == 1 for book in books)
        assert {b.title for b in books} == {
            "Blueprint for Armageddon I",
            "Blueprint for Armageddon II",
            "Prophets of Doom",
        }

    def test_the_show_is_the_author(self, show, tmp_path):
        books = list(scanner.scan_library(self._library(show, tmp_path, "episodes")))
        assert {b.author for b in books} == {"Hardcore History"}

    def test_each_episode_gets_a_distinct_key(self, show, tmp_path):
        books = list(scanner.scan_library(self._library(show, tmp_path, "episodes")))
        assert len({b.key for b in books}) == 3

    def test_a_new_episode_does_not_disturb_the_others(self, show, tmp_path):
        library = self._library(show, tmp_path, "episodes")
        before = {b.title: b.key for b in scanner.scan_library(library)}
        (show / "Podcasts" / "Hardcore History" / "Supernova in the East I.mp3").write_bytes(
            b"\0" * 128
        )
        after = {b.title: b.key for b in scanner.scan_library(library)}
        assert len(after) == 4
        assert all(after[title] == key for title, key in before.items())

    def test_the_same_folder_as_books_is_one_multipart_item(self, show, tmp_path):
        """The default is unchanged: differently-titled MP3s stay one work."""
        books = list(scanner.scan_library(self._library(show, tmp_path, "books")))
        assert len(books) == 1
        assert books[0].part_count == 3

