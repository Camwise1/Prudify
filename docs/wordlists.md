# Wordlists

A wordlist is a plain text file, one rule per line. Prudify ships two, plus an
allowlist, and you can edit or add your own from the Wordlists page in the UI.

## Syntax

| Form | Matches | Example |
| --- | --- | --- |
| `word` | that exact token, case-insensitively | `fuck` matches "Fuck!", not "fucking" |
| `two words` | consecutive tokens | `mother fucker` |
| `word*` | any token starting with the stem | `fuck*` matches fucking, fucker, fuckwit |
| `/regex/` | a regular expression against one token | `/^sh[i1]t$/` |
| `# comment` | ignored | |

Blank lines and duplicates are ignored. Longer phrases are evaluated first, so
`mother fucker` claims both tokens before `fucker` can match one of them — you
get a single interval covering the phrase, not two overlapping ones.

Prefix rules only apply in **prefix** or **fuzzy** match mode. In the default
**exact** mode a `word*` rule is parsed but never fires, which is deliberate:
turning prefix matching on is a decision worth making consciously.

## Match modes

**exact** (default) — whole-token equality after normalisation. Punctuation is
stripped, case folded, accents removed, curly apostrophes straightened. This is
the mode that will not surprise you.

**prefix** — additionally honours `word*` rules. Catches inflections you did
not enumerate, at the cost of needing a good allowlist.

**fuzzy** — prefix, plus a bounded edit distance (default 1) on tokens of four
characters or more, sharing a first letter. This exists because Whisper
mishears: *fuckin'* comes out as *fuckin*, *fucking* as *fuckng*. It will also
occasionally catch something you did not intend, so preview with dry run.

## Normalisation

Before matching, each token is:

1. Unicode-normalised and stripped of combining marks (`naïve` → `naive`)
2. Lowercased
3. Stripped of leading and trailing punctuation (`"Fucking--"` → `fucking`)
4. Compared both as-is and in a compacted form with internal hyphens and
   apostrophes removed (`fucked-up` → `fuckedup`)

If a token contains digits or `@`/`$`, a leetspeak-folded form is also tried
(`sh1t` → `shit`), so the obvious evasions do not slip through.

## The allowlist

`allowlist.txt` is checked **before** any rule. Anything in it is never
silenced, whatever your wordlist says. It exists for the Scunthorpe problem:
innocent words that contain a profane substring.

This matters most in prefix and fuzzy mode. In exact mode `class` was never
going to match `ass` anyway — but if you add `ass*` to your list, the allowlist
is the only thing standing between you and a muted "assessment".

## Bundled lists

**`strict`** — the F-word and C-word and their common inflections. Nothing
else. This is the default, and for most people wanting a "car with kids in the
back" version of a book, it is the right answer.

**`moderate`** — strict plus the next tier: shit, bitch, bastard, whore, prick,
twat, asshole and relatives. Still excludes mild profanity (damn, hell, crap)
and blasphemy.

Neither list contains slurs. That is a deliberate omission rather than an
oversight: a slur list is a different piece of work with different failure modes
(reclaimed usage, in-context quotation, dialect), it needs curating by people
who know the territory, and quietly bundling one under the label "moderate"
would be the wrong default. Add your own list if you want that filtering — the
UI makes it a five-minute job.

## Editing

Editing a bundled list in the UI writes your version to
`<data dir>/wordlists/<name>.txt`, which shadows the bundled copy. Upgrades
replace the bundled lists and leave yours alone. Delete your copy to fall back
to the shipped version.

The **Try it** box on the Wordlists page runs a sentence through the live
matcher and highlights what would be silenced. Use it before you queue 400
books.

## Tuning padding

Whisper's word boundaries are good but not perfect, and it tends to clip the
tail of a word rather than the head.

| Setting | Default | What it does |
| --- | --- | --- |
| Padding before | `0 ms` | Extends the silence backwards |
| Padding after | `100 ms` | Extends it forwards — the one that usually matters |
| Merge gap | `250 ms` | Two hits closer than this become one interval |

If you can still hear a consonant at the end of a muted word, raise *padding
after* to 150–200 ms. If muted passages are swallowing surrounding words, lower
it. Raising the merge gap makes a rapid string of profanity into one clean
silence rather than a stutter of several.

## Confidence threshold

Every word carries the transcriber's probability. Setting a **minimum
confidence** of, say, `0.5` discards matches Whisper was unsure about. This
trades recall for precision — useful with fuzzy matching, usually unnecessary
with exact.
