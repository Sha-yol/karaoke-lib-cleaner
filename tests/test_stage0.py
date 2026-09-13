"""Stage 0 invariant tests.

§1.3 requires idempotence to be asserted in tests, not asserted in prose. §5.2 requires a
deterministic survivor rule. Both are checked here against synthetic fixtures, so a bug shows
up now rather than after a multi-hour run over 53k files.

Run: python3 -m pytest tests/ -q     (or: python3 tests/test_stage0.py)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from karaokemp import db, stage0


def _fresh_db() -> tuple:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "t.sqlite3"
    conn = db.connect(path)
    conn.executescript(
        (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
    )
    return tmp, conn


def _entry(fid, path, size, md5, name=None):
    return {
        "ID": fid,
        "Path": path,
        "Name": name or Path(path).name,
        "Size": size,
        "Hashes": {"md5": md5} if md5 else {},
    }


def _statuses(conn):
    return {
        r["drive_file_id"]: (r["status"], r["archive_reason"])
        for r in conn.execute("SELECT drive_file_id, status, archive_reason FROM file_locations")
    }


def test_schema_is_strict():
    tmp, conn = _fresh_db()
    try:
        # A STRICT table rejects a non-integer in an INTEGER column instead of coercing it.
        conn.execute(
            "INSERT INTO file_locations (drive_file_id, filetype, size_bytes) VALUES (?,?,?)",
            ("x", "mp3", "not-an-integer"),
        )
    except Exception as exc:
        msg = str(exc).lower()
        assert "cannot store" in msg or "datatype mismatch" in msg, exc
    else:
        raise AssertionError("STRICT not enforced: bogus INTEGER accepted")
    finally:
        conn.close()
        tmp.cleanup()


def test_upsert_is_idempotent():
    tmp, conn = _fresh_db()
    try:
        entries = [
            _entry("idA", "a/one.mp3", 100, "m1"),
            _entry("idB", "b/two.cdg", 200, "m2"),
        ]
        first = stage0.upsert_locations(conn, entries)
        conn.commit()
        assert first["inserted"] == 2

        second = stage0.upsert_locations(conn, entries)
        conn.commit()
        assert second.get("inserted", 0) == 0, "re-enumeration must not duplicate rows"
        assert second["updated"] == 2
        n = conn.execute("SELECT COUNT(*) c FROM file_locations").fetchone()["c"]
        assert n == 2, f"expected 2 rows after re-enumeration, got {n}"
    finally:
        conn.close()
        tmp.cleanup()


def test_upsert_refreshes_path_but_keeps_identity():
    """§5.1: paths are informational and may change; drive_file_id is the identity key."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(conn, [_entry("idA", "old/loc.mp3", 100, "m1")])
        conn.commit()
        stats = stage0.upsert_locations(conn, [_entry("idA", "new/loc.mp3", 100, "m1")])
        conn.commit()
        assert stats["path_changed"] == 1
        row = conn.execute("SELECT remote_path FROM file_locations WHERE drive_file_id='idA'").fetchone()
        assert row["remote_path"] == "new/loc.mp3"
        assert conn.execute("SELECT COUNT(*) c FROM file_locations").fetchone()["c"] == 1
    finally:
        conn.close()
        tmp.cleanup()


def test_dedup_survivor_is_lexicographically_smallest_id():
    """§5.2 deterministic survivor rule."""
    tmp, conn = _fresh_db()
    try:
        # Same md5+size => one content, three copies. Insert in non-sorted order.
        stage0.upsert_locations(
            conn,
            [
                _entry("idZ", "z.mp3", 100, "same"),
                _entry("idA", "a.mp3", 100, "same"),
                _entry("idM", "m.mp3", 100, "same"),
            ],
        )
        conn.commit()
        res = stage0.exact_dup_pass(conn, dry_run=False)
        assert res["duplicate_groups"] == 1
        st = _statuses(conn)
        assert st["idA"] == ("remote_only", None), "smallest ID must survive"
        assert st["idM"] == ("excluded", "exact_dup")
        assert st["idZ"] == ("excluded", "exact_dup")
    finally:
        conn.close()
        tmp.cleanup()


def test_dedup_is_idempotent():
    """§1.3: running a stage twice back-to-back produces zero changes the second time."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn,
            [
                _entry("idZ", "z.mp3", 100, "same"),
                _entry("idA", "a.mp3", 100, "same"),
                _entry("idB", "b.mp3", 999, "other"),
            ],
        )
        conn.commit()
        stage0.exact_dup_pass(conn, dry_run=False)
        before = _statuses(conn)

        second = stage0.exact_dup_pass(conn, dry_run=False)
        after = _statuses(conn)
        assert before == after, "second run changed state"
        assert second["changes_planned"] == 0, (
            f"second run planned {second['changes_planned']} changes; must be 0"
        )
    finally:
        conn.close()
        tmp.cleanup()


def test_dedup_differing_size_is_not_a_duplicate():
    """Group key is (md5, size), not md5 alone."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn,
            [_entry("idA", "a.mp3", 100, "same"), _entry("idB", "b.mp3", 101, "same")],
        )
        conn.commit()
        res = stage0.exact_dup_pass(conn, dry_run=False)
        assert res["duplicate_groups"] == 0
        assert all(v == ("remote_only", None) for v in _statuses(conn).values())
    finally:
        conn.close()
        tmp.cleanup()


def test_dedup_dry_run_writes_nothing():
    """§1.4: every mutating stage has --dry-run that prints without executing."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn, [_entry("idZ", "z.mp3", 100, "same"), _entry("idA", "a.mp3", 100, "same")]
        )
        conn.commit()
        before = _statuses(conn)
        res = stage0.exact_dup_pass(conn, dry_run=True)
        assert res["changes_planned"] == 1
        assert _statuses(conn) == before, "dry run mutated the database"
    finally:
        conn.close()
        tmp.cleanup()


def test_dedup_does_not_touch_later_stage_statuses():
    """Stage 0 owns 'remote_only'/'excluded:exact_dup'. A row already staged/active belongs to a
    later stage and must survive a Stage 0 re-run untouched."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn, [_entry("idZ", "z.mp3", 100, "same"), _entry("idA", "a.mp3", 100, "same")]
        )
        conn.commit()
        conn.execute("UPDATE file_locations SET status='active' WHERE drive_file_id='idZ'")
        conn.commit()
        stage0.exact_dup_pass(conn, dry_run=False)
        st = _statuses(conn)
        assert st["idZ"] == ("active", None), "Stage 0 clobbered a later stage's status"
    finally:
        conn.close()
        tmp.cleanup()


def test_survivor_restored_when_previous_survivor_disappears():
    """If the survivor is deleted from Drive, an excluded copy must be promoted back — a group
    must never end up with every copy excluded."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn, [_entry("idA", "a.mp3", 100, "same"), _entry("idZ", "z.mp3", 100, "same")]
        )
        conn.commit()
        stage0.exact_dup_pass(conn, dry_run=False)
        assert _statuses(conn)["idZ"] == ("excluded", "exact_dup")

        # idA vanishes from Drive; only idZ remains in the group.
        conn.execute("DELETE FROM file_locations WHERE drive_file_id='idA'")
        conn.commit()
        stage0.exact_dup_pass(conn, dry_run=False)
        assert _statuses(conn)["idZ"] == ("remote_only", None), "sole remaining copy left excluded"
    finally:
        conn.close()
        tmp.cleanup()


def test_missing_id_is_counted_not_invented():
    tmp, conn = _fresh_db()
    try:
        stats = stage0.upsert_locations(
            conn, [_entry(None, "x.mp3", 10, "m"), _entry("idA", "a.mp3", 10, "m2")]
        )
        conn.commit()
        assert stats["missing_id"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM file_locations").fetchone()["c"] == 1
    finally:
        conn.close()
        tmp.cleanup()


def test_detect_removals_reports_without_mutating():
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn, [_entry("idA", "a.mp3", 1, "m1"), _entry("idB", "b.mp3", 2, "m2")]
        )
        conn.commit()
        gone = stage0.detect_removals(conn, [_entry("idA", "a.mp3", 1, "m1")])
        assert gone == ["idB"]
        assert _statuses(conn)["idB"] == ("remote_only", None), "removal detection mutated state"
    finally:
        conn.close()
        tmp.cleanup()


def test_filename_sample_is_deterministic():
    """§5.3's 200 random filenames must be reproducible, or two reviews see two reports."""
    tmp, conn = _fresh_db()
    try:
        stage0.upsert_locations(
            conn, [_entry(f"id{i:04d}", f"d/f{i}.mp3", i + 1, f"m{i}") for i in range(500)]
        )
        conn.commit()
        a = stage0.inventory_report(conn)["filename_sample"]
        b = stage0.inventory_report(conn)["filename_sample"]
        assert a == b, "filename sample is not reproducible across runs"
        assert len(a) == 200
    finally:
        conn.close()
        tmp.cleanup()


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
