"""Stage 3 §7.2.2 ID3 invariant tests.

The tag reader is injected as a fake returning chosen raw values, so the cleaning /
truncation / disagreement rules — derived from a 400-file sample of the live library — are
exercised without media files.

The load-bearing tests:
  * `test_truncated_prefix_is_skipped` — ID3v1's 30-byte fields truncate; §3.6 trusts id3 over
    filename, so writing "Don't Think I Don't Think Abou" would have v_metadata serve it over
    the filename's full title. A proper prefix of the filename value is the same fact, damaged.
  * `test_disagreement_writes_low_confidence_and_queues_once` — §7.2.2's review rule.
  * `test_untagged_items_leave_the_worklist` — resumability must not rescan 23k untagged files.

Run: python3 tests/test_stage3_id3.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from karaokemp import config, db, stage0, stage3


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


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


def _audio_item(conn, fid, sha, file_artist=None, file_title=None):
    """An audio_only item with its staged location and optional filename metadata."""
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": f"d/{fid}.mp3", "Name": f"{fid}.mp3",
                "Size": 1000, "Hashes": {"md5": "aa" + fid}}]
    )
    conn.execute("INSERT OR IGNORE INTO blobs (content_hash, size_bytes) VALUES (?, 1000)", (sha,))
    conn.execute(
        "UPDATE file_locations SET status='staged', local_path=?, content_hash=? "
        "WHERE drive_file_id=?",
        (str(config.STAGING_DIR / fid), sha, fid),
    )
    cur = conn.execute(
        "INSERT INTO media_items (format, quality_verdict) VALUES ('audio_only', 'pending')"
    )
    item_id = cur.lastrowid
    conn.execute(
        "INSERT INTO media_item_files (media_item_id, content_hash, role) VALUES (?,?, 'audio')",
        (item_id, sha),
    )
    for fld, val in (("artist", file_artist), ("title", file_title)):
        if val:
            conn.execute(
                "INSERT INTO song_metadata (media_item_id, field, value, source, confidence) "
                "VALUES (?,?,?,'filename',0.8)",
                (item_id, fld, val),
            )
    conn.commit()
    return item_id


def _fake_reader(mapping):
    def reader(path: Path):
        return mapping.get(path.name)
    return reader


def _id3_rows(conn, item_id):
    return {r["field"]: (r["value"], r["confidence"]) for r in conn.execute(
        "SELECT field, value, confidence FROM song_metadata "
        "WHERE media_item_id=? AND source='id3' AND value IS NOT NULL", (item_id,)
    )}


# --- cleaning rules -------------------------------------------------------------------------


def test_clean_id3_value_strips_decorations_and_unswaps_artist():
    check(stage3.clean_id3_value("Call Me Maybe [Karaoke]") == "Call Me Maybe")
    check(stage3.clean_id3_value("Jepsen, Carly Rae", is_artist=True) == "Carly Rae Jepsen")
    check(stage3.clean_id3_value("Michael, George", is_artist=True) == "George Michael")
    check(stage3.clean_id3_value("Earth, Wind & Fire", is_artist=True) == "Earth, Wind & Fire",
          "a comma-bearing band name must never be 'unswapped'")
    check(stage3.clean_id3_value("Crosby, Stills, Nash & Young", is_artist=True)
          == "Crosby, Stills, Nash & Young")
    check(stage3.clean_id3_value("Unknown Artist") is None)
    check(stage3.clean_id3_value("Track 07") is None)
    check(stage3.clean_id3_value("  ") is None)
    check(stage3.clean_id3_value(None) is None)


def test_id3_vs_filename_verdicts():
    check(stage3.id3_vs_filename("Waterloo", "Waterloo") == "agree")
    check(stage3.id3_vs_filename("Don't Think I Don't Think Abou",
                                 "Don't Think I Don't Think About It") == "truncated_prefix")
    check(stage3.id3_vs_filename("Waterloo", "Dancing Queen") == "disagree")
    check(stage3.id3_vs_filename("Waterloo", None) == "alone")
    check(stage3.id3_vs_filename(None, "Waterloo") == "absent")
    check(stage3.id3_vs_filename("Waterloo ABBA", "abba waterloo") == "agree",
          "token overlap is agreement regardless of order")


# --- DB behavior ----------------------------------------------------------------------------


def test_agreeing_tags_are_written_at_high_confidence():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item = _audio_item(conn, "f1", "sha1", file_artist="ABBA", file_title="Waterloo")
        rep = stage3.id3_all(conn, reader=_fake_reader(
            {"f1": {"artist": "ABBA", "title": "Waterloo", "date": "1974", "genre": "Pop"}}))
        check(rep.tagged == 1 and rep.disagreements == 0, rep.as_dict())
        rows = _id3_rows(conn, item)
        check(rows["artist"] == ("ABBA", stage3.ID3_CONFIDENCE_AGREE), rows)
        check(rows["year"] == ("1974", stage3.ID3_CONFIDENCE_ALONE))
        check(rows["genre"][0] == '["Pop"]')


def test_truncated_prefix_is_skipped():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item = _audio_item(conn, "f1", "sha1", file_artist="Darius Rucker",
                           file_title="Don't Think I Don't Think About It")
        rep = stage3.id3_all(conn, reader=_fake_reader(
            {"f1": {"artist": "Darius Rucker", "title": "Don't Think I Don't Think Abou"}}))
        check(rep.truncated_skipped == 1, rep.as_dict())
        rows = _id3_rows(conn, item)
        check("title" not in rows, "a truncated tag must never outrank the full filename title")
        check(rows["artist"][0] == "Darius Rucker")


def test_disagreement_writes_low_confidence_and_queues_once():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item = _audio_item(conn, "f1", "sha1", file_artist="ABBA", file_title="Waterloo")
        reader = _fake_reader({"f1": {"artist": "Boney M", "title": "Rasputin"}})
        rep = stage3.id3_all(conn, reader=reader)
        check(rep.disagreements == 1 and rep.metadata_match_queued == 1, rep.as_dict())
        rows = _id3_rows(conn, item)
        check(rows["artist"] == ("Boney M", stage3.ID3_CONFIDENCE_DISAGREE), rows)
        n = conn.execute(
            "SELECT COUNT(*) c FROM review_queue WHERE kind='metadata_match' AND media_item_id=?",
            (item,),
        ).fetchone()["c"]
        check(n == 1)
        rep2 = stage3.id3_all(conn, reader=reader)
        check(rep2.worklist == 0 and rep2.metadata_match_queued == 0,
              "an item with id3 rows leaves the worklist — never re-queued")


def test_one_agreeing_field_defuses_the_other_fields_disagreement():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item = _audio_item(conn, "f1", "sha1", file_artist="R U Mine", file_title="Arctic Monkeys")
        # Order-swapped filename parse: id3 'disagrees' per-field but it is the same song.
        rep = stage3.id3_all(conn, reader=_fake_reader(
            {"f1": {"artist": "Arctic Monkeys", "title": "R U Mine"}}))
        check(rep.disagreements == 0, rep.as_dict())
        rows = _id3_rows(conn, item)
        check(rows["artist"] == ("Arctic Monkeys", stage3.ID3_CONFIDENCE_AGREE), rows)


def test_untagged_items_leave_the_worklist():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item = _audio_item(conn, "f1", "sha1")
        rep = stage3.id3_all(conn, reader=_fake_reader({}))  # reader returns None: no tags
        check(rep.untagged == 1, rep.as_dict())
        check(stage3.id3_worklist(conn) == [], "marker row keeps it out of future runs")
        check(_id3_rows(conn, item) == {}, "the marker row carries no servable value")


def test_budget_stop_is_resumable():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for i in range(4):
            _audio_item(conn, f"f{i}", f"sha{i}", file_artist="A", file_title="T")
        reader = _fake_reader(
            {f"f{i}": {"artist": f"Z{i}", "title": f"Q{i}"} for i in range(4)})
        rep = stage3.id3_all(conn, reader=reader, budget=2)
        check(rep.over_budget, rep.as_dict())
        done = 4 - len(stage3.id3_worklist(conn))
        check(0 < done < 4, "stopped partway, work done so far is committed")
        rep2 = stage3.id3_all(conn, reader=reader, budget=100)
        check(len(stage3.id3_worklist(conn)) == 0, "raised budget => re-run finishes the rest")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passing")
