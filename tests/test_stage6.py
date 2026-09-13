"""Stage 6 §9.2 organize + fsck tests.

Three things here are worth more than the rest, because they are the ones that would ruin a
433 GB run and are invisible in tidy fixtures:

  * `test_shared_cdg_gets_a_hardlink_not_a_second_move` — 742 blobs in the live index (all
    `.cdg`) belong to more than one active item. Whichever item moves first "wins" the file;
    a naive second rename would leave the other item's directory missing its graphics, i.e.
    a karaoke track with no lyrics, 821 times over.
  * `test_rerun_is_a_noop` — §13 idempotency. A second run must move nothing.
  * `test_interrupted_run_adopts_what_is_already_there` — the crash window is between the
    rename and the commit. On resume the index still points at staging while the bytes are
    already at the destination; the pass must adopt, not overwrite or fail.

Run: python3 -m pytest tests/test_stage6.py -q   (or: python3 tests/test_stage6.py)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from karaokemp import config, db, stage6


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


# --- naming (pure, no DB) ------------------------------------------------------------------

def test_shard_strips_leading_article_but_the_name_keeps_it():
    check(stage6.shard_key("The Beatles") == "B", "leading 'The' must not file under T")
    check(stage6.shard_key("A Tribe Called Quest") == "T")
    check(stage6.shard_key("Theatre Of Tragedy") == "T", "'Theatre' is not the article 'The'")
    check(stage6.shard_key("thelma") == "T")
    name, _ = stage6.item_dirname(7, "The Beatles", "Help!", None)
    check(name.startswith("The Beatles"), "the DISPLAY name keeps the article")


def test_shard_buckets():
    check(stage6.shard_key("שלמה ארצי") == "ש")
    check(stage6.shard_key("2Pac") == "0-9")
    check(stage6.shard_key("Édith Piaf") == stage6.SHARD_OTHER, "non-ASCII latin -> _other")
    check(stage6.shard_key("") == stage6.SHARD_UNNAMED)
    check(stage6.shard_key(None) == stage6.SHARD_UNNAMED)


def test_sanitize_removes_bidi_and_unsafe_chars():
    # A real shape from this library: an RTL mark riding along inside a Hebrew title.
    check("‏" not in stage6.sanitize_component("‏שיר"), "bidi mark survived")
    check(stage6.sanitize_component('AC/DC') == "ACDC")
    check(stage6.sanitize_component('a: b?  c*') == "a b c")
    check(stage6.sanitize_component("trailing. ") == "trailing")
    check(stage6.sanitize_component("CON") == "_CON", "windows device name")


def test_dirname_falls_back_the_way_9_2_says():
    check(stage6.item_dirname(1, "A", "B", "raw")[0] == "A - B [1]")
    check(stage6.item_dirname(2, None, "B", "raw") == ("B [2]", "title_only"))
    check(stage6.item_dirname(3, None, None, "DK26-13")[0] == "DK26-13 [3]")
    check(stage6.item_dirname(4, None, None, None)[1] == "none")


def test_long_names_stay_under_the_filesystem_limit():
    name, _ = stage6.item_dirname(99, "א" * 300, "ב" * 300, None)
    check(len(name.encode("utf-8")) <= 255, f"{len(name.encode('utf-8'))} bytes")
    check(name.endswith(" [99]"), "the id must survive truncation — it is the join key")


# --- planning + execution (real DB, real filesystem) ---------------------------------------

_CONFIG_PATHS = ("LIBRARY_ROOT", "ACTIVE_DIR", "ARCHIVE_DIR", "STAGING_DIR", "LOGS_DIR",
                 "DB_DIR", "DB_PATH")


def _lib(monkey_root: Path) -> dict:
    """Point config at a throwaway library root, returning the originals to restore.

    These are module globals, and pytest runs the whole suite in ONE process — leaving them
    pointed at a deleted temp directory would break every later test module that reads a
    config path, in a way that looks like a failure in *that* module.
    """
    saved = {k: getattr(config, k) for k in _CONFIG_PATHS}
    config.LIBRARY_ROOT = monkey_root
    config.ACTIVE_DIR = monkey_root / "active"
    config.ARCHIVE_DIR = monkey_root / "archive"
    config.STAGING_DIR = monkey_root / "staging"
    config.LOGS_DIR = monkey_root / "logs"
    config.DB_DIR = monkey_root / "db"
    config.DB_PATH = monkey_root / "db" / "library.sqlite3"
    return saved


class Fixture:
    """A tiny library: one video item, one mp3g pair, one shared cdg, one broken video."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = _lib(self.root)
        (self.root / "db").mkdir(parents=True, exist_ok=True)
        (self.root / "staging" / "00").mkdir(parents=True, exist_ok=True)
        self.conn = db.connect(config.DB_PATH)
        self.conn.executescript(
            (Path(__file__).resolve().parent.parent / "schema.sql").read_text())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
        for k, v in self._saved.items():
            setattr(config, k, v)
        self.tmp.cleanup()

    def blob(self, h: str, data: bytes, ext: str, integrity: str = "decoded_ok") -> Path:
        p = self.root / "staging" / "00" / f"{h}{ext}"
        p.write_bytes(data)
        self.conn.execute(
            "INSERT INTO blobs (content_hash, size_bytes, integrity_status, first_hashed_at) "
            "VALUES (?,?,?,?)", (h, len(data), integrity, db.utcnow()))
        self.conn.execute(
            "INSERT INTO file_locations (drive_file_id, remote_path, local_path, filetype, "
            "size_bytes, content_hash, status, first_seen_at) VALUES (?,?,?,?,?,?, 'staged', ?)",
            (h, f"orig/{h}{ext}", str(p), ext.lstrip("."), len(data), h, db.utcnow()))
        return p

    def item(self, item_id: int, fmt: str, files: list[tuple[str, str]],
             artist: str | None, title: str | None, cluster: int | None = None) -> None:
        if cluster is not None:
            self.conn.execute(
                "INSERT OR IGNORE INTO clusters (id, method) VALUES (?, 'title_match')",
                (cluster,))
        self.conn.execute(
            "INSERT INTO media_items (id, format, cluster_id, status, created_at) "
            "VALUES (?,?,?, 'active', ?)", (item_id, fmt, cluster, db.utcnow()))
        for role, h in files:
            self.conn.execute(
                "INSERT INTO media_item_files (media_item_id, content_hash, role) "
                "VALUES (?,?,?)", (item_id, h, role))
        for field, value in (("artist", artist), ("title", title)):
            if value:
                self.conn.execute(
                    "INSERT INTO song_metadata (media_item_id, field, value, source, "
                    "confidence, updated_at) VALUES (?,?,?, 'filename', 0.9, ?)",
                    (item_id, field, value, db.utcnow()))
        self.conn.commit()


def _standard_fixture() -> Fixture:
    fx = Fixture()
    fx.blob("aaa", b"video-bytes", ".mp4")
    fx.blob("bbb", b"audio-bytes", ".mp3")
    fx.blob("ccc", b"graphics", ".cdg")          # shared by items 2 and 3
    fx.blob("ddd", b"audio2", ".mp3")
    fx.blob("eee", b"broken-video", ".mp4", integrity="broken")
    fx.item(1, "video", [("av", "aaa")], "The Beatles", "Help!", cluster=10)
    fx.item(2, "mp3g", [("audio", "bbb"), ("graphics", "ccc")], "שלמה ארצי", "ירושלים", cluster=11)
    fx.item(3, "mp3g", [("audio", "ddd"), ("graphics", "ccc")], "Sinatra, Frank", "My Way",
            cluster=12)
    fx.item(4, "video", [("av", "eee")], "Broken", "Thing", cluster=13)
    return fx


def test_plan_places_items_by_shard_and_id():
    fx = _standard_fixture()
    try:
        plans, orphans, stats = stage6.build_plan(fx.conn)
        by_id = {p.item_id: p for p in plans}
        check(by_id[1].shard == "B", f"article strip failed: {by_id[1].shard}")
        check(by_id[1].dirname == "The Beatles - Help! [1]", by_id[1].dirname)
        check(by_id[2].shard == "ש", by_id[2].shard)
        check(by_id[4].archive_reason == "broken", "broken defining blob must archive")
        check("archive" in str(by_id[4].dest_dir))
        check(stats["items_archived_broken"] == 1)
        check(stats["items_to_active"] == 3)
    finally:
        fx.close()


def test_shared_cdg_gets_a_hardlink_not_a_second_move():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False)
        d2 = config.ACTIVE_DIR / "ש" / "שלמה ארצי - ירושלים [2]"
        d3 = config.ACTIVE_DIR / "S" / "Sinatra, Frank - My Way [3]"
        cdg2 = d2 / "שלמה ארצי - ירושלים [2].cdg"
        cdg3 = d3 / "Sinatra, Frank - My Way [3].cdg"
        check(cdg2.exists(), "item 2 lost its graphics")
        check(cdg3.exists(), "item 3 lost its graphics — the 821-copy bug")
        check(cdg2.stat().st_ino == cdg3.stat().st_ino, "must be a hardlink, not a copy")
        # The hardlink is a real second physical location and gets its own index row.
        n = fx.conn.execute(
            "SELECT COUNT(*) FROM file_locations WHERE content_hash='ccc' "
            "AND local_path IS NOT NULL").fetchone()[0]
        check(n == 2, f"expected 2 locations for the shared blob, got {n}")
    finally:
        fx.close()


def test_files_land_with_the_directory_name_as_stem():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False)
        d = config.ACTIVE_DIR / "B" / "The Beatles - Help! [1]"
        check((d / "The Beatles - Help! [1].mp4").exists(), sorted(str(x) for x in d.iterdir()))
    finally:
        fx.close()


def test_broken_item_is_archived_and_marked():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False)
        check((config.ARCHIVE_DIR / "broken" / "Broken - Thing [4]"
               / "Broken - Thing [4].mp4").exists())
        status = fx.conn.execute("SELECT status FROM media_items WHERE id=4").fetchone()[0]
        check(status == "archived", status)
    finally:
        fx.close()


def test_keep_broken_active_opts_out():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False, archive_broken=False)
        status = fx.conn.execute("SELECT status FROM media_items WHERE id=4").fetchone()[0]
        check(status == "active", status)
        check((config.ACTIVE_DIR / "B" / "Broken - Thing [4]"
               / "Broken - Thing [4].mp4").exists())
    finally:
        fx.close()


def test_orphan_staged_file_is_archived():
    fx = _standard_fixture()
    try:
        fx.blob("fff", b"stray", ".cdg")   # referenced by no media_item
        fx.conn.commit()
        rep = stage6.organize(dry_run=False)
        check(rep["orphans_moved"] == 1, rep)
        check((config.ARCHIVE_DIR / "orphans" / "cdg" / "fff.cdg").exists())
        row = fx.conn.execute(
            "SELECT status, archive_reason FROM file_locations WHERE content_hash='fff'"
        ).fetchone()
        check(tuple(row) == ("archived", "orphan"), tuple(row))
    finally:
        fx.close()


def test_orphan_whose_staged_file_vanished_is_not_recorded_at_a_path_it_never_reached():
    """The live-run bug (2026-08-27): the orphan loop moved the file only `if src.exists()`
    but updated `local_path` to the destination UNCONDITIONALLY. 125 rows ended up naming a
    file that had never been created, and fsck failed on every one of them, permanently.

    The honest record for a staged file that is already gone is local_path=NULL. The Drive
    provenance survives on the same row, so the content is still recoverable."""
    fx = _standard_fixture()
    try:
        p = fx.blob("ggg", b"interrupted download", ".part")   # orphan: no media_item
        fx.conn.commit()
        p.unlink()                                             # ...and it is not on disk
        rep = stage6.organize(dry_run=False)
        check(rep["orphans_absent"] == 1, rep)
        check(rep["orphans_moved"] == 0, rep)
        row = fx.conn.execute(
            "SELECT local_path, status, archive_reason, drive_file_id "
            "FROM file_locations WHERE content_hash='ggg'").fetchone()
        check(row["local_path"] is None, f"pointed at a phantom file: {row['local_path']}")
        check(row["archive_reason"] == "orphan_missing", row["archive_reason"])
        check(row["drive_file_id"] == "ggg", "Drive provenance must survive — it is the way back")
        check(stage6.fsck()["ok"], "fsck must stay green")
    finally:
        fx.close()


def test_dry_run_moves_nothing_and_writes_a_full_plan():
    fx = _standard_fixture()
    try:
        rep = stage6.organize(dry_run=True)
        check(not config.ACTIVE_DIR.exists() or not any(config.ACTIVE_DIR.rglob("*.mp4")),
              "dry run touched the filesystem")
        staged = fx.conn.execute(
            "SELECT COUNT(*) FROM file_locations WHERE status='staged'").fetchone()[0]
        check(staged == 5, f"dry run mutated the index ({staged} still staged)")
        lines = Path(rep["plan_file"]).read_text(encoding="utf-8").splitlines()
        check(len(lines) == 1 + 6, f"one header + one line per file, got {len(lines)}")
    finally:
        fx.close()


def test_rerun_is_a_noop():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False)
        rep = stage6.organize(dry_run=False)
        actions = rep["actions"]
        check(set(actions) <= {"noop"}, f"second run was not idempotent: {actions}")
        check(rep.get("failed", 0) == 0)
    finally:
        fx.close()


def test_interrupted_run_adopts_what_is_already_there():
    """The crash window: bytes moved, commit never happened. Resume must adopt the file."""
    fx = _standard_fixture()
    try:
        plans, _orph, _s = stage6.build_plan(fx.conn)
        p1 = next(p for p in plans if p.item_id == 1)
        fp = p1.files[0]
        fp.dst.parent.mkdir(parents=True, exist_ok=True)
        fp.src.rename(fp.dst)                    # moved, but the index still says staging
        rep = stage6.organize(dry_run=False)
        check(rep.get("failed", 0) == 0, rep.get("errors"))
        check(fp.dst.exists(), "adopted file vanished")
        row = fx.conn.execute(
            "SELECT local_path, status FROM file_locations WHERE content_hash='aaa'").fetchone()
        check(row["local_path"] == str(fp.dst), row["local_path"])
        check(row["status"] == "active", row["status"])
    finally:
        fx.close()


def test_fsck_passes_after_organize_and_catches_a_deleted_file():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False)
        rep = stage6.fsck()
        check(rep["ok"], rep["counts"])
        victim = config.ACTIVE_DIR / "B" / "The Beatles - Help! [1]" / "The Beatles - Help! [1].mp4"
        victim.unlink()
        rep = stage6.fsck()
        check(not rep["ok"], "fsck missed a deleted file")
        check(rep["counts"]["missing_files"] == 1, rep["counts"])
    finally:
        fx.close()


def test_fsck_catches_a_stray_file():
    fx = _standard_fixture()
    try:
        stage6.organize(dry_run=False)
        (config.ACTIVE_DIR / "B" / "stray.txt").write_text("not in the index")
        rep = stage6.fsck()
        check(rep["counts"]["strays"] == 1, rep["counts"])
    finally:
        fx.close()


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
