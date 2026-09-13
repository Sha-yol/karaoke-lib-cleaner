"""Stage 3 §7.2.4 media-item creation invariant tests.

Pure DB fixtures — hashing and probing are modelled by inserting blobs with stored probe
summaries, exactly the shape probe_all writes. The load-bearing tests:

  * `test_pair_becomes_mp3g_item` / `test_orphan_mp3_becomes_audio_only` /
    `test_video_blob_becomes_video_item` — the §7.2.4 promotion table.
  * `test_cdg_check_failure_marks_suspect_and_queues_once` — failing pairs still become items
    (§1.2: the pairing was confirmed evidence; dropping it would be a destructive guess), but
    the graphics blob goes suspect and review is asked exactly once.
  * `test_multi_graphics_choice_prefers_passing_check` — the 15 real multi-graphics audio blobs
    need a deterministic, evidence-ranked pick; alternates are recorded, not dropped.
  * `test_rerun_is_idempotent` — items are upserted by natural key, never rebuilt, because
    review_queue/song_metadata/clusters reference them.
  * `test_excluded_twin_hints_reach_the_item` — the whole reason Stage 1 parsed excluded rows.

Run: python3 tests/test_stage3_items.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json
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


MP3_DETAIL = {"format_name": "mp3", "duration": "240.0",
              "streams": [{"codec_type": "audio", "codec_name": "mp3",
                           "sample_rate": "44100", "channels": 2, "bit_rate": "192000",
                           "duration": "240.0"}]}
MP4_DETAIL = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "200.0",
              "streams": [{"codec_type": "video", "codec_name": "h264",
                           "width": 640, "height": 360, "duration": "200.0"},
                          {"codec_type": "audio", "codec_name": "aac",
                           "sample_rate": "44100", "duration": "200.0"}]}
CDG_DETAIL = {"format_name": "cdg", "duration": "240.0",
              "streams": [{"codec_type": "video", "codec_name": "cdgraphics",
                           "width": 300, "height": 216, "duration": "240.0"}]}

GOOD_CDG_SIZE = int(240.0 * stage3.CDG_BYTES_PER_SEC)      # exact match vs the 240s mp3
BAD_CDG_SIZE = int(300.0 * stage3.CDG_BYTES_PER_SEC)       # graphics outlive audio by 60s — fails


def test_cdg_check_is_asymmetric():
    """§11 retune, measured on all 22,854 real pairs: graphics ending early (outro after the
    last lyric) is normal up to ~30s; graphics outliving the audio is never right."""
    mp3 = 240.0
    def delta(d):  # cdg longer than mp3 by d seconds
        return stage3.cdg_check(int((mp3 + d) * stage3.CDG_BYTES_PER_SEC), mp3)["status"]
    check(delta(0) == "ok")
    check(delta(-12) == "ok", "graphics stop at the last lyric, audio outro plays: benign")
    check(delta(-29) == "ok")
    check(delta(-31) == "failed", "whole-side recordings / dead air: review")
    check(delta(+2) == "ok")
    check(delta(+4) == "failed", "graphics outliving the audio is never right")
    check(stage3.cdg_check(None, mp3)["status"] == "skipped")


def _blob(conn, fid, path, filetype, md5, sha, size=1000, detail=None, status="probed_ok",
          loc_status="staged"):
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": path, "Name": Path(path).name,
                "Size": size, "Hashes": {"md5": md5}}]
    )
    if loc_status == "staged":
        conn.execute(
            "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status, "
            "integrity_detail) VALUES (?,?,?,?)",
            (sha, size, status, json.dumps(detail) if detail else None),
        )
        conn.execute(
            "UPDATE file_locations SET status='staged', local_path=?, content_hash=?, filetype=? "
            "WHERE drive_file_id=?",
            (str(config.STAGING_DIR / fid), sha, filetype, fid),
        )
    else:
        conn.execute(
            "UPDATE file_locations SET status=?, archive_reason='exact_dup', filetype=? "
            "WHERE drive_file_id=?",
            (loc_status, filetype, fid),
        )
    conn.commit()


def _parse(conn, fid, artist=None, title=None, confidence=0.8, layout="artist_title",
           language=None, is_instrumental=None):
    loc = conn.execute(
        "SELECT id FROM file_locations WHERE drive_file_id=?", (fid,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT OR REPLACE INTO location_parses (location_id, artist, title, language, "
        "is_instrumental, layout, confidence, payload, parser_version) "
        "VALUES (?,?,?,?,?,?,?,'{}','test')",
        (loc, artist, title, language, is_instrumental, layout, confidence),
    )
    conn.commit()


def _pair(conn, audio_md5, graphics_md5, witnesses=1):
    conn.execute(
        "INSERT OR IGNORE INTO provisional_pairs (audio_md5, graphics_md5, witnesses) "
        "VALUES (?,?,?)",
        (audio_md5, graphics_md5, witnesses),
    )
    conn.commit()


def _item_rows(conn):
    return conn.execute(
        "SELECT i.*, GROUP_CONCAT(f.role || ':' || f.content_hash) AS files FROM media_items i "
        "JOIN media_item_files f ON f.media_item_id=i.id GROUP BY i.id ORDER BY i.id"
    ).fetchall()


# --- promotion table ------------------------------------------------------------------------


def test_pair_becomes_mp3g_item():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song.mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        _blob(conn, "g1", "d/song.cdg", "cdg", "md5g", "sha_g", size=GOOD_CDG_SIZE,
              detail=CDG_DETAIL)
        _pair(conn, "md5a", "md5g")
        rep = stage3.build_items(conn)
        check(rep.created == {"mp3g": 1, "video": 0, "audio_only": 0}, rep.as_dict())
        check(rep.cdg_ok == 1, rep.as_dict())
        item = _item_rows(conn)[0]
        check(item["format"] == "mp3g")
        check(item["duration_sec"] == 240.0)
        check(item["audio_codec"] == "mp3" and item["audio_bitrate_kbps"] == 192)
        check(sorted(item["files"].split(",")) == ["audio:sha_a", "graphics:sha_g"])
        check(item["quality_verdict"] == "pending")
        roles = {r["drive_file_id"]: r["role"] for r in
                 conn.execute("SELECT drive_file_id, role FROM file_locations")}
        check(roles == {"a1": "audio", "g1": "graphics"}, roles)


def test_orphan_mp3_becomes_audio_only():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/full-original.mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        rep = stage3.build_items(conn)
        check(rep.created == {"mp3g": 0, "video": 0, "audio_only": 1}, rep.as_dict())


def test_video_blob_becomes_video_item():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "v1", "d/clip.mp4", "mp4", "md5v", "sha_v", detail=MP4_DETAIL)
        rep = stage3.build_items(conn)
        check(rep.created == {"mp3g": 0, "video": 1, "audio_only": 0}, rep.as_dict())
        item = _item_rows(conn)[0]
        check(item["video_codec"] == "h264" and item["width"] == 640)
        check(item["files"] == "av:sha_v")


def test_broken_video_still_gets_an_item_by_extension():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "v1", "d/dead.mpg", "mpg", "md5v", "sha_v", detail=None, status="broken")
        rep = stage3.build_items(conn)
        check(rep.created["video"] == 1,
              "a broken sole copy still needs an item — §7.4 archives it (sha-yol's 2026-07-19 "
              "correction) and the item row is what carries it onto the replacement list")


def test_non_media_blob_gets_no_item():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "t1", "d/thumbs.db", "db", "md5t", "sha_t", detail=None, status="unchecked")
        rep = stage3.build_items(conn)
        check(rep.created == {"mp3g": 0, "video": 0, "audio_only": 0})
        check(rep.unclassified_blobs == 1)


# --- CDG check ------------------------------------------------------------------------------


def test_cdg_check_failure_marks_suspect_and_queues_once():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song.mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        _blob(conn, "g1", "d/song.cdg", "cdg", "md5g", "sha_g", size=BAD_CDG_SIZE,
              detail=CDG_DETAIL)
        _pair(conn, "md5a", "md5g")
        rep = stage3.build_items(conn)
        check(rep.created["mp3g"] == 1, "a failing pair still becomes an item")
        check(rep.cdg_failed == 1 and rep.pair_mismatch_queued == 1, rep.as_dict())
        g = conn.execute(
            "SELECT integrity_status, integrity_detail FROM blobs WHERE content_hash='sha_g'"
        ).fetchone()
        check(g["integrity_status"] == "suspect")
        check(json.loads(g["integrity_detail"])["cdg_check"]["status"] == "failed")
        item = _item_rows(conn)[0]
        check(json.loads(item["quality_attrs"])["cdg_check"]["status"] == "failed")
        rep2 = stage3.build_items(conn)
        check(rep2.pair_mismatch_queued == 0, "never re-ask an already-asked question")
        n = conn.execute(
            "SELECT COUNT(*) c FROM review_queue WHERE kind='pair_mismatch'"
        ).fetchone()["c"]
        check(n == 1, n)


def test_cdg_check_skipped_when_mp3_duration_missing():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song.mp3", "mp3", "md5a", "sha_a", detail=None, status="broken")
        _blob(conn, "g1", "d/song.cdg", "cdg", "md5g", "sha_g", size=GOOD_CDG_SIZE,
              detail=CDG_DETAIL)
        _pair(conn, "md5a", "md5g")
        rep = stage3.build_items(conn)
        check(rep.cdg_skipped == 1 and rep.pair_mismatch_queued == 0, rep.as_dict())
        check(conn.execute("SELECT integrity_status FROM blobs WHERE content_hash='sha_g'")
              .fetchone()["integrity_status"] != "suspect",
              "an unverifiable check is not a failed check")


def test_multi_graphics_choice_prefers_passing_check():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song.mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        # The bad-duration cdg has MORE witnesses — the check must outrank popularity.
        _blob(conn, "gBad", "d/song.cdg", "cdg", "md5_bad", "sha_bad", size=BAD_CDG_SIZE,
              detail=CDG_DETAIL)
        _blob(conn, "gGood", "e/song.cdg", "cdg", "md5_good", "sha_good", size=GOOD_CDG_SIZE,
              detail=CDG_DETAIL)
        _pair(conn, "md5a", "md5_bad", witnesses=5)
        _pair(conn, "md5a", "md5_good", witnesses=1)
        rep = stage3.build_items(conn)
        check(rep.created["mp3g"] == 1 and rep.cdg_ok == 1, rep.as_dict())
        item = _item_rows(conn)[0]
        check("graphics:sha_good" in item["files"], item["files"])
        attrs = json.loads(item["quality_attrs"])
        check(attrs["graphics_alternates"] == ["sha_bad"], "the loser is recorded, not dropped")


# --- idempotence / upgrade ------------------------------------------------------------------


def test_rerun_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song.mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        _blob(conn, "g1", "d/song.cdg", "cdg", "md5g", "sha_g", size=GOOD_CDG_SIZE,
              detail=CDG_DETAIL)
        _blob(conn, "v1", "d/clip.mp4", "mp4", "md5v", "sha_v", detail=MP4_DETAIL)
        _pair(conn, "md5a", "md5g")
        _parse(conn, "a1", artist="ABBA", title="Waterloo")
        rep1 = stage3.build_items(conn)
        meta1 = stage3.promote_filename_metadata(conn)
        check(sum(rep1.created.values()) == 2 and meta1["inserted"] == 2, (rep1.as_dict(), meta1))
        rep2 = stage3.build_items(conn)
        meta2 = stage3.promote_filename_metadata(conn)
        check(sum(rep2.created.values()) == 0 and rep2.updated == 0, rep2.as_dict())
        check(rep2.existing == 2, rep2.as_dict())
        check(meta2["inserted"] == 0 and meta2["updated"] == 0, meta2)


def test_audio_only_upgrades_to_mp3g_when_pair_appears():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song.mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        _blob(conn, "g1", "d/song.cdg", "cdg", "md5g", "sha_g", size=GOOD_CDG_SIZE,
              detail=CDG_DETAIL)
        rep1 = stage3.build_items(conn)
        check(rep1.created["audio_only"] == 1)
        item_id = _item_rows(conn)[0]["id"]
        _pair(conn, "md5a", "md5g")  # e.g. a pair_mismatch verdict later confirmed via `pair`
        rep2 = stage3.build_items(conn)
        check(rep2.created["mp3g"] == 0 and rep2.format_upgraded == 1,
              "same item upgraded in place — references to it must survive")
        item = _item_rows(conn)[0]
        check(item["id"] == item_id and item["format"] == "mp3g")
        check("graphics:sha_g" in item["files"])


# --- metadata promotion ---------------------------------------------------------------------


def test_excluded_twin_hints_reach_the_item():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "u/drifters-under the boardwalk.mp3", "mp3", "md5a", "sha_a",
              detail=MP3_DETAIL)
        # The §5.2-excluded duplicate carries the better-labelled name and a better parse.
        _blob(conn, "a2", "n/SF018-02 - The Drifters - Under The Boardwalk.mp3", "mp3",
              "md5a", "sha_ignored", loc_status="excluded")
        _parse(conn, "a1", artist=None, title="drifters-under the boardwalk",
               confidence=0.4, layout="title_only")
        _parse(conn, "a2", artist="The Drifters", title="Under The Boardwalk", confidence=0.9)
        stage3.build_items(conn)
        stage3.promote_filename_metadata(conn)
        vals = {r["field"]: (r["value"], r["confidence"]) for r in conn.execute(
            "SELECT field, value, confidence FROM song_metadata WHERE source='filename'"
        )}
        check(vals["artist"] == ("The Drifters", 0.9), vals)
        check(vals["title"] == ("Under The Boardwalk", 0.9),
              "the excluded twin's better parse must win — it is why excluded rows were parsed")


def test_instrumental_flag_promoted_from_parse():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _blob(conn, "a1", "d/song [karaoke].mp3", "mp3", "md5a", "sha_a", detail=MP3_DETAIL)
        _parse(conn, "a1", artist="X", title="Y", is_instrumental="yes")
        stage3.build_items(conn)
        stage3.promote_filename_metadata(conn)
        check(_item_rows(conn)[0]["is_instrumental"] == "yes")


def test_over_budget_stops_without_writing():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for i in range(3):
            _blob(conn, f"a{i}", f"d/s{i}.mp3", "mp3", f"md5a{i}", f"sha_a{i}", detail=MP3_DETAIL)
            _blob(conn, f"g{i}", f"d/s{i}.cdg", "cdg", f"md5g{i}", f"sha_g{i}",
                  size=BAD_CDG_SIZE, detail=CDG_DETAIL)
            _pair(conn, f"md5a{i}", f"md5g{i}")
        rep_dry = stage3.build_items(conn, dry_run=True, budget=2)
        check(rep_dry.over_budget,
              "dry-run must PROJECT the queue (its own queued count), not read back the DB — "
              "otherwise it reports fine while the real run would stop")
        rep = stage3.build_items(conn, budget=2)
        check(rep.over_budget, rep.as_dict())
        check(conn.execute("SELECT COUNT(*) c FROM media_items").fetchone()["c"] == 0,
              "over budget => nothing written (§11: stop and retune, don't grind)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passing")
