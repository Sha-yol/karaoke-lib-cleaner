"""Database access, backups, and run bookkeeping.

The SQLite index is the source of truth (§1.1). Filesystem layout is a convenience.
"""

from __future__ import annotations

import datetime as _dt
import json
import socket
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config


def utcnow() -> str:
    """ISO-8601 UTC, as §3 requires for every timestamp."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def host() -> str:
    return socket.gethostname()


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Stage 2's downloader holds a connection for days and commits per file. Without a busy
    # timeout, any other writer (review resolutions, a later stage) fails instantly with
    # "database is locked" the moment a commit overlaps; WAL still permits only one writer.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: Path | None = None) -> Path:
    """Create the layout and apply the schema. Idempotent (schema is all IF NOT EXISTS)."""
    config.ensure_layout()
    target = path or config.DB_PATH
    with connect(target) as conn:
        conn.executescript(config.SCHEMA_PATH.read_text())
        _migrate(conn)
    return target


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place migrations for DBs created before a schema change (§10).

    `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a change to a column CHECK
    (SQLite cannot ALTER a CHECK) must rebuild the table. Only cluster_edges needs this so far:
    the §10 review tooling adds edge_type 'manual'. Idempotent — it rebuilds only when the live
    constraint is missing 'manual', and is a no-op on any DB freshly created from schema.sql.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cluster_edges'"
    ).fetchone()
    if row and row["sql"] and "'manual'" not in row["sql"]:
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE cluster_edges__new (
                item_a     INTEGER NOT NULL REFERENCES media_items(id),
                item_b     INTEGER NOT NULL REFERENCES media_items(id),
                edge_type  TEXT NOT NULL CHECK (edge_type IN
                               ('fingerprint','prefix_fingerprint','title_match','manual')),
                similarity REAL,
                PRIMARY KEY (item_a, item_b, edge_type)
            ) STRICT;
            INSERT INTO cluster_edges__new SELECT item_a, item_b, edge_type, similarity
                FROM cluster_edges;
            DROP TABLE cluster_edges;
            ALTER TABLE cluster_edges__new RENAME TO cluster_edges;
            PRAGMA foreign_keys = ON;
            """
        )
        conn.commit()


def backup_db(path: Path | None = None) -> Path | None:
    """Back up before a stage run (§13).

    Uses sqlite3's .backup API, never a raw file copy: copying a live WAL database can capture
    a torn snapshot. Keeps BACKUPS_TO_KEEP rotating, newest first.
    """
    src = path or config.DB_PATH
    if not src.exists():
        return None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = config.BACKUP_DIR / f"library-{stamp}.sqlite3"

    source = connect(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    backups = sorted(config.BACKUP_DIR.glob("library-*.sqlite3"), reverse=True)
    for stale in backups[config.BACKUPS_TO_KEEP :]:
        stale.unlink()
    return dest


@contextmanager
def pipeline_run(stage: str, notes: str | None = None,
                 backup: bool = True) -> Iterator[dict[str, Any]]:
    """Record a row in pipeline_runs (§3.8), backing the DB up first (§13).

    Yields a mutable dict; set 'report', 'items_processed', 'items_failed' on it. The row is
    written even if the body raises, so a crashed run leaves a trace rather than a silence.

    `backup=False` skips the §13 snapshot. Pass it ONLY for a body that writes nothing — a
    dry run has no state to restore, and snapshotting the live DB for one costs minutes and
    450 MB, which also rotates a real pre-change restore point out of the keep window.
    """
    if backup:
        backup_db()
    state: dict[str, Any] = {"report": {}, "items_processed": 0, "items_failed": 0}
    started = utcnow()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (stage, started_at, host, tool_versions, notes) "
            "VALUES (?,?,?,?,?)",
            (stage, started, host(), json.dumps(config.tool_versions()), notes),
        )
        run_id = cur.lastrowid
        conn.commit()
        state["run_id"] = run_id
        try:
            yield state
        finally:
            conn.execute(
                "UPDATE pipeline_runs SET finished_at=?, items_processed=?, items_failed=?, "
                "report=? WHERE id=?",
                (
                    utcnow(),
                    state.get("items_processed", 0),
                    state.get("items_failed", 0),
                    json.dumps(state.get("report", {}), ensure_ascii=False),
                    run_id,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def review_queue_sizes(conn: sqlite3.Connection) -> dict[str, int]:
    """Open review-queue size per kind. §11 requires every run report to carry these, so the
    operator sees the review burden *before* committing to it."""
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM review_queue WHERE resolution IS NULL GROUP BY kind"
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}
