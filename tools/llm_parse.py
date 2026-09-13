#!/usr/bin/env python3
"""§6.1 extension: LLM segmentation of the FULL PATH, for the layouts the regex parser could not resolve.

WHY THIS EXISTS
---------------
§6.1 makes the FILENAME this library's identity of record — ID3 is absent or useless across
whole source batches, and migration 003 already demoted it below `filename` for that reason. The
stage1 regex parser answers 46,425 of 53,674 locations with a structurally unambiguous layout
(`artist_title` 34,384 + `disc_artist_title` 12,041, all Latin). Those are NOT in this tool's
workset: a name with one hyphen and a Latin script has nothing left to decide, and running a
model over it can only introduce error.

What the regex CANNOT answer (measured 2026-08-11, `location_parses`):

    title_artist              he 1,853  latn 106      order not recoverable from punctuation
    title_only                he   579  latn 401 · ru 14 · unknown 17
    title_artist_from_folder  he   222  latn 207      the artist lives in a PARENT FOLDER
    disc_title_only           latn  65
    style_of                  latn  38
                              --------------------------------------------------
                              3,502 rows / 3,243 distinct stems

    artist_title              he 1,897   parsed, but Hebrew carries no reliable order cue, so
                                         which side is the artist is close to a coin flip

Union workset: 5,399 rows / 4,862 distinct stems (53,674 locations collapse to 29,399 stems —
mp3/cdg pairs share one, and so does every duplicate copy across folders).

THE HEBREW GAP, AND WHY NO CATALOGUE CLOSES IT
----------------------------------------------
Migration 006 measured the ceiling directly (n=40 Hebrew / n=25 Hebrew title-only, 2026-07-31):
Wikidata found the entity for 82.5% and MusicBrainz's ARTIST index found the artist for 62.5%,
both 100% in Hebrew script. But an arid-scoped recording search using an artist MBID we had
just confirmed produced a `song_mbid` for only 4/25 (16.0%). MusicBrainz knows the Israeli
ARTISTS; it does not hold their RECORDINGS. iTunes matched 5.0% of the same items and Deezer
2.5% — both useless for Hebrew and deliberately not run on it.

So for the Hebrew order population there is usually NO external row at all, at any precedence
rank. The segmentation has to come from reading the text, which is what this tool does. What it
buys is SEGMENTATION and canonical Hebrew spelling — never a `song_mbid`, which no source in
this project can supply for these items.

WHAT THIS CANNOT DO
-------------------
626 `opaque` locations + 1 `unparsed` — `SONG-<uuid>.mp4`, `Chapter_12-1112.avi`,
`Title_1001.avi` — carry ZERO information in the filename. No amount of reading recovers a name
that was never written down. They are excluded by construction and this source must never
invent one for them; they need title-card OCR (§9.1, karaokemp/titlecard.py) or a fingerprint.
137 `non_media` rows are excluded for the obvious reason.

THE SWEEP — AND WHY THE LAYOUT FILTER ABOVE IS NO LONGER TRUSTED
----------------------------------------------------------------
Everything above rests on one claim: that Latin `artist_title` / `disc_artist_title` are
structurally unambiguous and therefore SAFE TO SKIP. The §6.2 control arm exists precisely to
measure that claim rather than assume it, and measured 2026-08-11 it FALSIFIED it. n=30 control
stems drawn from those "trusted" layouts: 26 agreed with the regex, and all 4 disagreements
were the MODEL being right and the REGEX being wrong —

    Ernie Maresca - Shoutkaraoke            regex title `Shoutkaraoke`: a decoration fused
                                            into the title with NO separator to split on
    Irish Karaoke - Whiskey In The Jar [..]  regex artist `Irish`: that is a label, not a
                                            performer
    olivia n john-banks of the ohio         regex artist `olivia n john`; the parent folder
                                            spells the name correctly
    ZPBX1-1-06 - Aqua - Barbie Girl Duet    regex put the DISC ID in the artist field and
                                            fused artist+title into the title

0/4 in the regex's favour is not a filter that is "mostly safe" — it is a filter with no
measured floor at all. Owner's decision (sha-yol, 2026-08-11): stop extending the decoration
vocabulary one token at a time, stop treating any regex layout as trustworthy, run ONE pass
over everything that could still benefit, and be done.

`extract --sweep` is that pass. It drops the layout filter entirely and keeps two filters:

  1. ALREADY ANSWERED — and it takes BOTH halves. Skip a stem only when an item it maps to
     carries a `song_mbid` AND already has both a non-empty artist and a non-empty title whose
     WINNING row in `v_metadata` outranks `llm_parse`. Either half alone gets this wrong, in
     opposite directions:

       * MBID ALONE was the first version, and it is wrong for `musicbrainz_fp`. Measured
         2026-08-11, the fingerprint source wrote 3,511 mbids and only 3,260 titles; the 131
         items in that gap hold an identifier and NO winning title, which is exactly where an
         `llm_parse` title WOULD be the value `v_metadata` selects. 134 stems, excluded by
         accident from the one population this pass exists to serve.
       * NAMED ALONE drops 3,092 further stems: items named above rank 4 by `itunes_text`
         (1,610), `spotify_text` (650), `musicbrainz_artist` (592), `deezer_text` (169),
         `manual` (36), `wikidata` (4) and `title_card_ocr` (1), with no mbid anywhere.

     Keeping those 3,092 IN is a SPENDING DECISION, not an inertness argument, and the honest
     version is this: at today's ladder an `llm_parse` row for them is genuinely inert — rank 4
     sits below every one of those sources, so the row is written and never selected. They are
     parsed anyway because migration 007 flags rank 4 as PROVISIONAL pending the §6.2 gate. If
     the gate promotes `llm_parse` above the music catalogues, the parses already exist and
     this pass does not have to be re-run against a moved ladder. Cost: ~15 extra batches for
     rows that cannot surface today (sha-yol, 2026-08-11).
  2. A `media_item` must exist. `location_parses` runs over ALL locations including excluded
     ones, so 786 stems resolve to no active item; `promote` counts those `skipped_no_item` and
     writes nothing for them, so sending them to an agent buys nothing either.

`non_media` is still excluded. `opaque`/`unparsed` are NOT — and that is the one deliberate
reversal of the section above. The layout-filtered workset drops them because the FILENAME
carries nothing, which is true; the sweep sends them anyway because the PARENT FOLDER sometimes
names a disc or a performer, and rule 2 of the prompt makes null the required answer when it
genuinely cannot tell. A null costs one line of a batch; a name never recovered costs the item.

Measured 2026-08-11 on the live index (read-only): 29,399 stems total, 64 all-`non_media`,
20,013 already answered, 786 with no media_item ⇒ 8,536 stems / 43 batches of 200.

    latn artist_title             4,341     he   artist_title            1,014
    latn disc_artist_title        1,048     he   title_artist              864
    latn title_only                 261     he   title_only                437
    latn title_artist_from_folder   150     he   title_artist_from_folder  112
    latn opaque                     140     ru   artist_title               10
    latn disc_title_artist           65     ru   title_only                  8
    latn title_artist                49     unknown title_only              8
    latn disc_title_only             22     unknown opaque                  1
    latn style_of                     5     latn unparsed                   1

The intersection is what makes 8,536 the number rather than 8,402 or 5,444: mbid-alone skips
20,147 stems, named-alone skips 23,105, and the two sets are NOT nested — their intersection is
20,013. 29,335 non-`non_media` stems - 786 no-item - 20,013 answered = 8,536.

The layout-filtered mode is NOT deleted. `in_workset` / `is_control` and both goldset arms stay
exactly as they were — the sweep is an additional mode, and the gate that produced the evidence
against the filter has to remain reproducible.

PRECEDENCE
----------
Migration 007 ranks `llm_parse` at 4 — below every corroborated external catalogue (each of
those was accepted only on agreement with our own text, §3.6), above the `filename` regex parse
it replaces. A bad LLM parse therefore CANNOT override a confirmed catalogue match. The rank is
PROVISIONAL pending the §6.2 golden-set gate; see `extract --goldset`.

THE FOUR SUBCOMMANDS
--------------------
    tools/llm_parse.py extract --dry-run            # plan the workset, write nothing (§13)
    tools/llm_parse.py extract                      # emit batch-NNN.json + manifest.json
    tools/llm_parse.py extract --sweep              # EVERYTHING that could still benefit
    tools/llm_parse.py extract --goldset 200        # §6.2 gate: stratified + CONTROL arm
    tools/llm_parse.py extract --goldset-swaps 200  # §6.2 gate: the SELF-SCORING swap arm
    tools/llm_parse.py extract --decorations        # read-only §6.1 decoration-token report
    tools/llm_parse.py prompt --batch <file>        # exact system+user text, to hand to an agent
    tools/llm_parse.py ingest --results <file>      # validate strictly, requeue what fails
    tools/llm_parse.py promote --dry-run            # song_metadata rows, source='llm_parse'

Resumable and idempotent (§13): `extract` skips any stem already in `llm_parses` AT ANY PROMPT
VERSION, so a Ctrl-C mid-emit costs nothing and a second `promote` back-to-back makes exactly
zero changes. `llm_parses` survives `DELETE FROM song_metadata WHERE source='llm_parse'` on
purpose — re-tuning `config.LLM_PARSE_FLOOR` must never require re-running the agents, the same
lesson as `enrich_cache`/`mb_cache` (§11).

"AT ANY VERSION" rather than "at the current version" is what makes a PROMPT_VERSION bump
fix-forward: v2 changed the prompt without re-parsing the 1,594 stems v1 had already answered.
`extract --reparse` is the opt-in that asks them again. The corpus is therefore MIXED, and
anything comparing prompt revisions has to group by `prompt_version` — see PROMPT_VERSION.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from karaokemp import config, db  # noqa: E402

SOURCE = "llm_parse"
STAGE = "llm_parse"

# The prompt this file currently contains. It exists so `llm_parses.prompt_version` records
# WHICH prompt produced a row: two materially different prompts under one version string would
# make that column a lie, and every later comparison would be against a corpus it cannot
# describe.
#
# v2 (2026-08-12) fixed rule 8's layout vocabulary and the transliteration guard's scope.
#
# FIX-FORWARD, AND WHAT IT COSTS. A bump used to mean "every stem is eligible again". It does
# not any more: 1,594 stems (1,200 wave-one sweep + 394 goldset) were answered at v1, and the
# owner's instruction is that they are NOT re-parsed. `_cached_stems` therefore skips a stem
# that has been answered at ANY version, and `--reparse` is the explicit opt-in for asking again
# at the current one.
#
# The price is paid in the corpus, not in tokens, and it is real: `llm_parses` now holds a MIX
# of v1 and v2 rows. Those v1 rows carry the two defects v2 fixes — a `layout` drawn from a
# vocabulary that could not express `title_artist_from_folder` or `disc_title_artist`, and
# artist/title text that could only be copied verbatim wherever a Hebrew folder was present. So
# any later accuracy comparison, retune or gate measurement MUST GROUP BY `prompt_version`.
# Reading the table as one homogeneous population will attribute v1's prompt defects to the
# model, and the §6.2 goldset — all 394 stems of it answered at v1 — is exactly the arm most
# likely to be read that way.
PROMPT_VERSION = "v2"

BATCH_SIZE = 200
GOLDSET_SIZE = 200          # §6.2 says 200 filenames, human-verified, before mass insert
GOLDSET_CONTROL_FRAC = 0.15  # share of the goldset drawn from the EXCLUDED layouts

# Layouts the regex parser emitted as an admission that it could not decide. These are the
# workset.
HARD_LAYOUTS = frozenset({
    "title_artist",              # order not recoverable from punctuation
    "title_only",                # no artist segmented at all
    "title_artist_from_folder",  # the artist is in a parent folder, unverified
    "disc_title_only",
    "style_of",                  # "X in the style of Y", decoration-heavy
})

# `artist_title` PARSED cleanly, but only Latin script actually pins the order. Hebrew does not:
# both "<artist> - <title>" and "<title> - <artist>" are idiomatic in this library's filenames.
AMBIGUOUS_ARTIST_TITLE_LANGS = frozenset({"he"})

# Structurally unambiguous — deliberately NOT in the workset. They are the goldset's CONTROL arm:
# the point of sampling them is to verify the filter is safe to skip, not to assume it.
#
# `artist_title` appears here AND in the workset, split by language: latn is unambiguous, he is
# not. `is_control` resolves that by taking the COMPLEMENT of `in_workset` — otherwise the 1,897
# Hebrew artist_title stems would be sampled into both arms at once, and the control arm would be
# asserting "safe to skip" about the very rows this tool exists to fix.
CONTROL_LAYOUTS = frozenset({"artist_title", "disc_artist_title"})

# Filename carries zero information (opaque/unparsed) or is not media at all. Unrecoverable
# here by construction; never emitted, never guessed at.
UNRECOVERABLE_LAYOUTS = frozenset({"opaque", "unparsed", "non_media"})

# The SWEEP's entire layout filter. `non_media` is not a song and there is nothing to segment;
# every other layout — including `opaque`/`unparsed`, which the layout-filtered workset drops —
# is admitted. See the module docstring: the §6.2 control arm falsified the premise that any
# regex layout is safe to skip, so exclusion is now the thing that has to be argued for.
SWEEP_EXCLUDED_LAYOUTS = frozenset({"non_media"})

# THE PIPELINE'S OWN LAYOUT VOCABULARY — `SELECT DISTINCT layout FROM location_parses`, read
# off the live index read-only 2026-08-12. This is the ONE authoritative list; every other
# vocabulary in this file is derived from it so the three cannot drift apart again.
#
#     artist_title             36,307      opaque                      626
#     disc_artist_title        12,041      title_artist_from_folder    429
#     title_artist              1,959      non_media                   137
#     disc_title_artist         1,060      disc_title_only              65
#     title_only                1,011      style_of                     38
#                                          unparsed                      1
PIPELINE_LAYOUTS = frozenset({
    "artist_title", "disc_artist_title", "title_artist", "disc_title_artist", "title_only",
    "opaque", "title_artist_from_folder", "non_media", "disc_title_only", "style_of",
    "unparsed",
})

# WHAT THE MODEL MAY ANSWER — DERIVED from `PIPELINE_LAYOUTS`, never retyped beside it. Rule 8
# of the system prompt is rendered from this same constant, so a value the prompt tells the
# model to emit CANNOT be one the validator rejects. v1 had the two lists written out by hand
# and they disagreed; that is the defect this derivation exists to make impossible.
#
# Two pipeline layouts are withheld on purpose, and neither is a vocabulary gap:
#   * `non_media` — excluded from the sweep by construction (SWEEP_EXCLUDED_LAYOUTS), so the
#     model is never shown one, and "this is not a song" is not a call it should be inventing.
#   * `unparsed` — the REGEX's admission that none of its patterns fired, which says nothing
#     about the path. The model's equivalent is `opaque`, which rule 8 names explicitly. 1 row
#     library-wide.
MODEL_LAYOUTS = PIPELINE_LAYOUTS - {"non_media", "unparsed"}

# `artist_from_folder` was in v1's rule 8 and is NOT a layout this pipeline has ever emitted —
# it was invented in the prompt. It stays ACCEPTED but is no longer OFFERED: 37 of the 1,594 v1
# rows already ingested carry it, and re-validating one of those batch files (a requeue, a
# re-ingest) must not start failing on a value we ourselves asked for. Nothing may be added to
# this set — a new name here means the prompt and the pipeline have drifted again.
LEGACY_LAYOUTS = frozenset({"artist_from_folder"})

# Measured 2026-08-11 on the emitted sweep: the pipeline uses `title_artist_from_folder` (429
# stems library-wide) and `disc_title_artist` (61 stems in the sweep), and v1's rule 8 could
# express NEITHER. 3 of 5 wave-one agents independently hit this and remapped to the nearest
# allowed value, so `layout` is lossy on ~490 stems' worth of v1 answers — recorded here rather
# than repaired, because those answers are deliberately not being re-parsed (see PROMPT_VERSION).
VALID_LAYOUTS = MODEL_LAYOUTS | LEGACY_LAYOUTS

MAX_SIBLINGS = 10   # disc context: enough to see "this is track 15 of a numbered disc"
MAX_PATHS = 8       # a stem with 40 copies teaches nothing after the first few distinct folders

# Sources treated as AUTHORITATIVE when mining the swap ground truth below. Each was accepted
# only on agreement with our own text (§3.6), so where one disagrees with the filename parse by
# an exact field SWAP, the parser is the one that was wrong.
EXTERNAL_SOURCES = ("musicbrainz_text", "musicbrainz_freetext", "musicbrainz_fp",
                    "musicbrainz_artist", "wikidata")

_HEB_RE = re.compile(r"[֐-׿]")
_LAT_RE = re.compile(r"[A-Za-z]")
_WS_RE = re.compile(r"\s+")


# --- stem normalization ----------------------------------------------------------------------
# The join key for EVERYTHING here. mp3/cdg pairs share a stem, and so does every duplicate copy
# of a song across folders, which is what collapses 53,674 locations to 29,399 stems and lets one
# agent answer serve every media item that carries it.

def stem_of(path: str | None) -> str:
    """Basename minus extension, NFC, whitespace-collapsed, lowercased.

    NFC because Drive hands back decomposed Hebrew for some batches and composed for others, and
    two spellings of one filename must be one key or the cache silently doubles.
    """
    if not path:
        return ""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base[1:]:
        base = base.rsplit(".", 1)[0]
    base = unicodedata.normalize("NFC", base)
    return _WS_RE.sub(" ", base).strip().lower()


def loc_path(row) -> str:
    """The path we show the agent. `member_path` (zip members) wins when set; this library
    currently has none, but the parser records them and the ordering must not silently flip."""
    return row["member_path"] or row["remote_path"] or row["local_path"] or ""


def dirname_of(path: str) -> str:
    p = (path or "").replace("\\", "/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def has_hebrew(s: str | None) -> bool:
    return bool(_HEB_RE.search(s or ""))


def has_latin(s: str | None) -> bool:
    return bool(_LAT_RE.search(s or ""))


def script_of(s: str | None) -> str:
    if has_hebrew(s):
        return "he"
    return "latn" if has_latin(s) else "unknown"


# --- the layout filter -----------------------------------------------------------------------

def in_workset(layout: str | None, language: str | None) -> bool:
    """Pure, so the one decision that defines this whole pass is testable in isolation.

    Deliberately NOT expressed as "everything except the excluded layouts": an unknown future
    layout must default to OUT, not to being fed to a model.
    """
    if layout in UNRECOVERABLE_LAYOUTS:
        return False
    if layout in HARD_LAYOUTS:
        return True
    return layout == "artist_title" and language in AMBIGUOUS_ARTIST_TITLE_LANGS


def is_control(layout: str | None, language: str | None) -> bool:
    """The goldset's control arm: a structurally unambiguous layout the workset SKIPS.

    Strictly the complement of `in_workset` within CONTROL_LAYOUTS. Hebrew `artist_title` is the
    case that forces this to be a function rather than a set membership test — it shares a layout
    name with the unambiguous Latin rows but is the ambiguous population itself.
    """
    return layout in CONTROL_LAYOUTS and not in_workset(layout, language)


def in_sweep(layout: str | None) -> bool:
    """The sweep's layout test. Pure, for the same reason `in_workset` is.

    DELIBERATELY the mirror image of `in_workset`: that one enumerates what is IN, so an unknown
    future layout defaults OUT and is never fed to a model. This one enumerates what is OUT, so
    an unknown future layout defaults IN. The inversion is the whole point of the sweep — after
    the §6.2 control arm came back 4/4 against the regex on the layouts we had assumed safe,
    "this layout does not need a model" is a claim that now needs evidence, and we have none for
    a layout we have never seen. Language is not consulted at all; the Hebrew/Latin split only
    ever existed to decide which `artist_title` rows to SKIP.
    """
    return layout not in SWEEP_EXCLUDED_LAYOUTS


# --- workset construction --------------------------------------------------------------------

def _sibling_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """dirname -> basenames. Built once over ALL locations, not just the workset: the disc
    context that disambiguates `15 <title>.avi` is its NEIGHBOURS, most of which parsed fine and
    are therefore absent from the workset."""
    idx: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT remote_path, member_path, local_path FROM file_locations"):
        p = loc_path(r)
        if p:
            idx[dirname_of(p)].append(p.replace("\\", "/").rsplit("/", 1)[-1])
    for k in idx:
        idx[k].sort()
    return idx


def _cached_stems(conn: sqlite3.Connection, prompt_version: str | None = None) -> set[str]:
    """Stems already answered. THE resumability mechanism: an interrupted run re-derives the
    same workset and simply emits less of it.

    `prompt_version=None`, the DEFAULT, means ANY version — an answered stem is answered, and a
    prompt bump does not un-answer it. That is what makes a bump fix-forward instead of 1,594
    stems of rework (see PROMPT_VERSION). Pass a version to ask the narrower question "answered
    at THIS prompt?", which is what `--reparse` needs: a deliberate re-parse must still be
    resumable, so a --reparse run that is Ctrl-C'd halfway must not re-emit what it already
    re-answered.
    """
    sql = "SELECT stem FROM llm_parses"
    params: tuple = ()
    if prompt_version is not None:
        sql += " WHERE prompt_version = ?"
        params = (prompt_version,)
    try:
        return {r[0] for r in conn.execute(sql, params)}
    except sqlite3.OperationalError:
        return set()   # migration 007 not applied yet; --dry-run must still work


# `llm_parse`'s ladder position in v_metadata's precedence CASE (5 since migration 008). This
# is the DOCUMENTED EXPECTATION, never a fallback — `_llm_parse_rank` reads the live number out
# of the schema and refuses to guess. The constant exists so a test can assert the two agree and
# catch a renumbering nobody told this tool about.
LLM_PARSE_RANK = 5


def _llm_parse_rank(conn: sqlite3.Connection) -> int:
    """Read `llm_parse`'s rank back out of v_metadata's own CASE, rather than trusting a copy.

    Migration 007 renumbered the WHOLE ladder in one go and says plainly that a promotion or
    demotion of this source is a FOLLOW-UP migration. A hardcoded 4 here would survive that
    migration silently and start filtering against a rank the database no longer uses.

    NO FALLBACK, ON PURPOSE. A default of 4 fires in exactly one situation — migration 007 has
    not been applied — and that is precisely the database where 4 means something ELSE. The
    pre-007 ladder puts `deezer_text` at 4 and `title_card_ocr` at 3, so "> 4" silently selects
    a different set of sources: measured 2026-08-11, `--sweep --dry-run` reports 5,624 stems
    against the live pre-007 DB where the post-007 working copy reports 5,444. A 180-stem error
    that announces nothing is worse than a refusal, so this refuses.
    """
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='view' "
                           "AND name='v_metadata'").fetchone()
        sql = (row[0] if row else "") or ""
    except (sqlite3.Error, TypeError, IndexError):
        sql = ""
    m = re.search(r"WHEN\s+'llm_parse'\s+THEN\s+(\d+)", sql)
    if m:
        return int(m.group(1))
    raise SystemExit(
        "refusing to build the sweep: v_metadata has no 'llm_parse' rung, so this database "
        "predates migration 007 (now folded into schema.sql).\n"
        "  the skip filter asks 'does a source outranking llm_parse already name this item?', "
        "which needs llm_parse's rank to mean anything.\n"
        "  the PRE-007 ladder numbers OTHER sources at 4 (deezer_text) and 3 (title_card_ocr), "
        "so guessing 4 here silently selects the wrong sources — measured at 180 stems.\n"
        "  recreate v_metadata from schema.sql first.")


def _stems_with_mbid(conn: sqlite3.Connection,
                     items_by_stem: dict[str, list[int]]) -> set[str]:
    """Stems where SOME media_item carries a `song_mbid`. ANY, not ALL — see below."""
    items = {r[0] for r in conn.execute(
        "SELECT DISTINCT media_item_id FROM song_metadata "
        "WHERE field = 'song_mbid' AND value IS NOT NULL")}
    if not items:
        return set()
    return {s for s, ids in items_by_stem.items() if any(i in items for i in ids)}


def _stems_named_above_llm_parse(conn: sqlite3.Connection,
                                 items_by_stem: dict[str, list[int]]) -> set[str]:
    """Stems where SOME media_item already has BOTH artist and title winning from a source that
    outranks `llm_parse` — i.e. where a parse would be written and then never selected.

    `v_metadata` has already resolved each (item, field) to its WINNER and exposes that winner's
    `source_rank`, so "rank > llm_parse's rank" is exactly the question `promote`'s output would
    be judged by. Empty strings are excluded explicitly: v_metadata filters only NULL, and a ''
    title is not a name.
    """
    rank = _llm_parse_rank(conn)
    covered: dict[int, set[str]] = defaultdict(set)
    for r in conn.execute(
            "SELECT media_item_id, field FROM v_metadata "
            "WHERE field IN ('artist','title') AND source_rank > ? "
            "AND TRIM(COALESCE(value, '')) <> ''", (rank,)):
        covered[r[0]].add(r[1])
    named = {i for i, fields in covered.items() if fields == {"artist", "title"}}
    if not named:
        return set()
    return {s for s, ids in items_by_stem.items() if any(i in named for i in ids)}


def _stems_already_answered(conn: sqlite3.Connection,
                            items_by_stem: dict[str, list[int]]) -> set[str]:
    """The sweep's skip set: the INTERSECTION of "has an mbid" and "already named above rank 4".

    Both halves are needed, and the two failures they each let through are why:

      * MBID ALONE was the original filter and it is wrong for `musicbrainz_fp`. Measured
        2026-08-11, the fingerprint source wrote 3,511 mbids and only 3,260 titles; the 131
        items in that gap hold an identifier and NO winning title, which is precisely where an
        `llm_parse` title WOULD be the value `v_metadata` selects. 134 stems. Requiring the
        NAMED half as well hands them back.
      * NAMED ALONE excludes 3,092 further stems the mbid rule kept: items whose winning
        artist+title come from `itunes_text` (1,610), `spotify_text` (650),
        `musicbrainz_artist` (592), `deezer_text` (169), `manual` (36), `wikidata` (4) and
        `title_card_ocr` (1) without any mbid at all. Requiring the MBID half as well keeps them
        in the pass.

    AND KEEPING THEM IN IS A SPENDING DECISION, NOT AN INERTNESS ARGUMENT — record that
    honestly. At the CURRENT ladder an `llm_parse` row for those 3,092 is genuinely inert: rank
    4 sits below every one of those sources, so the row is written and never selected. They are
    parsed anyway because migration 007 flags rank 4 as PROVISIONAL pending the §6.2 gate. If
    the gate promotes `llm_parse` above the music catalogues, those parses already exist
    and this pass does not have to be re-run against a moved ladder. The cost is ~15 extra
    batches of agent time for rows that cannot surface today (sha-yol, 2026-08-11).

    ANY, not ALL, on both halves: one stem serves every copy of one song, so a single covered
    item means the text the agent would be reading is already answered.
    """
    return _stems_with_mbid(conn, items_by_stem) & _stems_named_above_llm_parse(
        conn, items_by_stem)


def build_workset(conn: sqlite3.Connection, *, control: bool = False,
                  sweep: bool = False, include_cached: bool = False,
                  prompt_version: str = PROMPT_VERSION, reparse: bool = False,
                  stats: dict | None = None) -> tuple[list[dict], int]:
    """Every workset stem, deduped, with the full path of every location that carries it.

    Returns (records, n_cached_skipped). `control=True` builds the EXCLUDED unambiguous layouts
    instead — the goldset's control arm. `sweep=True` drops the layout filter and applies the
    two filters described in the module docstring instead: no `song_mbid`, and a `media_item`
    must exist. `stats`, when passed, is filled with the per-stem skip counts the sweep needs to
    print — an out-parameter rather than a wider return type, so the existing two callers and
    their tests keep the tuple they have.

    `reparse=False` (the default) skips any stem answered at ANY prompt version, so bumping
    PROMPT_VERSION costs nothing already spent. `reparse=True` narrows the skip to
    `prompt_version` alone, which is how a stem answered under an older prompt is deliberately
    asked again — and it stays resumable, because the run's own answers still count as cached.
    """
    rows = conn.execute("""
        SELECT lp.location_id, lp.artist, lp.title, lp.layout, lp.language,
               lp.disc_series, lp.disc_id, lp.disc_track, lp.confidence,
               fl.remote_path, fl.member_path, fl.local_path
        FROM location_parses lp
        JOIN file_locations fl ON fl.id = lp.location_id
        ORDER BY lp.location_id
    """).fetchall()

    siblings = _sibling_index(conn)
    cached = set() if include_cached else _cached_stems(
        conn, prompt_version if reparse else None)

    # Both maps are built ONCE, outside the row loop, and the stem->items map is built once and
    # SHARED. `stem_to_items` is a full scan of file_locations x media_item_files; doing it per
    # row turns an 8k-stem plan into an hour, and doing it twice doubles the plan's cost.
    items_by_stem = stem_to_items(conn) if sweep else {}
    answered_stems = _stems_already_answered(conn, items_by_stem) if sweep else set()
    skipped_no_item: set[str] = set()
    skipped_answered: set[str] = set()

    by_stem: dict[str, dict] = {}
    n_cached = 0
    for r in rows:
        if sweep:
            wanted = in_sweep(r["layout"])
        elif control:
            wanted = is_control(r["layout"], r["language"])
        else:
            wanted = in_workset(r["layout"], r["language"])
        if not wanted:
            continue
        path = loc_path(r)
        stem = stem_of(path)
        if not stem:
            continue
        if stem in cached:
            n_cached += 1
            continue
        if sweep:
            # Counted as SETS, not as row counters: these are per-stem facts and a stem with 40
            # copies must not report as 40 skips.
            if not items_by_stem.get(stem):
                skipped_no_item.add(stem)
                continue
            if stem in answered_stems:
                skipped_answered.add(stem)
                continue
        rec = by_stem.get(stem)
        if rec is None:
            rec = by_stem[stem] = {
                "stem": stem,
                "paths": [],
                "current": {
                    "artist": r["artist"], "title": r["title"],
                    "layout": r["layout"], "language": r["language"],
                    "disc_series": r["disc_series"], "disc_id": r["disc_id"],
                    "disc_track": r["disc_track"],
                },
                "siblings": [],
                "_dirs": set(),
                "_locations": [],
            }
        rec["_locations"].append(r["location_id"])
        # FULL paths, not basenames: parent folders routinely carry the artist, e.g.
        # `קריוקי בעברית 1/.-דיסקים קריוקי/58 חלומות/15 <title>.avi`. That folder is the only
        # place the artist appears, which is the whole reason `title_artist_from_folder` exists.
        if path not in rec["paths"] and len(rec["paths"]) < MAX_PATHS:
            rec["paths"].append(path)
        rec["_dirs"].add(dirname_of(path))

    out = []
    for stem, rec in by_stem.items():
        sibs: list[str] = []
        for d in sorted(rec.pop("_dirs")):
            for b in siblings.get(d, []):
                if stem_of(b) != stem and b not in sibs:
                    sibs.append(b)
                if len(sibs) >= MAX_SIBLINGS:
                    break
            if len(sibs) >= MAX_SIBLINGS:
                break
        rec["siblings"] = sibs
        rec["locations"] = rec.pop("_locations")
        rec["script"] = script_of(stem) if rec["current"]["language"] in (None, "unknown") \
            else rec["current"]["language"]
        out.append(rec)
    out.sort(key=lambda r: r["stem"])
    if stats is not None:
        stats["skipped_no_item"] = len(skipped_no_item)
        stats["skipped_already_answered"] = len(skipped_answered)
        stats["cached_locations"] = n_cached
    return out, n_cached


# --- §6.2 golden set -------------------------------------------------------------------------

def stratify(records: list[dict], controls: list[dict], n: int,
             *, control_frac: float = GOLDSET_CONTROL_FRAC, seed: int = 6002) -> list[dict]:
    """§6.2's stratified sample: proportional across (script, layout), plus a CONTROL arm.

    The control arm is the point of the gate, not a garnish. Everything this tool does rests on
    the claim that `artist_title`/latn and `disc_artist_title` are structurally unambiguous and
    therefore safe to skip. That claim has never been measured. Sampling them alongside the hard
    cases is what turns "assumed safe" into "verified safe" — and if the control arm comes back
    with errors, the workset filter is wrong and no amount of tuning the hard-case prompt fixes it.

    Deterministic: a fixed seed, so re-emitting the gate batch gives the same 200 filenames and
    two prompt revisions are comparable on identical inputs.
    """
    rng = random.Random(seed)
    n_control = min(len(controls), int(round(n * control_frac)))
    n_main = max(0, n - n_control)

    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        strata[(r["script"], r["current"]["layout"])].append(r)

    picked: list[dict] = []
    if strata and n_main:
        keys = sorted(strata)
        # One from every stratum first — a stratum of 14 (`title_only`/ru) must not round to
        # zero, which is exactly what a purely proportional allocation does to it.
        for k in keys:
            if len(picked) < n_main:
                picked.append(rng.choice(strata[k]))
        chosen = {id(r) for r in picked}
        pool = [r for r in records if id(r) not in chosen]
        total = len(pool) or 1
        remaining = n_main - len(picked)
        if remaining > 0:
            quota = {k: max(0, len([r for r in strata[k] if id(r) not in chosen]))
                     for k in keys}
            order = sorted(keys, key=lambda k: -quota[k])
            per = {k: int(remaining * quota[k] / total) for k in keys}
            for k in order:
                bucket = [r for r in strata[k] if id(r) not in chosen]
                rng.shuffle(bucket)
                for r in bucket[:per[k]]:
                    picked.append(r)
                    chosen.add(id(r))
            leftovers = [r for r in pool if id(r) not in chosen]
            rng.shuffle(leftovers)
            picked.extend(leftovers[:max(0, n_main - len(picked))])

    ctl = list(controls)
    rng.shuffle(ctl)
    for r in ctl[:n_control]:
        r = dict(r, control=True)
        picked.append(r)
    rng.shuffle(picked)
    return picked[:n]


# --- the SELF-SCORING swap arm + the decoration report -----------------------------------------
#
# A free labeled evaluation set, measured 2026-08-11 over the 20,090 media_items that carry BOTH
# artist+title from `filename` AND both from an external source. After NFKC + apostrophe +
# punctuation + leading-article normalization:
#
#     agree                        ~73.6%
#     subset/superset on one field ~20.1%   undropped decorations -> the report below
#     EXACT SWAPS                    5.2%   (1,037; reproduced here at 1,034)
#     other                          1.1%   (sampled: also swaps, with trailing decoration)
#
# ~1,034 items are therefore CONFIRMED artist/title reversals with an authoritative correction
# already sitting in the DB. That is exactly the failure mode this tool exists to fix, and it is
# ground truth we did not have to pay for.
#
# THE ARM IS 75% HEBREW, WHICH WAS NOT THE EXPECTATION. Measured here 2026-08-11 over the 1,034
# swaps:
#
#     script   he 775  latn 259
#     layout   artist_title 893 · title_artist 94 · disc_artist_title 23 ·
#              disc_title_artist 19 · title_only 4 · title_artist_from_folder 1
#     777 of the 1,034 are ALSO in this tool's main workset
#
# The prior assumption was that this population is Latin-heavy by construction, on the grounds
# that it only exists where MusicBrainz held the RECORDING (§9.1.2 measured Hebrew recording
# coverage at 16.0%). That is wrong, and migration 006 is why: `wikidata` and `musicbrainz_artist`
# are in EXTERNAL_SOURCES, and both were measured at 82.5% / 62.5% on Hebrew ARTISTS with 100%
# Hebrew-script labels. They supply artist+title for Hebrew items without ever supplying a
# song_mbid — so the swap arm reaches precisely the Hebrew order population that is this tool's
# main target, and reaches it with free labels.
#
# It is still a SEPARATE arm and not a replacement for the hand-reviewed one. It can only contain
# items some external source already answered, so it is blind to exactly the stems where no
# catalogue responded — which is most of the workset, and the reason `llm_parse` exists at all.
# The stratified arm measures that population; the control arm measures whether the workset
# filter is safe at all. All three, always.
#
# Scoring the swap arm is fully automatic: the agent sees only the path, never the external
# answer, and its output is compared field-wise against the known-correct external artist/title.

_PUNCT_RE = re.compile(r"[^\w\s]", re.U)
_LEADING_ARTICLES = ("the", "a", "an")


def cmp_toks(s: str | None) -> frozenset[str]:
    """Comparison-only tokenization: NFKC, apostrophes folded, punctuation dropped, leading
    article removed. Never stored — this decides AGREEMENT, not text."""
    s = unicodedata.normalize("NFKC", s or "")
    for q in ("’", "ʼ", "`", "´"):
        s = s.replace(q, "'")
    t = [x for x in _PUNCT_RE.sub(" ", s).lower().split() if x]
    while t and t[0] in _LEADING_ARTICLES:
        t = t[1:]
    return frozenset(t)


def _filename_vs_external(conn: sqlite3.Connection) -> list[dict]:
    """Items carrying both fields from `filename` AND both from an authoritative source."""
    ph = ",".join("?" * len(EXTERNAL_SOURCES))
    fn = {r["id"]: (r["fa"], r["ft"]) for r in conn.execute("""
        SELECT media_item_id AS id,
               MAX(CASE WHEN field='artist' THEN value END) AS fa,
               MAX(CASE WHEN field='title'  THEN value END) AS ft
        FROM song_metadata WHERE source='filename' AND field IN ('artist','title')
        GROUP BY media_item_id HAVING fa IS NOT NULL AND ft IS NOT NULL""")}
    ex = {r["id"]: (r["ea"], r["et"]) for r in conn.execute(f"""
        SELECT media_item_id AS id,
               MAX(CASE WHEN field='artist' THEN value END) AS ea,
               MAX(CASE WHEN field='title'  THEN value END) AS et
        FROM song_metadata WHERE source IN ({ph}) AND field IN ('artist','title')
        GROUP BY media_item_id HAVING ea IS NOT NULL AND et IS NOT NULL""", EXTERNAL_SOURCES)}
    return [{"id": i, "fa": fn[i][0], "ft": fn[i][1], "ea": ex[i][0], "et": ex[i][1]}
            for i in sorted(set(fn) & set(ex))]


def classify_pair(fa: str, ft: str, ea: str, et: str) -> str:
    """'agree' | 'swap' | 'subset' | 'other'. Pure, so the ground-truth definition is testable."""
    Fa, Ft, Ea, Et = cmp_toks(fa), cmp_toks(ft), cmp_toks(ea), cmp_toks(et)
    if Fa == Ea and Ft == Et:
        return "agree"
    # The swap test is deliberately EXACT on both fields. A one-sided match is not evidence of a
    # reversal — it is evidence of a decoration, which is the other bucket.
    if Fa and Ft and Fa == Et and Ft == Ea:
        return "swap"
    if (Fa <= Ea or Ea <= Fa) and (Ft <= Et or Et <= Ft):
        return "subset"
    return "other"


def swap_groundtruth(conn: sqlite3.Connection) -> tuple[list[dict], dict[str, int]]:
    """The confirmed-reversal population, with the external answer attached as the LABEL."""
    tally: dict[str, int] = defaultdict(int)
    out = []
    for p in _filename_vs_external(conn):
        kind = classify_pair(p["fa"], p["ft"], p["ea"], p["et"])
        tally[kind] += 1
        if kind == "swap":
            out.append(p)
    return out, dict(tally)


def swap_records(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Turn labeled items into batch records. The record an agent SEES is identical in shape to
    any other — paths, siblings, the regex guess — and the label is carried OUT OF BAND in
    `_label`, stripped by `_public` before the batch file is written. The agent must never see
    the answer it is being scored against."""
    loc = defaultdict(list)
    for r in conn.execute("""
        SELECT fl.remote_path, fl.member_path, fl.local_path, fl.id AS lid,
               mif.media_item_id AS item_id, lp.artist, lp.title, lp.layout, lp.language,
               lp.disc_series, lp.disc_id, lp.disc_track
        FROM file_locations fl
        JOIN media_item_files mif ON mif.content_hash = fl.content_hash
        LEFT JOIN location_parses lp ON lp.location_id = fl.id
        WHERE fl.content_hash IS NOT NULL"""):
        loc[r["item_id"]].append(r)
    siblings = _sibling_index(conn)
    out = []
    seen: set[str] = set()
    for p in items:
        rows = loc.get(p["id"]) or []
        if not rows:
            continue
        r0 = rows[0]
        stem = stem_of(loc_path(r0))
        if not stem or stem in seen:
            continue
        seen.add(stem)
        paths, dirs = [], set()
        for r in rows:
            pa = loc_path(r)
            if pa and pa not in paths and len(paths) < MAX_PATHS:
                paths.append(pa)
            dirs.add(dirname_of(pa))
        sibs: list[str] = []
        for d in sorted(dirs):
            for b in siblings.get(d, []):
                if stem_of(b) != stem and b not in sibs and len(sibs) < MAX_SIBLINGS:
                    sibs.append(b)
        out.append({
            "stem": stem, "paths": paths, "siblings": sibs,
            "current": {"artist": r0["artist"], "title": r0["title"],
                        "layout": r0["layout"], "language": r0["language"],
                        "disc_series": r0["disc_series"], "disc_id": r0["disc_id"],
                        "disc_track": r0["disc_track"]},
            "script": r0["language"] if r0["language"] not in (None, "unknown")
                      else script_of(stem),
            "locations": [r["lid"] for r in rows],
            "arm": "swap",
            "_label": {"media_item_id": p["id"], "artist": p["ea"], "title": p["et"],
                       "filename_artist": p["fa"], "filename_title": p["ft"]},
        })
    return out


def decoration_report(conn: sqlite3.Connection, *, top: int = 200) -> tuple[str, dict[str, int]]:
    """Rank the tokens the FILENAME side carries that the external side does not.

    Read-only, and deliberately NOT a parser change: it surfaces candidates so §6.1's
    STRONG/WEAK decoration vocabularies (karaokemp/stage1.py) can be extended in a separate,
    argued commit. That separation matters — §6.1 records at length why a word that is also a
    real name ('live', 'clean', 'cc', 'christmas') may only be stripped inside brackets. This
    report proposes; it never strips.

    Tokens already covered by the §6.1 vocabulary are marked KNOWN so the eye goes straight to
    the new ones. Sampled finds this was written for: the Hebrew genre tag `ישראלי מזרחי`, the
    Hebrew suffix `שרים`, and site watermarks like `[<site>.com]` and `+ Lyrics`.
    Personal names WILL appear in this list (a dropped `feat.` co-artist looks identical to a
    decoration at token level) — that is why a human reads it.
    """
    # Whole WORDS lifted out of stage1's regexes, not a substring test: `\ba\b` and `\bvideo\b`
    # both contain "a", and marking the English article KNOWN would hide a real candidate behind
    # a marker that means nothing.
    known: set[str] = set()
    try:
        from karaokemp import stage1
        for pat, _ in stage1.STRONG_DECORATIONS + stage1.WEAK_DECORATIONS:
            cleaned = re.sub(r"\\b|\\s\+|\?|\\", " ", pat)
            known.update(w for w in re.findall(r"\w+", cleaned.lower()) if w)
    except Exception:
        pass

    counts: dict[str, int] = defaultdict(int)
    examples: dict[str, list[str]] = defaultdict(list)
    tally: dict[str, int] = defaultdict(int)
    contributing = 0
    for p in _filename_vs_external(conn):
        kind = classify_pair(p["fa"], p["ft"], p["ea"], p["et"])
        tally[kind] += 1
        if kind in ("agree", "swap"):
            continue
        F = cmp_toks(p["fa"]) | cmp_toks(p["ft"])
        E = cmp_toks(p["ea"]) | cmp_toks(p["et"])
        extra = F - E
        # Require real overlap: without it we are comparing two different songs and every token
        # looks like a decoration.
        if not extra or not (E & F):
            continue
        contributing += 1
        for t in extra:
            counts[t] += 1
            if len(examples[t]) < 3:
                examples[t].append(f"{p['fa']} | {p['ft']}   ->   {p['ea']} | {p['et']}")

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    lines = [
        "§6.1 decoration candidates — tokens present on the FILENAME side and absent from the",
        "external (MusicBrainz/Wikidata) side, over items where both sources name the same song.",
        "",
        f"generated_at  {db.utcnow()}",
        f"population    {sum(tally.values())} items with both filename and external artist+title",
        "  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())),
        f"contributing  {contributing} items with leftover tokens",
        "",
        "READ THIS AS A PROPOSAL, NOT A PATCH. Nothing here is stripped by any parser. A token",
        "that is also a real name may only ever be stripped inside brackets (§6.1 / stage1.py",
        "WEAK_DECORATIONS). Dropped `feat.` co-artists appear here and are NOT decorations.",
        "KNOWN = already covered by stage1's STRONG/WEAK vocabulary.",
        "",
        f"{'count':>7}  {'':5}  token",
    ]
    for tok, c in ranked:
        mark = "KNOWN" if tok in known else ""
        lines.append(f"{c:7}  {mark:5}  {tok}")
        for ex in examples[tok][:1]:
            lines.append(f"{'':16}e.g. {ex}")
    return "\n".join(lines) + "\n", dict(tally)


# --- batch emission --------------------------------------------------------------------------

def run_dir(run: str) -> Path:
    return config.ARTIFACTS_DIR / "llm-parse" / run


# Each arm gets its OWN default run directory. They used to share one date-derived name and
# the same `batch-NNN.json` filenames, so running two arms on the same day silently
# overwrote the first — batch, labels AND manifest, with no error and no trace it had
# happened. That is measurement data destroyed in a way nothing would have caught later; the
# collision guard in `emit_batches` is the belt to this suspenders.
RUN_SUFFIX = {"workset": "", "goldset": "-goldset", "goldset_swaps": "-swaps",
              "sweep": "-sweep"}


def resolve_run(explicit: str | None, kind: str) -> str:
    """An explicit --run always wins; otherwise the run name carries the arm."""
    if explicit:
        return explicit
    return "run-" + db.utcnow()[:10] + RUN_SUFFIX.get(kind, "")


def _public(rec: dict) -> dict:
    """What actually goes in a batch file. `locations` is kept — it is the audit trail from an
    agent answer back to the rows it came from.

    Every key starting with `_` is stripped here, and `_label` is the reason the rule exists: the
    swap arm's known-correct answer must NEVER reach the file an agent is handed, or the arm
    measures nothing.
    """
    out = {"stem": rec["stem"], "paths": rec["paths"], "current": rec["current"],
           "siblings": rec["siblings"], "script": rec["script"],
           "locations": rec["locations"]}
    if rec.get("control"):
        out["control"] = True
    if rec.get("arm"):
        out["arm"] = rec["arm"]
    return out


def emit_batches(records: list[dict], run: str, *, size: int = BATCH_SIZE,
                 dry_run: bool = False, kind: str = "workset",
                 force: bool = False) -> tuple[list[Path], Path | None]:
    dest = run_dir(run)
    # Never write into a run that already holds a manifest. Batch files are named
    # positionally (batch-001.json), so a second emission into the same directory
    # overwrites the first — including labels.json, which is an ANSWER KEY: losing it
    # silently invalidates the arm it belongs to. Refuse, and say which arm is already
    # there, rather than clobbering someone's measurement.
    prior = dest / "manifest.json"
    if prior.exists() and not (dry_run or force):
        try:
            was = json.loads(prior.read_text(encoding="utf-8")).get("kind", "?")
        except (OSError, ValueError):
            was = "?"
        raise SystemExit(
            f"refusing to write into {dest}: it already holds a '{was}' run "
            f"({prior.name} exists).\n"
            f"  batch files are named positionally, so this would overwrite that run's "
            f"batches"
            + (" AND its labels.json answer key" if (dest / 'labels.json').exists() else "")
            + ".\n"
            f"  use --run <name> to emit into a fresh directory, or --force to overwrite "
            f"deliberately.")
    batches: list[Path] = []
    chunks = [records[i:i + size] for i in range(0, len(records), size)] or []
    manifest = {
        "run": run, "kind": kind, "prompt_version": PROMPT_VERSION,
        "created_at": db.utcnow(), "host": db.host(),
        "stems": len(records), "batches": len(chunks), "batch_size": size,
        "files": [f"batch-{i:03d}.json" for i in range(1, len(chunks) + 1)],
    }
    if dry_run:
        return [dest / f for f in manifest["files"]], None
    dest.mkdir(parents=True, exist_ok=True)
    for i, chunk in enumerate(chunks, 1):
        p = dest / f"batch-{i:03d}.json"
        # Written whole, then named — a Ctrl-C never leaves a half-written batch that `ingest`
        # would later validate against.
        tmp = p.with_suffix(".json.part")
        # `kind` is carried on the BATCH as well as the manifest. `ingest` is handed a result
        # file and resolves the batch beside it; a manifest three directories away is not
        # something it, or anyone reading a single batch later, can rely on seeing. This is what
        # tells a sweep answer from a goldset answer after the fact.
        tmp.write_text(json.dumps(
            {"batch_id": f"{run}/batch-{i:03d}", "kind": kind,
             "prompt_version": PROMPT_VERSION,
             "items": [_public(r) for r in chunk]},
            ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        batches.append(p)
    labels = {r["stem"]: r["_label"] for r in records if r.get("_label")}
    if labels:
        # The swap arm's answer key, kept BESIDE the batch and never inside it. `ingest` scores
        # against this; the agent never sees it.
        manifest["labels"] = "labels.json"
        (dest / "labels.json").write_text(
            json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
    mpath = dest / "manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return batches, mpath


# --- the prompt ------------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are segmenting karaoke media filenames for a library index. You are given file PATHS and \
you return, for each one, which part is the ARTIST and which part is the TITLE.

RULES — all of them are hard:

1. SEGMENT THE TEXT THAT IS PRESENT IN THE PATH. You may correct spelling, strip decorations \
("karaoke", "instrumental", "playback", "HD", "with lyrics", "no lead vocal", "פלייבק", \
"קריוקי", track numbers, 11-character video ids), and you may resolve the artist/title \
ORDER using world knowledge about who performs what.

2. NEVER INVENT an artist or title that is not derivable from the path. If the path does not \
tell you the artist, return null for artist. Return null rather than guess. A confident wrong \
answer is far more expensive here than a null.

3. HEBREW STAYS IN HEBREW SCRIPT. Never transliterate — not the artist, not the title, not \
partially. "שרית חדד" must come back as "שרית חדד", never as "Sarit Hadad". This is a hard \
project rule: end users search in Hebrew, so a correct-but-Latin answer is unusable. The same \
applies to Russian (Cyrillic) and any other non-Latin script in the path.

4. PARENT FOLDER NAMES ARE A HINT, NEVER AUTHORITATIVE. A folder may name the artist, or a disc, \
or a compilation, or an event, or nothing at all. Use it to resolve the order or to supply a \
missing artist ONLY when it clearly names a performer; say so in `notes` when you do. If the \
folder names a compilation or a genre rather than a performer, do not use it as the artist.

5. FIX KEYBOARD-LAYOUT MOJIBAKE when — and only when — you are confident. Hebrew typed with an \
English keyboard layout produces strings like `ערב אוc vg,ev.odp`, where a run of Latin \
characters maps back to Hebrew letters through the standard Hebrew keyboard mapping. Decode it, \
and record what you did in `notes`. If you are not confident, leave the text as it is and lower \
the confidence.

6. CONFIDENCE is a number from 0.0 to 1.0 reflecting how sure you are about BOTH the \
segmentation and the order. Use the whole range honestly. A title-only path where you supplied \
no artist should still carry a high confidence if the TITLE is clear.

7. `notes` is a short free-text string for anything non-obvious: mojibake decoded, folder used \
as the artist, order flipped against the punctuation, decoration stripped, a name you corrected. \
Empty string when there is nothing to say.

8. `layout` is what the path ACTUALLY turned out to be, from this vocabulary and no other: \
__LAYOUT_VOCAB__. Use `title_artist_from_folder` when the artist came from a parent folder \
rather than the filename. Use `opaque` when the path carries no recoverable name at all — and \
then return null for both artist and title.

OUTPUT: strict JSON and nothing else. No prose before or after, no markdown fence.

{"results": [{"stem": "...", "artist": "..." or null, "title": "..." or null, \
"confidence": 0.0, "layout": "...", "notes": "..."}]}

Return EXACTLY ONE result object per input item, keyed by the item's `stem` verbatim. Do not \
add stems, do not drop stems, do not alter a stem's text.\
"""

# Rule 8's vocabulary is SUBSTITUTED IN from `MODEL_LAYOUTS` rather than written out in the
# prose above. `.replace` and not `.format`/f-string: the prompt contains a literal JSON example
# full of braces, and every one of them would have to be doubled to survive formatting.
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("__LAYOUT_VOCAB__", ", ".join(sorted(MODEL_LAYOUTS)))
assert "__LAYOUT_VOCAB__" not in SYSTEM_PROMPT


def user_prompt(batch: dict) -> str:
    items = batch.get("items", [])
    return (
        f"Batch {batch.get('batch_id', '?')} — {len(items)} items.\n\n"
        "For each item you get: `stem` (the join key — echo it back verbatim), `paths` (the full "
        "path of every copy of this file; parent folders often carry the artist), `current` "
        "(what a regex parser guessed — it is a hint you may overrule, and its `layout` says "
        "which pattern fired), and `siblings` (other filenames in the same directory, useful "
        "for recognising a numbered disc).\n\n"
        + json.dumps({"items": items}, ensure_ascii=False, indent=1)
        + "\n\nReturn the strict JSON object described in the system prompt, and nothing else."
    )


# --- strict validation -----------------------------------------------------------------------

def source_texts(item: dict, field: str, layout: str | None) -> list[str]:
    """The path text a given field could actually have been DERIVED from.

    This is what scopes the transliteration guard, and the scoping is the whole fix. A field is
    a transliteration only of the text it was reading; text elsewhere in the path is not its
    source and must not decide its verdict.

      * THE FILENAME is a source for both fields, always. It is what `stem_of` keys on and what
        rules 1-3 are written about.
      * PARENT FOLDERS are a source for the ARTIST, and only when the answer's own `layout` says
        so (`*_from_folder`). Rule 4 lets a folder supply a missing artist and nothing else, and
        every `*_from_folder` layout this pipeline emits is about the artist — no folder in this
        library supplies a TITLE. Reading the folder in unconditionally would put the guard
        straight back where it started: a Hebrew folder would again veto a Latin filename.

    TAKING THE LAYOUT AT ITS WORD LEAVES A RESIDUAL GAP, and it is an honest one rather than an
    oversight. An answer that claims `artist_title` while quietly transliterating a Hebrew
    FOLDER is scoped to the Latin filename, finds no Hebrew source, and passes. That gap cannot
    be closed from the path alone, because the thing it would have to be told apart from is the
    case this fix exists to permit: `Olivia Newton-John` from `olivia n john-banks of the ohio`
    under a Hebrew folder, and `Sarit Hadad` from `15 shir` under a Hebrew folder, are the SAME
    SHAPE — Latin text absent from a Latin filename, Hebrew somewhere in the path. Only the
    model knows which segment it read, and `layout` is where it says so.

    So the trade is deliberate: v1 rejected both (and cost 711 stems the right to be corrected),
    v2 accepts both and catches every answer that DECLARES a folder source. Rule 3 still asks
    for the right behaviour; this enforces the half that is decidable. If the gap ever shows up
    in practice, the evidence to look for is a `layout` of `artist_title` whose artist shares no
    token with the filename — measure it before tightening, because that shape is also what a
    legitimate correction looks like.
    """
    paths = item.get("paths") or []
    out = [stem_of(p) for p in paths]
    if field == "artist" and isinstance(layout, str) and layout.endswith("_from_folder"):
        for p in paths:
            out.extend(seg for seg in p.replace("\\", "/").split("/")[:-1] if seg)
    return [s for s in out if s]


def _looks_transliterated(value: str, sources: list[str]) -> bool:
    """A Latin-only answer is a transliteration only when its SOURCE TEXT is Hebrew — and then
    only when the Latin text was not literally there to begin with.

    TWO exemptions, and both are load-bearing:

      * LATIN SOURCE ⇒ NEVER A TRANSLITERATION. Measured 2026-08-11 on the emitted sweep, 711 of
        8,397 stems (8.5%) are a Latin-only filename under a Hebrew-named folder. The v1 guard
        fired whenever ANY path held Hebrew, so on all 711 the model could do nothing but copy
        the filename through verbatim — while rule 1 explicitly permits correcting spelling.
        Wave one hit it twice in one batch (a corrected performer spelling, and `ft.` ->
        `feat.`) and the agent reverted to the path's spelling to get past the validator. There
        is no Hebrew for a Latin answer to a Latin filename to be a transliteration OF.
      * HEBREW SOURCE, TEXT PRESENT VERBATIM. Hebrew folders holding English-language songs are
        common and legal (`בקשות/Before He Cheats ....mp4`), and a mixed-script filename must
        still be able to answer with the Latin half it contains.

    What survives untouched is the case the rule exists for: `שרית חדד` -> `Sarit Hadad`, whose
    source text is Hebrew and whose Latin tokens appear nowhere in it.
    """
    if has_hebrew(value) or not has_latin(value):
        return False
    if not any(has_hebrew(s) for s in sources):
        return False
    hay = " ".join(sources).lower()
    return not all(tok in hay for tok in re.findall(r"[A-Za-z]+", value.lower()))


def validate_results(batch: dict, payload: object) -> tuple[list[dict], list[str], list[str]]:
    """Strict validation. Returns (good_rows, bad_stems, errors).

    NOTHING partial is ever accepted. A row that fails any check has its stem returned in
    `bad_stems` so `ingest` can REQUEUE it — a garbled answer is a reason to ask again, never a
    reason to silently drop a filename from the library's identity pass.
    """
    errors: list[str] = []
    items = {it["stem"]: it for it in batch.get("items", [])}
    good: list[dict] = []
    seen: set[str] = set()

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return [], sorted(items), ["payload is not {'results': [...]}"]

    for i, res in enumerate(payload["results"]):
        if not isinstance(res, dict):
            errors.append(f"result[{i}]: not an object")
            continue
        stem = res.get("stem")
        if not isinstance(stem, str) or stem not in items:
            errors.append(f"result[{i}]: unknown stem {stem!r} — not in this batch")
            continue
        if stem in seen:
            errors.append(f"{stem!r}: duplicate result")
            seen.add(stem)
            continue
        seen.add(stem)
        item = items[stem]
        bad = None

        conf = res.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            bad = f"confidence {conf!r} is not a number"
        elif not (0.0 <= float(conf) <= 1.0):
            bad = f"confidence {conf!r} out of range"

        layout = res.get("layout")
        if bad is None and (not isinstance(layout, str) or layout not in VALID_LAYOUTS):
            bad = f"layout {layout!r} not in the allowed vocabulary"

        fields = {}
        for f in ("artist", "title"):
            v = res.get(f)
            if v is None:
                fields[f] = None
                continue
            if not isinstance(v, str):
                bad = bad or f"{f} {v!r} is not a string or null"
                continue
            v = _WS_RE.sub(" ", unicodedata.normalize("NFC", v)).strip()
            fields[f] = v or None

        notes = res.get("notes")
        if bad is None and notes is not None and not isinstance(notes, str):
            bad = f"notes {notes!r} is not a string or null"

        # The no-transliteration rule, enforced rather than merely requested — PER FIELD, against
        # the text that field was derived from.
        #
        # There is no outer "does this item look Hebrew at all?" trigger any more, and dropping
        # it is the fix rather than a shortcut. v1 gated on `script == 'he' or any Hebrew
        # anywhere in the path`, which is exactly the over-firing this now scopes away; and the
        # per-field check is already inert for a Latin item, since a Latin source can never make
        # `_looks_transliterated` return True. Running it unconditionally is both cheaper to
        # reason about and strictly more correct: a Hebrew filename filed under a Latin folder
        # is now caught too, which the `script`/`any` trigger only reached by luck.
        if bad is None:
            for f, v in fields.items():
                if v and _looks_transliterated(v, source_texts(item, f, layout)):
                    bad = f"{f} {v!r} looks transliterated — Hebrew must stay in Hebrew script"
                    break

        if bad is None and fields.get("artist") is None and fields.get("title") is None \
                and layout != "opaque":
            bad = "both artist and title are null but layout is not 'opaque'"

        if bad:
            errors.append(f"{stem!r}: {bad}")
            continue

        good.append({
            "stem": stem,
            "artist": fields.get("artist"),
            "title": fields.get("title"),
            "confidence": float(conf),
            "layout": layout,
            "notes": (notes or None),
            "script": item.get("script"),
            "batch_id": batch.get("batch_id"),
        })

    missing = sorted(set(items) - seen)
    for stem in missing:
        errors.append(f"{stem!r}: no result returned")
    bad_stems = sorted((set(items) - {g["stem"] for g in good}))
    return good, bad_stems, errors


# --- ingest ----------------------------------------------------------------------------------

def upsert_parses(conn: sqlite3.Connection, rows: list[dict], *, model: str,
                  prompt_version: str = PROMPT_VERSION) -> int:
    ts = db.utcnow()
    n = 0
    for r in rows:
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, notes, script, "
            "model, prompt_version, batch_id, parsed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(stem) DO UPDATE SET artist=excluded.artist, title=excluded.title, "
            "confidence=excluded.confidence, layout=excluded.layout, notes=excluded.notes, "
            "script=excluded.script, model=excluded.model, "
            "prompt_version=excluded.prompt_version, batch_id=excluded.batch_id, "
            "parsed_at=excluded.parsed_at",
            (r["stem"], r["artist"], r["title"], r["confidence"], r["layout"], r["notes"],
             r["script"], model, prompt_version, r["batch_id"], ts),
        )
        n += 1
    return n


def score_against_labels(rows: list[dict], labels: dict) -> dict:
    """Automatic scoring for the swap arm — the whole reason it is worth having.

    The agent saw only the path. Its answer is compared field-wise against the external source's
    known-correct one, using the same order-insensitive tokenization the ground truth was mined
    with, so the score measures SEGMENTATION and ORDER, not spelling or decoration.

    `still_swapped` is the number that matters: it counts answers that reproduced the parser's
    original reversal. If that is high, the prompt is not fixing the failure mode this tool
    exists for, and no amount of tuning the confidence floor helps.
    """
    out = {"scored": 0, "both_correct": 0, "artist_only": 0, "title_only": 0,
           "still_swapped": 0, "wrong": 0}
    for r in rows:
        lab = labels.get(r["stem"])
        if not lab:
            continue
        out["scored"] += 1
        a_ok = cmp_toks(r["artist"]) == cmp_toks(lab["artist"])
        t_ok = cmp_toks(r["title"]) == cmp_toks(lab["title"])
        if a_ok and t_ok:
            out["both_correct"] += 1
        elif cmp_toks(r["artist"]) == cmp_toks(lab["title"]) \
                and cmp_toks(r["title"]) == cmp_toks(lab["artist"]):
            out["still_swapped"] += 1
        elif a_ok:
            out["artist_only"] += 1
        elif t_ok:
            out["title_only"] += 1
        else:
            out["wrong"] += 1
    return out


def write_requeue(batch: dict, bad_stems: list[str], dest: Path, *,
                  dry_run: bool = False) -> Path | None:
    """A requeue file is a BATCH file — same shape, so it can be handed straight back to
    `prompt` and re-run with no special casing."""
    if not bad_stems:
        return None
    keep = [it for it in batch.get("items", []) if it["stem"] in set(bad_stems)]
    name = Path(batch.get("batch_id", "batch-000")).name.replace("batch-", "requeue-")
    out = dest / f"{name}.json"
    if dry_run:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.part")
    tmp.write_text(json.dumps(
        {"batch_id": f"{batch.get('batch_id')}/requeue", "prompt_version": PROMPT_VERSION,
         "items": keep}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return out


# --- promote ---------------------------------------------------------------------------------

def stem_to_items(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """stem -> every media_item that any location with that stem resolves to.

    location -> content_hash -> media_item_files -> media_item. That indirection is the point:
    Stage 0 collapsed duplicate copies to one surviving location per blob, so a stem answered
    once reaches every item that shares the content.
    """
    out: dict[str, set[int]] = defaultdict(set)
    for r in conn.execute("""
        SELECT fl.remote_path, fl.member_path, fl.local_path, mif.media_item_id
        FROM file_locations fl
        JOIN media_item_files mif ON mif.content_hash = fl.content_hash
        JOIN media_items mi ON mi.id = mif.media_item_id AND mi.status = 'active'
        WHERE fl.content_hash IS NOT NULL
    """):
        s = stem_of(loc_path(r))
        if s:
            out[s].add(r["media_item_id"])
    return {k: sorted(v) for k, v in out.items()}


def promote(conn: sqlite3.Connection, *, floor: float | None = None, limit: int | None = None,
            prompt_version: str | None = None,
            dry_run: bool = False) -> dict:
    """Write `song_metadata` rows with source='llm_parse'.

    Idempotent by CONSTRUCTION, not by ON CONFLICT: existing llm_parse rows are read first and a
    row is written only when the value or confidence actually differs. `DO UPDATE` would report
    a change on every rerun, and §13's "run twice => zero changes second time" has to be
    observably true, not merely harmless.

    ONE WINNER PER ITEM. Several stems routinely resolve to the same media_item — duplicate
    copies of one song whose filenames differ slightly (`... pitbull feat. ke$ha ...` beside
    `... pitbull ft. ke$ha ...`). Both are parsed, both target the same (item, field), and the
    old single-pass write let the LAST stem in sort order silently overwrite the first. That was
    deterministic but arbitrary, and it broke §13 outright: `existing` is read once per run, so
    the loser re-wrote its answer on every rerun forever (415 rows/run, 242 contested items, 181
    with genuinely different answers).

    So candidates are grouped by item first and resolved by HIGHEST CONFIDENCE, lowest stem
    breaking a tie (`parses` is ordered by stem and the incumbent is kept, so the tie-break is
    stable without a second sort). Confidence is a weak signal — the model's self-report, not a
    measurement — but it beats alphabetical order, and it fixed 31 items that were resolving to
    the lower-confidence answer. Contested items are reported in `contested` so a better rule
    (or a human) has something to work from.

    `prompt_version=None` — ALL versions — is the default, and the fix-forward decision is what
    makes it the only safe one. This used to default to PROMPT_VERSION, which was harmless while
    exactly one version existed and became a silent data loss the moment one did not: the v2
    bump would have made this select 0 of the 1,594 v1 rows already in the table and report a
    clean, cheerful no-op. The corpus is deliberately mixed (see PROMPT_VERSION), so promoting
    it means promoting all of it. Pass a version to promote one prompt's answers in isolation,
    which is a measurement, not the normal path.
    """
    floor = config.LLM_PARSE_FLOOR if floor is None else floor
    sql = "SELECT stem, artist, title, confidence, script FROM llm_parses"
    params: tuple = ()
    if prompt_version is not None:
        sql += " WHERE prompt_version = ?"
        params = (prompt_version,)
    parses = conn.execute(sql + " ORDER BY stem", params).fetchall()

    existing: dict[tuple[int, str], tuple] = {}
    for r in conn.execute(
            "SELECT media_item_id, field, value, confidence FROM song_metadata WHERE source = ?",
            (SOURCE,)):
        existing[(r["media_item_id"], r["field"])] = (r["value"], r["confidence"])

    smap = stem_to_items(conn)
    ts = db.utcnow()
    counters = {"parses_seen": len(parses), "skipped_low_confidence": 0,
                "skipped_no_item": 0, "skipped_empty": 0,
                "items_promoted": 0, "rows_written": 0, "contested_items": 0,
                "promoted_by_script": defaultdict(int)}

    # Pass 1 — resolve. One winning parse per media_item; nothing is written yet.
    winner: dict[int, tuple[str, list[tuple[str, str]], float, str]] = {}
    contested: dict[int, list[str]] = defaultdict(list)
    n = 0
    for p in parses:
        if p["confidence"] is None or p["confidence"] < floor:
            counters["skipped_low_confidence"] += 1
            continue
        vals = [(f, (p[f] or "").strip()) for f in ("artist", "title")]
        vals = [(f, v) for f, v in vals if v]        # never write NULL/empty (§3.6)
        if not vals:
            counters["skipped_empty"] += 1
            continue
        items = smap.get(p["stem"])
        if not items:
            counters["skipped_no_item"] += 1
            continue
        cand = (p["stem"], vals, p["confidence"], p["script"] or "unknown")
        for item_id in items:
            cur = winner.get(item_id)
            if cur is None:
                winner[item_id] = cand
                continue
            contested[item_id].append(p["stem"])
            if cand[2] > cur[2]:                     # strictly greater: incumbent holds a tie
                winner[item_id] = cand
        n += 1
        if limit and n >= limit:
            break

    # Pass 2 — write. Sorted so a Ctrl-C mid-run leaves a prefix, not a scatter.
    for item_id in sorted(winner):
        stem, vals, conf, script = winner[item_id]
        for field, value in vals:
            if existing.get((item_id, field)) == (value, conf):
                continue
            if not dry_run:
                conn.execute(
                    "INSERT INTO song_metadata (media_item_id, field, value, source, "
                    "confidence, updated_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(media_item_id, field, source) DO UPDATE SET "
                    "value=excluded.value, confidence=excluded.confidence, "
                    "updated_at=excluded.updated_at",
                    (item_id, field, value, SOURCE, conf, ts))
            counters["rows_written"] += 1
        counters["promoted_by_script"][script] += 1
        if not dry_run:
            conn.commit()      # per-item: Ctrl-C leaves a consistent partial run (§13)

    counters["items_promoted"] = len(winner)
    counters["contested_items"] = len(contested)
    counters["promoted_by_script"] = dict(counters["promoted_by_script"])
    counters["contested"] = {i: sorted([winner[i][0]] + s) for i, s in contested.items()}
    return counters


# --- CLI -------------------------------------------------------------------------------------

def _load_json(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    # Agents wrap JSON in a markdown fence more often than not; tolerating that at the OUTER
    # layer is not laxity — every field inside is still validated strictly.
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return json.loads(t)


def cmd_decorations(conn, args) -> int:
    text, tally = decoration_report(conn)
    dest = run_dir(args.run) / "decorations.txt"
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    if args.dry_run:
        print(f"--- DRY RUN — nothing written ---\n  would write {dest}")
        print("\n".join(text.splitlines()[:30]))
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"  wrote {dest}")
    return 0


def cmd_extract(args) -> int:
    conn = db.connect()
    if args.decorations:
        try:
            return cmd_decorations(conn, args)
        finally:
            conn.close()

    if args.goldset_swaps is not None:
        n = args.goldset_swaps or GOLDSET_SIZE
        labeled, tally = swap_groundtruth(conn)
        records = swap_records(conn, labeled)
        print(f"filename vs external ({len(EXTERNAL_SOURCES)} sources): "
              + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
        by_script = defaultdict(int)
        for r in records:
            by_script[r["script"]] += 1
        overlap = sum(1 for r in records
                      if in_workset(r["current"]["layout"], r["current"]["language"]))
        print(f"confirmed swaps: {len(labeled)} items -> {len(records)} distinct stems  "
              f"script={dict(sorted(by_script.items()))}  {overlap} also in the main workset")
        print("  blind to stems NO catalogue answered — run alongside --goldset, not instead")
        rng = random.Random(6002)
        rng.shuffle(records)
        records = records[:n]
        run = resolve_run(args.run, "goldset_swaps")
        batches, manifest = emit_batches(records, run, size=max(n, 1),
                                         dry_run=args.dry_run, kind="goldset_swaps",
                                         force=args.force)
        print(f"\n--- {'DRY RUN — nothing written' if args.dry_run else 'RESULTS'} ---")
        print(f"  {len(records)} self-scoring stems, {len(batches)} batch file(s) in "
              f"{run_dir(run)}")
        print("  the answer key is labels.json, written BESIDE the batch and never inside it")
        conn.close()
        return 0

    sweep = bool(getattr(args, "sweep", False))
    if sweep and args.goldset is not None:
        print("--sweep and --goldset are different populations; pick one", file=sys.stderr)
        conn.close()
        return 2

    reparse = bool(getattr(args, "reparse", False))
    stats: dict = {}
    records, n_cached = build_workset(conn, sweep=sweep, prompt_version=PROMPT_VERSION,
                                      reparse=reparse, stats=stats)
    kind = "sweep" if sweep else "workset"
    if args.goldset is not None:
        # Built lazily: the control arm is a second full scan and only the goldset uses it.
        controls, _ = build_workset(conn, control=True, include_cached=True,
                                    prompt_version=PROMPT_VERSION)
        n = args.goldset or GOLDSET_SIZE
        records = stratify(records, controls, n, control_frac=args.control_frac)
        kind = "goldset"
        size = max(n, 1)
    else:
        size = args.batch_size
        if args.limit:
            records = records[:args.limit]

    print(f"{kind}: {len(records)} stems "
          + (f"({n_cached} locations skipped — stem already parsed at prompt_version="
             f"{PROMPT_VERSION}; --reparse is ON, so answers at OTHER versions are being asked "
             f"again)"
             if reparse else
             f"({n_cached} locations skipped — stem already parsed at ANY prompt_version; "
             f"current is {PROMPT_VERSION}, pass --reparse to ask those again)"))
    if sweep:
        # Say plainly that the layout filter is OFF, and account for every stem that did not
        # make it: the §6.2 control arm's whole lesson is that a silent exclusion is how this
        # tool got it wrong the first time.
        print(f"    LAYOUT FILTER OFF — only {sorted(SWEEP_EXCLUDED_LAYOUTS)} excluded; "
              f"opaque/unparsed are INCLUDED (the parent folder may still name it)")
        print(f"    {stats.get('skipped_already_answered', 0):6} stems skipped — an item both "
              f"carries a song_mbid AND already has artist+title winning above llm_parse")
        print(f"    {stats.get('skipped_no_item', 0):6} stems skipped — no media_item, so "
              f"`promote` could never write anything for them")
    by = defaultdict(int)
    for r in records:
        by[(r["script"], r["current"]["layout"], bool(r.get("control")))] += 1
    for k in sorted(by, key=lambda k: -by[k]):
        print(f"    {k[0]:8} {k[1]:26} {'CONTROL' if k[2] else '':8} {by[k]:5}")

    run = resolve_run(args.run, kind)
    batches, manifest = emit_batches(records, run, size=size, dry_run=args.dry_run,
                                     kind=kind, force=args.force)
    if args.dry_run:
        print(f"\n--- DRY RUN — nothing written ---\n"
              f"  would emit {len(batches)} batch file(s) of <= {size} into {run_dir(run)}")
    else:
        print(f"\n  wrote {len(batches)} batch file(s) + {manifest}")
    if kind == "goldset":
        n_ctl = sum(1 for r in records if r.get("control"))
        print(f"  §6.2 gate: {len(records)} stems, {n_ctl} of them CONTROL "
              f"(excluded unambiguous layouts — verifies the filter is safe to skip)")
    if kind == "sweep":
        print("  resumable: re-run to emit what is left — a stem already in llm_parses at "
              + (f"prompt_version={PROMPT_VERSION} is never emitted twice"
                 if reparse else "ANY prompt_version is never emitted again"))
    conn.close()
    return 0


def cmd_prompt(args) -> int:
    batch = _load_json(Path(args.batch))
    if not isinstance(batch, dict):
        print("batch file is not an object", file=sys.stderr)
        return 2
    # A batch file is stamped with the version that EMITTED it; what an agent actually sees is
    # the prompt below, rendered live. Those parted company at the v2 bump — the sweep's pending
    # batch files were all emitted under v1 and are being answered under v2 — and `ingest`
    # records the LIVE version because that is the prompt that produced the answer. Say so, so
    # nobody reads the batch file's stamp as the provenance of the row it becomes.
    was = batch.get("prompt_version")
    if was and was != PROMPT_VERSION:
        print(f"note: this batch file was emitted at prompt_version={was}; the prompt below is "
              f"{PROMPT_VERSION} and that is what `ingest` will record.", file=sys.stderr)
    print("=== SYSTEM ===")
    print(SYSTEM_PROMPT)
    print("\n=== USER ===")
    print(user_prompt(batch))
    return 0


def _resolve_batch(results_path: Path, payload: object, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    bid = payload.get("batch_id") if isinstance(payload, dict) else None
    if bid:
        cand = config.ARTIFACTS_DIR / "llm-parse" / f"{bid}.json"
        if cand.exists():
            return cand
    guess = results_path.parent / results_path.name.replace("results-", "batch-")
    return guess if guess.exists() else None


def cmd_ingest(args) -> int:
    results_path = Path(args.results)
    payload = _load_json(results_path)
    bpath = _resolve_batch(results_path, payload, args.batch)
    if bpath is None or not bpath.exists():
        print(f"cannot find the batch file for {results_path} — pass --batch", file=sys.stderr)
        return 2
    batch = _load_json(bpath)

    good, bad_stems, errors = validate_results(batch, payload)
    for e in errors[:args.show]:
        print(f"  REJECT {e}")
    if len(errors) > args.show:
        print(f"  ... and {len(errors) - args.show} more")

    rq = write_requeue(batch, bad_stems, bpath.parent, dry_run=args.dry_run)
    print(f"\n--- {'DRY RUN — nothing written' if args.dry_run else 'INGEST'} ---")
    print(f"  batch        {bpath}")
    print(f"  valid        {len(good)}")
    print(f"  requeued     {len(bad_stems)}" + (f"  -> {rq}" if rq else ""))

    # Swap arm: score automatically against the answer key that was never in the batch.
    score = None
    lpath = bpath.parent / "labels.json"
    if lpath.exists():
        labels = _load_json(lpath)
        if isinstance(labels, dict):
            score = score_against_labels(good, labels)
            n = score["scored"] or 1
            print(f"\n  §6.2 SELF-SCORED swap arm ({score['scored']} labeled):")
            for k in ("both_correct", "artist_only", "title_only", "still_swapped", "wrong"):
                print(f"    {k:16} {score[k]:5}  ({100 * score[k] / n:5.1f}%)")

    if args.dry_run:
        return 0
    conn = db.connect()
    try:
        n = upsert_parses(conn, good, model=args.model)
        conn.commit()
    finally:
        conn.close()
    print(f"  llm_parses   {n} upserted (model={args.model}, prompt_version={PROMPT_VERSION})")

    with db.pipeline_run(STAGE, notes=f"ingest {bpath.name}", backup=False) as st:
        st["items_processed"] = n
        st["items_failed"] = len(bad_stems)
        rep = {"rows_ingested": n, "rows_requeued": len(bad_stems),
               "batch": str(bpath), "model": args.model,
               "prompt_version": PROMPT_VERSION}
        if score:
            rep["swap_arm_score"] = score
        st["report"] = rep
    return 0


def cmd_promote(args) -> int:
    conn = db.connect()
    try:
        counters = promote(conn, floor=args.floor, limit=args.limit, dry_run=args.dry_run)
    finally:
        conn.close()
    print(f"--- {'DRY RUN — nothing written' if args.dry_run else 'PROMOTE'} ---")
    for k in ("parses_seen", "items_promoted", "rows_written", "skipped_low_confidence",
              "skipped_empty", "skipped_no_item", "contested_items"):
        print(f"  {k:24} {counters[k]}")
    print(f"  promoted_by_script       {counters['promoted_by_script']}")
    if counters["contested_items"]:
        print(f"  {counters['contested_items']} item(s) claimed by >1 stem, resolved by highest "
              f"confidence (see the run report for the stems)")
    if not args.dry_run:
        with db.pipeline_run(STAGE, notes=f"promote source={SOURCE}", backup=True) as st:
            st["items_processed"] = counters["items_promoted"]
            st["report"] = counters
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="build the workset and emit batch files")
    e.add_argument("--run", default=None,
                   help="run directory under artifacts/llm-parse/ (default: run-<date> for the "
                        "workset, run-<date>-goldset / run-<date>-swaps for the gate arms, so "
                        "two arms on the same day cannot collide)")
    e.add_argument("--force", action="store_true",
                   help="overwrite a run directory that already holds a manifest. Destroys that "
                        "run's batches and any labels.json answer key — say so deliberately.")
    e.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    e.add_argument("--limit", type=int, default=None, help="cap stems considered")
    e.add_argument("--sweep", action="store_true",
                   help="drop the layout filter entirely and send EVERY stem that could still "
                        "benefit: no item that BOTH carries a song_mbid AND is already named "
                        "above llm_parse, and a media_item exists. Excludes non_media only — "
                        "opaque/unparsed ARE included, because the parent folder sometimes "
                        "names them. Measured 8,536 stems / 43 batches; resumable, so run it "
                        "in pieces")
    e.add_argument("--goldset", type=int, nargs="?", const=GOLDSET_SIZE, default=None,
                   metavar="N",
                   help="§6.2 gate: ONE stratified batch of N (default 200) incl. a control arm")
    e.add_argument("--goldset-swaps", type=int, nargs="?", const=GOLDSET_SIZE, default=None,
                   metavar="N",
                   help="§6.2 gate, SELF-SCORING arm: N stems whose artist/title an external "
                        "source already proved reversed. No human labeling needed. Measured 75%% "
                        "Hebrew — but blind to stems no catalogue answered, so run it ALONGSIDE "
                        "--goldset, never instead of it")
    e.add_argument("--reparse", action="store_true",
                   help=f"re-ask stems already answered under an OLDER prompt. Off by default: "
                        f"a PROMPT_VERSION bump is fix-forward here, so 1,594 stems answered at "
                        f"v1 stay skipped rather than being re-parsed at "
                        f"{PROMPT_VERSION}. Turning this on re-emits them — spend it "
                        f"deliberately")
    e.add_argument("--decorations", action="store_true",
                   help="read-only §6.1 report: filename-side tokens the external sources drop")
    e.add_argument("--control-frac", type=float, default=GOLDSET_CONTROL_FRAC,
                   help="share of the goldset drawn from the EXCLUDED unambiguous layouts")
    e.add_argument("--dry-run", action="store_true", help="plan only, write nothing (§13)")
    e.set_defaults(func=cmd_extract)

    p = sub.add_parser("prompt", help="print the exact system+user prompt for a batch file")
    p.add_argument("--batch", required=True)
    p.set_defaults(func=cmd_prompt)

    i = sub.add_parser("ingest", help="validate + load an agent result file")
    i.add_argument("--results", required=True)
    i.add_argument("--batch", default=None, help="the batch it answers (inferred when omitted)")
    i.add_argument("--model", required=True, help="model id that produced these results")
    i.add_argument("--show", type=int, default=25, help="how many rejections to print")
    i.add_argument("--dry-run", action="store_true", help="validate only, write nothing (§13)")
    i.set_defaults(func=cmd_ingest)

    m = sub.add_parser("promote", help="write song_metadata rows with source='llm_parse'")
    m.add_argument("--floor", type=float, default=None,
                   help=f"confidence floor (default config.LLM_PARSE_FLOOR="
                        f"{config.LLM_PARSE_FLOOR})")
    m.add_argument("--limit", type=int, default=None)
    m.add_argument("--dry-run", action="store_true", help="plan only, write nothing (§13)")
    m.set_defaults(func=cmd_promote)

    args = ap.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted — committed work is intact, re-run to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
