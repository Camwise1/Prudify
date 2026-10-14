"""Reading EPUBs and pairing them with audiobooks."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from prudify.core import ebooks
from prudify.core.ebooks import Ebook, best_match, core_title


def make_epub(
    path: Path,
    title: str = "The Test Book",
    author: str = "Jane Writer",
    chapters: tuple[str, ...] = ("<p>Once upon a time.</p>",),
    encrypted: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = "".join(
        f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapters))
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>'
            f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>"
            f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>",
        )
        for i, body in enumerate(chapters):
            archive.writestr(
                f"OEBPS/text/c{i}.xhtml",
                f"<html><head><style>p {{}}</style></head><body>{body}</body></html>",
            )
        if encrypted:
            archive.writestr(
                "META-INF/encryption.xml",
                '<encryption><EncryptedData><CipherData>'
                '<CipherReference URI="OEBPS/text/c0.xhtml"/></CipherData>'
                "</EncryptedData></encryption>",
            )
    return path


def test_reads_title_author_and_text_in_spine_order(tmp_path):
    path = make_epub(
        tmp_path / "x.epub", chapters=("<p>First words.</p>", "<p>Then the rest, don't stop.</p>")
    )
    meta = ebooks.read_metadata(path)
    assert (meta.title, meta.authors) == ("The Test Book", ["Jane Writer"])
    assert ebooks.read_words(path) == ["First", "words", "Then", "the", "rest", "don't", "stop"]


def test_drm_protected_text_is_refused_not_read_as_clean(tmp_path):
    path = make_epub(tmp_path / "drm.epub", encrypted=True)
    with pytest.raises(ebooks.EbookUnreadable):
        ebooks.read_words(path)


def test_synology_metadata_folders_are_ignored(tmp_path):
    make_epub(tmp_path / "Author/Book/book.epub")
    make_epub(tmp_path / "Author/Book/@eaDir/book.epub")
    assert [p.name for p in ebooks.find_ebooks([tmp_path])] == ["book.epub"]


@pytest.mark.parametrize(
    ("title", "author", "expected"),
    [
        ("The Dragon Reborn_ Book Three of The Wheel of Time [B002V1OIM2]", "Robert Jordan",
         "dragon reborn"),
        ("Brandon Sanderson - [Skyward 02] - Starsight (Unabr - 128k [2019])",
         "Brandon Sanderson", "starsight"),
        ("Dragonsteel_Prime_by_Brandon_Sanderson", "Brandon Sanderson", "dragonsteel prime"),
        ("Tress of the Emerald Sea - Multipart MP3", "Brandon Sanderson",
         "tress of the emerald sea"),
        ("The Bands of Mourning: A Mistborn Novel", "Brandon Sanderson", "bands of mourning"),
        ("Castle of Wizardry_ The Belgariad, Book 4 [B002V1O07U]", "David Eddings",
         "castle of wizardry"),
        ("Harry Potter and the Deathly Hallows, Book 7 [B017WJ5ZK6]", "J.K. Rowling",
         "harry potter and the deathly hallows"),
        ("The Book Thief", "Markus Zusak", "book thief"),
        ("The Book of Mormon_ Another Testament of Jesus Christ [B07K6YGCMR]", "",
         "book of mormon"),
        ("Book", "Author", "book"),
    ],
)
def test_core_title_finds_what_editions_agree_on(title, author, expected):
    assert core_title(title, author) == expected


def _ebook(title, *authors):
    return Ebook(path=Path(f"/books/{title}.epub"), title=title, authors=list(authors))


def test_pairs_an_audible_title_with_its_ebook():
    shelf = [_ebook("Skyward", "Brandon Sanderson"), _ebook("Starsight", "Brandon Sanderson")]
    found = best_match("Starsight [B07VYMVXK6]", "Brandon Sanderson", shelf)
    assert found is shelf[1]


def test_author_order_and_co_authors_do_not_matter():
    shelf = [_ebook("Pawn Of Prophecy", "Eddings, David")]
    assert best_match("Pawn of Prophecy_ The Belgariad, Book 1", "David Eddings", shelf)


def test_the_same_title_by_someone_else_is_not_a_match():
    """A wrong pairing would copy a book across uncleaned; refuse it."""
    shelf = [_ebook("Fallen Angel", "Margaret Weis")]
    assert best_match("Fallen Angel", "Some Other Author", shelf) is None


def test_a_merely_similar_title_is_not_a_match():
    shelf = [_ebook("Skyward Flight", "Brandon Sanderson")]
    assert best_match("Skyward [B07H7Q5D3M]", "Brandon Sanderson", shelf) is None
