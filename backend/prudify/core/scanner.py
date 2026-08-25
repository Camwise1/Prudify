"""Library scanning and book discovery.

A *book* is a directory that directly contains one or more audio files. That
single rule covers every layout Prudify has to deal with:

    Author/Title/Title.m4b                  -> one book, one part
    Author/Series/Title/Title.m4b           -> one book, one part
    Author/Title/01 - Chapter.mp3, 02 ...   -> one book, N parts
    Loose/Title.m4b                         -> one book, one part

Multi-part books are kept together and processed part-by-part so a folder of
250 MP3s produces 250 cleaned MP3s in the mirrored output tree, rather than
being silently skipped the way the original PowerShell queue did.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from ..config import AUDIO_EXTENSIONS, LibrarySettings
from . import audio as audio_mod

_NUMERIC = re.compile(r"(\d+)")

# Files that live beside audiobooks and should never be treated as parts.
_IGNORED_NAMES = {"sample", "sample.mp3", "trailer"}

# M4B and M4A are chapterised containers: one file is normally one whole book.
# MP3 and friends are normally one chapter each, so several of them in a folder
# are parts of a single book.
_SELF_CONTAINED = {".m4b", ".m4a"}

# Trailing "part 3", "disc 2", "CD03", "Book 1 of 4", or a bare "04".
_PART_MARKER = re.compile(
    r"""[\s._\-]*
        (?:\b(?:part|pt|disc|disk|cd|vol|volume|book|chapter|ch|track|file)\b[\s._\-]*)?
        \d{1,3}
        (?:\s*of\s*\d{1,3})?
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _looks_multipart(stems: list[str]) -> bool:
    """True when every filename is one title plus a part or disc number.

    "Columbus Day - Part 1/2/3" collapses to a single base name, so it is one
    book in three files. Thirty-two differently-titled M4Bs do not collapse,
    so they are thirty-two books that happen to share a folder.
    """
    if len(stems) < 2:
        return False
    bases = {_PART_MARKER.sub("", stem).strip(" -_.").lower() for stem in stems}
    # One shared base means one book. Purely numeric names ("01", "02") all
    # reduce to "" -- also one book, split by track.
    return len(bases) == 1


def _group_into_books(audio_files: list[Path]) -> list[list[Path]]:
    """Split one folder's audio files into one list per distinct book.

    The old behaviour -- one folder is always one book -- silently merges a
    flat "Author/*.m4b" shelf into a single monstrous multi-part entry, and
    cleaning that would concatenate unrelated novels into one file.
    """
    if len(audio_files) <= 1:
        return [audio_files]
    if _looks_multipart([f.stem for f in audio_files]):
        return [audio_files]
    if all(f.suffix.lower() in _SELF_CONTAINED for f in audio_files):
        return [[f] for f in audio_files]
    # Mixed or chapterised formats: treat the folder as one book, as before.
    return [audio_files]


def natural_key(text: str) -> tuple:
    """Sort "Part 2" before "Part 10".

    Every element is the same shape -- ``(kind, number, text)`` -- so sorting
    never compares an int against a str. The naive version of this function
    emits a bare int for digit runs and a bare str for everything else, which
    raises TypeError the moment one name starts with a digit and another does
    not ("01 - Chapter.mp3" beside "Intro.mp3"). ``kind`` sorts numbers ahead
    of text at the same position.
    """
    key: list[tuple[int, int, str]] = []
    for part in _NUMERIC.split(text):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part.lower()))
    return tuple(key)


@dataclass(slots=True)
class BookPart:
    path: Path
    relative_path: str
    size_bytes: int = 0
    duration: float = 0.0
    extension: str = ""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "duration": self.duration,
            "extension": self.extension,
        }


@dataclass(slots=True)
class DiscoveredBook:
    library_id: str
    key: str
    folder: Path
    relative_folder: str
    title: str
    author: str = ""
    parts: list[BookPart] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def part_count(self) -> int:
        return len(self.parts)

    @property
    def formats(self) -> list[str]:
        return sorted({p.extension for p in self.parts})

    def to_dict(self) -> dict:
        return {
            "library_id": self.library_id,
            "key": self.key,
            "folder": str(self.folder),
            "relative_folder": self.relative_folder,
            "title": self.title,
            "author": self.author,
            "part_count": self.part_count,
            "formats": self.formats,
            "total_bytes": self.total_bytes,
            "parts": [p.to_dict() for p in self.parts],
        }


def book_key(library_id: str, relative_folder: str) -> str:
    """Stable identifier that survives restarts and rescans."""
    digest = hashlib.sha1(f"{library_id}:{relative_folder}".encode()).hexdigest()
    return digest[:16]


def _is_audio(path: Path, allowed: Iterable[str]) -> bool:
    ext = path.suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        return False
    # Accept "m4b" as well as ".m4b" -- the config validator normalises, but a
    # LibrarySettings built by hand in a test or script may not have.
    normalised = {e if e.startswith(".") else f".{e}" for e in (x.lower() for x in allowed)}
    if normalised and ext not in normalised:
        return False
    if path.name.startswith(("._", ".")):
        return False
    return path.stem.lower() not in _IGNORED_NAMES


def _excluded(relative: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch(relative, pattern) or fnmatch(relative, f"*/{pattern}")
        for pattern in patterns
    )


def scan_library(library: LibrarySettings) -> list[DiscoveredBook]:
    return list(iter_library(library))


def iter_library(library: LibrarySettings) -> Iterator[DiscoveredBook]:
    """Walk a library root and yield one :class:`DiscoveredBook` per folder."""
    root = Path(library.source_path).expanduser()
    if not root.is_dir():
        return

    output_root = Path(library.output_path).expanduser().resolve()
    root_resolved = root.resolve()

    for folder, dirnames, filenames in _walk(root):
        # Never descend into the clean output tree if it lives inside the source.
        dirnames[:] = [
            d for d in dirnames if (folder / d).resolve() != output_root and not d.startswith(".")
        ]

        audio_files = sorted(
            (folder / name for name in filenames if _is_audio(folder / name, library.extensions)),
            key=lambda p: natural_key(p.name),
        )
        if not audio_files:
            continue

        try:
            relative_folder = folder.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            relative_folder = folder.name
        if relative_folder == ".":
            relative_folder = ""

        if _excluded(relative_folder, library.exclude_patterns):
            continue

        groups = _group_into_books(audio_files)
        # When one folder yields several books, each must be identified by its
        # own file -- otherwise they all hash to the same key and overwrite one
        # another in the database.
        split_folder = len(groups) > 1

        for group in groups:
            parts: list[BookPart] = []
            total = 0
            for file in group:
                try:
                    size = file.stat().st_size
                except OSError:
                    continue
                total += size
                rel = f"{relative_folder}/{file.name}" if relative_folder else file.name
                parts.append(
                    BookPart(
                        path=file,
                        relative_path=rel,
                        size_bytes=size,
                        extension=file.suffix.lower(),
                    )
                )

            if not parts:
                continue

            if split_folder:
                identity = (
                    f"{relative_folder}/{group[0].name}" if relative_folder else group[0].name
                )
            else:
                identity = relative_folder or folder.name

            title, author = _infer_title_author(
                folder, relative_folder, parts, standalone=split_folder
            )
            yield DiscoveredBook(
                library_id=library.id,
                key=book_key(library.id, identity),
                folder=folder,
                relative_folder=relative_folder,
                title=title,
                author=author,
                parts=parts,
                total_bytes=total,
            )


def _walk(root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        yield Path(dirpath), dirnames, filenames


def _infer_title_author(
    folder: Path,
    relative_folder: str,
    parts: list[BookPart],
    standalone: bool = False,
) -> tuple[str, str]:
    """Prefer the Author/Title folder convention, fall back to embedded tags.

    ``standalone`` marks one of several books sharing a folder. There the
    folder names the *author*, never the book, so the title has to come from
    the filename or the file's own tags.
    """
    segments = [s for s in relative_folder.split("/") if s]

    if standalone:
        title = Path(parts[0].path).stem
        author = segments[0] if segments else ""
        return _prefer_tags(parts, title, author)

    if len(segments) >= 2:
        # Author/[Series/]Title -- the last segment is the book, the first the
        # author. Returned as (title, author).
        return segments[-1], segments[0]
    if len(segments) == 1:
        title, author = segments[0], ""
    else:
        # A loose file sitting at the library root: name it after the file.
        title, author = Path(parts[0].path).stem, ""

    return _prefer_tags(parts, title, author)


def _prefer_tags(parts: list[BookPart], title: str, author: str) -> tuple[str, str]:
    """Let the file's own tags improve on a path-derived guess.

    Only reached when the path did not tell us enough; one ffprobe per book is
    noticeable on a slow network share.
    """
    try:
        info = audio_mod.probe(parts[0].path)
    except Exception:
        return title, author

    tags = info.tags
    tag_title = tags.get("album") or tags.get("title")
    tag_author = tags.get("artist") or tags.get("album_artist") or tags.get("author")
    if tag_title:
        title = tag_title.strip() or title
    if tag_author:
        author = tag_author.strip() or author
    return title, author


def output_path_for(
    library: LibrarySettings, part_relative_path: str, container: str = "same"
) -> Path:
    """Where a cleaned part is written, mirroring the source tree."""
    out_root = Path(library.output_path).expanduser()
    destination = out_root / part_relative_path
    if container and container != "same":
        destination = destination.with_suffix(f".{container.lstrip('.')}")
    return destination


def enrich_durations(book: DiscoveredBook) -> DiscoveredBook:
    """Fill in per-part durations. Costs one ffprobe per file, so it is opt-in."""
    for part in book.parts:
        if part.duration:
            continue
        try:
            part.duration = audio_mod.probe(part.path).duration
        except Exception:
            part.duration = 0.0
    return book
