"""Wordlist parsing and profanity matching over word-level timestamps.

The matcher turns a :class:`~prudify.core.transcribe.Transcript` into a list of
time intervals to silence. Two properties matter more than raw cleverness:

* **No false positives.** An allowlist is consulted before any rule, and exact
  whole-token matching is the default. Muting "class" because it contains a
  substring is worse than missing a word.
* **Explainability.** Every interval records which word triggered it, which
  rule matched, and the transcriber's confidence, so the UI can show the user
  exactly what will be cut before anything is written.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .transcribe import Word

MatchMode = Literal["exact", "prefix", "fuzzy"]

_PUNCT_STRIP = re.compile(r"^[^\w']+|[^\w']+$", re.UNICODE)
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "´": "'", "`": "'"})

# Deliberately conservative: only substitutions people actually use to sneak
# words past filters, and only applied when leet_normalise is enabled.
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)


def normalize(text: str) -> str:
    """Lowercase, fold accents, and strip surrounding punctuation."""
    text = unicodedata.normalize("NFKD", text.translate(_APOSTROPHES))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().lower()
    return _PUNCT_STRIP.sub("", text)


def compact(text: str) -> str:
    """Collapse a token to letters and digits only ("fucked-up" -> "fuckedup")."""
    return re.sub(r"[^a-z0-9]", "", text)


@dataclass(slots=True)
class Rule:
    """A single wordlist entry, compiled."""

    raw: str
    kind: Literal["word", "phrase", "prefix", "regex"]
    tokens: tuple[str, ...] = ()
    pattern: re.Pattern | None = None

    @property
    def length(self) -> int:
        return max(1, len(self.tokens))


def parse_wordlist(lines: Iterable[str]) -> list[Rule]:
    """Compile wordlist text into rules. See wordlists/strict.txt for syntax."""
    rules: list[Rule] = []
    seen: set[str] = set()

    for line in lines:
        entry = line.split("#", 1)[0].strip() if not line.lstrip().startswith("#") else ""
        if not entry:
            continue
        if entry in seen:
            continue
        seen.add(entry)

        if entry.startswith("/") and entry.endswith("/") and len(entry) > 2:
            try:
                rules.append(
                    Rule(raw=entry, kind="regex", pattern=re.compile(entry[1:-1], re.IGNORECASE))
                )
            except re.error:
                continue
            continue

        if entry.endswith("*"):
            stem = normalize(entry[:-1])
            if stem:
                rules.append(Rule(raw=entry, kind="prefix", tokens=(stem,)))
            continue

        parts = tuple(p for p in (normalize(part) for part in entry.split()) if p)
        if not parts:
            continue
        rules.append(
            Rule(raw=entry, kind="phrase" if len(parts) > 1 else "word", tokens=parts)
        )

    # Longest phrases first so "mother fucker" wins over "fucker".
    rules.sort(key=lambda r: (-r.length, r.raw))
    return rules


def load_wordlist_file(path: Path) -> list[Rule]:
    return parse_wordlist(path.read_text(encoding="utf-8").splitlines())


def bundled_wordlist_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "wordlists"


def available_wordlists() -> list[str]:
    directory = bundled_wordlist_dir()
    return sorted(p.stem for p in directory.glob("*.txt") if p.stem != "allowlist")


@dataclass(slots=True)
class Match:
    start: float
    end: float
    text: str
    rule: str
    confidence: float = 1.0
    word_index: int = 0

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "rule": self.rule,
            "confidence": self.confidence,
            "word_index": self.word_index,
        }


@dataclass(slots=True)
class MatchReport:
    matches: list[Match] = field(default_factory=list)
    intervals: list[tuple[float, float]] = field(default_factory=list)
    word_count: int = 0

    @property
    def total_muted_seconds(self) -> float:
        return sum(end - start for start, end in self.intervals)

    @property
    def counts_by_word(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for match in self.matches:
            key = normalize(match.text) or match.text
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def to_dict(self) -> dict:
        return {
            "match_count": len(self.matches),
            "word_count": self.word_count,
            "muted_seconds": round(self.total_muted_seconds, 3),
            "counts_by_word": self.counts_by_word,
            "matches": [m.to_dict() for m in self.matches],
            "intervals": [[round(s, 3), round(e, 3)] for s, e in self.intervals],
        }


def _levenshtein(a: str, b: str, max_distance: int) -> int:
    """Bounded edit distance; returns max_distance + 1 when it exceeds the cap."""
    if abs(len(a) - len(b)) > max_distance:
        return max_distance + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
            best = min(best, current[-1])
        previous = current
        if best > max_distance:
            return max_distance + 1
    return previous[-1]


class ProfanityMatcher:
    def __init__(
        self,
        rules: Sequence[Rule],
        allowlist: Sequence[Rule] = (),
        mode: MatchMode = "exact",
        fuzzy_max_distance: int = 1,
        min_confidence: float = 0.0,
        leet_normalise: bool = True,
    ) -> None:
        self.rules = list(rules)
        self.allow = list(allowlist)
        self.mode = mode
        self.fuzzy_max_distance = fuzzy_max_distance
        self.min_confidence = min_confidence
        self.leet_normalise = leet_normalise

        self._exact: dict[str, str] = {}
        self._prefixes: list[tuple[str, str]] = []
        self._regexes: list[tuple[re.Pattern, str]] = []
        self._phrases: list[Rule] = []

        for rule in self.rules:
            if rule.kind == "word":
                self._exact.setdefault(rule.tokens[0], rule.raw)
                self._exact.setdefault(compact(rule.tokens[0]), rule.raw)
            elif rule.kind == "prefix":
                self._prefixes.append((rule.tokens[0], rule.raw))
            elif rule.kind == "regex" and rule.pattern is not None:
                self._regexes.append((rule.pattern, rule.raw))
            else:
                self._phrases.append(rule)

        self._allow_exact: set[str] = set()
        self._allow_prefix: list[str] = []
        self._allow_regex: list[re.Pattern] = []
        for rule in self.allow:
            if rule.kind == "prefix":
                self._allow_prefix.append(rule.tokens[0])
            elif rule.kind == "regex" and rule.pattern is not None:
                self._allow_regex.append(rule.pattern)
            else:
                for token in rule.tokens:
                    self._allow_exact.add(token)
                    self._allow_exact.add(compact(token))

    # -- token helpers ----------------------------------------------------

    def _forms(self, text: str) -> tuple[str, str]:
        base = normalize(text)
        squashed = compact(base)
        if self.leet_normalise and any(ch.isdigit() or ch in "@$" for ch in squashed):
            squashed = compact(squashed.translate(_LEET))
        return base, squashed

    def _allowed(self, base: str, squashed: str) -> bool:
        if base in self._allow_exact or squashed in self._allow_exact:
            return True
        if any(squashed.startswith(p) for p in self._allow_prefix):
            return True
        return any(p.search(base) for p in self._allow_regex)

    def _rule_for_token(self, base: str, squashed: str) -> str | None:
        if not base:
            return None
        if self._allowed(base, squashed):
            return None

        hit = self._exact.get(base) or self._exact.get(squashed)
        if hit:
            return hit

        if self.mode in ("prefix", "fuzzy"):
            for stem, raw in self._prefixes:
                if squashed.startswith(stem):
                    return raw

        for pattern, raw in self._regexes:
            if pattern.search(base) or pattern.search(squashed):
                return raw

        if self.mode == "fuzzy" and self.fuzzy_max_distance > 0 and len(squashed) >= 4:
            for candidate, raw in self._exact.items():
                if len(candidate) < 4 or candidate[0] != squashed[0]:
                    continue
                if _levenshtein(squashed, candidate, self.fuzzy_max_distance) <= (
                    self.fuzzy_max_distance
                ):
                    return raw
        return None

    # -- public API -------------------------------------------------------

    def find(self, words: Sequence[Word]) -> list[Match]:
        forms = [self._forms(w.text) for w in words]
        matches: list[Match] = []
        consumed: set[int] = set()

        # Phrases first, so a multi-word rule claims its tokens before the
        # single-word pass can match a component in isolation.
        for rule in self._phrases:
            span = len(rule.tokens)
            for i in range(len(words) - span + 1):
                if any(idx in consumed for idx in range(i, i + span)):
                    continue
                ok = True
                for offset, token in enumerate(rule.tokens):
                    base, squashed = forms[i + offset]
                    if self._allowed(base, squashed):
                        ok = False
                        break
                    if base != token and squashed != compact(token):
                        ok = False
                        break
                if not ok:
                    continue
                group = words[i : i + span]
                confidence = min(w.probability for w in group)
                if confidence < self.min_confidence:
                    continue
                consumed.update(range(i, i + span))
                matches.append(
                    Match(
                        start=group[0].start,
                        end=group[-1].end,
                        text=" ".join(w.text.strip() for w in group),
                        rule=rule.raw,
                        confidence=confidence,
                        word_index=i,
                    )
                )

        for index, word in enumerate(words):
            if index in consumed:
                continue
            base, squashed = forms[index]
            rule = self._rule_for_token(base, squashed)
            if not rule:
                continue
            if word.probability < self.min_confidence:
                continue
            matches.append(
                Match(
                    start=word.start,
                    end=word.end,
                    text=word.text.strip(),
                    rule=rule,
                    confidence=word.probability,
                    word_index=index,
                )
            )

        matches.sort(key=lambda m: m.start)
        return matches


def build_intervals(
    matches: Sequence[Match],
    pad_before_ms: int = 0,
    pad_after_ms: int = 100,
    merge_gap_ms: int = 250,
    duration: float = 0.0,
    words: Sequence[Word] | None = None,
    neighbour_guard_ms: int = 30,
) -> list[tuple[float, float]]:
    """Pad each match, then merge anything that overlaps or nearly touches.

    When ``words`` is supplied, padding is not allowed to run into the
    neighbouring words. Padding exists to cover the slop in Whisper's word
    boundaries, but a fixed amount is too blunt on its own: where the next
    word follows immediately, 100 ms of trailing pad audibly clips its onset,
    which listeners hear as the silence "bleeding" into the surrounding
    speech. Each interval is therefore clamped to stop ``neighbour_guard_ms``
    short of the adjacent word, while never trimming inside the matched word
    itself -- the profanity is always fully covered.
    """
    if not matches:
        return []

    pad_before = pad_before_ms / 1000.0
    pad_after = pad_after_ms / 1000.0
    gap = merge_gap_ms / 1000.0
    guard = max(0, neighbour_guard_ms) / 1000.0

    raw = []
    for match in matches:
        start = match.start - pad_before
        end = match.end + pad_after

        if words:
            # A phrase rule matches several words; the neighbours sit either
            # side of the whole span.
            span = max(1, len(match.text.split()))
            previous_index = match.word_index - 1
            following_index = match.word_index + span

            if 0 <= previous_index < len(words):
                # Never start before the previous word has finished speaking,
                # but never clip into the match itself either.
                floor = words[previous_index].end + guard
                start = max(start, min(floor, match.start))

            if 0 <= following_index < len(words):
                ceiling = words[following_index].start - guard
                end = min(end, max(ceiling, match.end))

        start = max(0.0, start)
        if duration:
            end = min(end, duration)
        if end > start:
            raw.append((start, end))

    raw.sort()
    merged: list[tuple[float, float]] = []
    for start, end in raw:
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def analyse(
    words: Sequence[Word],
    matcher: ProfanityMatcher,
    pad_before_ms: int = 0,
    pad_after_ms: int = 100,
    merge_gap_ms: int = 250,
    duration: float = 0.0,
    neighbour_guard_ms: int = 30,
) -> MatchReport:
    matches = matcher.find(words)
    intervals = build_intervals(
        matches,
        pad_before_ms=pad_before_ms,
        pad_after_ms=pad_after_ms,
        merge_gap_ms=merge_gap_ms,
        duration=duration,
        words=words,
        neighbour_guard_ms=neighbour_guard_ms,
    )
    return MatchReport(matches=matches, intervals=intervals, word_count=len(words))


def resolve_wordlist_path(name: str, user_dir: Path | None = None) -> Path | None:
    """A user list of the same name shadows the bundled one."""
    if user_dir is not None:
        candidate = user_dir / f"{name}.txt"
        if candidate.exists():
            return candidate
    candidate = bundled_wordlist_dir() / f"{name}.txt"
    return candidate if candidate.exists() else None


def build_matcher_from_settings(
    filtering, extra_words: Sequence[str] = (), user_dir: Path | None = None
) -> ProfanityMatcher:
    """Assemble a matcher from a :class:`FilterSettings` instance."""
    lines: list[str] = []
    directory = bundled_wordlist_dir()

    if filtering.wordlist and filtering.wordlist != "custom":
        selected = resolve_wordlist_path(filtering.wordlist, user_dir)
        if selected is not None:
            lines += selected.read_text(encoding="utf-8").splitlines()
    lines += list(filtering.custom_words)
    lines += list(extra_words)

    allow_lines: list[str] = []
    allow_file = resolve_wordlist_path("allowlist", user_dir) or (directory / "allowlist.txt")
    if allow_file.exists():
        allow_lines += allow_file.read_text(encoding="utf-8").splitlines()
    allow_lines += list(filtering.custom_allowlist)

    return ProfanityMatcher(
        rules=parse_wordlist(lines),
        allowlist=parse_wordlist(allow_lines),
        mode=filtering.match_mode,
        fuzzy_max_distance=filtering.fuzzy_max_distance,
        min_confidence=filtering.min_confidence,
    )
