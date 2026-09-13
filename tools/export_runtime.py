#!/usr/bin/env python3
"""Export the RUNTIME DB from the pipeline index — docs/runtime-db-spec.md §3.

The spec carries the rationale; this module carries the mechanics. In one line: the pipeline
DB stores every parse, fingerprint and API response needed to *derive* an answer, and the
runtime DB stores only the answers — four flat tables the karaoke program queries directly
(§1 "winners, not evidence").

Two mechanical decisions worth stating up front, because both are load-bearing:

READ-ONLY SOURCE. The pipeline DB is opened `mode=ro`. Export must never be able to disturb
the index it reads — not its WAL, not its content — because a long enrichment pass may well be
holding it (see config.KARAOKEMP_DB). The output is a brand-new file, ATTACHed read-write, so
every mapping below is an `INSERT ... SELECT` executed inside SQLite rather than tens of
thousands of Python round trips.

v_songs IS QUERIED EXACTLY ONCE. schema.sql documents it at 55-60 seconds per query on the
live index, and that cost is re-deriving the whole pipeline, not scanning rows. It is folded
into a temp table here and joined against from then on — the pattern stage5.materialize_metadata
established for v_metadata. Do not add a second reference to the view anywhere in this file.

Usage:
    tools/export_runtime.py --out /path/to/karaokemp-runtime.db
    tools/export_runtime.py --db /path/to/library.sqlite3 --out … --force
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from karaokemp import config  # noqa: E402

# --- §2.2 the one normalize() -------------------------------------------------------------

_NORM_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_NORM_WS_RE = re.compile(r"\s+")


def normalize(s: str | None) -> str | None:
    """THE `_norm` function of docs/runtime-db-spec.md §2.2. Every runtime write path that touches
    `songs.artist`/`songs.title` — this export, a name fix, a merge, ingest — must recompute
    the norms through this function and no other. §2.2 exists because the alternative (each
    caller rolling its own) silently splits the search key into dialects.

    lower + trim + collapse internal whitespace + strip punctuation. Hebrew passes through
    unchanged: it has no case, and `\\w` keeps its letters, so a Hebrew name normalizes to
    itself modulo spacing. That is the point — Hebrew stays in Hebrew script end to end (no
    transliteration, anywhere in this project).

    Deliberately NOT the pipeline's matching keys. cluster.norm_key strips diacritics and
    everything outside [a-z0-9 Hebrew]; stage5.norm_name additionally drops articles. Both are
    tuned for *deciding whether two names are the same thing* and are lossy in ways that would
    make a search box surprising ("The Beatles" not matching a typed "the"). This one only
    flattens what a user cannot reasonably be expected to type: case, spacing, punctuation.

    NFKC first so that a composed and a decomposed spelling of the same name — and full-width
    vs ASCII forms — normalize to one key rather than two that merely look identical.
    """
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", s).lower()
    s = _NORM_PUNCT_RE.sub(" ", s)
    return _NORM_WS_RE.sub(" ", s).strip()


# --- §2 schema ----------------------------------------------------------------------------
# Copied from docs/runtime-db-spec.md §2 verbatim in structure. If the two ever disagree, the spec
# wins and this string is the bug. STRICT + WAL + foreign keys, same conventions as schema.sql.
#
# `{db}` is the schema qualifier: the export ATTACHes its output as `rt` and creates the
# tables there, so every name has to be qualified. Anything else that wants a bare runtime DB
# passes db="main" — hence the placeholder rather than a hardcoded prefix.

RUNTIME_SCHEMA = """
CREATE TABLE {db}.songs (
    id          INTEGER PRIMARY KEY,
    artist      TEXT,
    title       TEXT,
    artist_norm TEXT,
    title_norm  TEXT,
    language    TEXT,
    year        INTEGER,
    genre       TEXT,
    song_mbid   TEXT,
    created_at  TEXT
) STRICT;
CREATE INDEX {db}.ix_songs_norm ON songs(artist_norm, title_norm);

CREATE TABLE {db}.versions (
    id              INTEGER PRIMARY KEY,
    song_id         INTEGER NOT NULL REFERENCES songs(id),
    format          TEXT NOT NULL CHECK (format IN ('video','mp3g','audio_only','audio_lrc')),
    rank            INTEGER NOT NULL,
    duration_sec    REAL,
    is_instrumental TEXT CHECK (is_instrumental IS NULL OR is_instrumental IN ('yes','no','unknown')),
    unplayable_at   TEXT,
    unplayable_note TEXT,
    ingested_at     TEXT
) STRICT;
CREATE INDEX {db}.ix_versions_song ON versions(song_id, rank);

CREATE TABLE {db}.files (
    version_id   INTEGER NOT NULL REFERENCES versions(id),
    role         TEXT NOT NULL CHECK (role IN ('av','audio','graphics','lyrics')),
    relpath      TEXT NOT NULL,
    size_bytes   INTEGER,
    content_hash TEXT,
    PRIMARY KEY (version_id, role)
) STRICT;
CREATE INDEX {db}.ix_files_hash ON files(content_hash);

CREATE TABLE {db}.issues (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    song_id     INTEGER REFERENCES songs(id),
    version_id  INTEGER REFERENCES versions(id),
    payload     TEXT,
    resolution  TEXT,
    created_at  TEXT,
    resolved_at TEXT
) STRICT;
CREATE INDEX {db}.ix_issues_open ON issues(kind, resolved_at);
"""


@dataclass
class ExportReport:
    songs: int = 0
    versions: int = 0
    files: int = 0
    issues: int = 0
    songs_dropped_no_version: int = 0
    versions_dropped_integrity: int = 0
    files_remote_fallback: int = 0
    ranks_missing: int = 0          # quality_rank NULL ⇒ deterministic fallback was used
    songs_with_mbid: int = 0
    path_root: str = ""             # the root `files.relpath` is relative TO (§2, "relpath")
    path_mode: str = "library"      # library | drive_id (see PATH_MODES)
    timings: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["timings"] = {k: round(v, 2) for k, v in self.timings.items()}
        return d


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# --- materialization ----------------------------------------------------------------------

PATH_MODES = ("library", "drive_id")
"""What `files.relpath` holds.

`library` — the spec'd mode (§2): a path relative to config.LIBRARY_ROOT, resolved against
whatever root the media is mounted under at the event.

`drive_id` — the DEMO mode (sha-yol, 2026-08-27). `relpath` holds the Google Drive **file id**
instead of a path, so the DustMic player can stream straight from the shared folder before the
~432 GB library has physically changed hands. Ids, not paths, for one reason that matters:
**a Drive file id is immutable across moves and renames.** The owner is going to restructure
that folder (the move script in this repo does exactly that), and every path-keyed reference
would break the moment they run it — while an id-keyed one keeps working before, during and
after. The same property covers our own churn: Organize renames the local tree without
touching a single id.

The schema is IDENTICAL in both modes on purpose. The player is written once, against the
real four tables; only the step that turns a `files` row into a byte stream differs
(`files.get(fileId, alt='media')` vs `open(root / relpath)`). Nothing built on the demo is
throwaway.

Reading it needs read access to the shared folder, which is owned by a third party —
the owner has to grant it; it is not ours to give.
"""


def _materialize(conn: sqlite3.Connection, report: ExportReport,
                 path_mode: str = "library") -> None:
    """Fold every expensive derivation into temp tables, once.

    Order matters only in that rt_songs (the v_songs fold) is the one slow step; everything
    else is sub-second and joins against it.
    """
    t = time.monotonic()

    # (1) Per-item winning metadata. Same shape as stage5.materialize_metadata, widened to the
    # fields the runtime carries. v_metadata already resolved the trust ladder; the pivot just
    # turns its long form into one row per item.
    conn.execute("DROP TABLE IF EXISTS temp.rt_meta")
    conn.execute(
        """
        CREATE TEMP TABLE rt_meta AS
        SELECT v.media_item_id AS item_id,
               MAX(CASE WHEN v.field='artist'    THEN v.value END) AS artist,
               MAX(CASE WHEN v.field='title'     THEN v.value END) AS title,
               MAX(CASE WHEN v.field='language'  THEN v.value END) AS language,
               MAX(CASE WHEN v.field='year'      THEN v.value END) AS year,
               MAX(CASE WHEN v.field='genre'     THEN v.value END) AS genre,
               MAX(CASE WHEN v.field='song_mbid' THEN v.value END) AS song_mbid
        FROM v_metadata v
        GROUP BY v.media_item_id
        """
    )
    conn.execute("CREATE INDEX temp.ix_rt_meta ON rt_meta(item_id)")
    report.timings["meta"] = time.monotonic() - t

    # (2) content_hash → a concrete path. INTERIM MODE (spec §3, "Path source"): the spec'd
    # final state is post-Organize `active/`-relative paths, but Organize has not run, so a
    # path today is whatever file_locations recorded when the blob was staged — an ABSOLUTE
    # path under config.STAGING_DIR.
    #
    # It is stored RELATIVE to config.LIBRARY_ROOT, because §2 defines `files.relpath` as
    # "relative to the library root" and an absolute path is not portable: it hard-codes one
    # machine's home directory into a DB meant to be copied to the event laptop. (An earlier
    # revision stored these as-is, reasoning that there was no honest root to make them
    # relative to. That was wrong: the library root is exactly that root.)
    #
    # LIBRARY_ROOT and not STAGING_DIR: Organize (stage 6) moves winners staging/ -> active/,
    # so a path is under staging/ today and under active/ tomorrow. Stripping the staging
    # prefix would make every post-Organize path fail the guard below; stripping the library
    # root yields `staging/…` now and `active/…` after, and keeps working across the move.
    #
    # The root is NOT stored in the runtime DB. Four tables is the whole schema (§1.5), and
    # the consumer supplies the root it resolves paths beneath — so the same export works
    # unchanged wherever the media is mounted. main() prints the root it stripped.
    #
    # A re-export after Organize rewrites these to active/-relative; export is repeatable
    # until cutover (§4), so that is a re-run, not a migration.
    #
    # MIN(id) picks "any location with a non-null local_path" deterministically — several
    # locations legitimately share one blob (that is what blobs are FOR), and they are
    # interchangeable by definition since they are the same bytes.
    #
    # The remote_path fallback exists so a blob that was never downloaded still produces a
    # files row rather than silently dropping a version: 'remote:' marks it unplayable-by-path
    # for a human reading the row, and the relink/fsck tool (§7) is what repairs it.
    # substr() rather than LIKE: a filesystem path may legitimately contain `_` or `%`, both
    # of which are LIKE wildcards, and an over-match here would strip the wrong prefix length.
    library_root = str(config.LIBRARY_ROOT).rstrip("/") + "/"
    report.path_mode = path_mode

    conn.execute("DROP TABLE IF EXISTS temp.rt_path")
    if path_mode not in PATH_MODES:
        raise ValueError(f"path_mode must be one of {PATH_MODES}, got {path_mode!r}")

    if path_mode == "drive_id":
        # `relpath` holds a Drive file id (see PATH_MODES). No root, no fallback chain: a blob
        # with no drive_file_id anywhere cannot be streamed from Drive at all, so it yields
        # NULL here and _write_files fails loudly rather than shipping an unplayable row.
        # Hardlink rows created by Organize carry drive_file_id NULL by construction, hence
        # the IS NOT NULL filter — MIN(id) alone would sometimes pick one of those.
        report.path_root = f"gdrive:{config.DRIVE_ROOT_FOLDER_ID}"
        conn.execute(
            """
            CREATE TEMP TABLE rt_path AS
            SELECT content_hash,
                   (SELECT l.drive_file_id FROM file_locations l
                     WHERE l.content_hash = g.content_hash AND l.drive_file_id IS NOT NULL
                     ORDER BY l.id LIMIT 1) AS relpath,
                   1 AS is_local
            FROM (SELECT DISTINCT content_hash FROM file_locations
                   WHERE content_hash IS NOT NULL) g
            """
        )
    else:
        report.path_root = library_root
        conn.execute(
            """
            CREATE TEMP TABLE rt_path AS
            SELECT content_hash,
                   COALESCE(
                       (SELECT CASE
                                 WHEN substr(l.local_path, 1, length(:root)) = :root
                                     THEN substr(l.local_path, length(:root) + 1)
                                 ELSE l.local_path    -- escapes the root; _write_files rejects it
                               END
                          FROM file_locations l
                         WHERE l.content_hash = g.content_hash AND l.local_path IS NOT NULL
                         ORDER BY l.id LIMIT 1),
                       'remote:' || (SELECT l.remote_path FROM file_locations l
                                      WHERE l.content_hash = g.content_hash
                                        AND l.remote_path IS NOT NULL
                                      ORDER BY l.id LIMIT 1)
                   ) AS relpath,
                   EXISTS (SELECT 1 FROM file_locations l
                            WHERE l.content_hash = g.content_hash
                              AND l.local_path IS NOT NULL) AS is_local
            FROM (SELECT DISTINCT content_hash FROM file_locations
                   WHERE content_hash IS NOT NULL) g
            """,
            {"root": library_root},
        )

    conn.execute("CREATE UNIQUE INDEX temp.ix_rt_path ON rt_path(content_hash)")
    report.timings["paths"] = time.monotonic() - t - report.timings["meta"]

    # (3) THE expensive one. See the module docstring: 55-60s, queried exactly once.
    # v_songs already decides the representative name per song by summed metadata trust,
    # tie-broken by the §7.4 rank — replicating that here would be a second implementation of
    # a rule that changes whenever clustering does.
    t3 = time.monotonic()
    conn.execute("DROP TABLE IF EXISTS temp.rt_songs")
    conn.execute(
        """
        CREATE TEMP TABLE rt_songs AS
        SELECT song_id, artist, title, language, song_mbid,
               representative_item_id, distinct_names
        FROM v_songs
        """
    )
    conn.execute("CREATE UNIQUE INDEX temp.ix_rt_songs ON rt_songs(song_id)")
    report.timings["v_songs"] = time.monotonic() - t3

    # (4) The versions that survive the §3 integrity filter. "Known-broken files do not ship";
    # there are no integrity columns at runtime, so everything present is presumed playable
    # until a DJ marks it otherwise (§5.4).
    #
    # The filter reads the AUDIO/AV blob only. A video item has one blob and the question is
    # simple; an mp3g item's graphics blob is checked separately and a bad .cdg costs lyrics,
    # not playability — mirroring v_songs.has_playable, which asks the same question the same
    # way.
    #
    # fallback_rank: quality_rank should be non-NULL after any `verdicts` run (it is a TOTAL
    # order per §7.4, and is 100% populated on the live index). If some future state leaves it
    # NULL, COALESCE lands on this row_number, which orders NULLs last — so a NULL item's
    # number is (count of ranked peers) + k, i.e. strictly above every real rank in its group,
    # and cannot collide with one. report.ranks_missing makes the substitution visible.
    t4 = time.monotonic()
    conn.execute("DROP TABLE IF EXISTS temp.rt_versions")
    conn.execute(
        """
        CREATE TEMP TABLE rt_versions AS
        SELECT i.id                AS id,
               i.cluster_id        AS song_id,
               i.format            AS format,
               i.quality_rank      AS quality_rank,
               ROW_NUMBER() OVER (PARTITION BY i.cluster_id, i.format
                                  ORDER BY COALESCE(i.quality_rank, 1000000), i.id)
                                   AS fallback_rank,
               i.duration_sec      AS duration_sec,
               i.is_instrumental   AS is_instrumental
        FROM media_items i
        JOIN media_item_files f ON f.media_item_id = i.id AND f.role IN ('audio','av')
        JOIN blobs b            ON b.content_hash = f.content_hash
        WHERE i.status = 'active'
          AND i.cluster_id IS NOT NULL
          AND b.integrity_status IN ('probed_ok','decoded_ok')
        """
    )
    conn.execute("CREATE UNIQUE INDEX temp.ix_rt_versions ON rt_versions(id)")
    conn.execute("CREATE INDEX temp.ix_rt_versions_song ON rt_versions(song_id)")
    report.timings["versions"] = time.monotonic() - t4


# --- the export ---------------------------------------------------------------------------

def _write_songs(conn: sqlite3.Connection, report: ExportReport, now: str) -> None:
    """songs ← v_songs (id ← cluster_id) + v_metadata winners for year/genre.

    Only songs with at least one surviving version (§3). The representative artist/title stay
    exactly what v_songs chose, even in the rare case where the chosen representative item is
    itself integrity-filtered out: the name is the best-attested name this song has, and a
    broken copy's filename is no less true for the copy being broken. Dropping the song
    entirely is the filter's job, and it only fires when NO copy survives.
    """
    conn.execute(
        """
        INSERT INTO rt.songs (id, artist, title, artist_norm, title_norm,
                              language, year, genre, song_mbid, created_at)
        SELECT s.song_id,
               s.artist,
               s.title,
               rt_norm(s.artist),
               rt_norm(s.title),
               s.language,
               -- song_metadata.year is TEXT (a value with provenance, not a number); the
               -- runtime column is INTEGER for future facets. Guard the cast so a
               -- non-numeric value becomes NULL rather than 0.
               CASE WHEN m.year GLOB '[0-9][0-9][0-9][0-9]' THEN CAST(m.year AS INTEGER) END,
               m.genre,          -- JSON array, carried through verbatim
               s.song_mbid,
               ?
        FROM rt_songs s
        LEFT JOIN rt_meta m ON m.item_id = s.representative_item_id
        WHERE EXISTS (SELECT 1 FROM rt_versions v WHERE v.song_id = s.song_id)
        """,
        (now,),
    )
    report.songs = conn.execute("SELECT COUNT(*) FROM rt.songs").fetchone()[0]
    report.songs_dropped_no_version = (
        conn.execute("SELECT COUNT(*) FROM rt_songs").fetchone()[0] - report.songs
    )
    report.songs_with_mbid = conn.execute(
        "SELECT COUNT(*) FROM rt.songs WHERE song_mbid IS NOT NULL").fetchone()[0]


def _write_versions(conn: sqlite3.Connection, report: ExportReport) -> None:
    """versions ← media_items WHERE status='active', quality_rank → rank.

    unplayable_at / unplayable_note / ingested_at are all NULL by construction: nothing is
    marked unplayable until a DJ says so at the event (§5.4), and everything in this export is
    original library material rather than ingest (§5.5).

    RANK IS COPIED VERBATIM, AND IT TIES ACROSS FORMATS. §7.4 ranks within (cluster, FORMAT)
    because formats are peers, so a song holding both a video and an mp3g copy exports TWO
    rank-1 versions. Measured on the 2026-08-09 export: 529 of 21,533 songs (2.5%). §2's own
    column comment anticipates this ("1 = default copy per (song, arbitrary format)"), so the
    verbatim mapping is the specified one and is what this does — but it does mean §5.3's
    `ORDER BY rank LIMIT 1` picks between formats arbitrarily rather than by
    config.PRIMARY_FORMAT_PREFERENCE ('video'). If the player is ever to prefer video, that is
    a decision to make HERE (a format term folded into rank at export) rather than in the
    player, since rank freezes at cutover. Flagged, deliberately not decided unilaterally.
    """
    report.ranks_missing = conn.execute(
        "SELECT COUNT(*) FROM rt_versions WHERE quality_rank IS NULL").fetchone()[0]
    conn.execute(
        """
        INSERT INTO rt.versions (id, song_id, format, rank, duration_sec, is_instrumental,
                                 unplayable_at, unplayable_note, ingested_at)
        SELECT v.id, v.song_id, v.format,
               COALESCE(v.quality_rank, v.fallback_rank),
               v.duration_sec, v.is_instrumental,
               NULL, NULL, NULL
        FROM rt_versions v
        JOIN rt.songs s ON s.id = v.song_id
        """
    )
    report.versions = conn.execute("SELECT COUNT(*) FROM rt.versions").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM media_items WHERE status='active'").fetchone()[0]
    report.versions_dropped_integrity = active - report.versions


def _write_files(conn: sqlite3.Connection, report: ExportReport) -> None:
    """files ← media_item_files joined through blobs to a concrete path (§3).

    Every role of an exported version ships, not just the audio/av one the integrity filter
    looked at: an mp3g version is only playable as audio+graphics together (§5.3).
    """
    # In drive_id mode this MUST run before the INSERT: `files.relpath` is NOT NULL, so a
    # missing id would surface as a bare IntegrityError naming a column, with nothing to say
    # WHICH content could not be resolved. Check first and name the rows.
    if report.path_mode == "drive_id":
        missing = conn.execute(
            """
            SELECT f.media_item_id, f.role, f.content_hash
            FROM media_item_files f
            JOIN rt.versions v ON v.id = f.media_item_id
            JOIN rt_path p     ON p.content_hash = f.content_hash
            WHERE f.role IN ('av','audio','graphics','lyrics') AND p.relpath IS NULL
            """
        ).fetchall()
        if missing:
            raise RuntimeError(
                f"{len(missing)} files rows have no Drive file id — that content is not in "
                f"the shared folder, so it cannot be streamed. Samples "
                f"(version_id, role, hash): {[tuple(r) for r in missing[:5]]}"
            )

    conn.execute(
        """
        INSERT INTO rt.files (version_id, role, relpath, size_bytes, content_hash)
        SELECT f.media_item_id, f.role, p.relpath, b.size_bytes, b.content_hash
        FROM media_item_files f
        JOIN rt.versions v ON v.id = f.media_item_id
        JOIN blobs b       ON b.content_hash = f.content_hash
        JOIN rt_path p     ON p.content_hash = f.content_hash
        WHERE f.role IN ('av','audio','graphics','lyrics')   -- 'container' is not a runtime role
        """
    )
    report.files = conn.execute("SELECT COUNT(*) FROM rt.files").fetchone()[0]
    report.files_remote_fallback = conn.execute(
        "SELECT COUNT(*) FROM rt.files WHERE relpath LIKE 'remote:%'").fetchone()[0]

    # Both modes fail loudly rather than shipping a row that cannot resolve to bytes. What
    # counts as unresolvable differs, so the check does too.
    if report.path_mode == "drive_id":
        return          # ids are not paths; the pre-insert check above is this mode's guard

    # §2: `relpath` is relative, full stop. Anything still absolute here escaped the library
    # root, which means the export cannot express it portably — fail the run rather than ship
    # a DB that is silently unusable on any machine but this one. Loud beats subtle: a stray
    # absolute path would otherwise surface as a file-not-found at the event.
    stray = conn.execute(
        "SELECT relpath FROM rt.files WHERE relpath LIKE '/%' LIMIT 5").fetchall()
    if stray:
        n = conn.execute(
            "SELECT COUNT(*) FROM rt.files WHERE relpath LIKE '/%'").fetchone()[0]
        raise RuntimeError(
            f"{n} files rows hold an absolute path — they are not under the library root "
            f"{report.path_root!r}, so this export cannot make them relative (§2). "
            f"Samples: {[r[0] for r in stray]}"
        )


def _write_issues(conn: sqlite3.Connection, report: ExportReport, now: str) -> None:
    """issues ← the one export-time flag worth carrying: distinct_names > 1 (§3, Appendix A).

    The pipeline `review_queue` itself does NOT migrate — a pipeline resolution is a durable
    fact that re-runs re-apply, a runtime issue is a to-do item (§6). What does carry is
    v_songs' honesty column: the §7.3(b) name key is order-insensitive and containment-aware
    on purpose, so one song can legitimately hold several raw name strings, and >1 means "look
    before trusting the displayed name".

    The payload lists the distinct crude names, recomputed here with v_songs' own key
    (lower+trim, 'artist | title') so the list and the count cannot disagree. Names are
    collected over ALL active items of the cluster, matching v_songs; an issue is only FILED
    for songs that survived the export.
    """
    conn.execute(
        """
        INSERT INTO rt.issues (kind, song_id, version_id, payload,
                               resolution, created_at, resolved_at)
        SELECT 'duplicate_suspect', n.song_id, NULL,
               json_object('distinct_names', json_group_array(n.crude_name),
                           'source', 'export: v_songs.distinct_names > 1'),
               NULL, ?, NULL
        FROM (
            SELECT DISTINCT i.cluster_id AS song_id,
                   lower(trim(COALESCE(m.artist,''))) || ' | '
                                 || lower(trim(COALESCE(m.title,''))) AS crude_name
            FROM media_items i
            JOIN rt_meta m ON m.item_id = i.id
            JOIN rt_songs s ON s.song_id = i.cluster_id
            WHERE i.status = 'active'
              AND s.distinct_names > 1
              AND (m.artist IS NOT NULL OR m.title IS NOT NULL)
              AND EXISTS (SELECT 1 FROM rt.songs r WHERE r.id = i.cluster_id)
            ORDER BY 1, 2
        ) n
        GROUP BY n.song_id
        """,
        (now,),
    )
    report.issues = conn.execute("SELECT COUNT(*) FROM rt.issues").fetchone()[0]


def export(db_path: Path, out_path: Path, *, force: bool = False,
           path_mode: str = "library") -> ExportReport:
    """Read `db_path` (read-only), write a fresh runtime DB at `out_path`."""
    db_path = Path(db_path)
    out_path = Path(out_path)
    if not db_path.exists():
        raise SystemExit(f"pipeline DB not found: {db_path}")
    if out_path.exists():
        if not force:
            raise SystemExit(f"{out_path} exists (use --force to overwrite)")
        # A fresh file, never an incremental update: §4 makes export cheap and repeatable, so
        # "regenerate" is always the right verb and merging into a previous export never is.
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(out_path) + suffix)
            if p.exists():
                p.unlink()

    report = ExportReport()
    started = time.monotonic()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        conn.create_function("rt_norm", 1, normalize, deterministic=True)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("ATTACH DATABASE ? AS rt", (str(out_path),))
        conn.execute("PRAGMA rt.journal_mode = WAL")
        conn.executescript(RUNTIME_SCHEMA.format(db="rt"))

        _materialize(conn, report, path_mode=path_mode)

        t = time.monotonic()
        now = _utcnow()
        with conn:
            _write_songs(conn, report, now)
            _write_versions(conn, report)
            _write_files(conn, report)
            _write_issues(conn, report, now)
        report.timings["write"] = time.monotonic() - t

        # ANALYZE so the search query in §5.1 gets the ix_songs_norm plan without a first-run
        # penalty; the runtime DB is read-mostly, so the stats never go stale.
        conn.execute("ANALYZE rt")
        conn.execute("PRAGMA rt.wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    report.timings["total"] = time.monotonic() - started
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=None,
                    help="pipeline DB (default: config.DB_PATH / $KARAOKEMP_DB)")
    ap.add_argument("--out", type=Path, required=True, help="runtime DB to write")
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out")
    ap.add_argument(
        "--path-mode", choices=PATH_MODES, default="library",
        help="what files.relpath holds: 'library' = path relative to the library root (the "
             "spec'd mode, §2); 'drive_id' = the Google Drive file id, for streaming straight "
             "from the shared folder before the media has changed hands (see PATH_MODES)")
    args = ap.parse_args()

    db_path = args.db or config.DB_PATH
    print(f"pipeline DB : {db_path} (read-only)")
    print(f"runtime DB  : {args.out}")
    print(f"path mode   : {args.path_mode}")
    report = export(db_path, args.out, force=args.force, path_mode=args.path_mode)

    size_mb = os.path.getsize(args.out) / 1e6
    print(f"\nwrote {args.out}  ({size_mb:.1f} MB)")
    if report.path_mode == "drive_id":
        print(f"  relpath holds : Google Drive FILE IDs (folder {report.path_root})")
    else:
        print(f"  relpath root (NOT stored; supply at read time) : {report.path_root}")
    for name in ("songs", "versions", "files", "issues"):
        print(f"  {name:9s} {getattr(report, name):7d} rows")
    print(f"  songs dropped (no surviving version) : {report.songs_dropped_no_version}")
    print(f"  active items dropped (integrity)     : {report.versions_dropped_integrity}")
    print(f"  files rows on 'remote:' fallback     : {report.files_remote_fallback}")
    print(f"  songs carrying a song_mbid           : {report.songs_with_mbid}")
    if report.ranks_missing:
        print(f"  !! quality_rank was NULL for {report.ranks_missing} versions — "
              f"deterministic fallback rank used")
    print("  timings (s): " + ", ".join(
        f"{k}={v:.1f}" for k, v in report.timings.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
