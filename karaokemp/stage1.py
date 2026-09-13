"""Stage 1 — filename parsing & provisional pairing (spec §6).

Needs local bytes: no. Mutates filesystem: no. Mutates Drive: NEVER (§0).

This stage reads `file_locations.remote_path` — nothing else — and turns names into hints.
It runs over ALL locations including `excluded` ones (§5.2, §6.1): the exact-dup pass threw
away *bytes*, never *evidence*, and a duplicate's filename often carries better metadata than
the survivor's.

The parser is the highest-variance component in the pipeline (§6.2), so everything here is
built to be inspected: each parse records which pattern fired (`layout`), how much we trust it
(`confidence`), and what was thrown away (`flags`). Nothing is silently guessed.

Patterns implemented here were derived from a census of the real 53,674-row listing, not from
the spec's illustrative examples — several of which do not occur in this library at all. See
docs/history/HISTORY.md "Stage 1 parser census" for the evidence behind each choice.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

# --- normalization (§6.1) ----------------------------------------------------------------

# Hebrew combining marks to strip, by explicit codepoint (§6.1). The spec is emphatic about
# NOT using a blanket range, and it is right: maqaf U+05BE is a *hyphen* (stripping it glues
# words together), sof pasuq U+05C3 and paseq U+05C0 are punctuation. All three sit inside the
# naive 0591–05C7 range and must survive.
HEBREW_MARKS = (
    {chr(c) for c in range(0x0591, 0x05BE)}  # 0591–05BD: cantillation + niqqud (excludes 05BE)
    | {"ֿ"}                              # rafe
    | {"ׁ", "ׂ"}                    # shin/sin dot
    | {"ׄ", "ׅ"}                    # upper/lower dot
    | {"ׇ"}                              # qamats qatan
)

HEBREW_BLOCK = ("֐", "׿")
VIDEO_ID_LEN = 11

# Junk that shows up mid-name and means nothing about the song.
SITE_TAGS = re.compile(r"\((?:www\.)?[\w.-]+\.(?:com|net|pm|org|co\.il|ru)\)", re.I)

# Files that are not media at all (§5.3 counted 106). Parsing 'Thumbs.db' as a song produces a
# confident-looking artist/title out of Windows shell cruft, which is worse than no row at all.
NON_MEDIA_EXT = {
    "db", "ini", "bat", "jar", "sfk", "part", "jpg", "jpeg", "png", "gif", "bmp",
    "txt", "doc", "docx", "pdf", "url", "lnk", "nfo", "m3u", "pls", "sfv", "exe", "none",
}

# macOS AppleDouble sidecars: a resource fork written beside the real file. All 29 in this
# library are exactly 4096 bytes — they carry a real filename but no media whatsoever.
APPLEDOUBLE = re.compile(r"^\._")

# Names that encode no metadata at all. Splitting these on a dash manufactures an artist out of
# nothing: 'SONG-<uuid>' became artist='SONG', 'Chapter_03-113' became artist='Chapter 03'.
# §5.3 predicted this ("those chapter files carry near-zero filename metadata; §6.1's
# parent-folder hints will be the only signal for them") — so the parent folder is recorded as a
# hint and, per §6.1, is NOT auto-promoted. Stage 5 gets to decide what the folder means.
OPAQUE_NAME = re.compile(
    r"^(?:song[-_][0-9a-f]{8}-[0-9a-f-]{8,}"   # SONG-<uuid>       (308 rows)
    r"|chapter[_ ]?\d+(?:[-_]\d+)?"             # Chapter_03-113    (286 rows)
    r"|title[_ ]?\d+"                           # Title_1101        (30 rows)
    r"|vts[_ ]?\d+[_ ]?\d*"                     # DVD authoring artefacts
    r"|track\s*\d+"                             # §6.1's own 'Track01' example
    r"|\d{1,3})$",                              # a bare number
    re.I,
)

# A trailing 4–5 digit catalogue number: "עד סוף הקיץ - רפאל מירילה - 6904". 1,033 rows, of which
# 1,026 are the 'קריוקי בעברית 1' batch. Left in place it glues onto the artist ('רפאל מירילה 6904').
TRAILING_CATALOGUE = re.compile(r"\s*-\s*(\d{4,5})\s*$")

# A stale extension left inside the stem: "…Hoochie Coochie Man.mpg [KARAOKE].cdg" — the ".mpg"
# is a fossil from an earlier conversion and is not this file's type.
EMBEDDED_EXT = re.compile(r"\.(?:mp3|cdg|mpg|mpeg|avi|mp4|vob|wmv|dat|mkv|zip)\b", re.I)


def strip_hebrew_diacritics(s: str) -> str:
    """Remove Hebrew combining marks, preserving Hebrew punctuation (§6.1).

    Only marks in HEBREW_MARKS go; a mark from another script (e.g. Arabic, Latin combining
    accents) is left alone, since §6.1 scopes this to the Hebrew block.
    """
    return "".join(ch for ch in s if ch not in HEBREW_MARKS)


def has_hebrew(s: str) -> bool:
    return any(HEBREW_BLOCK[0] <= ch <= HEBREW_BLOCK[1] for ch in s)


def detect_script(s: str) -> str:
    """Script detection, which §6.1 says suffices for language. Never transliterate."""
    if has_hebrew(s):
        return "he"
    if any("Ѐ" <= ch <= "ӿ" for ch in s):
        return "ru"
    if any("؀" <= ch <= "ۿ" for ch in s):
        return "ar"
    if any(ch.isalpha() and ch.isascii() for ch in s):
        return "latn"
    return "unknown"


def _looks_like_video_id(tok: str) -> bool:
    """True for an 11-character video ID, false for an English word of the same length.

    Both are 11 chars of [A-Za-z0-9_-], so length alone is not a discriminator: it matches
    `tobewithyou` and `wet_wet_wet` — real title words in this library — as readily as
    `VZwiiKF3F7Y`. Requiring a digit AND an uppercase letter separates them, because a real ID
    is base64-ish random while a title fragment is lowercase prose. This is a heuristic, but a
    false negative just leaves a suffix on the title (visible, fixable) while a false positive
    silently eats a word.
    """
    if len(tok) != VIDEO_ID_LEN:
        return False
    return any(c.isdigit() for c in tok) and any(c.isupper() for c in tok)


def normalize(stem: str) -> str:
    """Unicode NFC, entity decode, junk strip, whitespace collapse (§6.1)."""
    s = unicodedata.normalize("NFC", stem)
    s = html.unescape(s)  # '&quot;' appears literally in filenames here
    s = SITE_TAGS.sub(" ", s)
    s = EMBEDDED_EXT.sub(" ", s)

    # URL-safe naming: some files encode every space as '_' ("Angie_-_The_Rolling_Stones").
    # Only convert when the name has underscores and no real spaces, so we do not damage a
    # title that legitimately contains an underscore among normal spaces.
    if "_" in s and " " not in s:
        s = s.replace("_", " ")
    s = s.replace("+", " ") if "+" in s and " " not in s else s

    # Trailing video ID: this library has them as a bare '-XXXXXXXXXXX' suffix, never in the
    # brackets the spec shows. Strip only a trailing one, only if it looks like an ID.
    parts = re.split(r"[-_]", s)
    if len(parts) > 1 and _looks_like_video_id(parts[-1]):
        s = s[: s.rfind(parts[-1])].rstrip(" -_")

    # §6.1 says "collapse whitespace", but doing that unconditionally destroys a real signal:
    # a run of 3+ spaces is the separator in a common disc-label house style
    # ("AMS1060 08   Pras & Mya   Ghetto Superstar") and in a batch of Russian/Hebrew files
    # ("Passenger   Let Her Go") — 48 rows. Canonicalising those runs to ' - ' preserves the
    # separator in a form the layout ladder already understands, then collapses the rest.
    # A run of exactly 2 spaces is NOT treated as a separator: the census shows those are mostly
    # typos or a Hebrew title sitting beside its transliteration
    # ("טונה - סרט ערבי  Tuna - Seret Aravi"), where splitting would invent a bogus artist.
    s = re.sub(r"\s{3,}", " - ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -_")


# --- decorations & instrumental hints (§6.1) ---------------------------------------------

# Decorations come in two strengths, because "is this word a decoration or part of the name?"
# has a different answer depending on the word — and getting it wrong silently eats a title.
#
# STRONG: karaoke-domain terms that essentially never appear inside a real artist or song name.
# Safe to remove anywhere in the string.
STRONG_DECORATIONS: list[tuple[str, str]] = [
    (r"karaoke\s+version", "karaoke_version"),
    (r"karaoke\s+with\s+lyrics", "karaoke_lyrics"),
    (r"karaoke\s+lyrics", "karaoke_lyrics"),
    # Must precede the bare \bkaraoke\b rule below: matching 'karaoke' first would leave a bare
    # 'video' that the weak rules deliberately refuse to touch, stranding it in the artist name.
    (r"karaoke\s+video", "video"),
    (r"with\s+lyrics", "lyrics"),
    # 'HD' is a decoration in all 23 bare rows here and is never a word in a real title —
    # unlike 'video', which is ("Video Killed The Radio Star" appears 4 times).
    (r"\bhd\b", "hd"),
    (r"no\s+lead\s+vocals?", "no_lead_vocal"),
    (r"with\s+backing\s+vocals?", "backing_vocals"),
    (r"backing\s+track", "backing_track"),
    (r"\bkarafun\b", "karafun"),
    (r"\bmultiplex\b", "multiplex"),
    (r"\bminus\s+one\b", "minus_one"),
    (r"\binstrumental\b", "instrumental"),
    (r"\bplayback\b", "playback"),
    (r"\bkaraoke\b", "karaoke"),
    (r"\bинструментал\b", "instrumental"),
    (r"\bкараоке\b", "karaoke"),
    (r"\bקריוקי\b", "karaoke"),
    (r"\bפלייבק\b", "playback"),
]

# WEAK: ordinary words that are decorations *in context* but are also real names. Removed only
# inside brackets/parens, where the intent is unambiguous — a bare occurrence is left alone.
# The census is unambiguous about why:
#     'clean'     → "Clean Bandit Feat. Jess Glynne - Rather Be"  (a band, 24 bare rows)
#     'cc'        → "10 cc-dreadlock holiday"                     (a band, 29 bare rows)
#     'christmas' → "Do They Know It's Christmas"                 (a title, 234 bare rows)
#     'live'      → "How Do I Live"                               (a title, 172 bare rows)
# Stripping these bare would corrupt hundreds of real names to catch a handful of genuine
# decorations. The trade is deliberately asymmetric: a decoration left in a title is visible and
# fixable downstream, a word eaten out of an artist name is gone silently.
WEAK_DECORATIONS: list[tuple[str, str]] = [
    (r"\bcon\s+voz\b", "con_voz"),
    (r"\bduet\b", "duet"),
    (r"\bclean\s+version\b", "clean"),
    (r"\bclean\b", "clean"),
    (r"\bradio\s+version\b", "radio_version"),
    (r"\bchristmas\b", "christmas"),
    (r"\blive\b", "live"),
    (r"\bremix\b", "remix"),
    (r"\bbacking\b", "backing"),
    (r"\bvideo\b", "video"),
    (r"\bcc\b", "cc"),
    (r"\blyrics\b", "lyrics"),   # bare 'lyrics' can be a title word; '[ Lyrics ]' cannot
    (r"\bרמיקס\b", "remix"),
]

# Captures the delimiters so a region with surviving content keeps its own bracket style.
BRACKETED = re.compile(r"([\[\(])([^\]\)]*)([\]\)])")

# §6.1's instrumental hint list, plus the Russian/Hebrew tags and the karaoke-industry phrasings
# the census turned up. Any of these at filename confidence ⇒ is_instrumental='yes'.
INSTRUMENTAL_FLAGS = {
    "karaoke", "karaoke_version", "karaoke_lyrics", "instrumental", "playback",
    "backing", "backing_track", "minus_one", "no_lead_vocal", "karafun", "multiplex",
}

# The polarity of a decoration is not always "instrumental". 'con voz' is Spanish for "with
# voice" and 'with backing vocals' means a guide vocal is present — both are evidence the track
# is NOT a clean instrumental. Recording them as negative evidence rather than ignoring them is
# what lets Stage 4's vocal-presence check corroborate instead of contradict.
VOCAL_PRESENT_FLAGS = {"con_voz", "backing_vocals"}


def _apply_decorations(text: str, patterns: list[tuple[str, str]]) -> tuple[str, list[str]]:
    flags: list[str] = []
    out = text
    for pattern, flag in patterns:
        rx = re.compile(pattern, re.I)
        if rx.search(out):
            if flag not in flags:
                flags.append(flag)
            out = rx.sub(" ", out)
    return out, flags


def extract_decorations(s: str) -> tuple[str, list[str]]:
    """Move decorations out of the name and into flags (§6.1). Returns (cleaned, flags).

    Bracketed regions get both strong and weak patterns; bare text gets strong only (see
    WEAK_DECORATIONS for why). A bracketed region whose content is entirely decoration is
    dropped; one with content left over keeps its brackets and its remaining words, so
    "Stay (I Missed You)" survives intact rather than being truncated to "Stay (I Missed You".
    """
    flags: list[str] = []

    def _region(m: re.Match) -> str:
        open_, inner, close = m.groups()
        reduced, f = _apply_decorations(inner, STRONG_DECORATIONS + WEAK_DECORATIONS)
        flags.extend(f)
        reduced = re.sub(r"\s+", " ", reduced).strip(" -_")
        return f"{open_}{reduced}{close}" if reduced else " "

    out = BRACKETED.sub(_region, s)
    out, bare_flags = _apply_decorations(out, STRONG_DECORATIONS)
    flags.extend(bare_flags)

    seen: list[str] = []
    for f in flags:
        if f not in seen:
            seen.append(f)
    out = re.sub(r"\s+", " ", out)
    # Trim separators only — never brackets. Stripping '()' here truncated 1,093 real titles
    # ("Guerrilla Radio (Pixel", "צבעים (גרסת בנות") before the bracket-aware pass above existed.
    return out.strip(" -_"), seen


def instrumental_verdict(flags: list[str]) -> str:
    """§6.1: filename-confidence is_instrumental. 'unknown' unless the name actually says."""
    if any(f in INSTRUMENTAL_FLAGS for f in flags):
        # A guide-vocal marker downgrades certainty but does not flip it: a 'karaoke [con voz]'
        # is still a karaoke track, just one with a vocal guide. Stage 4 decides; we only hint.
        return "yes"
    if any(f in VOCAL_PRESENT_FLAGS for f in flags):
        return "no"
    return "unknown"


# --- disc IDs (§6.1) ----------------------------------------------------------

# §6.1's family: ^([A-Z]{2,4})[- ]?(\d{2,5})[- ](\d{1,2}). Two amendments from the census:
#   1. Case-insensitive — 'sf012-08-mrbig-tobewithyou' and 'dk089-01_-_…' are real rows that
#      the uppercase-only form silently drops.
#   2. Separator may be a run of spaces, not just '-': 'AMS1060 08   Pras & Mya   Ghetto
#      Superstar' is a common disc-label house style.
DISC_ID = re.compile(
    r"^(?P<series>[A-Za-z]{2,4})[- ]?(?P<disc>\d{2,5})(?:-(?P<part>\d{1,2}))?[- \s]+(?P<track>\d{1,2})(?![\d])\s*[-\s]\s*",
)

# Artist/Title order is NOT a property of the series. The census disproves that directly: the
# same SF series is Artist-Title under New/ ("SF222-06 - Duran Duran - Sunrise", 10,715 rows)
# but Title-Artist under Unorginized/ ("SF314-09 - R U Mine - Arctic Monkeys"), and MRH flips
# the same way between New/ and English karaoke/. Order tracks the *source batch* — each top-level
# folder is a different origin with different house style — and some batches are mixed even
# internally, so no lookup table can be complete.
#
# Only batches whose order was verified against real rows appear here, keyed on
# (top-level folder, series). Everything else is deliberately absent: §6.1 says both orders get
# "scored against MB later", so an unlisted batch records the commoner reading at a confidence
# that says "unresolved" rather than inventing certainty. Claiming 0.95 on a guess is worse
# than claiming 0.5 on the same guess — it tells Stage 5 not to bother checking.
DISC_BATCH_ORDER = {
    ("New", "SF"): "artist_title",              # 10,715 rows — "SF222-06 - Duran Duran - Sunrise"
    ("New", "MRH"): "artist_title",             # 432 — "MRH123-07 - Veronicas - You Ruin Me"
    ("New", "BHK"): "artist_title",             # 242 — "BHK023-10 - Florence & The Machine - …"
    ("Unorginized", "DK"): "title_artist",      # 988 — "DK30-15 - Venus - Frankie Avalon"
    ("English karaoke", "DK"): "title_artist",  # 56 — "DK23-17 - Candle In The Wind - Elton John"
    ("Unorginized", "SAVP"): "artist_title",    # 136 — "SAVP20 - 15 Fugees - Killing Me Softly"
    ("English karaoke", "SAVP"): "artist_title",  # 36 — "SAVP 17 - 08 Billy Joel - My Life"
    ("English karaoke", "BS"): "artist_title",  # 120 — "BS8017 - 06 Queen - We Are The Champions"
    ("Unorginized", "BS"): "artist_title",      # 54 — "BS5417 - 13 Lisa Loeb & Nine Stories - …"
    ("English karaoke", "MRH"): "title_artist",  # 34 — "MRH91-04 - Chasing The Sun - Wanted, The"
    # Deliberately absent — verified MIXED, both orders present in one batch:
    #   ("Unorginized", "SF")     'sf019-03 - crow, sheryl - …' vs 'SF314-09 - R U Mine - …'
    #   ("English karaoke", "SF") 'sf016-13 - zz top - …'       vs 'SF314-04 - Girl Gone Wild - …'
}

# Series still recognised as disc IDs even where the batch order is unknown.
KNOWN_SERIES = {"SF", "DK", "MRH", "BHK", "BS", "SAVP", "SC", "PI", "CBEP", "AMS"}

# Non-disc batches whose house style was verified against real rows. Same principle as
# DISC_BATCH_ORDER: order is a property of the batch, and a folder that was produced once is
# internally consistent. Only folders actually checked appear here.
#
# 'קריוקי בעברית 1' is the big Hebrew batch: "Title - Artist - NNNN", artist LAST, 1,026 rows.
#   ערב טוב - ליאור נרקיס - 55454      → artist ליאור נרקיס
#   כאן - שירי ילדות - אורנה ומשה דץ - 7701  → artist אורנה ומשה דץ (3 parts; artist still last)
# Confirmed by sha-yol's §6.2 golden-set review, which marked every row of this shape wrong.
FOLDER_BATCH_ORDER = {
    "קריוקי בעברית 1": "title_artist",
}


def parse_disc_id(s: str) -> tuple[dict[str, str] | None, str]:
    """Split a leading disc ID off the name. Returns (disc_fields|None, rest)."""
    m = DISC_ID.match(s)
    if not m:
        return None, s
    raw_series = m.group("series")
    series = raw_series.upper()
    # Case-insensitivity is only safe for series we already know. An English word followed by
    # numbers has exactly the shape of a disc ID — "Slow 12-8 Blues Backing Track in E" parsed
    # as series SLOW, disc 12, track 8, mangling the title. Requiring a non-uppercase token to
    # name a KNOWN series keeps the real lowercase rows ('sf012-08…', 'dk089-01…') while
    # restoring §6.1's [A-Z]{2,4} strictness for everything else.
    if series not in KNOWN_SERIES and raw_series != series:
        return None, s
    disc = m.group("disc")
    fields = {
        "disc_series": series,
        "disc_id": f"{series}{disc}" + (f"-{m.group('part')}" if m.group("part") else ""),
        "disc_track": m.group("track"),
    }
    return fields, s[m.end():].strip()


# --- layout ladder (§6.1) ----------------------------------------------------------------

# A dash is a separator when it has whitespace on *either* side, not only on both: 302 rows use
# a one-sided form ("DDT- CHTO TAKOEE LETO", "Drunk Groove -Maruv & Boosin", "למה- מאיה אברהם").
# A dash with no whitespace at all is left alone — that is 'Spider-Man', not a separator, and
# the tight-dash fallback in parse_stem handles the genuine 'lauren hill-nothing' case instead.
SEP = re.compile(r"\s+-\s+|\s+-(?=\S)|(?<=\S)-\s+|\s{2,}|\s+[–—]\s+")

# Folder roots where the immediate child folder is verified to be an artist, checked against
# real rows (31 folders / 560 files, and 8 folders / 75 files respectively — every one a real
# Israeli artist). §6.1 says folder names are "never auto-promoted", and that rule is right for
# a folder like 'M' or 'English karaoke'. It is wrong here, and sha-yol's §6.2 review marked every
# one of these rows bad for exactly that reason. Promotion is therefore scoped to these verified
# roots AND to rows where the filename yielded no artist at all — it can only ever add
# information, never override what the name actually said.
ARTIST_FOLDER_ROOTS = (
    "קריוקי בעברית 1/karaoke",
    "קריוקי בעברית 1/קריוקי מרביד השרירי/ים תיכוני",
)
# "Dixon, Willie" / "Michael, George" / "Wanted, The" — the inverted-name house style. The tail
# is a single token (a first name, or an article): requiring that keeps "Hello, Goodbye" — a
# title, not a name — from reading as an inverted artist.
LASTNAME_FIRST = re.compile(r"^([^,]{2,30}),\s+(\S{1,15})$")
STYLE_OF = re.compile(r"^(?P<title>.+?)\s+in\s+the\s+style\s+of\s+(?P<artist>.+)$", re.I)


def _unswap_lastname(name: str) -> tuple[str, bool]:
    """'Michael, George' → 'George Michael'. Returns (name, was_swapped)."""
    m = LASTNAME_FIRST.match(name.strip())
    if not m:
        return name.strip(), False
    return f"{m.group(2).strip()} {m.group(1).strip()}", True


def _resolve_order(a: str, b: str, series: str, batch: str | None) -> tuple[str, str]:
    """Decide which of the two fields is the artist. Returns (order, why).

    Three tiers, strongest first:

    1. `comma` — exactly one field is an inverted name ("Murs, Olly"). This is per-row evidence
       and it outranks any batch default, because it is a fact about *this* filename rather
       than a generalisation about its neighbours. It is what correctly splits the two mixed
       sub-batches inside Unorginized/SF, which no folder-level rule can do.
    2. `batch` — the (folder, series) combination has a verified house style.
    3. `unresolved` — record the commoner Artist-Title reading, but say so. §6.1 hands this case
       to MusicBrainz scoring at Stage 5; the parser's job is to flag it, not to win it.
    """
    a_name = bool(LASTNAME_FIRST.match(a))
    b_name = bool(LASTNAME_FIRST.match(b))
    if a_name != b_name:
        return ("artist_title" if a_name else "title_artist"), "comma"

    order = DISC_BATCH_ORDER.get((batch or "", series))
    if order:
        return order, "batch"
    return "artist_title", "unresolved"


@dataclass
class Parse:
    """One location's parse payload (§6.1). Every field is a *hint*, never a fact."""

    location_id: int | None = None
    remote_path: str | None = None
    filetype: str | None = None
    normalized: str = ""
    artist: str | None = None
    title: str | None = None
    disc_series: str | None = None
    disc_id: str | None = None
    disc_track: str | None = None
    catalogue_code: str | None = None  # trailing "- NNNN" of the Hebrew batch; not a disc series
    is_instrumental: str = "unknown"
    language: str = "unknown"
    layout: str = "unparsed"
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)
    folder_hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# Confidence by which pattern fired (§6.1: "a disc-ID-structured name outranks Track01.mp3").
# These are ordinal, not probabilities — their only job is to order hint conflicts.
CONF = {
    "disc_comma": 0.95,       # disc structure + an inverted name pins the order on this very row
    "disc_batch": 0.85,       # disc structure + a house style verified for this (folder, series)
    "disc_unresolved": 0.50,  # disc structure known, order genuinely undecidable from the name
    "style_of": 0.90,         # "Title in the style of Artist" — explicit and unambiguous
    "folder_batch": 0.85,     # a single-batch folder with a verified house style
    "artist_from_folder": 0.70,  # artist taken from a verified artist-folder root (§6.1 dev.)
    "artist_title": 0.60,     # generic 'X - Y'; order is a convention, not a guarantee
    "title_only": 0.30,
    "unparsed": 0.0,
}


def parse_stem(
    stem: str, folder_hints: list[str] | None = None, batch: str | None = None
) -> Parse:
    """Parse one filename stem into hints. Pure function — no DB, no IO, easy to golden-test.

    `batch` is the top-level folder, used only to look up a verified disc house style; it is
    evidence about the file's origin, not about the song.
    """
    p = Parse(folder_hints=folder_hints or [])
    p.normalized = normalize(stem)
    p.language = detect_script(p.normalized)

    if APPLEDOUBLE.match(stem):
        # A 4096-byte macOS resource fork wearing the real file's name.
        p.layout, p.confidence = "non_media", 0.0
        p.notes.append("appledouble_sidecar")
        return p

    body, flags = extract_decorations(p.normalized)
    p.flags = flags
    p.is_instrumental = instrumental_verdict(flags)

    if OPAQUE_NAME.match(body.strip()):
        # No metadata to extract. Say so, rather than manufacturing an artist from a dash.
        p.layout, p.confidence = "opaque", 0.0
        p.notes.append("opaque_name")
        return p

    m = TRAILING_CATALOGUE.search(body)
    if m:
        p.catalogue_code = m.group(1)
        body = TRAILING_CATALOGUE.sub("", body)
        p.notes.append("catalogue_code_stripped")

    if has_hebrew(body):
        # §6.1: keep Hebrew script as-is; the stripped form is a *separate* normalized field,
        # not a replacement. Never transliterate for storage.
        stripped = strip_hebrew_diacritics(body)
        if stripped != body:
            p.notes.append("hebrew_diacritics_stripped")
            body = stripped

    disc, rest = parse_disc_id(body)
    if disc:
        p.disc_series, p.disc_id, p.disc_track = (
            disc["disc_series"], disc["disc_id"], disc["disc_track"],
        )
        parts = [x.strip() for x in SEP.split(rest) if x.strip()]
        if len(parts) >= 2:
            a, b = parts[0], " ".join(parts[1:])
            order, why = _resolve_order(a, b, p.disc_series, batch)
            if order == "title_artist":
                p.title, p.artist = a, b
            else:
                p.artist, p.title = a, b
            p.layout = f"disc_{order}"
            p.confidence = CONF[f"disc_{why}"]
            if why == "unresolved":
                p.notes.append("order_unresolved")
            else:
                p.notes.append(f"order_from_{why}")
            p.artist, swapped = _unswap_lastname(p.artist or "")
            if swapped:
                p.notes.append("lastname_first_unswapped")
            return p
        if parts:
            p.title = parts[0]
            p.layout, p.confidence = "disc_title_only", CONF["title_only"]
            return p

    m = STYLE_OF.match(body)
    if m:
        p.title = m.group("title").strip(" \"'")
        p.artist = m.group("artist").strip(" \"'")
        p.layout, p.confidence = "style_of", CONF["style_of"]
        return p

    parts = [x.strip() for x in SEP.split(body) if x.strip()]
    if len(parts) == 1:
        # Try a tight dash ("lauren hill-nothing even matters") only when there is no spaced
        # separator: a tight dash is also a legitimate character inside a title, so this is a
        # weaker signal and only worth trying as a fallback.
        tight = [x.strip() for x in re.split(r"(?<=\w)-(?=\w)", parts[0], maxsplit=1) if x.strip()]
        if len(tight) == 2:
            parts = tight
            p.notes.append("tight_dash_split")

    if len(parts) >= 2:
        folder_order = FOLDER_BATCH_ORDER.get(batch or "")
        if folder_order == "title_artist":
            # A verified single-batch folder: the artist is the LAST field, whether the name has
            # two parts or three ("כאן - שירי ילדות - אורנה ומשה דץ").
            p.artist, swapped = _unswap_lastname(parts[-1])
            p.title = " ".join(parts[:-1])
            p.layout, p.confidence = "title_artist", CONF["folder_batch"]
            p.notes.append("order_from_folder_batch")
        else:
            p.artist, swapped = _unswap_lastname(parts[0])
            p.title = " ".join(parts[1:])
            # §6.1 lists 'Artist - Title' before 'Title - Artist' and says both orders get
            # scored against MB later. We record the commoner reading and keep confidence
            # deliberately middling — for Hebrew this is more often Title - Artist than not
            # (see census), so the order here is a coin-flip we are not pretending to have won.
            p.layout, p.confidence = "artist_title", CONF["artist_title"]
            if p.language == "he":
                p.notes.append("hebrew_order_ambiguous")
                p.confidence = 0.45
        if swapped:
            p.notes.append("lastname_first_unswapped")
    elif parts:
        p.title = parts[0]
        p.layout, p.confidence = "title_only", CONF["title_only"]
    else:
        p.layout, p.confidence = "unparsed", CONF["unparsed"]
    return p


# Folder names that carry no artist signal — pure structure. Recorded but not offered as hints.
STRUCTURAL_FOLDER = re.compile(
    r"^(?:[A-Z]|\d{1,5}|[א-ת]|[א-ת]-[א-ת]|\d+\s*-\s*\d+|karaoke|new|unorginized|"
    r"to-rename|songs?|misc|various|cd\d*|disc\s*\d*)$",
    re.I,
)


def folder_hints_for(path: str) -> list[str]:
    """Parent folder names as low-confidence hints (§6.1) — recorded, never auto-promoted.

    'English karaoke/M/Mariah Carey/Mariah Carey - …' has the artist in the folder, and for
    'קריוקי בעברית 1/karaoke/אייל גולן/יפה שלי 5.avi' the folder is the *only* artist signal.
    Single-letter and numeric folders ('M', '01234', 'א-ב') are alphabetical shelving, not
    metadata, so they are dropped here rather than offered as an artist named "M".
    """
    parts = Path(path).parts[:-1]
    return [p for p in parts if not STRUCTURAL_FOLDER.match(p.strip())]


def artist_folder_for(remote_path: str) -> str | None:
    """The artist named by the containing folder, if this path sits under a verified root."""
    for root in ARTIST_FOLDER_ROOTS:
        if remote_path.startswith(root + "/"):
            rest = remote_path[len(root) + 1 :].split("/")
            if len(rest) >= 2 and rest[0].strip():  # an artist folder, then the file
                return rest[0].strip()
    return None


def parse_location(location_id: int, remote_path: str, filetype: str) -> Parse:
    parts = Path(remote_path).parts
    batch = parts[0] if len(parts) > 1 else None
    if filetype in NON_MEDIA_EXT:
        # Thumbs.db / desktop.ini / stray jpgs. §5.3 flagged these; Stage 1 must not try to
        # parse them as songs. Recorded as a row so fsck (§1.1) can still reconcile them.
        p = Parse(layout="non_media", confidence=0.0, notes=["non_media_filetype"])
        p.normalized = normalize(Path(remote_path).stem)
    else:
        p = parse_stem(Path(remote_path).stem, folder_hints_for(remote_path), batch=batch)
        if p.artist is None and p.layout == "title_only":
            folder_artist = artist_folder_for(remote_path)
            if folder_artist:
                p.artist = folder_artist
                p.layout, p.confidence = "title_artist_from_folder", CONF["artist_from_folder"]
                p.notes.append("artist_from_folder")
    p.location_id = location_id
    p.remote_path = remote_path
    p.filetype = filetype
    return p


# --- §6.1 mass insert --------------------------------------------------------------------


@lru_cache(maxsize=1)
def parser_version() -> str:
    """A fingerprint of the parser source that produced a stored parse.

    Derived from this file's bytes rather than a hand-bumped constant, because a constant only
    works if every future editor remembers to bump it — and the failure mode of forgetting is
    silent: `location_parses` keeps serving verdicts from a parser that no longer exists, and
    nothing says so. A source hash cannot be forgotten.

    It is deliberately over-sensitive (a comment edit changes it). That costs nothing: re-parsing
    all 53,674 filenames is pure string work, a few seconds, and `parse` rewrites only rows whose
    payload actually changed — so a cosmetic edit refreshes the stamp without churning content.
    """
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def write_parses(conn, dry_run: bool = False) -> dict[str, Any]:
    """Parse every location and persist the payloads to `location_parses` (§6.1).

    Runs over ALL locations including `excluded` ones. §5.2 and §6.1 are explicit that the
    exact-dup pass discarded *bytes*, never *evidence*: a duplicate's filename often carries
    better metadata than the survivor's ('SF018-02 - The Drifters - Under The Boardwalk' vs
    'drifters-under the boardwalk'), and Stage 3 merges the hints of a whole hash group onto the
    survivor's media item. Dropping excluded rows here would throw that away permanently.

    Idempotent (§1.3): the desired payload is recomputed from scratch as a pure function of the
    filename, then diffed against what is stored. A re-run with an unchanged parser writes
    nothing. A re-run after a parser change rewrites exactly the rows whose output moved.
    """
    from . import db

    now = db.utcnow()
    version = parser_version()
    rows = conn.execute(
        "SELECT id, remote_path, filetype FROM file_locations WHERE remote_path IS NOT NULL"
    ).fetchall()
    existing = {
        r["location_id"]: (r["payload"], r["parser_version"])
        for r in conn.execute("SELECT location_id, payload, parser_version FROM location_parses")
    }

    stats = Counter()
    changed: list[str] = []
    for r in rows:
        p = parse_location(r["id"], r["remote_path"], r["filetype"])
        payload = json.dumps(p.to_json(), ensure_ascii=False, sort_keys=True)
        prev = existing.get(r["id"])
        if prev == (payload, version):
            stats["unchanged"] += 1
            continue
        if prev is None:
            stats["inserted"] += 1
        elif prev[0] != payload:
            stats["reparsed"] += 1
            if len(changed) < 50:
                changed.append(r["remote_path"])
        else:
            stats["restamped"] += 1  # same verdict, newer parser: refresh the stamp only

        if not dry_run:
            conn.execute(
                "INSERT INTO location_parses (location_id, artist, title, disc_series, disc_id,"
                " disc_track, catalogue_code, is_instrumental, language, layout, confidence,"
                " payload, parser_version, parsed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(location_id) DO UPDATE SET"
                "   artist=excluded.artist, title=excluded.title,"
                "   disc_series=excluded.disc_series, disc_id=excluded.disc_id,"
                "   disc_track=excluded.disc_track, catalogue_code=excluded.catalogue_code,"
                "   is_instrumental=excluded.is_instrumental, language=excluded.language,"
                "   layout=excluded.layout, confidence=excluded.confidence,"
                "   payload=excluded.payload, parser_version=excluded.parser_version,"
                "   parsed_at=excluded.parsed_at",
                (
                    r["id"], p.artist, p.title, p.disc_series, p.disc_id, p.disc_track,
                    p.catalogue_code, p.is_instrumental, p.language, p.layout, p.confidence,
                    payload, version, now,
                ),
            )
    # A location that vanished from file_locations leaves a dangling parse. Nothing deletes
    # locations today (§5.1 removals are reported, not mutated), so this is a guard, not a path.
    stale = set(existing) - {r["id"] for r in rows}
    if stale and not dry_run:
        conn.executemany(
            "DELETE FROM location_parses WHERE location_id=?", [(i,) for i in stale]
        )
    if not dry_run:
        conn.commit()
    return {
        "parser_version": version,
        "locations": len(rows),
        **dict(stats),
        "stale_deleted": len(stale),
        "changes": stats["inserted"] + stats["reparsed"] + stats["restamped"],
        "sample_reparsed": changed,
    }


# --- §6.3 provisional MP3+CDG pairing ----------------------------------------------------


def _dir_stem_groups(rows) -> dict[tuple[str, str], dict[str, set[str]]]:
    """Group mp3/cdg locations by (directory, case-folded basename) — §6.3's pairing rule.

    Over the FULL pre-exclusion listing. See provisional_pairs in schema.sql for why: pairing
    over survivors alone shreds 1,120 measured pairs into orphans.
    """
    groups: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        p = Path(r["remote_path"])
        groups[(str(p.parent), p.stem.lower())][r["filetype"]].add(r["gdrive_md5"])
    return groups


def _pair_over_listing(rows) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    """Returns (pairs keyed on (audio_md5, graphics_md5), basename-collision groups)."""
    pairs: dict[tuple[str, str], dict] = {}
    collisions: list[dict] = []
    for (folder, stem), v in sorted(_dir_stem_groups(rows).items()):
        mp3s, cdgs = v.get("mp3", set()), v.get("cdg", set())
        if not (mp3s and cdgs):
            continue
        if len(mp3s) > 1 or len(cdgs) > 1:
            # Drive permits duplicate names in one folder (§5.1), so one basename can carry two
            # different mp3s. Which cdg belongs to which is unknowable from the listing — §6.3
            # says route it to review rather than guess.
            collisions.append(
                {"folder": folder, "stem": stem,
                 "audio_md5s": sorted(mp3s), "graphics_md5s": sorted(cdgs)}
            )
            continue
        key = (next(iter(mp3s)), next(iter(cdgs)))
        rec = pairs.setdefault(key, {"witnesses": 0, "example_path": f"{folder}/{stem}"})
        rec["witnesses"] += 1
    return pairs, collisions


# Recovery keys for orphan CDGs whose mp3 sits in the same folder under a differently-typed
# basename. Both are scoped to the file's own directory, so they can only ever re-unite halves
# that §6.3's exact-basename rule was already looking at and narrowly missed.
#
# `disc` is the stronger of the two and is tried first: a catalogue number identifies
# exactly one track on exactly one disc, so it survives damage that destroys the name outright.
# Both of the rows it recovers here prove the point — 'SF275-16 - Pink - Sober.cdg' pairs with
# 'SF275-16 - Sober - Pink.MP3' (artist/title typed in opposite orders on the two halves, so no
# name-based rule can match them), and SF217-05's two halves have had their middles mangled by a
# find-and-replace and still agree on the catalogue number.
#
# `name` catches the far commoner case of punctuation drift ('alesha -lipstick' / 'alesha-lipstick').


def _disc_key(location_id: int, remote_path: str, filetype: str) -> tuple | None:
    """(directory, series, disc, track) — catalogue identity."""
    p = parse_location(location_id, remote_path, filetype)
    if not (p.disc_series and p.disc_id and p.disc_track):
        return None
    return (str(Path(remote_path).parent), p.disc_series, p.disc_id, p.disc_track)


def _parse_key(location_id: int, remote_path: str, filetype: str) -> tuple | None:
    """(directory, artist, title) — name identity, as the §6.2-gated parser reads it."""
    p = parse_location(location_id, remote_path, filetype)
    if not p.title:
        return None
    return (str(Path(remote_path).parent), (p.artist or "").strip().lower(), p.title.strip().lower())


RECOVERY_KEYS = (("disc", _disc_key), ("name", _parse_key))


def _pair_verdicts(conn) -> tuple[list[dict], set[str]]:
    """Resolved orphan-cdg review verdicts: (confirmed payloads, rejected graphics_md5s).

    `pair_mp3g` rebuilds `provisional_pairs` from scratch on every run, so a confirmed pair
    written directly into that table would be silently wiped by the next re-run. The durable
    fact is the verdict in `review_queue.resolution`; the pairing pass re-applies it each
    rebuild, the same way it re-derives everything else from the listing.
    """
    confirmed: list[dict] = []
    rejected: set[str] = set()
    rows = conn.execute(
        "SELECT payload, resolution FROM review_queue "
        "WHERE kind='pair_mismatch' AND resolution IS NOT NULL"
    )
    for r in rows:
        payload = json.loads(r["payload"])
        if payload.get("kind") != "orphan_cdg_candidate":
            continue
        verdict = json.loads(r["resolution"]).get("verdict")
        if verdict == "confirm":
            confirmed.append(payload)
        elif verdict == "reject":
            rejected.add(payload["graphics_md5"])
    return confirmed, rejected


def resolve_pair_mismatch(conn, rq_id: int, verdict: str, source: str, note: str = "") -> None:
    """Record a reviewer's verdict on one pair_mismatch row (§3.7, §6.3).

    Same discipline as the goldset ingest: a verdict already given is never overwritten, and
    `verdict_source` records who decided, so a later reader can audit the call. The verdict
    takes effect on the next `pair` run — see `_pair_verdicts`.
    """
    if verdict not in ("confirm", "reject"):
        raise ValueError(f"verdict must be 'confirm' or 'reject', not {verdict!r}")
    row = conn.execute(
        "SELECT kind, resolution FROM review_queue WHERE id=?", (rq_id,)
    ).fetchone()
    if row is None or row["kind"] != "pair_mismatch":
        raise ValueError(f"review_queue row {rq_id} is not an open pair_mismatch")
    if row["resolution"] is not None:
        raise ValueError(f"row {rq_id} is already resolved; verdicts are never overwritten")
    from . import db

    resolution = {"verdict": verdict, "verdict_source": source, "comment": note}
    conn.execute(
        "UPDATE review_queue SET resolution=?, resolved_at=? WHERE id=?",
        (json.dumps(resolution, ensure_ascii=False), db.utcnow(), rq_id),
    )
    conn.commit()


def pair_mp3g(conn, dry_run: bool = False) -> dict[str, Any]:
    """§6.3 provisional MP3+CDG pairing, expressed at content level.

    Pairs are computed over the full pre-exclusion listing and stored keyed on gdrive_md5, so
    §5.2's pair-blind survivor rule cannot split them (see schema.sql, provisional_pairs).

    Orphan handling per §6.3, with one deliberate softening. §6.3 says orphan CDG → excluded, and
    for a cdg with no mp3 anywhere that is right. But its pairing rule is exact-basename, and this
    library has cdgs whose mp3 sits in the same folder under a stem differing by one space
    ('alesha -lipstick.cdg' / 'alesha-lipstick.mp3'). Auto-excluding those loses a working track
    to a typo. Rather than widen the pairing rule (that would be guessing, and §6.3 explicitly
    refuses to guess), an orphan cdg whose parsed (artist, title) matches an mp3 in its own
    directory goes to `review_queue(pair_mismatch)` — the same escape hatch §6.3 already uses for
    the collision case — and is left downloadable pending a human. Only orphans with no candidate
    at all are excluded.

    Idempotent (§1.3): desired state is recomputed from the listing and diffed. Exclusion is
    reversible — if an mp3 later appears on Drive, its cdg is restored to remote_only rather than
    stranded as excluded forever (the §5.2 survivor bug, same shape).
    """
    from . import config, db

    now = db.utcnow()
    rows = conn.execute(
        "SELECT id, remote_path, filetype, gdrive_md5, status, archive_reason FROM file_locations "
        "WHERE filetype IN ('mp3','cdg') AND remote_path IS NOT NULL AND gdrive_md5 IS NOT NULL"
    ).fetchall()

    pairs, collisions = _pair_over_listing(rows)

    # Re-apply reviewed verdicts (see _pair_verdicts): a confirmed orphan-cdg candidate becomes
    # a real pair on every rebuild; a rejected one is forced to true-orphan below.
    confirmed_payloads, rejected_graphics = _pair_verdicts(conn)
    for p in confirmed_payloads:
        for audio_md5 in p["candidate_audio_md5s"]:
            pairs.setdefault(
                (audio_md5, p["graphics_md5"]),
                {"witnesses": 1, "example_path": p["remote_path"]},
            )

    paired_audio = {a for a, _ in pairs}
    paired_graphics = {c for _, c in pairs}

    # Near-miss recovery for orphan cdgs, using the §6.2-gated parser as the matcher.
    mp3_index: dict[str, dict[tuple, set[str]]] = {name: defaultdict(set) for name, _ in RECOVERY_KEYS}
    for r in rows:
        if r["filetype"] != "mp3":
            continue
        for name, keyfn in RECOVERY_KEYS:
            k = keyfn(r["id"], r["remote_path"], r["filetype"])
            if k:
                mp3_index[name][k].add(r["gdrive_md5"])

    # A cdg caught in a basename collision has no pair, so it looks like an orphan — but the
    # collision row already asks the reviewer about exactly this folder+basename. Re-queuing it
    # as an orphan candidate would ask the same question twice under two different framings, and
    # excluding it would pre-empt the very verdict being asked for. Leave it to its collision row.
    collision_graphics = {md5 for c in collisions for md5 in c["graphics_md5s"]}

    orphan_cdg_rows = [
        r
        for r in rows
        if r["filetype"] == "cdg"
        and r["gdrive_md5"] not in paired_graphics
        and r["gdrive_md5"] not in collision_graphics
    ]
    recoverable: list[dict] = []
    true_orphan_md5: set[str] = set()
    for r in orphan_cdg_rows:
        if r["gdrive_md5"] in rejected_graphics:
            # A reviewer already looked at this cdg's candidates and said no. Re-queueing it
            # would re-ask a settled question; it is a true orphan and §6.3 excludes it.
            true_orphan_md5.add(r["gdrive_md5"])
            continue
        for name, keyfn in RECOVERY_KEYS:
            k = keyfn(r["id"], r["remote_path"], r["filetype"])
            cands = sorted(mp3_index[name].get(k, set())) if k else []
            if cands:
                recoverable.append(
                    {"location_id": r["id"], "remote_path": r["remote_path"],
                     "graphics_md5": r["gdrive_md5"], "candidate_audio_md5s": cands,
                     "matched_on": name, "match_key": list(k[1:]),
                     "reason": f"basename differs from its mp3, but {name} identity matches "
                               f"an mp3 in this same folder — confirm before excluding the cdg"}
                )
                break
        else:
            true_orphan_md5.add(r["gdrive_md5"])

    # --- §11 budget check, BEFORE committing anyone to a review -------------------------
    queued = [{"kind": "basename_collision", **c} for c in collisions] + [
        {"kind": "orphan_cdg_candidate", **c} for c in recoverable
    ]
    open_now = db.review_queue_sizes(conn).get("pair_mismatch", 0)
    over_budget = open_now + len(queued) > config.REVIEW_QUEUE_BUDGET

    # --- write ---------------------------------------------------------------------------
    plan: list[tuple[str, str]] = []
    newly_queued = 0
    if not dry_run and not over_budget:
        conn.execute("DELETE FROM provisional_pairs")
        conn.executemany(
            "INSERT INTO provisional_pairs (audio_md5, graphics_md5, witnesses, example_path,"
            " created_at) VALUES (?,?,?,?,?)",
            [(a, c, v["witnesses"], v["example_path"], now) for (a, c), v in sorted(pairs.items())],
        )
        newly_queued = _queue_pair_mismatches(conn, queued, now)

    # Orphan-cdg exclusion. Only rows this stage owns are touched: an exact_dup exclusion belongs
    # to Stage 0 and stays as it is (it is already excluded and already never downloaded, so
    # overwriting its reason would destroy Stage 0's bookkeeping for no gain).
    under_review = {r["graphics_md5"] for r in recoverable} | collision_graphics
    excluded = restored = 0
    for r in rows:
        if r["filetype"] != "cdg" or r["archive_reason"] == "exact_dup":
            continue
        if r["status"] not in ("remote_only", "excluded"):
            continue  # staged/active/archived belong to later stages
        orphan = r["gdrive_md5"] in true_orphan_md5 and r["gdrive_md5"] not in under_review
        want = ("excluded", "orphan_cdg") if orphan else ("remote_only", None)
        have = (r["status"], r["archive_reason"])
        if want == have:
            continue
        if orphan:
            excluded += 1
        else:
            restored += 1
        if len(plan) < 50:
            plan.append(("exclude" if orphan else "restore", r["remote_path"]))
        if not dry_run and not over_budget:
            conn.execute(
                "UPDATE file_locations SET status=?, archive_reason=?, updated_at=? WHERE id=?",
                (want[0], want[1], now, r["id"]),
            )
    if not dry_run and not over_budget:
        conn.commit()

    orphan_mp3_md5 = {
        r["gdrive_md5"] for r in rows if r["filetype"] == "mp3" and r["gdrive_md5"] not in paired_audio
    }
    return {
        "over_budget": over_budget,
        "pairs": {
            "content_pairs": len(pairs),
            "paired_audio_blobs": len(paired_audio),
            "paired_graphics_blobs": len(paired_graphics),
            "graphics_shared_across_audio": sum(
                1 for c, n in Counter(c for _, c in pairs).items() if n > 1
            ),
        },
        "orphan_mp3": {
            "blobs": len(orphan_mp3_md5),
            "note": "kept: future audio_only + vocal-presence check; never archived (§6.3)",
        },
        "orphan_cdg": {
            "excluded_locations": excluded,
            "restored_locations": restored,
            "true_orphan_blobs": len(true_orphan_md5),
            "recoverable_queued": len(recoverable),
        },
        "review_queue": {
            # `candidates` is what this run identified; `newly_queued` is what it actually
            # inserted. They differ on a re-run, where every question is already on the queue —
            # and §11 exists so the operator can trust this number as the review burden, so it
            # must not report 16 new questions when it asked none.
            "pair_mismatch_candidates": len(queued),
            "pair_mismatch_newly_queued": newly_queued,
            "basename_collisions": len(collisions),
            "orphan_cdg_candidates": len(recoverable),
        },
        "plan": plan,
    }


def _queue_pair_mismatches(conn, payloads: list[dict], now: str) -> int:
    """Insert pair_mismatch rows (§3.7, §6.3), never duplicating an existing question.

    Idempotent on the payload's identity, same discipline as write_goldset: a re-run must not
    ask the same question twice, and must never overwrite a verdict already given.
    """
    existing = {
        r["k"]
        for r in conn.execute(
            "SELECT COALESCE(json_extract(payload,'$.location_id'),"
            "                json_extract(payload,'$.folder')||'/'||json_extract(payload,'$.stem'))"
            "       AS k FROM review_queue WHERE kind='pair_mismatch'"
        )
        if r["k"] is not None
    }
    added = 0
    for p in payloads:
        key = p.get("location_id") or f"{p.get('folder')}/{p.get('stem')}"
        if key in existing:
            continue
        conn.execute(
            "INSERT INTO review_queue (kind, payload, created_at) VALUES ('pair_mismatch',?,?)",
            (json.dumps(p, ensure_ascii=False), now),
        )
        added += 1
    return added


def parse_stats(conn) -> dict[str, Any]:
    """Full-library parse statistics — a dry run (§1.3), writes nothing.

    This is the evidence for the §6.2 gate: it says how the parser behaves at 53k scale before
    anyone commits to reviewing 200 rows or to a mass insert.
    """
    rows = conn.execute(
        "SELECT id, remote_path, filetype FROM file_locations WHERE remote_path IS NOT NULL"
    ).fetchall()
    layout = Counter()
    lang = Counter()
    instr = Counter()
    flags = Counter()
    series = Counter()
    notes = Counter()
    no_artist = 0
    no_title = 0
    conf_sum = 0.0
    for r in rows:
        p = parse_location(r["id"], r["remote_path"], r["filetype"])
        layout[p.layout] += 1
        lang[p.language] += 1
        instr[p.is_instrumental] += 1
        for f in p.flags:
            flags[f] += 1
        for n in p.notes:
            notes[n.split(":")[0]] += 1
        if p.disc_series:
            series[p.disc_series] += 1
        no_artist += p.artist is None
        no_title += p.title is None
        conf_sum += p.confidence
    n = len(rows) or 1
    return {
        "locations_parsed": len(rows),
        "mean_confidence": round(conf_sum / n, 3),
        "by_layout": dict(layout.most_common()),
        "by_language": dict(lang.most_common()),
        "is_instrumental": dict(instr.most_common()),
        "disc_series": dict(series.most_common(12)),
        "top_flags": dict(flags.most_common(12)),
        "notes": dict(notes.most_common(10)),
        "missing_artist": {"count": no_artist, "pct": round(100 * no_artist / n, 1)},
        "missing_title": {"count": no_title, "pct": round(100 * no_title / n, 1)},
    }
