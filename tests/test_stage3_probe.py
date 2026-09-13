"""Stage 3 §7.2.3 probe invariant tests.

ffprobe is the one impure edge; it is injected as a fake `prober` returning realistic ffprobe
JSON shapes (captured from the live library). The classification rules are what these tests pin:

  * a cdg has no audio stream by nature and must NOT be broken for it;
  * a `.part` file whose container probes clean is capped at 'suspect' — the live library's two
    truncated downloads probe clean (an intact moov atom claims 208s over 714 KB), so trusting
    the probe there would crown a truncated file;
  * probe failure / no streams / zero duration => broken;
  * re-run is a no-op (worklist is integrity_status='unchecked').

Run: python3 tests/test_stage3_probe.py    (or: python3 -m pytest tests/ -q)
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


def _staged_blob(conn, fid: str, path: str, filetype: str, sha: str):
    """A staged, already-hashed location + its blob (probe runs strictly after hashing)."""
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": path, "Name": Path(path).name,
                "Size": 1000, "Hashes": {"md5": "aa" + fid}}]
    )
    conn.execute("INSERT OR IGNORE INTO blobs (content_hash, size_bytes) VALUES (?, 1000)", (sha,))
    conn.execute(
        "UPDATE file_locations SET status='staged', local_path=?, content_hash=?, filetype=? "
        "WHERE drive_file_id=?",
        (str(config.STAGING_DIR / fid), sha, filetype, fid),
    )
    conn.commit()


# Realistic ffprobe shapes, captured from the live library 2026-07-18.
MP3_PROBE = {
    "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100",
                 "channels": 2, "bit_rate": "128000", "duration": "305.136327"}],
    "format": {"format_name": "mp3", "duration": "305.161000", "bit_rate": "128021"},
}
CDG_PROBE = {
    "streams": [{"codec_type": "video", "codec_name": "cdgraphics",
                 "width": 300, "height": 216, "duration": "241.293333"}],
    "format": {"format_name": "cdg", "duration": "241.293333"},
}
MP4_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
         "duration": "208.600000"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100",
         "channels": 2, "duration": "208.654512"},
    ],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "208.655000",
               "bit_rate": "27402"},
}
SILENT_MP4_PROBE = {
    "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
                 "duration": "100.0"}],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "100.0"},
}


# --- pure classification --------------------------------------------------------------------


def test_mp3_probes_ok():
    status, detail = stage3.classify_probe(MP3_PROBE, "mp3", "song.mp3")
    check(status == "probed_ok", status)
    check(detail["streams"][0]["codec_name"] == "mp3")


def test_cdg_without_audio_is_ok():
    status, _ = stage3.classify_probe(CDG_PROBE, "cdg", "song.cdg")
    check(status == "probed_ok", "cdg is graphics-only by nature; missing audio is not damage")


def test_video_without_audio_is_broken():
    status, detail = stage3.classify_probe(SILENT_MP4_PROBE, "mp4", "song.mp4")
    check(status == "broken", status)
    check("no audio" in detail["probe_error"])


def test_part_file_is_capped_at_suspect_even_when_probe_is_clean():
    status, detail = stage3.classify_probe(MP4_PROBE, "part", "song.mp4.part")
    check(status == "suspect",
          "a truncated download's container can probe clean; the probe must not crown it")
    check("incomplete download" in detail["probe_note"])


def test_probe_error_zero_duration_and_no_streams_are_broken():
    check(stage3.classify_probe({"error": "boom"}, "mp3", "x.mp3")[0] == "broken")
    check(stage3.classify_probe({"streams": [], "format": {}}, "mp3", "x.mp3")[0] == "broken")
    nodur = {"streams": [{"codec_type": "audio", "codec_name": "mp3"}], "format": {}}
    check(stage3.classify_probe(nodur, "mp3", "x.mp3")[0] == "broken", "no duration anywhere")


def test_duration_falls_back_to_streams():
    d = stage3.probe_duration({"streams": [{"duration": "12.5"}, {"duration": "13.0"}]})
    check(d == 13.0, d)


# --- DB behavior ----------------------------------------------------------------------------


def _fake_prober(mapping):
    def prober(path: Path) -> dict:
        return mapping[path.name]
    return prober


def test_probe_writes_status_and_detail_and_rerun_is_noop():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _staged_blob(conn, "f1", "d/a.mp3", "mp3", "sha_mp3")
        _staged_blob(conn, "f2", "d/a.cdg", "cdg", "sha_cdg")
        _staged_blob(conn, "f3", "d/bad.mp4", "mp4", "sha_bad")
        prober = _fake_prober({"f1": MP3_PROBE, "f2": CDG_PROBE, "f3": {"error": "moov not found"}})
        rep = stage3.probe_all(conn, prober=prober, workers=1)
        check(rep.probed == 3 and rep.probed_ok == 2 and rep.broken == 1, rep.as_dict())
        row = conn.execute(
            "SELECT integrity_status, integrity_detail FROM blobs WHERE content_hash='sha_mp3'"
        ).fetchone()
        check(row["integrity_status"] == "probed_ok")
        check("mp3" in row["integrity_detail"], "stream facts recorded for item creation")
        bad = conn.execute(
            "SELECT integrity_status FROM blobs WHERE content_hash='sha_bad'"
        ).fetchone()
        check(bad["integrity_status"] == "broken")
        rep2 = stage3.probe_all(conn, prober=prober, workers=1)
        check(rep2.worklist == 0 and rep2.probed == 0, "second run must be a no-op")


def test_non_media_blobs_are_skipped_not_probed():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _staged_blob(conn, "f1", "d/thumbs.db", "db", "sha_db")
        _staged_blob(conn, "f2", "d/cover.jpg", "jpg", "sha_jpg")
        _staged_blob(conn, "f3", "d/song.mp333", "mp333", "sha_typo")
        rep = stage3.probe_all(conn, prober=_fake_prober({"f3": MP3_PROBE}), workers=1)
        check(rep.worklist == 1 and rep.skipped_non_media == 2, rep.as_dict())
        check(conn.execute(
            "SELECT integrity_status FROM blobs WHERE content_hash='sha_db'"
        ).fetchone()["integrity_status"] == "unchecked", "detritus stays unchecked, not broken")
        check(conn.execute(
            "SELECT integrity_status FROM blobs WHERE content_hash='sha_typo'"
        ).fetchone()["integrity_status"] == "probed_ok", "typo'd extension IS probed (content sniff)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passing")
