"""§6.1/§6.2 LLM filename parsing — the pure decision logic and the write path.

Every rule pinned here is one a later retune would otherwise break silently:

  * `stem_of` is the join key for the WHOLE pass — mp3/cdg pairs and duplicate copies collapse
    onto it, so NFC/case/whitespace/extension handling decides whether one agent answer serves
    every copy or silently doubles the cache;
  * the layout filter is the pass's entire premise: `opaque`/`unparsed`/`non_media` are
    unrecoverable from a filename and must NEVER be emitted, and an unknown future layout
    defaults OUT rather than being fed to a model;
  * the validator is strict and REQUEUES — a garbled or transliterated answer is a reason to ask
    again, never a reason to drop a filename from the library's identity pass;
  * transliteration detection has to survive this library's real shape: Hebrew folders holding
    English-language songs are common and legal, `שרית חדד` -> `Sarit Hadad` is not;
  * `promote` writes nothing NULL/empty and is idempotent by construction — §13's "run twice ⇒
    zero changes second time" has to be observably true, not merely harmless;
  * the swap arm's answer key never reaches the batch file an agent is handed.

Fixtures build a small pipeline DB from schema.sql + migration 007, the same way the stage
tests do.

Run: python3 tests/test_llm_parse.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from karaokemp import config, db  # noqa: E402

import llm_parse as lp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


_seq = [0]


def _fresh_env(tmp: Path):
    config.LIBRARY_ROOT = tmp
    config.ACTIVE_DIR = tmp / "active"
    config.ARCHIVE_DIR = tmp / "archive"
    config.STAGING_DIR = tmp / "staging"
    config.ARTIFACTS_DIR = tmp / "artifacts"
    config.DB_DIR = tmp / "db"
    config.DB_PATH = tmp / "db" / "library.sqlite3"
    config.LOGS_DIR = tmp / "logs"
    config.BACKUP_DIR = tmp / "db" / "backups"
    config.ENUM_DIR = tmp / "enumerations"
    config.ensure_layout()
    conn = db.connect(config.DB_PATH)
    # The real schema (migrations 001-008 are folded into it), so a change to the ladder or a
    # table shape breaks these tests rather than drifting past them.
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _loc(conn, path, *, layout="title_artist", language="he", artist=None, title=None,
         sha=None, item=True):
    """One file_location + its location_parse, optionally wired to a media_item."""
    _seq[0] += 1
    n = _seq[0]
    sha = sha or f"sha{n:04d}"
    conn.execute("INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
                 "VALUES (?,?, 'probed_ok')", (sha, 1000))
    lid = conn.execute(
        "INSERT INTO file_locations (drive_file_id, remote_path, filetype, content_hash, status) "
        "VALUES (?,?,?,?, 'staged')",
        (f"drv{n}", path, path.rsplit(".", 1)[-1] if "." in path else "mp3", sha)).lastrowid
    conn.execute(
        "INSERT INTO location_parses (location_id, artist, title, layout, language, confidence, "
        "payload, parser_version) VALUES (?,?,?,?,?,?,?, 'test')",
        (lid, artist, title, layout, language, 0.5, "{}"))
    item_id = None
    if item:
        item_id = conn.execute(
            "INSERT INTO media_items (format, status) VALUES ('video','active')").lastrowid
        conn.execute("INSERT INTO media_item_files (media_item_id, content_hash, role) "
                     "VALUES (?,?, 'av')", (item_id, sha))
    conn.commit()
    return lid, item_id


# --- stem normalization ----------------------------------------------------------------------

def test_stem_strips_directory_and_extension():
    check(lp.stem_of("a/b/c/15 Song.avi") == "15 song")
    check(lp.stem_of("Song.mp3") == "song")
    check(lp.stem_of("Song") == "song", "no extension is fine")


def test_stem_pairs_mp3_and_cdg_onto_one_key():
    """The reason the cache is keyed on the stem at all: one agent answer serves both halves."""
    check(lp.stem_of("d/SF001-01.mp3") == lp.stem_of("d/SF001-01.cdg"))


def test_stem_is_case_and_whitespace_insensitive():
    check(lp.stem_of("Some  Song   Here.mp3") == "some song here")
    check(lp.stem_of("SOME SONG HERE.MP3") == lp.stem_of("some song here.mp3"))
    check(lp.stem_of("  padded .mp3") == "padded")


def test_stem_nfc_folds_decomposed_accents():
    """The reason `stem_of` normalizes at all: a filename that arrived decomposed (macOS and
    some Drive clients store NFD) and the same name composed must be ONE key, or the cache
    silently doubles and the same stem is sent to an agent twice."""
    composed = unicodedata.normalize("NFC", "Céline Dion - Pour Que Tu M'aimes Encore.mp3")
    decomposed = unicodedata.normalize("NFD", composed)
    check(composed != decomposed, "fixture must actually differ before normalization")
    check(lp.stem_of(composed) == lp.stem_of(decomposed))
    check("é" in lp.stem_of(decomposed), "and the composed form is the one we keep")


def test_stem_leaves_hebrew_niqqud_alone():
    """Hebrew has no precomposed letter+niqqud, so NFC is a no-op there — the point is that it
    stays a NO-OP. NFKD would strip the vowel points and silently merge two distinct spellings;
    §6.1 handles niqqud deliberately elsewhere, never as a side effect of the join key."""
    check(lp.stem_of("שָׁלוֹם.mp3") == "שָׁלוֹם")
    check(lp.stem_of("שלום.mp3") != lp.stem_of("שָׁלוֹם.mp3"))


def test_stem_keeps_dotted_names_intact():
    check(lp.stem_of("01.Madonna - Frozen.mp3") == "01.madonna - frozen")


def test_stem_of_empty_is_empty():
    check(lp.stem_of(None) == "" and lp.stem_of("") == "")


# --- the layout filter -----------------------------------------------------------------------

def test_hard_layouts_are_in_the_workset():
    for layout in ("title_artist", "title_only", "title_artist_from_folder",
                   "disc_title_only", "style_of"):
        check(lp.in_workset(layout, "latn"), f"{layout} must be in the workset")
        check(lp.in_workset(layout, "he"), f"{layout}/he must be in the workset")


def test_opaque_and_unparsed_are_excluded():
    """626 opaque + 1 unparsed carry ZERO information in the filename (`SONG-<uuid>.mp4`).
    Emitting them invites exactly the invention the prompt forbids."""
    for layout in ("opaque", "unparsed", "non_media"):
        check(not lp.in_workset(layout, "latn"), f"{layout} must be excluded")
        check(not lp.in_workset(layout, "he"), f"{layout}/he must be excluded")


def test_artist_title_is_in_only_for_hebrew():
    """34,384 latn rows are structurally unambiguous and must not be spent on. The 1,897 Hebrew
    ones parsed too, but Hebrew carries no order cue."""
    check(lp.in_workset("artist_title", "he"))
    check(not lp.in_workset("artist_title", "latn"))
    check(not lp.in_workset("artist_title", "ru"))
    check(not lp.in_workset("disc_artist_title", "latn"))
    check(not lp.in_workset("disc_artist_title", "he"),
          "disc_artist_title pins the order with its disc prefix regardless of script")


def test_unknown_layout_defaults_out():
    """A layout this tool has never seen must default to OUT, not to being fed to a model."""
    check(not lp.in_workset("some_future_layout", "he"))
    check(not lp.in_workset(None, "he"))


# --- workset construction --------------------------------------------------------------------

def test_workset_dedupes_by_stem_and_keeps_every_full_path():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "קריוקי בעברית 1/58 חלומות/15 ניסים.avi", layout="title_artist_from_folder")
        _loc(conn, "backup/other/15 ניסים.avi", layout="title_artist_from_folder", sha="shaX")
        _loc(conn, "junk/SONG-1234abcd.mp4", layout="opaque", language="latn")
        recs, _ = lp.build_workset(conn)
        check(len(recs) == 1, f"two copies of one stem are one record: {recs}")
        r = recs[0]
        check(r["stem"] == "15 ניסים")
        check(len(r["paths"]) == 2, "the FULL path of every copy is kept")
        check("קריוקי בעברית 1/58 חלומות/15 ניסים.avi" in r["paths"],
              "parent folders carry the artist; they must survive into the batch")
        check(all("SONG-1234abcd" not in p for p in sum([r["paths"]], [])),
              "opaque never enters the workset")
        conn.close()


def test_workset_carries_sibling_context():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "disc/15 ניסים.avi", layout="title_only")
        for i in (14, 16):
            _loc(conn, f"disc/{i} track.avi", layout="artist_title", language="latn")
        recs, _ = lp.build_workset(conn)
        check(len(recs) == 1)
        sibs = recs[0]["siblings"]
        check("14 track.avi" in sibs and "16 track.avi" in sibs,
              f"siblings come from ALL locations, not just workset ones: {sibs}")
        conn.close()


def test_workset_skips_stems_already_parsed_at_this_prompt_version():
    """The resumability mechanism (§13): an interrupted run re-derives the same workset and
    simply emits less of it."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/first.avi", layout="title_only")
        _loc(conn, "d/second.avi", layout="title_only")
        check(len(lp.build_workset(conn)[0]) == 2)
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('first', 'A', 'T', 0.9, 'title_only', 'm', ?)",
            (lp.PROMPT_VERSION,))
        conn.commit()
        recs, n_cached = lp.build_workset(conn)
        check([r["stem"] for r in recs] == ["second"], f"{recs}")
        check(n_cached == 1, n_cached)
        # A different prompt version does NOT make it eligible again. This assertion was
        # REVERSED at the v2 bump, deliberately: a bump used to re-open every answered stem,
        # which at v2 would have meant re-parsing 1,594 stems the owner ruled fix-forward.
        # `--reparse` is now the only way back in — see the next test.
        check(len(lp.build_workset(conn, prompt_version="v-next")[0]) == 1,
              "a prompt bump must not re-open an answered stem")
        conn.close()


def test_control_workset_is_the_excluded_layouts():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/hebrew hard.mp3", layout="title_artist", language="he")
        main, _ = lp.build_workset(conn)
        ctl, _ = lp.build_workset(conn, control=True, include_cached=True)
        check([r["stem"] for r in main] == ["hebrew hard"])
        check([r["stem"] for r in ctl] == ["queen - bohemian"])
        conn.close()


def test_control_arm_is_strictly_the_complement_of_the_workset():
    """`artist_title` spans both arms, split by LANGUAGE. Sampling Hebrew artist_title as a
    control would have the gate assert 'safe to skip' about the exact 1,897 stems this tool
    exists to fix — and put them in both arms of the same batch at once."""
    check(lp.is_control("artist_title", "latn"))
    check(lp.is_control("disc_artist_title", "latn"))
    check(not lp.is_control("artist_title", "he"), "the ambiguous population is NEVER a control")
    check(not lp.is_control("title_artist", "he"))
    check(not lp.is_control("opaque", "latn"), "unrecoverable is not the same as unambiguous")
    for layout, lang in (("artist_title", "he"), ("artist_title", "latn"),
                         ("disc_artist_title", "latn"), ("title_only", "ru"),
                         ("opaque", "latn")):
        check(not (lp.in_workset(layout, lang) and lp.is_control(layout, lang)),
              f"{layout}/{lang} must not be in both arms")


def test_control_workset_excludes_ambiguous_hebrew_artist_title():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/עברית - שיר.mp3", layout="artist_title", language="he")
        main, _ = lp.build_workset(conn)
        ctl, _ = lp.build_workset(conn, control=True, include_cached=True)
        check([r["stem"] for r in main] == ["עברית - שיר"], main)
        check([r["stem"] for r in ctl] == ["queen - bohemian"], ctl)
        conn.close()


# --- the sweep ---------------------------------------------------------------------------------
# The §6.2 control arm measured the layout filter and FALSIFIED it: n=30 stems from the
# "structurally unambiguous, safe to skip" layouts, 4 disagreements, 4/4 the regex being wrong
# (a decoration fused into a title with no separator, a label read as a performer, a misspelled
# artist the parent folder spells correctly, a disc ID parked in the artist field). So the sweep
# inverts the filter: everything is IN except `non_media`, minus what a parse could not change.
# Every rule below is one that decides whether an 8,402-stem, 43-batch pass is the right 8,402.


def _mbid(conn, item_id, value="mbid-1234", source="musicbrainz_text"):
    conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                 "VALUES (?, 'song_mbid', ?, ?)", (item_id, value, source))
    conn.commit()


def _named(conn, item_id, source="musicbrainz_text", artist="Queen", title="Bohemian Rhapsody"):
    """Artist+title from a source that outranks `llm_parse` — what the sweep's second filter
    actually tests. `_mbid` alone no longer excludes anything."""
    for f, v in (("artist", artist), ("title", title)):
        if v is not None:
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                         "VALUES (?,?,?,?)", (item_id, f, v, source))
    conn.commit()


def test_sweep_admits_every_layout_the_workset_excludes():
    """The inversion, stated as a pure test. `opaque`/`unparsed` are the deliberate reversal:
    the filename carries nothing, but the parent folder sometimes names a disc or a performer,
    and the prompt makes null the required answer when it genuinely cannot tell."""
    for layout in ("artist_title", "disc_artist_title", "disc_title_artist",
                   "opaque", "unparsed", "title_artist", "title_only", "style_of",
                   "disc_title_only", "title_artist_from_folder"):
        check(lp.in_sweep(layout), f"{layout} must be in the sweep")
        check(not lp.in_workset(layout, "latn") or lp.in_sweep(layout),
              "the sweep is a SUPERSET of the layout-filtered workset")


def test_sweep_still_excludes_non_media():
    check(not lp.in_sweep("non_media"), "not a song; there is nothing to segment")


def test_sweep_admits_an_unknown_future_layout():
    """The mirror image of `in_workset`, on purpose: that one enumerates what is IN so an unseen
    layout defaults OUT. After the control arm, 'this layout needs no model' is the claim that
    requires evidence — and we have none for a layout we have never seen."""
    check(lp.in_sweep("some_future_layout"))
    check(lp.in_sweep(None))


def test_sweep_sends_opaque_which_the_layout_filtered_workset_drops():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "דיסק 58 חלומות/SONG-1234abcd.mp4", layout="opaque", language="latn")
        check(lp.build_workset(conn)[0] == [], "the layout-filtered workset still drops it")
        recs, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["song-1234abcd"], recs)
        check("דיסק 58 חלומות/SONG-1234abcd.mp4" in recs[0]["paths"],
              "and it reaches the agent WITH the parent folder — the only reason to send it")
        conn.close()


def test_sweep_sends_latin_artist_title_the_workset_calls_safe():
    """`Ernie Maresca - Shoutkaraoke` is exactly this shape, and the regex kept the decoration
    fused into the title because there is no separator to split on."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/Ernie Maresca - Shoutkaraoke.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/ZPBX1-1-06 - Aqua - Barbie Girl Duet.mp3",
             layout="disc_artist_title", language="latn")
        check(lp.build_workset(conn)[0] == [], "both are 'trusted' layouts today")
        recs, _ = lp.build_workset(conn, sweep=True)
        check(len(recs) == 2, [r["stem"] for r in recs])
        conn.close()


def test_sweep_excludes_non_media_rows_from_the_db_too():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/folder.jpg", layout="non_media", language="latn")
        _loc(conn, "d/real song.mp3", layout="artist_title", language="latn")
        recs, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["real song"], recs)
        conn.close()


def _answered(conn, item_id, **kw):
    """The full skip condition: an mbid AND artist+title winning above `llm_parse`."""
    _mbid(conn, item_id, source=kw.get("source", "musicbrainz_text"))
    _named(conn, item_id, **kw)


def test_sweep_excludes_a_stem_that_is_already_answered():
    """A parse for it would be INERT: `musicbrainz_text` sits at ladder rank 13 and `llm_parse`
    at 5, so `v_metadata` would never select the row we paid an agent for."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, known = _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/Nirvana - Lithium.mp3", layout="artist_title", language="latn")
        _answered(conn, known)
        recs, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["nirvana - lithium"],
              f"only the unanswered item is worth asking about: {[r['stem'] for r in recs]}")
        conn.close()


def test_sweep_excludes_a_stem_if_any_copy_is_already_answered():
    """ANY, not ALL: one stem serves every copy of one song, so a single answered item means the
    text the agent would be reading is already resolved above rank 5."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, a = _loc(conn, "one/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _loc(conn, "two/Queen - Bohemian.mp3", layout="artist_title", language="latn",
             sha="shaOther")
        _answered(conn, a)
        recs, _ = lp.build_workset(conn, sweep=True)
        check(recs == [], f"the whole stem drops out, not just the answered copy: {recs}")
        conn.close()


def test_sweep_includes_an_item_with_an_mbid_but_no_title():
    """THE `musicbrainz_fp` SHAPE, and half the reason the skip test is an INTERSECTION. The
    fingerprint source wrote 3,511 mbids and only 3,260 titles; the 131 items in that gap hold
    an identifier and NO winning title, so an `llm_parse` title is exactly the value
    `v_metadata` would select. An mbid alone must never exclude anything. 134 stems."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _mbid(conn, item, source="musicbrainz_fp")
        _named(conn, item, source="musicbrainz_fp", title=None)   # artist only, as fp does
        recs, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["queen - bohemian"],
              f"an mbid without a winning title must NOT exclude: {recs}")
        conn.close()


def test_sweep_includes_a_catalogue_named_item_that_has_no_mbid():
    """The OTHER half of the intersection, and a deliberate spending choice. 3,092 stems are
    named above rank 5 by iTunes/Spotify/Deezer/the MB artist index with no mbid anywhere. A
    parse for them IS inert at today's rank 5 — they are swept because migration 007 flags that
    rank as provisional, so if the §6.2 gate promotes `llm_parse` the parses already exist
    (sha-yol, 2026-08-11)."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _named(conn, item, source="itunes_text")      # named above rank 5, but NO song_mbid
        recs, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["queen - bohemian"],
              f"named-but-no-mbid stays IN the sweep: {recs}")
        conn.close()


def test_sweep_needs_BOTH_fields_covered_to_skip():
    """One field above rank 5 is not enough — the other is still ours to answer."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, a = _loc(conn, "d/artist only.mp3", layout="artist_title", language="latn")
        _, t = _loc(conn, "d/title only.mp3", layout="artist_title", language="latn")
        _answered(conn, a, title=None)
        _answered(conn, t, artist=None)
        recs, _ = lp.build_workset(conn, sweep=True)
        check(sorted(r["stem"] for r in recs) == ["artist only", "title only"], recs)
        conn.close()


def test_sweep_ignores_names_that_do_not_outrank_llm_parse():
    """A `filename` artist+title at rank 4 is exactly what this pass exists to beat, and an
    empty string is not a name however high it ranks."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, low = _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _, blank = _loc(conn, "d/Nirvana - Lithium.mp3", layout="artist_title", language="latn")
        _answered(conn, low, source="filename")
        _answered(conn, blank, source="musicbrainz_text", artist="  ", title="")
        recs, _ = lp.build_workset(conn, sweep=True)
        check(sorted(r["stem"] for r in recs) == ["nirvana - lithium", "queen - bohemian"], recs)
        conn.close()


def test_sweep_reads_the_llm_parse_rank_out_of_the_ladder():
    """Migration 007 renumbered the WHOLE ladder at once and says a promotion or demotion of
    this source is a follow-up migration. A hardcoded 4 would survive that silently and filter
    against a rank the DB no longer uses."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        check(lp._llm_parse_rank(conn) == lp.LLM_PARSE_RANK,
              "the view and the documented constant must agree today")
        conn.close()


def test_sweep_refuses_a_database_without_migration_007():
    """NO FALLBACK. A default of 4 fires in exactly one case — 007 not applied — and that is the
    database where 4 means something else: the pre-007 ladder puts `deezer_text` at 4 and
    `title_card_ocr` at 3, so '> 4' silently selects different sources. Measured at 180 stems'
    difference against the live DB. A wrong answer that announces nothing is worse than a
    refusal."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        # schema.sql has been rolled forward past 007, so the pre-007 state has to be restored
        # deliberately. This CASE is copied VERBATIM from the live DB's v_metadata as it stands
        # today (read-only, 2026-08-11) — the fixture is the real thing, not an approximation,
        # and `deezer_text` really does sit at 4 there.
        conn.executescript("""
            DROP VIEW IF EXISTS v_songs;
            DROP VIEW IF EXISTS v_metadata;
            CREATE VIEW v_metadata AS
            SELECT media_item_id, field, value, source, confidence, source_rank FROM (
              SELECT media_item_id, field, value, source, confidence, source_rank,
                     ROW_NUMBER() OVER (PARTITION BY media_item_id, field
                         ORDER BY source_rank DESC, COALESCE(confidence,0) DESC, source ASC
                     ) AS rn
              FROM (
                SELECT media_item_id, field, value, source, confidence,
                       CASE source
                           WHEN 'manual'               THEN 12
                           WHEN 'musicbrainz_fp'       THEN 11
                           WHEN 'musicbrainz_text'     THEN 10
                           WHEN 'musicbrainz_freetext' THEN  9
                           WHEN 'musicbrainz_artist'   THEN  8
                           WHEN 'wikidata'             THEN  7
                           WHEN 'spotify_text'         THEN  6
                           WHEN 'itunes_text'          THEN  5
                           WHEN 'deezer_text'          THEN  4
                           WHEN 'title_card_ocr'       THEN  3
                           WHEN 'filename'             THEN  2
                           WHEN 'id3'                  THEN  1
                           ELSE 0 END AS source_rank
                FROM song_metadata WHERE value IS NOT NULL)) WHERE rn = 1;
        """)
        conn.commit()
        _loc(conn, "d/anything.mp3", layout="artist_title", language="latn")
        try:
            lp.build_workset(conn, sweep=True)
        except SystemExit as exc:
            msg = str(exc)
            check("007" in msg, f"the error names the missing migration: {msg}")
            check("llm_parse" in msg, msg)
        else:
            check(False, "a pre-007 database must refuse, not guess a rank")
        conn.close()


def test_sweep_excludes_a_stem_with_no_media_item():
    """`location_parses` runs over ALL locations including excluded ones (§6.1), so 786 stems
    resolve to no item. `promote` counts those `skipped_no_item` and writes nothing for them —
    sending them to an agent buys nothing either."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/orphan.mp3", layout="artist_title", language="latn", item=False)
        _loc(conn, "d/attached.mp3", layout="artist_title", language="latn")
        recs, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["attached"], recs)
        conn.close()


def test_sweep_dedupes_by_stem_and_keeps_every_full_path():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/SF001-01.mp3", layout="artist_title", language="latn", sha="pair")
        _loc(conn, "a/SF001-01.cdg", layout="artist_title", language="latn", sha="pair",
             item=False)
        _loc(conn, "backup/SF001-01.mp3", layout="artist_title", language="latn", sha="copy")
        recs, _ = lp.build_workset(conn, sweep=True)
        check(len(recs) == 1, f"mp3/cdg pair + duplicate copy are ONE key: {recs}")
        check(sorted(recs[0]["paths"]) == ["a/SF001-01.cdg", "a/SF001-01.mp3",
                                           "backup/SF001-01.mp3"], recs[0]["paths"])
        conn.close()


def test_sweep_skips_stems_already_parsed_at_this_prompt_version():
    """Resumability is load-bearing at 43 batches: the pass has to be doable in pieces, and a
    Ctrl-C mid-emit must cost nothing."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/first.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/second.mp3", layout="disc_artist_title", language="latn")
        check(len(lp.build_workset(conn, sweep=True)[0]) == 2)
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('first', 'A', 'T', 0.9, 'artist_title', 'm', ?)",
            (lp.PROMPT_VERSION,))
        conn.commit()
        recs, n_cached = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["second"], recs)
        check(n_cached == 1, n_cached)
        # REVERSED at the v2 bump, deliberately — see the workset test above and PROMPT_VERSION.
        check(len(lp.build_workset(conn, sweep=True, prompt_version="v-next")[0]) == 1,
              "a prompt bump must not re-open an answered stem")
        conn.close()


def test_a_stem_parsed_at_v1_is_skipped_once_the_prompt_version_is_v2():
    """THE FIX-FORWARD RULE. 1,200 wave-one sweep stems plus 394 goldset stems were answered at
    v1; the v2 bump fixed the prompt and must not re-open a single one of them. `_cached_stems`
    therefore asks "answered at ANY version?", never "answered at THIS version?" — the whole
    difference between a 36-batch remainder and a 43-batch one."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/answered.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/fresh.mp3", layout="artist_title", language="latn")
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('answered', 'A', 'T', 0.9, 'artist_title', 'm', 'v1')")
        conn.commit()
        check(lp.PROMPT_VERSION != "v1", "this test is meaningless if v1 IS the current version")
        recs, n_cached = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in recs] == ["fresh"],
              f"the v1 answer must still count as answered at {lp.PROMPT_VERSION}: {recs}")
        check(n_cached == 1, n_cached)
        # And the same holds for the layout-filtered workset — one skip rule, not two.
        _loc(conn, "d/hebrew one.mp3", layout="title_only", language="he")
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('hebrew one', 'A', 'T', 0.9, 'title_only', 'm', 'v1')")
        conn.commit()
        check(lp.build_workset(conn)[0] == [], "a v1 answer skips in the workset arm too")
        conn.close()


def test_reparse_re_emits_stems_answered_under_an_older_prompt():
    """The explicit opt-in. Without it an answered stem is never asked again; with it, answers
    from OTHER versions are re-opened — and it stays resumable, because an answer at the CURRENT
    version still counts as cached (a Ctrl-C'd --reparse run must not redo its own work)."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/old.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/current.mp3", layout="artist_title", language="latn")
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('old', 'A', 'T', 0.9, 'artist_title', 'm', 'v1')")
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('current', 'A', 'T', 0.9, 'artist_title', 'm', ?)",
            (lp.PROMPT_VERSION,))
        conn.commit()
        check(lp.build_workset(conn, sweep=True)[0] == [], "default: both stay skipped")
        recs, _ = lp.build_workset(conn, sweep=True, reparse=True)
        check([r["stem"] for r in recs] == ["old"],
              f"--reparse re-opens the v1 answer and only that one: {recs}")
        conn.close()


def test_reparse_is_off_unless_asked_for():
    """A flag that spends 1,594 stems of agent time must never be reachable by accident. The
    keyword defaults off, and `cmd_extract` reads it with a `getattr` default so an older caller
    that never heard of it cannot re-emit either."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/x.mp3", layout="artist_title", language="latn")
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, model, "
            "prompt_version) VALUES ('x', 'A', 'T', 0.9, 'artist_title', 'm', 'v1')")
        conn.commit()
        check(lp.build_workset(conn, sweep=True)[0] == [], "the keyword defaults to OFF")
        conn.close()
    src = (ROOT / "tools" / "llm_parse.py").read_text(encoding="utf-8")
    check('getattr(args, "reparse", False)' in src,
          "cmd_extract must tolerate a namespace without --reparse, defaulting to off")


def test_promote_reads_every_prompt_version():
    """The other half of fix-forward, and the one that fails SILENTLY if it is wrong. `promote`
    used to filter on the current PROMPT_VERSION — harmless while one version existed, a clean
    no-op over 1,594 rows the moment v2 arrived."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "d/old.mp3", layout="artist_title", language="latn")
        conn.execute(
            "INSERT INTO llm_parses (stem, artist, title, confidence, layout, script, model, "
            "prompt_version) VALUES ('old', 'A', 'T', 0.95, 'artist_title', 'latn', 'm', 'v1')")
        conn.commit()
        c = lp.promote(conn)
        check(c["parses_seen"] == 1 and c["rows_written"] == 2,
              f"a v1 parse must still promote under v{lp.PROMPT_VERSION}: {c}")
        conn.close()


def test_sweep_reports_what_it_skipped_and_why():
    """Every stem that did not make it is accounted for. The control arm's lesson is that a
    silent exclusion is how this tool got it wrong the first time."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, known = _loc(conn, "d/known.mp3", layout="artist_title", language="latn")
        _answered(conn, known)
        _loc(conn, "d/orphan.mp3", layout="artist_title", language="latn", item=False)
        _loc(conn, "d/wanted.mp3", layout="artist_title", language="latn")
        stats: dict = {}
        recs, _ = lp.build_workset(conn, sweep=True, stats=stats)
        check([r["stem"] for r in recs] == ["wanted"], recs)
        check(stats["skipped_already_answered"] == 1 and stats["skipped_no_item"] == 1, stats)
        conn.close()


def test_sweep_skip_counts_are_per_stem_not_per_location():
    """A stem with 40 copies must not report as 40 skips, or the accounting stops adding up."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, a = _loc(conn, "one/dup.mp3", layout="artist_title", language="latn")
        _loc(conn, "two/dup.mp3", layout="artist_title", language="latn", sha="shaB")
        _loc(conn, "three/dup.mp3", layout="artist_title", language="latn", sha="shaC")
        _answered(conn, a)
        stats: dict = {}
        lp.build_workset(conn, sweep=True, stats=stats)
        check(stats["skipped_already_answered"] == 1, stats)
        conn.close()


def test_sweep_leaves_the_layout_filtered_workset_untouched():
    """The sweep is an ADDITIONAL mode, not a replacement — the gate that produced the evidence
    against the layout filter has to stay reproducible."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "d/Queen - Bohemian.mp3", layout="artist_title", language="latn")
        _loc(conn, "d/עברית - שיר.mp3", layout="artist_title", language="he")
        _loc(conn, "d/SONG-abcd.mp4", layout="opaque", language="latn")
        main, _ = lp.build_workset(conn)
        ctl, _ = lp.build_workset(conn, control=True, include_cached=True)
        swept, _ = lp.build_workset(conn, sweep=True)
        check([r["stem"] for r in main] == ["עברית - שיר"], main)
        check([r["stem"] for r in ctl] == ["queen - bohemian"], ctl)
        check(len(swept) == 3, [r["stem"] for r in swept])
        conn.close()


def test_sweep_gets_its_own_default_run_directory():
    names = {k: lp.resolve_run(None, k)
             for k in ("workset", "goldset", "goldset_swaps", "sweep")}
    check(len(set(names.values())) == 4,
          f"a sweep must not be able to land on another arm's run dir: {names}")
    check(names["sweep"].startswith("run-") and names["sweep"].endswith("-sweep"), names)
    check(lp.resolve_run("my-run", "sweep") == "my-run", "an explicit --run still wins")


def test_the_collision_guard_covers_the_sweep_unchanged():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        lp.emit_batches(_rec(), "sweepcollide", kind="sweep")
        try:
            lp.emit_batches(_rec("other"), "sweepcollide", kind="goldset")
        except SystemExit as exc:
            check("sweep" in str(exc), "the error names the arm already there")
        else:
            check(False, "a second emission into a sweep run dir must refuse, not overwrite")
        conn.close()


def test_sweep_batches_are_labelled_as_a_sweep():
    """`ingest` is handed a result file and resolves the batch beside it, so the mode has to
    survive on the batch itself — a sweep answer must be tellable from a goldset answer later."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        batches, manifest = lp.emit_batches(_rec(), "sweepkind", kind="sweep")
        check(json.loads(batches[0].read_text("utf-8"))["kind"] == "sweep")
        check(json.loads(manifest.read_text("utf-8"))["kind"] == "sweep")
        conn.close()


# --- §6.2 goldset ---------------------------------------------------------------------------

def test_goldset_is_stratified_and_carries_a_control_arm():
    recs = [{"stem": f"he-ta-{i}", "script": "he", "current": {"layout": "title_artist"}}
            for i in range(500)]
    recs += [{"stem": f"latn-to-{i}", "script": "latn", "current": {"layout": "title_only"}}
             for i in range(300)]
    recs += [{"stem": f"ru-to-{i}", "script": "ru", "current": {"layout": "title_only"}}
             for i in range(14)]     # the real ru stratum size — must not round to zero
    ctl = [{"stem": f"ctl-{i}", "script": "latn", "current": {"layout": "artist_title"}}
           for i in range(200)]

    picked = lp.stratify(recs, ctl, 200)
    check(len(picked) == 200, len(picked))
    n_ctl = sum(1 for r in picked if r.get("control"))
    check(n_ctl == 30, f"15% control arm: {n_ctl}")
    scripts = {r["script"] for r in picked}
    check(scripts == {"he", "latn", "ru"}, f"every script represented: {scripts}")
    check(any(r["script"] == "ru" for r in picked),
          "a 14-stem stratum must not round to zero — that is why every stratum seeds first")


def test_goldset_is_deterministic():
    """Two prompt revisions have to be comparable on identical inputs."""
    recs = [{"stem": f"s{i}", "script": "he", "current": {"layout": "title_artist"}}
            for i in range(100)]
    ctl = [{"stem": f"c{i}", "script": "latn", "current": {"layout": "artist_title"}}
           for i in range(50)]
    a = [r["stem"] for r in lp.stratify(recs, ctl, 40)]
    b = [r["stem"] for r in lp.stratify(recs, ctl, 40)]
    check(a == b, "a fixed seed means the gate batch is reproducible")


# --- batch emission --------------------------------------------------------------------------

def test_batches_are_chunked_and_manifested():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        recs = [{"stem": f"s{i}", "paths": [f"d/s{i}.mp3"], "current": {"layout": "title_only"},
                 "siblings": [], "script": "he", "locations": [i]} for i in range(450)]
        batches, manifest = lp.emit_batches(recs, "t1", size=200)
        check(len(batches) == 3, len(batches))
        first = json.loads(batches[0].read_text(encoding="utf-8"))
        check(len(first["items"]) == 200 and first["prompt_version"] == lp.PROMPT_VERSION)
        check(first["batch_id"] == "t1/batch-001")
        m = json.loads(manifest.read_text(encoding="utf-8"))
        check(m["stems"] == 450 and m["batches"] == 3)
        check(not list(lp.run_dir("t1").glob("*.part")), "no half-written batch survives")
        conn.close()


def test_dry_run_emits_nothing():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        recs = [{"stem": "s", "paths": ["d/s.mp3"], "current": {"layout": "title_only"},
                 "siblings": [], "script": "he", "locations": [1]}]
        lp.emit_batches(recs, "dry", dry_run=True)
        check(not lp.run_dir("dry").exists(), "§13: a dry run writes nothing at all")
        conn.close()


# --- run-directory collision ------------------------------------------------------------------
# Regression: both gate arms defaulted to the same date-derived run name AND the same
# positional batch-NNN.json filenames, so running --goldset then --goldset-swaps on one day
# silently overwrote the first arm's batch, manifest and labels.json answer key. No error, no
# trace. Two independent guards, both pinned here: distinct default names, and a hard refusal
# to write into a directory that already carries a manifest.

def _rec(stem="s"):
    return [{"stem": stem, "paths": [f"d/{stem}.mp3"], "current": {"layout": "title_only"},
             "siblings": [], "script": "he", "locations": [1]}]


def test_each_arm_gets_its_own_default_run_directory():
    names = {k: lp.resolve_run(None, k) for k in ("workset", "goldset", "goldset_swaps")}
    check(len(set(names.values())) == 3,
          f"the three arms must not share a default run dir: {names}")
    for kind, name in names.items():
        check(name.startswith("run-"), name)
    check(names["workset"] != names["goldset"] != names["goldset_swaps"])


def test_explicit_run_always_wins():
    for kind in ("workset", "goldset", "goldset_swaps"):
        check(lp.resolve_run("my-run", kind) == "my-run", kind)


def test_emit_refuses_to_clobber_an_existing_run():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        lp.emit_batches(_rec(), "collide", kind="goldset_swaps")
        (lp.run_dir("collide") / "labels.json").write_text('{"s": {}}', encoding="utf-8")
        try:
            lp.emit_batches(_rec("other"), "collide", kind="goldset")
        except SystemExit as exc:
            msg = str(exc)
            check("goldset_swaps" in msg, "the error names the arm already there")
            check("labels.json" in msg, "and warns the answer key would go with it")
        else:
            check(False, "a second emission into the same run dir must refuse, not overwrite")
        # the original arm is untouched
        first = json.loads((lp.run_dir("collide") / "batch-001.json").read_text("utf-8"))
        check(first["items"][0]["stem"] == "s", "the prior batch survived intact")
        conn.close()


def test_force_overwrites_deliberately():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        lp.emit_batches(_rec(), "forced", kind="goldset_swaps")
        lp.emit_batches(_rec("other"), "forced", kind="goldset", force=True)
        got = json.loads((lp.run_dir("forced") / "batch-001.json").read_text("utf-8"))
        check(got["items"][0]["stem"] == "other", "--force is an explicit overwrite")
        conn.close()


def test_dry_run_never_trips_the_collision_guard():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        lp.emit_batches(_rec(), "dryguard", kind="goldset_swaps")
        lp.emit_batches(_rec("other"), "dryguard", kind="goldset", dry_run=True)
        got = json.loads((lp.run_dir("dryguard") / "batch-001.json").read_text("utf-8"))
        check(got["items"][0]["stem"] == "s", "§13: planning must never mutate or refuse")
        conn.close()


# --- the prompt ------------------------------------------------------------------------------

def test_prompt_states_every_hard_rule():
    text = lp.SYSTEM_PROMPT
    for needle in ("null", "transliterate", "hint", "keyboard", "confidence", "notes",
                   "results"):
        check(needle in text.lower(), f"the prompt must state the {needle!r} rule")
    check("שרית חדד" in text, "the no-transliteration rule is stated with a concrete example")


def test_user_prompt_embeds_the_batch_items_verbatim():
    batch = {"batch_id": "r/batch-001",
             "items": [{"stem": "15 ניסים", "paths": ["a/58 חלומות/15 ניסים.avi"],
                        "current": {"layout": "title_only"}, "siblings": [], "script": "he",
                        "locations": [1]}]}
    text = lp.user_prompt(batch)
    check("15 ניסים" in text and "58 חלומות" in text, "Hebrew is not escaped away")
    check("a/58 חלומות/15 ניסים.avi" in text, "the FULL path reaches the agent")


# --- strict validation + requeue ---------------------------------------------------------------

def _batch(*items):
    return {"batch_id": "r/batch-001", "items": list(items)}


def _item(stem, *, paths=None, script="latn"):
    return {"stem": stem, "paths": paths or [f"d/{stem}.mp3"], "siblings": [],
            "current": {"layout": "title_artist"}, "script": script, "locations": [1]}


def test_validator_accepts_a_clean_row():
    b = _batch(_item("queen - bohemian"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "queen - bohemian", "artist": "Queen", "title": "Bohemian Rhapsody",
         "confidence": 0.95, "layout": "artist_title", "notes": ""}]})
    check(len(good) == 1 and not bad and not errs, (good, bad, errs))
    check(good[0]["artist"] == "Queen" and good[0]["confidence"] == 0.95)


def test_validator_rejects_an_unknown_stem_and_requeues_the_real_one():
    b = _batch(_item("real"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "hallucinated", "artist": "A", "title": "T", "confidence": 0.9,
         "layout": "artist_title"}]})
    check(not good, good)
    check(bad == ["real"], f"the unanswered real stem is REQUEUED, not dropped: {bad}")
    check(any("unknown stem" in e for e in errs), errs)


def test_validator_rejects_out_of_range_and_non_numeric_confidence():
    for conf in (1.5, -0.1, "high", None, True):
        b = _batch(_item("s"))
        good, bad, _ = lp.validate_results(b, {"results": [
            {"stem": "s", "artist": "A", "title": "T", "confidence": conf,
             "layout": "artist_title"}]})
        check(not good and bad == ["s"], f"confidence={conf!r} must be refused and requeued")


def test_validator_accepts_the_layouts_the_pipeline_actually_emits():
    """v1's rule 8 offered `artist_from_folder` — a name this pipeline has never produced — and
    could express NEITHER `title_artist_from_folder` (429 stems library-wide) nor
    `disc_title_artist` (61 in the sweep). 3 of 5 wave-one agents remapped to the nearest
    allowed value, making `layout` lossy on ~490 stems."""
    for layout in ("title_artist_from_folder", "disc_title_artist"):
        b = _batch(_item("s"))
        good, bad, errs = lp.validate_results(b, {"results": [
            {"stem": "s", "artist": "A", "title": "T", "confidence": 0.9, "layout": layout}]})
        check(len(good) == 1 and not bad, f"{layout} must validate: {errs}")
        check(good[0]["layout"] == layout, "and be recorded verbatim, not remapped")


def test_the_prompt_offers_exactly_the_layouts_the_validator_accepts():
    """The v1 defect was two hand-written lists that disagreed. Rule 8 is now RENDERED from
    MODEL_LAYOUTS, so a value the prompt tells the model to emit cannot be one the validator
    rejects — this asserts the property, not the mechanism."""
    check(lp.MODEL_LAYOUTS <= lp.VALID_LAYOUTS,
          "every offered layout must validate")
    for layout in lp.MODEL_LAYOUTS:
        check(layout in lp.SYSTEM_PROMPT, f"rule 8 must name {layout}")
    check("__LAYOUT_VOCAB__" not in lp.SYSTEM_PROMPT, "the placeholder must be substituted")


def test_the_model_vocabulary_is_derived_from_the_pipelines_own():
    """`SELECT DISTINCT layout FROM location_parses` on the live index, 2026-08-12, is the
    authority. Two are withheld from the model on purpose and neither is a gap: `non_media` is
    excluded from the sweep by construction, and `unparsed` is the REGEX's admission that its
    patterns did not fire — the model's equivalent is `opaque`."""
    check(lp.MODEL_LAYOUTS <= lp.PIPELINE_LAYOUTS,
          "the model may not be offered a layout the pipeline has no name for")
    check(lp.PIPELINE_LAYOUTS - lp.MODEL_LAYOUTS == {"non_media", "unparsed"},
          f"exactly two withheld: {lp.PIPELINE_LAYOUTS - lp.MODEL_LAYOUTS}")
    for layout in ("non_media", "unparsed"):
        check(layout not in lp.SYSTEM_PROMPT.split("vocabulary and no other:")[1].split(".")[0],
              f"{layout} must not appear in rule 8's list")


def test_artist_from_folder_is_accepted_but_no_longer_offered():
    """v1 invented it and 37 of the 1,594 already-ingested rows carry it. Re-validating one of
    those batch files must not start failing on a value we ourselves asked for — but the prompt
    must stop offering a name the pipeline does not use."""
    check("artist_from_folder" in lp.VALID_LAYOUTS, "still accepted for the 37 v1 rows")
    check("artist_from_folder" not in lp.MODEL_LAYOUTS, "no longer offered")
    rule8 = lp.SYSTEM_PROMPT.split("vocabulary and no other:")[1].split(".")[0]
    check("title_artist_from_folder" in rule8, "the pipeline's real name IS offered")
    check("artist_from_folder" not in rule8.replace("title_artist_from_folder", ""),
          "the invented name is not")
    b = _batch(_item("s"))
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "s", "artist": "A", "title": "T", "confidence": 0.9,
         "layout": "artist_from_folder"}]})
    check(len(good) == 1 and not bad, "a v1 row still re-validates")


def test_validator_rejects_a_bad_layout_and_bad_types():
    b = _batch(_item("s"))
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "s", "artist": "A", "title": "T", "confidence": 0.9, "layout": "freeform"}]})
    check(not good and bad == ["s"], "layout must come from the fixed vocabulary")
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "s", "artist": ["A"], "title": "T", "confidence": 0.9,
         "layout": "artist_title"}]})
    check(not good and bad == ["s"], "artist must be a string or null")


def test_validator_rejects_a_non_object_payload_and_requeues_everything():
    b = _batch(_item("a"), _item("b"))
    good, bad, errs = lp.validate_results(b, "I could not parse these files.")
    check(not good and bad == ["a", "b"], f"a garbled payload requeues the WHOLE batch: {bad}")
    check(errs, errs)


def test_validator_requeues_missing_rows():
    b = _batch(_item("a"), _item("b"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "a", "artist": "A", "title": "T", "confidence": 0.9,
         "layout": "artist_title"}]})
    check([g["stem"] for g in good] == ["a"])
    check(bad == ["b"], f"a dropped stem is requeued: {bad}")
    check(any("no result returned" in e for e in errs), errs)


def test_validator_rejects_transliterated_hebrew():
    """The hard project rule, ENFORCED rather than merely requested. End users search in
    Hebrew, so a correct-but-Latin answer is unusable."""
    b = _batch(_item("שרית חדד - אני ואתה", paths=["heb/שרית חדד - אני ואתה.mp3"], script="he"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "שרית חדד - אני ואתה", "artist": "Sarit Hadad", "title": "Ani Ve'ata",
         "confidence": 0.95, "layout": "artist_title"}]})
    check(not good, good)
    check(bad == ["שרית חדד - אני ואתה"], f"requeued, not dropped: {bad}")
    check(any("transliterated" in e for e in errs), errs)


def test_validator_allows_hebrew_answers_for_hebrew_input():
    b = _batch(_item("שרית חדד - אני ואתה", paths=["heb/שרית חדד - אני ואתה.mp3"], script="he"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "שרית חדד - אני ואתה", "artist": "שרית חדד", "title": "אני ואתה",
         "confidence": 0.95, "layout": "artist_title"}]})
    check(len(good) == 1 and not bad, (good, bad, errs))


def test_validator_allows_a_latin_song_inside_a_hebrew_folder():
    """This library is full of these (`בקשות/Before He Cheats ....mp4`). A blanket
    'he input ⇒ he output' rule would reject every one of them — which is why the check
    exempts Latin text that was literally in the path."""
    b = _batch(_item("before he cheats in the style of carrie underwood",
                     paths=["בקשות/Before He Cheats in the Style of Carrie Underwood.mp4"],
                     script="he"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "before he cheats in the style of carrie underwood",
         "artist": "Carrie Underwood", "title": "Before He Cheats",
         "confidence": 0.9, "layout": "style_of"}]})
    check(len(good) == 1 and not bad, (good, bad, errs))


def test_validator_allows_a_spelling_correction_on_a_latin_filename_under_a_hebrew_folder():
    """THE GUARD'S SCOPE FIX. Measured 2026-08-11: 711 of 8,397 sweep stems (8.5%) are a
    Latin-only filename under a Hebrew-named folder. v1 fired the guard whenever ANY path held
    Hebrew and then demanded the Latin output appear LITERALLY in the path — which permits
    copying and forbids correcting, while rule 1 explicitly permits correcting spelling. Wave
    one hit this twice in one batch and the agent reverted to the path's misspelling to get
    past the validator. A Latin answer to a Latin filename has no Hebrew to be a
    transliteration OF."""
    b = _batch(_item("olivia n john-banks of the ohio",
                     paths=["קריוקי בעברית 1/olivia n john-banks of the ohio.mp3"], script="he"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "olivia n john-banks of the ohio", "artist": "Olivia Newton-John",
         "title": "Banks of the Ohio", "confidence": 0.9, "layout": "artist_title",
         "notes": "corrected the performer's name"}]})
    check(len(good) == 1 and not bad, f"a correction must pass: {errs}")
    check(good[0]["artist"] == "Olivia Newton-John", "and be kept as corrected, not reverted")


def test_validator_allows_ft_expanded_to_feat_under_a_hebrew_folder():
    """The second wave-one casualty. `feat` is nowhere in the path — under v1's literal-presence
    rule that alone rejected the row."""
    b = _batch(_item("song ft. someone", paths=["מוזיקה/Song ft. Someone.mp4"], script="he"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "song ft. someone", "artist": "Someone", "title": "Song feat. Someone",
         "confidence": 0.8, "layout": "title_artist"}]})
    check(len(good) == 1 and not bad, f"`ft.` -> `feat.` must pass: {errs}")


def test_validator_still_rejects_transliteration_of_a_hebrew_filename():
    """The scope fix must not cost the rule its teeth. Here the SOURCE TEXT is Hebrew, so a
    Latin answer is exactly what rule 3 forbids — regardless of what the folder is named."""
    b = _batch(_item("שרית חדד - אני ואתה",
                     paths=["Music/Israeli/שרית חדד - אני ואתה.mp3"], script="latn"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "שרית חדד - אני ואתה", "artist": "Sarit Hadad", "title": "Ani Ve'ata",
         "confidence": 0.95, "layout": "artist_title"}]})
    check(not good and bad == ["שרית חדד - אני ואתה"],
          f"a Hebrew filename still may not come back in Latin: {good}")
    check(any("transliterated" in e for e in errs), errs)


def test_validator_rejects_a_transliterated_folder_artist():
    """The one hole the scoping could have opened, closed. Latin filename, Hebrew folder, and
    the answer itself says the artist came FROM the folder — so the folder IS that field's
    source text and a Latin artist is a transliteration of it."""
    b = _batch(_item("15 shir", paths=["קריוקי/שרית חדד/15 shir.mp3"], script="he"))
    good, bad, errs = lp.validate_results(b, {"results": [
        {"stem": "15 shir", "artist": "Sarit Hadad", "title": "Shir", "confidence": 0.8,
         "layout": "title_artist_from_folder"}]})
    check(not good and bad == ["15 shir"], f"the folder artist must be refused: {good}")
    check(any("transliterated" in e for e in errs), errs)
    # …and the Hebrew answer for the same path is accepted.
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "15 shir", "artist": "שרית חדד", "title": "Shir", "confidence": 0.8,
         "layout": "title_artist_from_folder"}]})
    check(len(good) == 1 and not bad, "Hebrew from the folder is the RIGHT answer")


def test_the_residual_gap_is_recorded_rather_than_claimed_shut():
    """PINS A KNOWN GAP, deliberately — read the assertion as documentation, not approval.

    An answer that claims `artist_title` while quietly transliterating the Hebrew FOLDER is
    scoped to the Latin filename, finds no Hebrew source, and PASSES. That cannot be decided
    from the path, because the shape it would have to be distinguished from is the one this fix
    exists to permit: `Olivia Newton-John` from `olivia n john-...` and `Sarit Hadad` from
    `15 shir`, both under a Hebrew folder, are Latin text absent from a Latin filename. Only the
    model knows which segment it read, and `layout` is where it says so — which is why the
    DECLARED-folder version of this exact input is caught (test above).

    v1 rejected both and cost 711 stems the right to be corrected; v2 accepts both. If this
    ever bites, the evidence is an `artist_title` answer sharing no token with the filename —
    measure it before tightening, because a legitimate correction looks the same."""
    b = _batch(_item("15 shir", paths=["קריוקי/שרית חדד/15 shir.mp3"], script="he"))
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "15 shir", "artist": "Sarit Hadad", "title": "Shir", "confidence": 0.8,
         "layout": "artist_title"}]})
    check(len(good) == 1 and not bad,
          "KNOWN GAP: an undeclared folder source is not decidable from the path")


def test_source_texts_scopes_folders_to_the_artist_only():
    """Folders may supply an ARTIST (rule 4) and nothing else — no folder in this library
    supplies a title, and reading them in for both fields would put the guard straight back
    where it started."""
    item = {"paths": ["קריוקי/שרית חדד/15 shir.mp3"]}
    art = lp.source_texts(item, "artist", "title_artist_from_folder")
    check(any(lp.has_hebrew(s) for s in art), f"the folder reaches the artist: {art}")
    for layout in ("title_artist_from_folder", "artist_title"):
        tit = lp.source_texts(item, "title", layout)
        check(not any(lp.has_hebrew(s) for s in tit), f"never the title ({layout}): {tit}")
    plain = lp.source_texts(item, "artist", "artist_title")
    check(not any(lp.has_hebrew(s) for s in plain),
          f"and not the artist either unless the layout says so: {plain}")


def test_validator_requires_opaque_when_both_fields_are_null():
    b = _batch(_item("s"))
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "s", "artist": None, "title": None, "confidence": 0.9,
         "layout": "title_only"}]})
    check(not good and bad == ["s"], "a double null is only legal as an explicit 'opaque'")
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "s", "artist": None, "title": None, "confidence": 0.2, "layout": "opaque"}]})
    check(len(good) == 1 and not bad, "null over a guess is the REQUIRED answer, not a failure")


def test_validator_accepts_a_null_artist_on_a_title_only_path():
    b = _batch(_item("some title"))
    good, bad, _ = lp.validate_results(b, {"results": [
        {"stem": "some title", "artist": None, "title": "Some Title", "confidence": 0.9,
         "layout": "title_only"}]})
    check(len(good) == 1 and good[0]["artist"] is None and not bad)


def test_validator_normalizes_whitespace_and_empties_to_null():
    b = _batch(_item("s"))
    good, _, _ = lp.validate_results(b, {"results": [
        {"stem": "s", "artist": "  Queen   Band ", "title": "   ", "confidence": 0.9,
         "layout": "title_only"}]})
    check(good[0]["artist"] == "Queen Band" and good[0]["title"] is None)


def test_requeue_file_is_itself_a_batch_file():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        b = _batch(_item("a"), _item("b"))
        out = lp.write_requeue(b, ["b"], tmp)
        check(out is not None and out.name == "requeue-001.json", out)
        rq = json.loads(out.read_text(encoding="utf-8"))
        check([i["stem"] for i in rq["items"]] == ["b"],
              "only the failed stems, in batch shape, ready to hand straight back")
        check(lp.user_prompt(rq), "a requeue file feeds `prompt` with no special casing")


def test_requeue_is_a_noop_when_nothing_failed():
    with tempfile.TemporaryDirectory() as td:
        check(lp.write_requeue(_batch(_item("a")), [], Path(td)) is None)


# --- promote ---------------------------------------------------------------------------------

def _parse(conn, stem, artist, title, conf, script="he"):
    conn.execute(
        "INSERT INTO llm_parses (stem, artist, title, confidence, layout, script, model, "
        "prompt_version) VALUES (?,?,?,?, 'artist_title', ?, 'test-model', ?)",
        (stem, artist, title, conf, script, lp.PROMPT_VERSION))
    conn.commit()


def test_promote_writes_llm_parse_rows_for_every_item_sharing_the_stem():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item_a = _loc(conn, "a/שיר.mp3", sha="shared")
        item_b = conn.execute(
            "INSERT INTO media_items (format, status) VALUES ('mp3g','active')").lastrowid
        conn.execute("INSERT INTO media_item_files (media_item_id, content_hash, role) "
                     "VALUES (?, 'shared', 'audio')", (item_b,))
        conn.commit()
        _parse(conn, "שיר", "שרית חדד", "אני ואתה", 0.95)
        c = lp.promote(conn)
        check(c["items_promoted"] == 2, c)
        rows = conn.execute(
            "SELECT media_item_id, field, value, confidence FROM song_metadata "
            "WHERE source='llm_parse' ORDER BY media_item_id, field").fetchall()
        check(len(rows) == 4, f"artist+title for both items: {[tuple(r) for r in rows]}")
        check({r["value"] for r in rows} == {"שרית חדד", "אני ואתה"})
        check(all(r["confidence"] == 0.95 for r in rows))
        check(sorted({r["media_item_id"] for r in rows}) == sorted([item_a, item_b]))
        conn.close()


def test_promote_is_idempotent():
    """§13: run twice back-to-back ⇒ zero changes the second time. Observably zero, not merely
    harmless — `total_changes` must not move."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/שיר.mp3")
        _parse(conn, "שיר", "שרית חדד", "אני ואתה", 0.95)
        first = lp.promote(conn)
        check(first["rows_written"] == 2, first)
        before = conn.total_changes
        second = lp.promote(conn)
        check(second["rows_written"] == 0, f"second run must write nothing: {second}")
        check(second["items_promoted"] == 1, "it still REPORTS the items it covers")
        check(conn.total_changes == before, "no INSERT/UPDATE executed at all")
        check(conn.execute("SELECT COUNT(*) FROM song_metadata WHERE source='llm_parse'"
                           ).fetchone()[0] == 2, "and no rows doubled")
        conn.close()


def test_promote_honours_the_confidence_floor():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/high.mp3")
        _loc(conn, "a/low.mp3")
        _parse(conn, "high", "A", "T", 0.9)
        _parse(conn, "low", "B", "U", 0.5)
        c = lp.promote(conn, floor=0.75)
        check(c["skipped_low_confidence"] == 1, c)
        vals = {r[0] for r in conn.execute(
            "SELECT value FROM song_metadata WHERE source='llm_parse'")}
        check(vals == {"A", "T"}, vals)
        # Re-tuning the floor never needs the agents again — the parse is still cached.
        check(conn.execute("SELECT COUNT(*) FROM llm_parses").fetchone()[0] == 2)
        c = lp.promote(conn, floor=0.4)
        check(c["skipped_low_confidence"] == 0 and c["rows_written"] == 2, c)
        conn.close()


def test_promote_never_writes_null_or_empty_values():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/only.mp3")
        _parse(conn, "only", None, "Just A Title", 0.9)
        lp.promote(conn)
        rows = conn.execute("SELECT field, value FROM song_metadata "
                            "WHERE source='llm_parse'").fetchall()
        check([tuple(r) for r in rows] == [("title", "Just A Title")], [tuple(r) for r in rows])
        conn.close()


def test_promote_skips_empty_string_values():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/blank.mp3")
        _parse(conn, "blank", "   ", "", 0.9)
        c = lp.promote(conn)
        check(c["skipped_empty"] == 1 and c["rows_written"] == 0, c)
        conn.close()


def test_promote_dry_run_writes_nothing_but_reports_the_plan():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/שיר.mp3")
        _parse(conn, "שיר", "שרית חדד", "אני ואתה", 0.95)
        c = lp.promote(conn, dry_run=True)
        check(c["rows_written"] == 2 and c["items_promoted"] == 1, c)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata WHERE source='llm_parse'"
                           ).fetchone()[0] == 0, "§13: a dry run writes nothing")
        conn.close()


def test_promote_reports_yield_per_script():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/heb.mp3")
        _loc(conn, "a/lat.mp3")
        _parse(conn, "heb", "שרית חדד", "אני ואתה", 0.9, script="he")
        _parse(conn, "lat", "Queen", "Bohemian", 0.9, script="latn")
        c = lp.promote(conn)
        check(c["promoted_by_script"] == {"he": 1, "latn": 1}, c["promoted_by_script"])
        conn.close()


def test_promote_survives_a_parse_with_no_media_item():
    """`location_parses` runs over ALL locations including excluded ones (§6.1), so a stem with
    no surviving media item is normal, not an error."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _parse(conn, "orphan", "A", "T", 0.9)
        c = lp.promote(conn)
        check(c["skipped_no_item"] == 1 and c["rows_written"] == 0, c)
        conn.close()


def test_llm_parse_source_is_accepted_by_the_check_constraint():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "a/x.mp3")
        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                     "VALUES (?, 'artist', 'A', 'llm_parse')", (item,))
        conn.commit()
        conn.close()


def test_llm_parse_ranks_between_title_card_ocr_and_filename():
    """Migration 007's whole safety argument: a bad LLM parse can never override a corroborated
    catalogue, but always beats the regex parse it replaces."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "a/x.mp3")
        for src, val in (("filename", "FromFilename"), ("llm_parse", "FromLLM"),
                         ("title_card_ocr", "FromOCR")):
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                         "confidence) VALUES (?, 'artist', ?, ?, 0.5)", (item, val, src))
        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                     "confidence) VALUES (?, 'title', 'T-LLM', 'llm_parse', 0.5)", (item,))
        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                     "confidence) VALUES (?, 'title', 'T-FN', 'filename', 0.9)", (item,))
        conn.commit()
        win = {r["field"]: (r["value"], r["source_rank"]) for r in conn.execute(
            "SELECT field, value, source_rank FROM v_metadata WHERE media_item_id=?", (item,))}
        check(win["artist"] == ("FromOCR", 6), f"ocr outranks llm_parse: {win}")
        check(win["title"] == ("T-LLM", 5),
              f"llm_parse outranks filename even on lower confidence: {win}")
        conn.close()


def test_deleting_llm_parse_rows_restores_prior_behaviour_and_keeps_the_cache():
    """Migration 007's reversibility note, and the §11 lesson: re-tuning acceptance must never
    require re-running the agents."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _loc(conn, "a/x.mp3")
        _parse(conn, "x", "A", "T", 0.9)
        lp.promote(conn)
        conn.execute("DELETE FROM song_metadata WHERE source='llm_parse'")
        conn.commit()
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == 0)
        check(conn.execute("SELECT COUNT(*) FROM llm_parses").fetchone()[0] == 1,
              "llm_parses deliberately survives the rollback")
        c = lp.promote(conn)
        check(c["rows_written"] == 2, "and re-promotes from cache with no agent run")
        conn.close()


# --- the self-scoring swap arm ---------------------------------------------------------------

def test_classify_pair_finds_exact_swaps_only():
    check(lp.classify_pair("Queen", "Bohemian Rhapsody", "Queen", "Bohemian Rhapsody") == "agree")
    check(lp.classify_pair("Bohemian Rhapsody", "Queen", "Queen", "Bohemian Rhapsody") == "swap")
    # One-sided agreement is a DECORATION, not evidence of a reversal.
    check(lp.classify_pair("Queen", "Bohemian Rhapsody (Karaoke)",
                           "Queen", "Bohemian Rhapsody") == "subset")
    check(lp.classify_pair("Queen", "A", "Nirvana", "B") == "other")


def test_classify_pair_normalizes_articles_case_and_apostrophes():
    check(lp.classify_pair("The Beatles", "Help!", "Beatles", "Help") == "agree")
    check(lp.classify_pair("Don’t Stop", "Journey", "Journey", "Don't Stop") == "swap")


def test_swap_groundtruth_mines_the_labeled_population():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, swapped = _loc(conn, "d/Bohemian Rhapsody - Queen.mp3",
                          layout="title_artist", language="latn")
        _, ok = _loc(conn, "d/Nirvana - Lithium.mp3", layout="artist_title", language="latn")
        for item, a, t, src in ((swapped, "Bohemian Rhapsody", "Queen", "filename"),
                                (swapped, "Queen", "Bohemian Rhapsody", "musicbrainz_text"),
                                (ok, "Nirvana", "Lithium", "filename"),
                                (ok, "Nirvana", "Lithium", "musicbrainz_text")):
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                         "VALUES (?, 'artist', ?, ?)", (item, a, src))
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                         "VALUES (?, 'title', ?, ?)", (item, t, src))
        conn.commit()
        labeled, tally = lp.swap_groundtruth(conn)
        check(tally == {"agree": 1, "swap": 1}, tally)
        check([p["id"] for p in labeled] == [swapped])
        check((labeled[0]["ea"], labeled[0]["et"]) == ("Queen", "Bohemian Rhapsody"))

        recs = lp.swap_records(conn, labeled)
        check(len(recs) == 1 and recs[0]["arm"] == "swap")
        check(recs[0]["_label"]["artist"] == "Queen")
        conn.close()


def test_the_answer_key_never_reaches_the_batch_file():
    """If the label leaks into the batch, the arm measures nothing."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        rec = {"stem": "bohemian rhapsody - queen", "paths": ["d/Bohemian Rhapsody - Queen.mp3"],
               "siblings": [], "current": {"layout": "title_artist"}, "script": "latn",
               "locations": [1], "arm": "swap",
               "_label": {"media_item_id": 1, "artist": "Queen", "title": "Bohemian Rhapsody"}}
        batches, manifest = lp.emit_batches([rec], "swaps", size=200, kind="goldset_swaps")
        text = batches[0].read_text(encoding="utf-8")
        check("_label" not in text and "media_item_id" not in text, text)
        check(json.loads(text)["items"][0]["arm"] == "swap")
        labels = json.loads((lp.run_dir("swaps") / "labels.json").read_text(encoding="utf-8"))
        check(labels["bohemian rhapsody - queen"]["artist"] == "Queen",
              "the key lives beside the batch, not inside it")
        check(json.loads(manifest.read_text())["labels"] == "labels.json")
        conn.close()


def test_swap_arm_scores_itself():
    labels = {"a": {"artist": "Queen", "title": "Bohemian Rhapsody"},
              "b": {"artist": "Journey", "title": "Don't Stop Believin'"},
              "c": {"artist": "Nirvana", "title": "Lithium"},
              "d": {"artist": "Abba", "title": "Waterloo"}}
    rows = [
        {"stem": "a", "artist": "Queen", "title": "Bohemian Rhapsody"},       # correct
        {"stem": "b", "artist": "Don’t Stop Believin'", "title": "Journey"},  # still swapped
        {"stem": "c", "artist": "Nirvana", "title": "Smells Like Teen Spirit"},
        {"stem": "d", "artist": "Blur", "title": "Song 2"},                   # wrong
    ]
    s = lp.score_against_labels(rows, labels)
    check(s["scored"] == 4, s)
    check(s["both_correct"] == 1 and s["still_swapped"] == 1, s)
    check(s["artist_only"] == 1 and s["wrong"] == 1, s)


def test_scoring_ignores_unlabeled_rows():
    s = lp.score_against_labels([{"stem": "x", "artist": "A", "title": "T"}], {})
    check(s["scored"] == 0, s)


# --- the §6.1 decoration report ----------------------------------------------------------------

def test_decoration_report_ranks_leftover_filename_tokens():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for n in range(3):
            _, item = _loc(conn, f"d/song{n}.mp3", layout="artist_title", language="he")
            for f, v, src in (("artist", "זהר ארגוב", "filename"),
                              ("title", f"שיר {n} ישראלי מזרחי", "filename"),
                              ("artist", "זהר ארגוב", "wikidata"),
                              ("title", f"שיר {n}", "wikidata")):
                conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                             "VALUES (?,?,?,?)", (item, f, v, src))
        conn.commit()
        text, tally = lp.decoration_report(conn)
        check(tally.get("subset") == 3, tally)
        check("ישראלי" in text and "מזרחי" in text,
              "the Hebrew genre tag the §6.1 vocabulary is missing must surface")
        check("PROPOSAL, NOT A PATCH" in text,
              "the report must say plainly that it changes no parser")
        conn.close()


def test_decoration_report_marks_tokens_the_parser_already_knows():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "d/x.mp3", layout="artist_title", language="latn")
        for f, v, src in (("artist", "Queen", "filename"),
                          ("title", "Bohemian Rhapsody karaoke", "filename"),
                          ("artist", "Queen", "musicbrainz_text"),
                          ("title", "Bohemian Rhapsody", "musicbrainz_text")):
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                         "VALUES (?,?,?,?)", (item, f, v, src))
        conn.commit()
        text, _ = lp.decoration_report(conn)
        line = [l for l in text.splitlines() if l.strip().endswith("karaoke")]
        check(line and "KNOWN" in line[0], f"already in stage1's vocabulary: {line}")
        conn.close()


def test_decoration_report_ignores_swaps_and_unrelated_pairs():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _, item = _loc(conn, "d/x.mp3", layout="title_artist", language="latn")
        for f, v, src in (("artist", "Bohemian Rhapsody", "filename"),
                          ("title", "Queen", "filename"),
                          ("artist", "Queen", "musicbrainz_text"),
                          ("title", "Bohemian Rhapsody", "musicbrainz_text")):
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                         "VALUES (?,?,?,?)", (item, f, v, src))
        conn.commit()
        text, tally = lp.decoration_report(conn)
        check(tally == {"swap": 1}, tally)
        check("contributing  0 items" in text, "a swap is not a decoration")
        conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
