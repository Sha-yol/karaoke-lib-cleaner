"""§13 backup-retention invariants. Pinned:

  * tools/backup_db.sh's local KEEP default matches config.BACKUPS_TO_KEEP — both rotate the
    same directory, so a tighter number in either one silently prunes restore points the other
    intended to keep (live incident 2026-07-26: KEEP=3 vs BACKUPS_TO_KEEP=20 deleted snapshots
    §13 wanted). REMOTE_KEEP is deliberately independent — Drive is off-site DR, not history;
  * pipeline_run(backup=False) takes no snapshot, and the default still does — a dry run has
    no state to restore and must not spend a 450 MB snapshot (or rotate a real one out).

Run: python3 tests/test_backup_retention.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from karaokemp import config, db


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "backup_db.sh"


def _script_default(var: str) -> int:
    """The N in `VAR="${VAR:-N}"`."""
    m = re.search(rf'^{var}="\$\{{{var}:-(\d+)\}}"', SCRIPT.read_text(), re.MULTILINE)
    check(m is not None, f"{var} default not found in {SCRIPT.name}")
    return int(m.group(1))


def test_script_local_keep_matches_config():
    keep = _script_default("KEEP")
    check(keep == config.BACKUPS_TO_KEEP,
          f"backup_db.sh KEEP={keep} but config.BACKUPS_TO_KEEP={config.BACKUPS_TO_KEEP} — "
          "the smaller one wins over the shared backup dir; keep them equal")


def test_script_remote_keep_is_separate_and_no_larger():
    remote = _script_default("REMOTE_KEEP")
    check(remote <= config.BACKUPS_TO_KEEP,
          f"REMOTE_KEEP={remote} exceeds local retention — Drive is DR, not the history")


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
    conn.close()


def _snapshots() -> list[Path]:
    return sorted(config.BACKUP_DIR.glob("library-*.sqlite3"))


def test_pipeline_run_backup_false_writes_no_snapshot():
    with tempfile.TemporaryDirectory() as td:
        _fresh_env(Path(td))
        check(_snapshots() == [], "precondition: no snapshots yet")
        with db.pipeline_run("test_dry", notes="dry-run", backup=False):
            pass
        check(_snapshots() == [], f"backup=False must not snapshot, found {_snapshots()}")
        # the run itself is still recorded — skipping the backup must not skip the bookkeeping
        with db.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM pipeline_runs WHERE stage='test_dry'").fetchone()[0]
        check(n == 1, f"pipeline_runs row still expected, got {n}")


def test_pipeline_run_snapshots_by_default():
    with tempfile.TemporaryDirectory() as td:
        _fresh_env(Path(td))
        with db.pipeline_run("test_real", notes="full"):
            pass
        check(len(_snapshots()) == 1, f"default must snapshot, found {_snapshots()}")


if __name__ == "__main__":
    import sys
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
