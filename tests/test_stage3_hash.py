"""Stage 3 §7.2.1 hashing invariant tests.

Everything runs against a temp library with real files on disk — hashing has no remote edge to
fake, so these tests exercise the actual sha256/md5 read path.

The load-bearing tests:
  * `test_md5_drift_never_links_a_blob` — bytes that changed since Stage 2 verified them must
    not enter content identity; the row stays hashable for a post-recovery re-run.
  * `test_rerun_is_idempotent` — second run hashes nothing, changes nothing.
  * `test_shared_bytes_share_one_blob_and_extra_is_marked_exact_dup` — the §7.2.1 safety net,
    with §5.2's own survivor rule.

Run: python3 tests/test_stage3_hash.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import hashlib
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


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _staged(conn, fid: str, path: str, body: bytes, md5: str | None = None) -> Path:
    """Insert an already-staged location whose bytes sit in staging, as Stage 2 leaves them."""
    md5 = md5 or _md5(body)
    dest = config.STAGING_DIR / md5[:2] / f"{fid}.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": path, "Name": Path(path).name,
                "Size": len(body), "Hashes": {"md5": md5}}]
    )
    conn.execute(
        "UPDATE file_locations SET status='staged', local_path=? WHERE drive_file_id=?",
        (str(dest), fid),
    )
    conn.commit()
    return dest


def _row(conn, fid):
    return conn.execute(
        "SELECT * FROM file_locations WHERE drive_file_id=?", (fid,)
    ).fetchone()


# --- pure helper ----------------------------------------------------------------------------


def test_sha256_and_md5_agree_with_hashlib():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f"
        body = b"x" * (stage3.HASH_CHUNK + 17)  # spans a chunk boundary
        p.write_bytes(body)
        sha, md5, size = stage3.sha256_and_md5(p)
        check(sha == _sha(body) and md5 == _md5(body) and size == len(body))


# --- linking behavior -----------------------------------------------------------------------


def test_happy_path_links_blob_and_location():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"some song bytes"
        _staged(conn, "f1", "d/a.mp3", body)
        rep = stage3.hash_all(conn)
        check(rep.hashed == 1 and rep.new_blobs == 1, rep.as_dict())
        row = _row(conn, "f1")
        check(row["content_hash"] == _sha(body))
        blob = conn.execute("SELECT * FROM blobs WHERE content_hash=?", (_sha(body),)).fetchone()
        check(blob["size_bytes"] == len(body), "blob size is the verified local count")
        check(blob["first_hashed_at"] is not None)


def test_md5_drift_never_links_a_blob():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        good, rotted = b"original bytes", b"rotted bytes!!"
        # Staged with the md5 of the *original* bytes, but the file on disk has changed since.
        _staged(conn, "f1", "d/a.mp3", rotted, md5=_md5(good))
        rep = stage3.hash_all(conn)
        check(rep.md5_drift == 1 and rep.hashed == 0, rep.as_dict())
        row = _row(conn, "f1")
        check(row["content_hash"] is None, "corrupt bytes must not enter content identity")
        check(conn.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"] == 0)
        check(len(stage3.hash_worklist(conn)) == 1, "row stays hashable after recovery")


def test_missing_file_is_a_read_error_not_a_crash():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        dest = _staged(conn, "f1", "d/a.mp3", b"bytes")
        dest.unlink()
        rep = stage3.hash_all(conn)
        check(rep.read_errors == 1 and rep.hashed == 0, rep.as_dict())
        check(_row(conn, "f1")["content_hash"] is None)


def test_rerun_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _staged(conn, "f1", "d/a.mp3", b"one")
        _staged(conn, "f2", "d/b.mp3", b"two")
        rep1 = stage3.hash_all(conn)
        check(rep1.hashed == 2, rep1.as_dict())
        rep2 = stage3.hash_all(conn)
        check(rep2.worklist == 0 and rep2.hashed == 0, "second run must be a no-op")
        check(conn.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"] == 2)


def test_shared_bytes_share_one_blob_and_extra_is_marked_exact_dup():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"identical bytes in two staged rows"
        # Same bytes staged twice (differing gdrive_md5 claims is how §5.2 could miss them —
        # here we just force two staged rows whose local bytes coincide).
        _staged(conn, "fA", "d/a.mp3", body)
        # different declared md5 -> different shard, so both rows coexist on disk
        _staged(conn, "fB", "e/b.mp3", body, md5="ff" + _md5(body)[2:])
        # fB's declared md5 doesn't match its bytes -> would be drift; clear the claim to model
        # the same-bytes-different-claim case §5.2 groups apart.
        conn.execute("UPDATE file_locations SET gdrive_md5=NULL WHERE drive_file_id='fB'")
        conn.commit()
        rep = stage3.hash_all(conn)
        check(rep.hashed == 2 and rep.new_blobs == 1 and rep.shared_blobs == 1, rep.as_dict())
        check(rep.new_exact_dups == 1, rep.as_dict())
        a, b = _row(conn, "fA"), _row(conn, "fB")
        check(a["content_hash"] == b["content_hash"], "both link the one blob")
        # §5.2 survivor rule: smallest drive_file_id survives; the other is marked, not excluded.
        check(a["archive_reason"] is None and b["archive_reason"] == "exact_dup")
        check(b["status"] == "staged", "bytes are local; 'excluded' means never-downloaded")
        # Idempotent: re-marking changes nothing.
        check(stage3.mark_new_exact_dups(conn) == [], "second pass marks nothing new")


def test_exact_dup_marking_never_clobbers_an_existing_reason():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"same bytes"
        _staged(conn, "fA", "d/a.mp3", body)
        _staged(conn, "fB", "e/b.mp3", body, md5="ff" + _md5(body)[2:])
        conn.execute(
            "UPDATE file_locations SET gdrive_md5=NULL, archive_reason='orphan_cdg' "
            "WHERE drive_file_id='fB'"
        )
        conn.commit()
        stage3.hash_all(conn)
        check(_row(conn, "fB")["archive_reason"] == "orphan_cdg",
              "an existing archive_reason is never overwritten (Stage 1 trap, same rule)")


def test_limit_and_resume():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for i in range(3):
            _staged(conn, f"f{i}", f"d/{i}.mp3", f"body {i}".encode())
        rep1 = stage3.hash_all(conn, limit=2)
        check(rep1.hashed == 2)
        rep2 = stage3.hash_all(conn)
        check(rep2.worklist == 1 and rep2.hashed == 1, "resume picks up exactly the remainder")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passing")
