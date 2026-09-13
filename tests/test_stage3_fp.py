"""Stage 3 §7.2.5 fingerprint invariant tests.

fpcalc is the impure edge, injected as a fake returning chosen raw fingerprints. Pinned here:

  * encode/decode round-trip (the `zb64:` encoding is a documented contract, §3.4);
  * similarity math: identical -> 1.0, unrelated -> ~0.5, small alignment shifts recovered;
  * fpcalc failure on a probed-ok blob => 'suspect' (§7.2.5), and the blob leaves the worklist
    only via a fingerprint row — failures stay retryable;
  * broken blobs and cdg/graphics blobs are never fingerprinted.

Run: python3 tests/test_stage3_fp.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import random
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


def _item_with_blob(conn, fid, sha, role="audio", fmt="audio_only", integrity="probed_ok"):
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": f"d/{fid}", "Name": fid,
                "Size": 1000, "Hashes": {"md5": "aa" + fid}}]
    )
    conn.execute(
        "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
        "VALUES (?, 1000, ?)", (sha, integrity),
    )
    conn.execute(
        "UPDATE file_locations SET status='staged', local_path=?, content_hash=? "
        "WHERE drive_file_id=?",
        (str(config.STAGING_DIR / fid), sha, fid),
    )
    cur = conn.execute(
        "INSERT INTO media_items (format, quality_verdict) VALUES (?, 'pending')", (fmt,)
    )
    conn.execute(
        "INSERT INTO media_item_files (media_item_id, content_hash, role) VALUES (?,?,?)",
        (cur.lastrowid, sha, role),
    )
    conn.commit()
    return cur.lastrowid


# --- encoding + similarity ------------------------------------------------------------------


def test_encode_decode_roundtrip():
    rng = random.Random(7)
    ints = [rng.getrandbits(32) for _ in range(950)]
    text = stage3.encode_fp(ints)
    check(text.startswith(stage3.FP_PREFIX))
    back = stage3.decode_fp(text)
    check(list(back) == ints, "round-trip must be exact")


def test_similarity_identical_and_unrelated():
    rng = random.Random(11)
    a = [rng.getrandbits(32) for _ in range(500)]
    b = [rng.getrandbits(32) for _ in range(500)]
    da, db_ = stage3.decode_fp(stage3.encode_fp(a)), stage3.decode_fp(stage3.encode_fp(b))
    check(stage3.fp_similarity(da, da) == 1.0)
    s = stage3.fp_similarity(da, db_)
    check(0.4 < s < 0.6, f"random fingerprints must sit near 0.5, got {s}")


def test_similarity_recovers_small_alignment_shift():
    rng = random.Random(13)
    a = [rng.getrandbits(32) for _ in range(500)]
    shifted = a[2:]  # same audio, container padding shifted the start by 2 frames
    da = stage3.decode_fp(stage3.encode_fp(a))
    ds = stage3.decode_fp(stage3.encode_fp(shifted))
    check(stage3.fp_similarity(da, ds) == 1.0, "±3 offset search must recover the alignment")


def test_similarity_truncated_prefix_still_matches():
    rng = random.Random(17)
    a = [rng.getrandbits(32) for _ in range(900)]
    trunc = a[:300]  # a truncated copy: same prefix, 1/3 the length
    da = stage3.decode_fp(stage3.encode_fp(a))
    dt = stage3.decode_fp(stage3.encode_fp(trunc))
    check(stage3.fp_similarity(da, dt) == 1.0,
          "overlap-prefix comparison is what catches truncated copies (§7.3c)")


# --- DB behavior ----------------------------------------------------------------------------


def _fake_fper(mapping):
    def fper(path: Path) -> dict:
        return mapping[path.name]
    return fper


def test_fingerprint_written_and_rerun_noop():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _item_with_blob(conn, "f1", "sha1")
        fper = _fake_fper({"f1": {"duration": 240.0, "fingerprint": [1, 2, 3, 4] * 20}})
        rep = stage3.fingerprint_all(conn, fper=fper, workers=1)
        check(rep.fingerprinted == 1, rep.as_dict())
        row = conn.execute("SELECT * FROM fingerprints WHERE content_hash='sha1'").fetchone()
        check(row["fp_duration_sec"] == 240.0)
        check(list(stage3.decode_fp(row["chromaprint"])) == [1, 2, 3, 4] * 20)
        rep2 = stage3.fingerprint_all(conn, fper=fper, workers=1)
        check(rep2.worklist == 0, "second run must be a no-op")


def test_fpcalc_failure_marks_suspect_and_stays_retryable():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _item_with_blob(conn, "f1", "sha1")
        rep = stage3.fingerprint_all(conn, fper=_fake_fper({"f1": {"error": "decode died"}}),
                                     workers=1)
        check(rep.failed == 1 and rep.marked_suspect == 1, rep.as_dict())
        b = conn.execute(
            "SELECT integrity_status, integrity_detail FROM blobs WHERE content_hash='sha1'"
        ).fetchone()
        check(b["integrity_status"] == "suspect", "§7.2.5: fpcalc failure on probed-ok => suspect")
        check("decode died" in b["integrity_detail"])
        check(len(stage3.fingerprint_worklist(conn)) == 1,
              "no fingerprint row => still on the worklist for a retry after repair")


def test_broken_and_graphics_blobs_are_never_fingerprinted():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _item_with_blob(conn, "f1", "sha_broken", integrity="broken")
        item = _item_with_blob(conn, "f2", "sha_audio")
        conn.execute(
            "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
            "VALUES ('sha_cdg', 500, 'probed_ok')"
        )
        conn.execute(
            "INSERT INTO media_item_files (media_item_id, content_hash, role) "
            "VALUES (?, 'sha_cdg', 'graphics')", (item,),
        )
        conn.commit()
        wl = stage3.fingerprint_worklist(conn)
        check([r["content_hash"] for r in wl] == ["sha_audio"],
              "broken blobs skipped; graphics blobs carry no audio")
        check(stage3.count_skipped_broken(conn) == 1)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passing")
