"""Stage 2 §7.1 download invariant tests.

The download itself is one impure edge (`rclone backend copyid`), injected here as a fake `fetch`
that writes chosen bytes to the destination. Everything the pipeline depends on — verify-then-
commit atomicity, resumability, quota deferral, and pair-aware ordering — is exercised without
touching Drive.

The load-bearing tests:
  * `test_verify_mismatch_never_marks_staged` — a corrupt transfer must NOT be trusted (§7.1).
  * `test_interrupt_leaves_row_remote_only` — Ctrl-C mid-run can never leave a half-staged row
    that a resume would skip.
  * `test_deferred_file_is_retried_on_rerun` — a quota-failed file stays remote_only and a later
    run picks it up. One stubborn file must not strand the rest.

Run: python3 tests/test_stage2.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from karaokemp import config, db, stage0, stage2


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


def _fresh_env(tmp: Path):
    """Point the library at a temp dir and open a fresh schema'd DB there."""
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


def _entry(fid, path, md5, size=1000):
    return {"ID": fid, "Path": path, "Name": Path(path).name, "Size": size, "Hashes": {"md5": md5}}


def _fake_fetch(contents: dict[str, bytes]):
    """A fetch that writes contents[file_id] to dest; missing id => rc=1 (a quota-style failure)."""
    def fetch(fid: str, dest: Path) -> subprocess.CompletedProcess:
        if fid not in contents:
            return subprocess.CompletedProcess([], 1, "", "downloadQuotaExceeded")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(contents[fid])
        return subprocess.CompletedProcess([], 0, "", "")
    return fetch


def _no_sleep(_sec: float) -> None:
    """Retry-path tests must not serve real backoff (5+10+20+40s per exhausted file)."""


def _status(conn, path):
    r = conn.execute(
        "SELECT status, local_path FROM file_locations WHERE remote_path=?", (path,)
    ).fetchone()
    return (r["status"], r["local_path"])


# --- pure helpers ---------------------------------------------------------------------------


def test_ext_of_rejects_junk_and_none():
    check(stage2.ext_of("mp3") == "mp3")
    check(stage2.ext_of("mp333") == "mp333", "typo'd but clean extension is kept")
    check(stage2.ext_of("none") == "", "the literal 'none' is not an extension")
    check(stage2.ext_of("") == "")
    check(stage2.ext_of(None) == "")
    check(stage2.ext_of("הב - סאבלימינל") == "", "dotted-name garbage is not an extension")
    check(stage2.ext_of("toolongext") == "", ">5 chars is not a real extension here")


def test_staging_path_is_sharded_and_deterministic():
    p1 = stage2.staging_path("ab12ff", "FILEID1", "mp3")
    p2 = stage2.staging_path("ab12ff", "FILEID1", "mp3")
    check(p1 == p2, "must be deterministic for resume/skip")
    check(p1.parent.name == "ab", "sharded by md5 prefix")
    check(p1.name == "FILEID1.mp3")
    check(stage2.staging_path("cd", "X", "none").name == "X", "no junk suffix")


# --- verify/commit/resume behavior ----------------------------------------------------------


def test_happy_path_stages_and_verifies():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"a real song's bytes"
        stage0.upsert_locations(conn, [_entry("m1", "d/song.mp3", _md5(body), size=len(body))])
        conn.commit()
        rep = stage2.download_all(conn, fetch=_fake_fetch({"m1": body}))
        check(rep.staged == 1, f"staged={rep.staged}")
        check(rep.bytes_downloaded == len(body))
        status, local = _status(conn, "d/song.mp3")
        check(status == "staged", status)
        check(Path(local).read_bytes() == body, "staged file has the right bytes")


def test_verify_mismatch_never_marks_staged():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        # DB expects the md5 of `good`, but the fetch delivers `bad` every time.
        stage0.upsert_locations(conn, [_entry("m1", "d/song.mp3", _md5(b"good"), size=4)])
        conn.commit()
        rep = stage2.download_all(conn, fetch=_fake_fetch({"m1": b"bad!"}), sleep=_no_sleep)
        check(rep.staged == 0, "a hash mismatch must never be staged")
        check(rep.verify_failed == 1, f"verify_failed={rep.verify_failed}")
        check(_status(conn, "d/song.mp3")[0] == "remote_only", "row must stay remote_only")
        # no leftover .part or final file
        leftovers = list((config.STAGING_DIR).rglob("*"))
        check(all(f.is_dir() for f in leftovers), f"left junk: {leftovers}")


def test_size_claim_mismatch_is_flagged_but_still_staged():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"bytes"  # 5 bytes, but Drive claimed 999 at enumeration
        stage0.upsert_locations(conn, [_entry("m1", "d/s.mp3", _md5(body), size=999)])
        conn.commit()
        rep = stage2.download_all(conn, fetch=_fake_fetch({"m1": body}))
        check(rep.staged == 1, "md5 matched => bytes are correct => stage it")
        check(rep.size_claim_mismatches == 1, "Drive's size claim disagreed; that is recorded")


def test_deferred_file_is_retried_on_rerun():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a, b = b"AAAA", b"BBBB"
        stage0.upsert_locations(conn, [
            _entry("ok", "d/a.mp3", _md5(a), size=4),
            _entry("quota", "d/b.mp3", _md5(b), size=4),
        ])
        conn.commit()
        # First run: only "ok" is fetchable; "quota" fails all retries and defers.
        rep1 = stage2.download_all(conn, fetch=_fake_fetch({"ok": a}), sleep=_no_sleep)
        check(rep1.staged == 1 and rep1.deferred == 1, f"{rep1.staged}/{rep1.deferred}")
        check(_status(conn, "d/a.mp3")[0] == "staged", "the good file staged despite its neighbor")
        check(_status(conn, "d/b.mp3")[0] == "remote_only", "deferred file stays remote_only")
        # Second run: quota has 'reset' — b is now fetchable and the worklist re-includes it.
        wl = stage2.build_worklist(conn)
        check([r["drive_file_id"] for r in wl] == ["quota"], "resume worklist = just the deferred one")
        rep2 = stage2.download_all(conn, fetch=_fake_fetch({"quota": b}))
        check(rep2.staged == 1, "the once-deferred file staged on re-run")


def test_adopts_correct_preexisting_file_without_refetch():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"already here"
        stage0.upsert_locations(conn, [_entry("m1", "d/s.mp3", _md5(body), size=len(body))])
        conn.commit()
        # Pre-place the correct file (as an interrupted prior run would have left it).
        dest = stage2.staging_path(_md5(body), "m1", "mp3")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        # A fetch that would RAISE if called — proves we adopted, not re-downloaded.
        def boom(fid, d):
            raise AssertionError("must not re-fetch a correct pre-existing file")
        rep = stage2.download_all(conn, fetch=boom)
        check(rep.already_staged == 1, f"already_staged={rep.already_staged}")
        check(_status(conn, "d/s.mp3")[0] == "staged")


def test_interrupt_leaves_row_remote_only():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        bodies = {f"f{i}": bytes([i]) * 8 for i in range(4)}
        entries = [_entry(fid, f"d/{fid}.mp3", _md5(b), size=8) for fid, b in bodies.items()]
        stage0.upsert_locations(conn, entries)
        conn.commit()
        calls = {"n": 0}

        def fetch(fid, dest):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bodies[fid])
            return subprocess.CompletedProcess([], 0, "", "")

        rep = stage2.download_all(conn, fetch=fetch)
        check(rep.stopped_reason == "interrupted", rep.stopped_reason)
        staged = conn.execute("SELECT COUNT(*) AS n FROM file_locations WHERE status='staged'").fetchone()["n"]
        remote = conn.execute("SELECT COUNT(*) AS n FROM file_locations WHERE status='remote_only'").fetchone()["n"]
        check(staged == 2 and remote == 2, f"staged={staged} remote={remote}")
        # The interrupted file left no .part masquerading as staged.
        parts = [f for f in config.STAGING_DIR.rglob("*.part")]
        check(parts == [], f"left a .part: {parts}")


def test_limit_caps_the_worklist():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        bodies = {f"f{i}": bytes([i]) * 8 for i in range(5)}
        stage0.upsert_locations(
            conn, [_entry(fid, f"d/{fid}.mp3", _md5(b), size=8) for fid, b in bodies.items()]
        )
        conn.commit()
        rep = stage2.download_all(conn, limit=2, fetch=_fake_fetch(bodies))
        check(rep.worklist == 2 and rep.staged == 2, f"{rep.worklist}/{rep.staged}")


def test_excluded_rows_are_never_downloaded():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"dup"
        stage0.upsert_locations(conn, [_entry("m1", "d/s.mp3", _md5(body), size=3)])
        conn.execute("UPDATE file_locations SET status='excluded', archive_reason='exact_dup'")
        conn.commit()
        rep = stage2.download_all(conn, fetch=_fake_fetch({"m1": body}))
        check(rep.worklist == 0 and rep.staged == 0, "excluded rows are out of scope for §7.1")


def test_pair_halves_are_adjacent_in_worklist():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        # Two songs, each mp3+cdg, interleaved by id so only pair-ordering makes halves adjacent.
        stage0.upsert_locations(conn, [
            _entry("a1", "d/x.mp3", "AUDIO_A"),
            _entry("z9", "d/x.cdg", "GFX_A"),
            _entry("a2", "d/y.mp3", "AUDIO_B"),
            _entry("z8", "d/y.cdg", "GFX_B"),
        ])
        conn.execute(
            "INSERT INTO provisional_pairs (audio_md5, graphics_md5, witnesses, created_at) "
            "VALUES ('AUDIO_A','GFX_A',1,''),('AUDIO_B','GFX_B',1,'')"
        )
        conn.commit()
        order = [r["gdrive_md5"] for r in stage2.build_worklist(conn)]
        # each audio md5 sits immediately next to its graphics md5
        check(abs(order.index("AUDIO_A") - order.index("GFX_A")) == 1, order)
        check(abs(order.index("AUDIO_B") - order.index("GFX_B")) == 1, order)


# --- batched fetch --------------------------------------------------------------------------


def _fake_batch(deliveries: dict[str, bytes]):
    """A batch fetch that materializes deliveries[remote_path] under batch_dir, like rclone copy."""
    def fetch_batch(remote_paths, batch_dir: Path):
        for p in remote_paths:
            if p in deliveries:
                dst = batch_dir / p
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(deliveries[p])
        return subprocess.CompletedProcess([], 0, "", "")
    return fetch_batch


def test_batch_arrivals_are_verified_then_staged():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a, b = b"song A", b"song B bytes"
        stage0.upsert_locations(conn, [
            _entry("m1", "d/a.mp3", _md5(a), size=len(a)),
            _entry("m2", "d/b.mp3", _md5(b), size=len(b)),
        ])
        conn.commit()
        rep = stage2.download_batched(
            conn, fetch_batch=_fake_batch({"d/a.mp3": a, "d/b.mp3": b}), sleep=_no_sleep
        )
        check(rep.staged == 2, f"staged={rep.staged}")
        check(_status(conn, "d/a.mp3")[0] == "staged")
        st, local = _status(conn, "d/b.mp3")
        check(st == "staged" and Path(local).read_bytes() == b, "staged the verified bytes")
        check(not (config.STAGING_DIR / ".batch-tmp").exists(), "batch temp dir not cleaned up")


def test_batch_wrong_bytes_never_staged_and_by_id_fallback_recovers():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        good = b"the real bytes"
        stage0.upsert_locations(conn, [_entry("m1", "d/s.mp3", _md5(good), size=len(good))])
        conn.commit()
        # The path delivers an impostor (duplicate name in the folder); by-ID delivers the truth.
        rep = stage2.download_batched(
            conn,
            fetch_batch=_fake_batch({"d/s.mp3": b"impostor!"}),
            fetch=_fake_fetch({"m1": good}),
            sleep=_no_sleep,
        )
        check(rep.staged == 1, f"staged={rep.staged}")
        st, local = _status(conn, "d/s.mp3")
        check(st == "staged" and Path(local).read_bytes() == good,
              "fallback must stage the by-ID bytes, never the impostor's")


def test_batch_miss_falls_back_to_by_id():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"quota'd in batch"
        stage0.upsert_locations(conn, [_entry("m1", "d/s.mp3", _md5(body), size=len(body))])
        conn.commit()
        rep = stage2.download_batched(
            conn, fetch_batch=_fake_batch({}), fetch=_fake_fetch({"m1": body}), sleep=_no_sleep
        )
        check(rep.staged == 1, "a batch miss must be recovered by the by-ID path")


def test_batch_adopts_concurrently_staged_file():
    """A concurrent sequential run may stage a file between worklist build and batch arrival."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        body = b"already fetched by the other run"
        stage0.upsert_locations(conn, [_entry("m1", "d/s.mp3", _md5(body), size=len(body))])
        conn.commit()
        dest = stage2.staging_path(_md5(body), "m1", "mp3")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        rep = stage2.download_batched(conn, fetch_batch=_fake_batch({}), fallback=False)
        check(rep.already_staged == 1 and rep.staged == 0, "must adopt, not refetch")
        check(_status(conn, "d/s.mp3")[0] == "staged")


def test_batch_from_end_reverses_the_worklist():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        bodies = {f"f{i}": bytes([i]) * 4 for i in range(4)}
        stage0.upsert_locations(
            conn, [_entry(fid, f"d/{fid}.mp3", _md5(b), size=4) for fid, b in bodies.items()]
        )
        conn.commit()
        forward = [r["drive_file_id"] for r in stage2.build_worklist(conn)]
        seen: list[str] = []

        def spy_batch(remote_paths, batch_dir):
            seen.extend(Path(p).stem for p in remote_paths)
            return subprocess.CompletedProcess([], 0, "", "")

        stage2.download_batched(conn, fetch_batch=spy_batch, fallback=False, limit=2, from_end=True)
        check(seen == list(reversed(forward))[:2], f"tail-first order wrong: {seen}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passing")
    raise SystemExit(1 if failed else 0)
