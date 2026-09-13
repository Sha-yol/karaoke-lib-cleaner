"""Runtime-DB export tests — docs/runtime-db-spec.md §2/§3. Pinned:

  * the OUTPUT SHAPE is exactly the spec's four tables, all STRICT, with the spec's indexes
    and nothing else — no views, no pipeline vocabulary, no derivation machinery (§1.4);
  * the INTEGRITY FILTER is the export's only quality gate: broken/suspect/unchecked audio
    never ships, and it is the AUDIO/AV blob that decides — a bad .cdg costs lyrics, not
    playability;
  * a song whose every copy failed that filter is DROPPED, not exported empty (§3);
  * `normalize()` is the single §2.2 implementation: case, punctuation and whitespace are
    flattened and HEBREW PASSES THROUGH — this project never transliterates;
  * INTERIM path mode: relpath comes from file_locations.local_path, RELATIVE to the LIBRARY
    root (never a machine-local absolute — §2) so that it survives Organize moving staging/ ->
    active/, taken deterministically when several locations share a blob, and falls back to
    'remote:'+remote_path rather than dropping a row for a blob that was never downloaded; a
    path that escapes the library root fails the export loudly;
  * `duplicate_suspect` is seeded from v_songs' distinct_names > 1 with the actual names in
    the payload — and only for songs that survived the export;
  * quality_rank maps to rank verbatim, and a NULL one (a state `verdicts` does not produce)
    falls back to a deterministic order that cannot collide with a real rank;
  * re-export is regeneration, not merge: --force yields the same DB, never doubled rows.

Fixtures build a small pipeline DB from schema.sql, the same way the stage tests do.

Run: python3 tests/test_export_runtime.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from karaokemp import config, db  # noqa: E402

import export_runtime as ex  # noqa: E402


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
    conn.executescript((Path(__file__).resolve().parent.parent / "schema.sql").read_text())
    return conn


def _cluster(conn):
    return conn.execute("INSERT INTO clusters (method) VALUES ('title_match')").lastrowid


def _blob(conn, sha, integrity="probed_ok", *, size=1000, local=True, remote_only=False):
    """One blob plus a file_location for it. `local=False` exercises the 'remote:' fallback —
    a hashed blob that has no downloaded copy."""
    conn.execute(
        "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
        "VALUES (?,?,?)", (sha, size, integrity))
    if not remote_only:
        conn.execute(
            "INSERT INTO file_locations (drive_file_id, remote_path, local_path, filetype, "
            "content_hash, status) VALUES (?,?,?,?,?, 'staged')",
            (f"drv-{sha}", f"drive/{sha}.mp3",
             f"{config.STAGING_DIR}/{sha}.mp3" if local else None, "mp3", sha))
    return sha


def _item(conn, *, cluster, fmt="mp3g", artist=None, title=None, integrity="probed_ok",
          rank=1, dur=200.0, sha=None, graphics_integrity=None, language=None,
          year=None, genre=None, mbid=None, source="filename", status="active",
          local=True):
    """One active media item with its blob(s) and metadata. mp3g gets audio+graphics."""
    _seq[0] += 1
    n = _seq[0]
    sha = sha or f"sha{n:04d}"
    _blob(conn, sha, integrity, local=local)
    item_id = conn.execute(
        "INSERT INTO media_items (format, cluster_id, duration_sec, quality_rank, "
        "quality_verdict, status) VALUES (?,?,?,?, 'winner', ?)",
        (fmt, cluster, dur, rank, status)).lastrowid
    role = "av" if fmt == "video" else "audio"
    conn.execute("INSERT INTO media_item_files (media_item_id, content_hash, role) "
                 "VALUES (?,?,?)", (item_id, sha, role))
    if fmt == "mp3g":
        gsha = f"cdg{n:04d}"
        _blob(conn, gsha, graphics_integrity or "probed_ok", size=500)
        conn.execute("INSERT INTO media_item_files (media_item_id, content_hash, role) "
                     "VALUES (?,?, 'graphics')", (item_id, gsha))
    for fld, val in (("artist", artist), ("title", title), ("language", language),
                     ("year", year), ("genre", genre), ("song_mbid", mbid)):
        if val is not None:
            conn.execute(
                "INSERT INTO song_metadata (media_item_id, field, value, source, confidence) "
                "VALUES (?,?,?,?, 0.9)", (item_id, fld, str(val), source))
    conn.commit()
    return item_id


def _run(conn, tmp: Path, **kw):
    """Close the writer, export, and hand back a connection to the runtime DB."""
    conn.commit()
    conn.close()
    out = tmp / "runtime.db"
    report = ex.export(config.DB_PATH, out, **kw)
    rt = sqlite3.connect(out)
    rt.row_factory = sqlite3.Row
    return rt, report, out


# --- §2 schema shape -----------------------------------------------------------------------

def test_schema_is_exactly_the_four_spec_tables():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), artist="Queen", title="Bohemian Rhapsody")
        rt, _, _ = _run(conn, tmp)
        tables = [r[0] for r in rt.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        check(tables == ["files", "issues", "songs", "versions"],
              f"§2 is four tables and only four: {tables}")
        check(not rt.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall(),
              "§1.4: no derivation machinery at runtime — no views")
        strict = [r[0] for r in rt.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%STRICT%'")]
        check(sorted(strict) == ["files", "issues", "songs", "versions"], f"all STRICT: {strict}")
        idx = sorted(r[0] for r in rt.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'"))
        check(idx == ["ix_files_hash", "ix_issues_open", "ix_songs_norm", "ix_versions_song"],
              f"the §2 indexes: {idx}")
        check(rt.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "WAL per §2")
        cols = {r["name"] for r in rt.execute("PRAGMA table_info(songs)")}
        check(cols == {"id", "artist", "title", "artist_norm", "title_norm", "language",
                       "year", "genre", "song_mbid", "created_at"}, cols)


def test_ids_are_frozen_from_the_pipeline():
    """§2 principle 2: songs.id IS the cluster_id and versions.id IS the media_items.id —
    that is what makes archaeology against the archived pipeline DB possible (§4)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        c = _cluster(conn)
        item = _item(conn, cluster=c, artist="Queen", title="Bohemian Rhapsody",
                     year=1975, genre='["rock"]', mbid="mbid-1", language="en")
        rt, rep, _ = _run(conn, tmp)
        row = rt.execute("SELECT * FROM songs").fetchone()
        check(row["id"] == c, f"songs.id must be the cluster_id: {row['id']} != {c}")
        check((row["artist"], row["title"]) == ("Queen", "Bohemian Rhapsody"))
        check(row["year"] == 1975 and isinstance(row["year"], int), "year is INTEGER")
        check(row["genre"] == '["rock"]' and row["song_mbid"] == "mbid-1")
        check(row["language"] == "en" and row["created_at"])
        v = rt.execute("SELECT * FROM versions").fetchone()
        check(v["id"] == item, "versions.id must be the media_items.id")
        check(v["song_id"] == c and v["rank"] == 1 and v["format"] == "mp3g")
        check(v["unplayable_at"] is None and v["ingested_at"] is None,
              "original library material is playable and not ingested")
        check(rep.songs == 1 and rep.versions == 1 and rep.songs_with_mbid == 1)


# --- §2.2 normalize ------------------------------------------------------------------------

def test_normalize_case_punctuation_whitespace():
    check(ex.normalize("  The   BEATLES  ") == "the beatles", "lower + trim + collapse")
    check(ex.normalize("Guns N' Roses") == "guns n roses", "punctuation becomes a separator")
    check(ex.normalize("AC/DC") == "ac dc")
    check(ex.normalize("Don't Stop Me Now!!!") == "don t stop me now")
    check(ex.normalize("") == "" and ex.normalize(None) is None,
          "None in, None out — a missing name is not an empty one")


def test_normalize_hebrew_passes_through():
    """Hebrew has no case and must never be transliterated (project-wide rule). It comes out
    the other side as itself, modulo the same spacing/punctuation flattening."""
    heb = "שלמה ארצי"
    check(ex.normalize(heb) == heb, f"Hebrew unchanged: {ex.normalize(heb)!r}")
    check(ex.normalize("  אריק  איינשטיין  ") == "אריק איינשטיין", "spacing only")
    check(ex.normalize("עוד יום - יבוא") == "עוד יום יבוא", "punctuation still stripped")
    mixed = ex.normalize("Idan Raichel / עידן רייכל")
    check(mixed == "idan raichel עידן רייכל", f"mixed-script row: {mixed!r}")


def test_norms_are_written_from_that_function():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), artist="Guns N' Roses", title="Sweet Child O' Mine")
        _item(conn, cluster=_cluster(conn), artist="שלמה ארצי", title="ירח")
        rt, _, _ = _run(conn, tmp)
        rows = {r["artist"]: (r["artist_norm"], r["title_norm"]) for r in
                rt.execute("SELECT artist, artist_norm, title_norm FROM songs")}
        check(rows["Guns N' Roses"] == ("guns n roses", "sweet child o mine"), rows)
        check(rows["שלמה ארצי"] == ("שלמה ארצי", "ירח"), rows)
        # §5.1's actual search shape has to find the Hebrew row by a Hebrew substring.
        hit = rt.execute("SELECT id FROM songs WHERE artist_norm LIKE ?",
                         ("%" + ex.normalize("ארצי") + "%",)).fetchall()
        check(len(hit) == 1, "Hebrew substring search must hit")


# --- §3 export filters ---------------------------------------------------------------------

def test_integrity_filter_drops_broken_audio():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        keep, drop = [], []
        for status in ("probed_ok", "decoded_ok"):
            keep.append(_item(conn, cluster=_cluster(conn), title=f"ok {status}",
                              integrity=status))
        for status in ("broken", "suspect", "unchecked"):
            drop.append(_item(conn, cluster=_cluster(conn), title=f"bad {status}",
                              integrity=status))
        rt, rep, _ = _run(conn, tmp)
        got = {r[0] for r in rt.execute("SELECT id FROM versions")}
        check(got == set(keep), f"only probed_ok/decoded_ok ship: {got} vs {keep}")
        check(rep.versions == 2 and rep.versions_dropped_integrity == 3, rep.as_dict())


def test_broken_graphics_does_not_drop_the_version():
    """The filter reads the AUDIO/AV blob. A damaged .cdg costs lyrics, not playability —
    the same question v_songs.has_playable asks, asked the same way."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item = _item(conn, cluster=_cluster(conn), title="ok audio bad graphics",
                     integrity="probed_ok", graphics_integrity="broken")
        rt, _, _ = _run(conn, tmp)
        check([r[0] for r in rt.execute("SELECT id FROM versions")] == [item])
        roles = sorted(r[0] for r in rt.execute("SELECT role FROM files WHERE version_id=?",
                                                (item,)))
        check(roles == ["audio", "graphics"], f"both halves of an mp3g still ship: {roles}")


def test_song_with_no_surviving_version_is_dropped():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        good = _cluster(conn)
        dead = _cluster(conn)
        _item(conn, cluster=good, artist="Live", title="Song", integrity="probed_ok")
        _item(conn, cluster=dead, artist="Dead", title="Song", integrity="broken")
        _item(conn, cluster=dead, artist="Dead", title="Song", integrity="suspect")
        rt, rep, _ = _run(conn, tmp)
        ids = [r[0] for r in rt.execute("SELECT id FROM songs")]
        check(ids == [good], f"a song with zero playable copies must not ship: {ids}")
        check(rep.songs_dropped_no_version == 1, rep.as_dict())
        check(rt.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2,
              "and its files go with it (audio+graphics of the survivor only)")


def test_archived_items_never_ship():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        c = _cluster(conn)
        live = _item(conn, cluster=c, title="Song", rank=1)
        _item(conn, cluster=c, title="Song", rank=2, status="archived")
        rt, _, _ = _run(conn, tmp)
        check([r[0] for r in rt.execute("SELECT id FROM versions")] == [live])


# --- §3 interim path mode ------------------------------------------------------------------

def test_relpath_comes_from_local_path_and_is_deterministic():
    """INTERIM MODE: Organize has not run, so relpath is whatever file_locations recorded —
    made RELATIVE to the LIBRARY root (§2: relpath is relative, never a machine-local absolute).
    Library root and not staging root: Organize moves winners staging/ -> active/, and the same
    stripping has to survive that move.
    Several locations legitimately share one blob; the pick must be stable across re-exports."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item = _item(conn, cluster=_cluster(conn), fmt="video", title="Song", sha="shaZZ")
        # A second, later location for the same bytes — that is what blobs are for.
        conn.execute(
            "INSERT INTO file_locations (drive_file_id, remote_path, local_path, filetype, "
            "content_hash, status) VALUES ('drv-2','drive/other.mp4',?,'mp4','shaZZ','staged')",
            (f"{config.STAGING_DIR}/other.mp4",))
        rt, rep, out = _run(conn, tmp)
        row = rt.execute("SELECT * FROM files").fetchone()
        check(row["relpath"] == "staging/shaZZ.mp3",
              f"lowest location id wins, stably, and relative: {row['relpath']}")
        check(not row["relpath"].startswith("/"), "never absolute (§2)")
        check(row["role"] == "av" and row["version_id"] == item)
        check(row["content_hash"] == "shaZZ" and row["size_bytes"] == 1000,
              "size/hash come from blobs — the relink prefilter of §7")
        check(rep.files_remote_fallback == 0)


def test_blob_with_no_local_copy_falls_back_to_remote():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), fmt="video", title="Never downloaded",
              sha="shaRR", local=False)
        rt, rep, _ = _run(conn, tmp)
        row = rt.execute("SELECT relpath FROM files").fetchone()
        check(row["relpath"] == "remote:drive/shaRR.mp3",
              f"the row still ships, marked: {row['relpath']}")
        check(rep.files_remote_fallback == 1, rep.as_dict())
        check(not row["relpath"].startswith("/"), "the marker is not an absolute path either")


def test_path_escaping_the_library_root_fails_the_export():
    """§2 is absolute about relpath being relative. A staged path somewhere else cannot be
    expressed portably, and shipping it would surface as a file-not-found at the event — so
    the export must die here rather than write a DB that only works on this one machine."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), fmt="video", title="Elsewhere", sha="shaXX")
        conn.execute("UPDATE file_locations SET local_path = '/somewhere/else/shaXX.mp4' "
                     "WHERE content_hash = 'shaXX'")
        try:
            _run(conn, tmp)
        except RuntimeError as e:
            check("absolute path" in str(e), f"names the problem: {e}")
            check("/somewhere/else/shaXX.mp4" in str(e), f"shows the offender: {e}")
        else:
            raise AssertionError("an absolute path escaped the export silently")


def test_relpath_survives_organize_moving_staging_to_active():
    """The regression that picked LIBRARY_ROOT over STAGING_DIR. Organize (stage 6) moves
    winners staging/ -> active/<Artist> - <Title> [<id>]/. Stripping the staging prefix would
    leave every post-Organize path absolute and hard-fail the export; stripping the library
    root keeps working across the move."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), fmt="video", title="Moved", sha="shaMV")
        conn.execute("UPDATE file_locations SET local_path = ? WHERE content_hash = 'shaMV'",
                     (f"{config.ACTIVE_DIR}/Queen - Bohemian Rhapsody [1]/shaMV.mp4",))
        rt, _, _ = _run(conn, tmp)
        row = rt.execute("SELECT relpath FROM files").fetchone()
        check(row["relpath"] == "active/Queen - Bohemian Rhapsody [1]/shaMV.mp4",
              f"post-Organize path is relative to the library root: {row['relpath']}")


# --- §3 / Appendix A duplicate_suspect -----------------------------------------------------

def test_duplicate_suspect_seeded_from_distinct_names():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        mixed = _cluster(conn)
        clean = _cluster(conn)
        _item(conn, cluster=mixed, artist="Pink", title="Just Give Me A Reason")
        _item(conn, cluster=mixed, artist="Pink & Nate Ruess", title="Just Give Me A Reason")
        _item(conn, cluster=clean, artist="Queen", title="Somebody To Love")
        _item(conn, cluster=clean, artist="Queen", title="Somebody To Love", rank=2)
        rt, rep, _ = _run(conn, tmp)
        rows = rt.execute("SELECT * FROM issues").fetchall()
        check(len(rows) == 1 and rep.issues == 1,
              f"one row, only for the cluster with >1 crude name: {[dict(r) for r in rows]}")
        r = rows[0]
        check(r["kind"] == "duplicate_suspect" and r["song_id"] == mixed)
        check(r["resolution"] is None and r["resolved_at"] is None, "seeded OPEN (§6)")
        names = json.loads(r["payload"])["distinct_names"]
        check(sorted(names) == ["pink & nate ruess | just give me a reason",
                                "pink | just give me a reason"], names)


def test_duplicate_suspect_not_filed_for_a_dropped_song():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        dead = _cluster(conn)
        _item(conn, cluster=dead, artist="A", title="X", integrity="broken")
        _item(conn, cluster=dead, artist="B", title="X", integrity="broken")
        rt, rep, _ = _run(conn, tmp)
        check(rep.songs == 0 and rep.issues == 0,
              "no song shipped, so there is nothing for an operator to look at")


# --- §3 rank ------------------------------------------------------------------------------

def test_quality_rank_maps_verbatim_and_null_falls_back():
    """quality_rank is a TOTAL order after any `verdicts` run and is 100% populated live.
    The fallback is defence: it orders NULLs last, so their numbers sit strictly above every
    real rank in the (song, format) group and cannot collide."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        c = _cluster(conn)
        a = _item(conn, cluster=c, title="Song", rank=1)
        b = _item(conn, cluster=c, title="Song", rank=2)
        n = _item(conn, cluster=c, title="Song", rank=None)
        rt, rep, _ = _run(conn, tmp)
        got = {r["id"]: r["rank"] for r in rt.execute("SELECT id, rank FROM versions")}
        check(got[a] == 1 and got[b] == 2, got)
        check(got[n] == 3, f"NULL rank sorts last, no collision: {got}")
        check(rep.ranks_missing == 1, rep.as_dict())
        # §5.3's actual playback query still returns a single default copy.
        default = rt.execute(
            "SELECT id FROM versions WHERE song_id=? AND unplayable_at IS NULL "
            "ORDER BY rank LIMIT 1", (c,)).fetchone()[0]
        check(default == a)


# --- §4 export is repeatable ----------------------------------------------------------------

def test_reexport_regenerates_rather_than_merges():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), artist="Queen", title="Bohemian Rhapsody")
        rt, first, out = _run(conn, tmp)
        rt.close()
        second = ex.export(config.DB_PATH, out, force=True)
        check((second.songs, second.versions, second.files) ==
              (first.songs, first.versions, first.files),
              f"§4: export is repeatable — {second.as_dict()} vs {first.as_dict()}")
        rt2 = sqlite3.connect(out)
        check(rt2.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1,
              "a re-export replaces the file; it never doubles rows into it")


def test_refuses_to_clobber_without_force():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), title="Song")
        rt, _, out = _run(conn, tmp)
        rt.close()
        try:
            ex.export(config.DB_PATH, out)
        except SystemExit:
            return
        raise AssertionError("an existing --out must require --force")


# --- drive_id path mode (the DustMic demo export, 2026-08-27) ------------------------------

def test_drive_id_mode_puts_file_ids_in_relpath():
    """`relpath` carries the Drive file id, and NOTHING else about the schema changes — that
    is the whole point: the player is written once against the real four tables."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item = _item(conn, cluster=_cluster(conn), fmt="video", title="Song", sha="shaAA")
        rt, rep, _ = _run(conn, tmp, path_mode="drive_id")
        row = rt.execute("SELECT * FROM files").fetchone()
        check(row["relpath"] == "drv-shaAA", f"want the drive id, got {row['relpath']!r}")
        check(row["version_id"] == item and row["role"] == "av")
        check(row["content_hash"] == "shaAA" and row["size_bytes"] == 1000,
              "hash and size still come from blobs, exactly as in library mode")
        check(rep.path_mode == "drive_id")
        check(rep.path_root.startswith("gdrive:"), rep.path_root)


def test_drive_id_mode_ships_a_blob_that_was_never_downloaded():
    """Library mode marks an un-downloaded blob 'remote:' because it has no local path. In
    drive_id mode there is nothing wrong with it at all — Drive is the source, and the file is
    sitting right there."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _item(conn, cluster=_cluster(conn), fmt="video", title="Never downloaded",
              sha="shaRR", local=False)
        rt, rep, _ = _run(conn, tmp, path_mode="drive_id")
        row = rt.execute("SELECT relpath FROM files").fetchone()
        check(row["relpath"] == "drv-shaRR", row["relpath"])
        check(rep.files_remote_fallback == 0, "no 'remote:' concept in this mode")


def test_drive_id_mode_ignores_locations_with_no_drive_id():
    """Organize writes hardlink locations with drive_file_id NULL (a hardlink is a local
    second copy, not a Drive file). Those must never be chosen as the id."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item = _item(conn, cluster=_cluster(conn), fmt="mp3g", title="Paired", sha="shaBB")
        gsha = conn.execute(
            "SELECT content_hash FROM media_item_files WHERE media_item_id=? AND role='graphics'",
            (item,)).fetchone()[0]
        conn.execute(
            "INSERT INTO file_locations (drive_file_id, local_path, filetype, content_hash, "
            "status) VALUES (NULL, ?, 'cdg', ?, 'active')",
            (f"{config.ACTIVE_DIR}/S/Paired [1]/Paired [1].cdg", gsha))
        rt, _rep, _ = _run(conn, tmp, path_mode="drive_id")
        got = {r["role"]: r["relpath"] for r in rt.execute("SELECT role, relpath FROM files")}
        check(got["graphics"] == f"drv-{gsha}", f"hardlink row won the pick: {got}")


def test_drive_id_mode_fails_loudly_when_content_has_no_drive_id():
    """A local-only file (schema: drive_file_id is NULL for those) cannot be streamed from the
    shared folder. Shipping the row would surface as a dead file at demo time."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        cl = _cluster(conn)
        conn.execute("INSERT INTO blobs (content_hash, size_bytes, integrity_status) "
                     "VALUES ('shaLOCAL', 1000, 'probed_ok')")
        conn.execute(
            "INSERT INTO file_locations (drive_file_id, local_path, filetype, content_hash, "
            "status) VALUES (NULL, ?, 'mp4', 'shaLOCAL', 'staged')",
            (f"{config.STAGING_DIR}/local-only.mp4",))
        item = conn.execute(
            "INSERT INTO media_items (format, cluster_id, duration_sec, quality_rank, "
            "quality_verdict, status) VALUES ('video',?,200.0,1,'winner','active')",
            (cl,)).lastrowid
        conn.execute("INSERT INTO media_item_files (media_item_id, content_hash, role) "
                     "VALUES (?, 'shaLOCAL', 'av')", (item,))
        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                     "confidence) VALUES (?, 'title', 'Local only', 'filename', 0.9)", (item,))
        conn.commit()
        conn.close()
        try:
            ex.export(config.DB_PATH, tmp / "runtime.db", path_mode="drive_id")
        except RuntimeError as exc:
            check("no Drive file id" in str(exc), str(exc))
            return
        raise AssertionError("export should have failed on a blob with no Drive file id")


def test_drive_id_and_library_mode_agree_on_every_id():
    """The demo export and the real one must describe the SAME library. Only the resolution
    step differs, so songs/versions/files must line up row for row."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        cl = _cluster(conn)
        _item(conn, cluster=cl, fmt="video", artist="Queen", title="Bohemian Rhapsody")
        _item(conn, cluster=_cluster(conn), fmt="mp3g", artist="שלמה ארצי", title="ירושלים")
        conn.commit()
        conn.close()
        a = tmp / "lib.db"
        b = tmp / "drv.db"
        ex.export(config.DB_PATH, a, path_mode="library")
        ex.export(config.DB_PATH, b, path_mode="drive_id")
        for table, cols in (("songs", "id, artist, title, artist_norm, title_norm"),
                            ("versions", "id, song_id, format, rank"),
                            ("files", "version_id, role, content_hash, size_bytes")):
            ra = sqlite3.connect(a).execute(f"SELECT {cols} FROM {table} ORDER BY 1,2").fetchall()
            rb = sqlite3.connect(b).execute(f"SELECT {cols} FROM {table} ORDER BY 1,2").fetchall()
            check(ra == rb, f"{table} differs between modes")


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
