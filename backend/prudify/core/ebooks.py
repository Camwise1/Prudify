"""Read ebooks, and find the one that matches an audiobook.

An audiobook is, give or take a narrator's ad-libs, its book read aloud. When
the ebook is on hand, running the wordlists over its text answers "will this
need cleaning at all?" in a second or two, where transcription takes hours.

Only EPUB is read: it is a zip of XHTML, which the standard library handles.
MOBI/AZW3 and the split RAR archives scene releases come in would need
external tools and are skipped.
"""

from __future__ import annotations

import difflib
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

EBOOK_EXTENSIONS = (".epub",)

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_WORD = re.compile(r"[\w*@$!#]+(?:['’][\w]+)*", re.UNICODE)
# Bracketed asides in audiobook names: [B002V1OIM2], (Unabridged), [Skyward 02].
_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# A series position tacked on the end: ", Book 7", " Volume Two". Only a
# numbered one, or one after a comma -- "The Book Thief" and "The Book of
# Mormon" are titles, not series markers.
_SERIES_TAIL = re.compile(
    r",\s*(book|volume|vol|part)\b.*$"
    r"|\s+(book|volume|vol|part)\s+(\d+|[ivxlc]+|one|two|three|four|five|six|seven|eight"
    r"|nine|ten|eleven|twelve|thirteen|fourteen|fifteen)\b.*$",
    re.IGNORECASE,
)
_ARTICLES = {"the", "a", "an"}
# Words rippers add to folder names that are not part of any title.
_NOISE = {"mp3", "mp3s", "m4b", "m4a", "multipart", "unabridged", "unabr", "audiobook"}


@dataclass
class Ebook:
    path: Path
    title: str
    authors: list[str] = field(default_factory=list)
    mtime: float = 0.0


class _TextCollector(HTMLParser):
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _opf_path(archive: zipfile.ZipFile) -> str | None:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    except (KeyError, ElementTree.ParseError):
        return None
    rootfile = container.find(".//c:rootfile", _NS)
    return rootfile.get("full-path") if rootfile is not None else None


def read_metadata(path: Path) -> Ebook:
    """Title and authors from the EPUB's package file, else from its filename."""
    title, authors = "", []
    try:
        with zipfile.ZipFile(path) as archive:
            opf = _opf_path(archive)
            if opf:
                package = ElementTree.fromstring(archive.read(opf))
                node = package.find(".//dc:title", _NS)
                title = (node.text or "").strip() if node is not None else ""
                authors = [
                    (n.text or "").strip()
                    for n in package.findall(".//dc:creator", _NS)
                    if (n.text or "").strip()
                ]
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        pass
    if not title:
        title = path.stem.replace(".", " ").replace("_", " ")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return Ebook(path=path, title=title, authors=authors, mtime=mtime)


class EbookUnreadable(Exception):
    """The file is an EPUB, but its text cannot be trusted or read."""


def read_words(path: Path) -> list[str]:
    """Every word of the book's text, in reading order.

    Raises :class:`EbookUnreadable` for DRM-protected books: their content
    documents are ciphertext, which reads as a few garbage "words" and no
    profanity -- exactly the answer that would wrongly mark a book clean.
    """
    with zipfile.ZipFile(path) as archive:
        if "META-INF/encryption.xml" in archive.namelist():
            encryption = archive.read("META-INF/encryption.xml").decode("utf-8", "replace")
            # Font obfuscation also lives here and is harmless; anything else
            # encrypted is the text itself.
            if re.search(r"<(?:\w+:)?CipherReference[^>]+URI=\"[^\"]+\.x?html?\"", encryption):
                raise EbookUnreadable("the ebook is DRM-protected")
        documents = _spine(archive)
        words: list[str] = []
        for name in documents:
            try:
                raw = archive.read(name).decode("utf-8", errors="replace")
            except KeyError:
                continue
            collector = _TextCollector()
            collector.feed(raw)
            words.extend(_WORD.findall(" ".join(collector.parts)))
    return words


def _spine(archive: zipfile.ZipFile) -> list[str]:
    """Content documents in reading order; every HTML file if that fails."""
    opf = _opf_path(archive)
    if opf:
        try:
            package = ElementTree.fromstring(archive.read(opf))
            base = posixpath.dirname(opf)
            manifest = {
                item.get("id"): posixpath.normpath(posixpath.join(base, item.get("href", "")))
                for item in package.findall(".//opf:manifest/opf:item", _NS)
            }
            order = [
                manifest[ref.get("idref")]
                for ref in package.findall(".//opf:spine/opf:itemref", _NS)
                if ref.get("idref") in manifest
            ]
            if order:
                return order
        except (KeyError, ElementTree.ParseError):
            pass
    return sorted(
        name for name in archive.namelist() if name.lower().endswith((".xhtml", ".html", ".htm"))
    )


def find_ebooks(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            parts = path.relative_to(root).parts
            if any(p.startswith((".", "@")) for p in parts):
                continue  # dotfiles and Synology's @eaDir metadata
            if path.suffix.lower() in EBOOK_EXTENSIONS and path.is_file():
                found.append(path)
    return sorted(found)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def core_title(title: str, author: str = "") -> str:
    """The part of a title two editions of one book will agree on.

    Audible names arrive as "The Dragon Reborn_ Book Three of The Wheel of
    Time [B002V1OIM2]" (":" becomes "_"), rips as "Brandon Sanderson -
    [Skyward 02] - Starsight (Unabr - 128k [2019])", and EPUB metadata as plain
    "The Dragon Reborn". What survives in all three is the main title: so drop
    bracketed asides, the author's name, the subtitle, and a leading article.
    """
    text = _BRACKETED.sub(" ", title)
    # Audible writes "Title: Subtitle" as "Title_ Subtitle"; a bare underscore
    # is a filename's stand-in for a space ("Dragonsteel_Prime_by_...").
    text = text.replace("_ ", ": ").replace("_", " ")
    for name in [author, *re.split(r",|&| and ", author)]:
        name = name.strip()
        if len(name) > 3:
            text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    pieces = re.split(r":| - |—|–", text.strip(" -"))
    main = next((piece for piece in pieces if _fold(piece)), "")
    main = _SERIES_TAIL.sub("", main)
    words = [w for w in _fold(main).split() if w not in _NOISE]
    if words and words[-1] == "by":
        words = words[:-1]
    while words and words[0] in _ARTICLES and len(words) > 1:
        words = words[1:]
    return " ".join(words)


def _surnames(author: str) -> set[str]:
    names = set()
    for person in re.split(r",|&|;| and ", author):
        tokens = _fold(person).split()
        if tokens:
            names.add(tokens[-1])
    return names


def best_match(title: str, author: str, ebooks: list[Ebook]) -> Ebook | None:
    """The ebook of this audiobook, or None when no candidate is convincing.

    Errs towards None: the cost of a wrong match is a book copied across
    uncleaned, while the cost of a missed one is only a transcription.
    """
    wanted = core_title(title, author)
    if not wanted:
        return None
    surnames = _surnames(author)
    best: tuple[float, Ebook] | None = None
    for ebook in ebooks:
        ebook_authors = ", ".join(ebook.authors)
        if surnames and ebook.authors and not (surnames & _surnames(ebook_authors)):
            continue
        if surnames and not ebook.authors and not any(
            s in _fold(str(ebook.path)) for s in surnames
        ):
            continue
        candidate = core_title(ebook.title, ebook_authors)
        if not candidate:
            continue
        score = difflib.SequenceMatcher(None, wanted, candidate).ratio()
        if best is None or score > best[0]:
            best = (score, ebook)
    if best and best[0] >= 0.9:
        return best[1]
    return None
