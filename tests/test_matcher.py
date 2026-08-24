"""Matching behaviour. These are the tests that keep false positives out."""

from __future__ import annotations

from pathlib import Path

import pytest

from prudify.core import matcher as M
from prudify.core.transcribe import Word

WORDLIST_DIR = Path(__file__).resolve().parents[1] / "backend" / "prudify" / "wordlists"


def words(text: str) -> list[Word]:
    return [Word(start=i, end=i + 0.5, text=token) for i, token in enumerate(text.split())]


@pytest.fixture
def strict():
    return M.ProfanityMatcher(
        M.load_wordlist_file(WORDLIST_DIR / "strict.txt"),
        M.load_wordlist_file(WORDLIST_DIR / "allowlist.txt"),
        mode="exact",
    )


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [("Fuck!", "fuck"), ('"Fucking--"', "fucking"), ("  fuck,  ", "fuck"),
         ("FUCK", "fuck"), ("don’t", "don't"), ("naïve", "naive")],
    )
    def test_normalize(self, raw, expected):
        assert M.normalize(raw) == expected

    def test_compact_strips_internal_punctuation(self):
        assert M.compact(M.normalize("fucked-up")) == "fuckedup"


class TestParsing:
    def test_rule_kinds(self):
        rules = M.parse_wordlist(["fuck", "fuck*", "mother fucker", "/^sh[i1]t$/", "# comment", ""])
        kinds = {rule.raw: rule.kind for rule in rules}
        assert kinds == {
            "fuck": "word",
            "fuck*": "prefix",
            "mother fucker": "phrase",
            "/^sh[i1]t$/": "regex",
        }

    def test_phrases_sort_first(self):
        rules = M.parse_wordlist(["fucker", "mother fucker"])
        assert rules[0].kind == "phrase"

    def test_invalid_regex_is_ignored(self):
        assert M.parse_wordlist(["/([unclosed/"]) == []

    def test_bundled_lists_parse(self):
        for name in M.available_wordlists():
            assert M.load_wordlist_file(WORDLIST_DIR / f"{name}.txt")


class TestMatching:
    def test_matches_plain_word(self, strict):
        found = strict.find(words("he said fucking hell"))
        assert [m.text for m in found] == ["fucking"]

    def test_ignores_punctuation_and_case(self, strict):
        found = strict.find(words('Fuck! he shouted "Fucking--" then stopped'))
        assert len(found) == 2

    def test_phrase_beats_component_word(self, strict):
        found = strict.find(words("the mother fucker ran"))
        assert len(found) == 1
        assert found[0].text == "mother fucker"
        assert found[0].rule == "mother fucker"

    @pytest.mark.parametrize(
        "sentence", ["a classic assessment", "the bass passed", "welcome to Scunthorpe"]
    )
    def test_no_false_positives(self, strict, sentence):
        assert strict.find(words(sentence)) == []

    def test_prefix_mode_requires_prefix_rules(self):
        rules = M.parse_wordlist(["fuck*"])
        allow = M.load_wordlist_file(WORDLIST_DIR / "allowlist.txt")
        exact = M.ProfanityMatcher(rules, allow, mode="exact")
        prefix = M.ProfanityMatcher(rules, allow, mode="prefix")
        assert exact.find(words("fuckwit")) == []
        assert [m.text for m in prefix.find(words("fuckwit"))] == ["fuckwit"]

    def test_allowlist_wins_over_prefix(self):
        matcher = M.ProfanityMatcher(
            M.parse_wordlist(["cock*"]), M.parse_wordlist(["cockpit", "cocktail"]), mode="prefix"
        )
        assert [m.text for m in matcher.find(words("cockpit cocktail cockwomble"))] == [
            "cockwomble"
        ]

    def test_fuzzy_catches_mishearings(self):
        matcher = M.ProfanityMatcher(
            M.parse_wordlist(["fucking"]), [], mode="fuzzy", fuzzy_max_distance=1
        )
        assert len(matcher.find(words("he was fuckng tired"))) == 1

    def test_fuzzy_does_not_match_distant_words(self):
        matcher = M.ProfanityMatcher(
            M.parse_wordlist(["fucking"]), [], mode="fuzzy", fuzzy_max_distance=1
        )
        assert matcher.find(words("packing tracking backing")) == []

    def test_min_confidence_filters_low_probability_hits(self):
        matcher = M.ProfanityMatcher(M.parse_wordlist(["fuck"]), [], min_confidence=0.6)
        low = [Word(start=0, end=0.4, text="fuck", probability=0.3)]
        high = [Word(start=0, end=0.4, text="fuck", probability=0.9)]
        assert matcher.find(low) == []
        assert len(matcher.find(high)) == 1

    def test_regex_rule(self):
        matcher = M.ProfanityMatcher(M.parse_wordlist([r"/^fu+ck$/"]), [])
        assert len(matcher.find(words("fuuuck"))) == 1


class TestIntervals:
    def test_padding_applied(self):
        intervals = M.build_intervals(
            [M.Match(start=10.0, end=10.4, text="x", rule="x")],
            pad_before_ms=200, pad_after_ms=100,
        )
        assert intervals == [(9.8, 10.5)]

    def test_nearby_hits_merge(self):
        intervals = M.build_intervals(
            [
                M.Match(start=10.0, end=10.4, text="a", rule="a"),
                M.Match(start=10.5, end=10.9, text="b", rule="b"),
                M.Match(start=30.0, end=30.6, text="c", rule="c"),
            ],
            pad_after_ms=100, merge_gap_ms=250,
        )
        assert len(intervals) == 2
        assert intervals[0] == pytest.approx((10.0, 11.0))

    def test_clamped_to_duration(self):
        intervals = M.build_intervals(
            [M.Match(start=59.8, end=60.0, text="x", rule="x")],
            pad_after_ms=1000, duration=60.0,
        )
        assert intervals[0][1] == pytest.approx(60.0)

    def test_never_negative(self):
        intervals = M.build_intervals(
            [M.Match(start=0.1, end=0.4, text="x", rule="x")], pad_before_ms=5000
        )
        assert intervals[0][0] == 0.0


def test_report_counts_by_word(strict):
    report = M.analyse(words("fuck fuck fucking cunt"), strict)
    assert report.counts_by_word == {"fuck": 2, "cunt": 1, "fucking": 1}
    assert report.word_count == 4
