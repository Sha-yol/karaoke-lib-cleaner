"""Stage 0 — remote inventory + logical exact-dup (spec §5).

Needs local bytes: no. Mutates filesystem: no. Mutates Drive: NEVER (§0).

Everything here is index-only. "Excluding" a duplicate means writing a status to our own
SQLite row; nothing on Drive moves, and the authorized token is scope=drive.readonly so it
could not move even if this code were wrong.
"""

from __future__ import annotations

import json
import random
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from . import config, db

# Deterministic sample seed: the §5.3 report's "200 random filenames" must be reproducible,
# or reviewing it twice means reviewing two different reports.
SAMPLE_SEED = 20260714
SAMPLE_SIZE = 200


# --- enumeration -------------------------------------------------------------------------


def enumerate_drive(dest: Path | None = None, timeout: int = 7200) -> Path:
    """Run `rclone lsjson -R` over the pinned folder and dump raw JSON to disk.

    The dump is written before any parsing so that a parse bug never costs a re-fetch of 53k
    files. Returns the dump path.
    """
    config.require_drive_folder_id()
    config.ensure_layout()
    dest = dest or config.ENUM_DIR / f"lsjson-{db.utcnow().replace(':', '')}.json"
    cmd = [
        "rclone",
        "lsjson",
        f"{config.RCLONE_REMOTE}:",
        *config.RCLONE_BASE_FLAGS,
        "--recursive",
        "--files-only",
        "--hash",
    ]
    with dest.open("wb") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"rclone lsjson failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[:2000]}"
        )
    return dest


def load_enumeration(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())


def filetype_of(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix or "none"


def upsert_locations(conn, entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Insert/update file_locations keyed on drive_file_id (§5.1).

    Upsert, not insert: re-enumeration refreshes paths and is safe to run repeatedly.
    Only remote-facing fields are touched — status/content_hash/local_path belong to later
    stages and must survive a re-enumeration untouched.
    """
    now = db.utcnow()
    stats = Counter()
    for e in entries:
        fid = e.get("ID")
        if not fid:
            # drive_file_id is the remote identity key; a row without one cannot be
            # reconciled later. Count and skip rather than invent an identity.
            stats["missing_id"] += 1
            continue
        md5 = (e.get("Hashes") or {}).get("md5")
        row = conn.execute(
            "SELECT id, remote_path FROM file_locations WHERE drive_file_id = ?", (fid,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO file_locations "
                "(drive_file_id, remote_path, filetype, size_bytes, gdrive_md5, status, "
                " first_seen_at, updated_at) VALUES (?,?,?,?,?,'remote_only',?,?)",
                (fid, e.get("Path"), filetype_of(e.get("Name", "")), e.get("Size"), md5, now, now),
            )
            stats["inserted"] += 1
        else:
            conn.execute(
                "UPDATE file_locations SET remote_path=?, filetype=?, size_bytes=?, "
                "gdrive_md5=?, updated_at=? WHERE id=?",
                (
                    e.get("Path"),
                    filetype_of(e.get("Name", "")),
                    e.get("Size"),
                    md5,
                    now,
                    row["id"],
                ),
            )
            stats["updated"] += 1
            if row["remote_path"] != e.get("Path"):
                stats["path_changed"] += 1
    return dict(stats)


def detect_removals(conn, entries: Iterable[dict[str, Any]]) -> list[str]:
    """Drive file IDs we have indexed but that no longer appear in the enumeration.

    Reported, never auto-mutated: §3.2 has no 'removed' status, and inventing one to silently
    retire rows would be exactly the kind of destructive guess §1.2 forbids. Surfacing them
    lets a human decide.
    """
    seen = {e["ID"] for e in entries if e.get("ID")}
    known = {
        r["drive_file_id"]
        for r in conn.execute(
            "SELECT drive_file_id FROM file_locations WHERE drive_file_id IS NOT NULL"
        )
    }
    return sorted(known - seen)


# --- §5.2 logical exact-dup pass ---------------------------------------------------------


def exact_dup_pass(conn, dry_run: bool = False) -> dict[str, Any]:
    """Group by (gdrive_md5, size_bytes); keep one survivor, exclude the rest.

    Deterministic survivor rule (§5.2): lexicographically smallest drive_file_id. Content-
    independent and re-run-safe, so re-runs choose identically.

    Idempotent (§1.3): the desired state is recomputed from scratch as a pure function of the
    group's contents, then diffed against actual state. Re-running writes only the diff, which
    is empty on an unchanged library.

    Singleton groups are ranked too, not filtered out. That matters: if a survivor is deleted
    from Drive, its group collapses to one member, and that member must be *restored* to
    remote_only. Filtering to groups of >1 would strand the last remaining copy as `excluded`
    forever, so it would never be downloaded — silently losing the song, which is exactly what
    §1.2 forbids.

    Only rows owned by Stage 0 ('remote_only', or 'excluded' for 'exact_dup') are touched;
    statuses set by later stages (staged/active/archived) and exclusions with other reasons are
    left alone.
    """
    rows = conn.execute(
        """
        SELECT id, drive_file_id, remote_path, status, archive_reason, gdrive_md5, size_bytes,
               ROW_NUMBER() OVER (
                   PARTITION BY gdrive_md5, size_bytes
                   ORDER BY drive_file_id ASC
               ) AS rn,
               COUNT(*) OVER (PARTITION BY gdrive_md5, size_bytes) AS group_size
        FROM file_locations
        WHERE gdrive_md5 IS NOT NULL
          AND status IN ('remote_only','excluded')
          AND (archive_reason IS NULL OR archive_reason = 'exact_dup')
        """
    ).fetchall()

    planned: list[tuple[str, str, str]] = []  # (action, drive_file_id, remote_path)
    now = db.utcnow()
    dup_groups: set[tuple] = set()
    excluded_total = 0

    for r in rows:
        is_survivor = r["rn"] == 1
        if r["group_size"] > 1:
            dup_groups.add((r["gdrive_md5"], r["size_bytes"]))
        if not is_survivor:
            excluded_total += 1

        want = ("remote_only", None) if is_survivor else ("excluded", "exact_dup")
        have = (r["status"], r["archive_reason"])
        if want == have:
            continue  # already correct; a re-run makes no change here (§1.3)

        action = "restore_survivor" if is_survivor else "exclude"
        planned.append((action, r["drive_file_id"], r["remote_path"]))
        if not dry_run:
            conn.execute(
                "UPDATE file_locations SET status=?, archive_reason=?, updated_at=? WHERE id=?",
                (want[0], want[1], now, r["id"]),
            )

    if not dry_run:
        conn.commit()

    return {
        "duplicate_groups": len(dup_groups),
        "locations_excluded": excluded_total,
        "changes_planned": len(planned),
        "plan": planned if dry_run else planned[:50],
    }


# --- §5.3 inventory report ---------------------------------------------------------------

# Extension → coarse class, following the breakdown established in docs/history/investigation-summary.md.
VIDEO_EXT = {"mp4", "avi", "mpg", "vob", "mpeg", "wmv", "dat", "mkv", "webm", "mov", "flv"}
AUDIO_EXT = {"mp3", "flac", "wav", "m4a", "wma", "ogg"}
GRAPHICS_EXT = {"cdg"}
CONTAINER_EXT = {"zip", "rar", "7z"}


def _klass(ft: str) -> str:
    if ft in VIDEO_EXT:
        return "video"
    if ft in AUDIO_EXT:
        return "audio"
    if ft in GRAPHICS_EXT:
        return "graphics"
    if ft in CONTAINER_EXT:
        return "container"
    return "other"


def inventory_report(conn) -> dict[str, Any]:
    """Build the §5.3 report. This is a phase gate: review it before finalizing Stage 1–3 code."""
    rows = conn.execute(
        "SELECT drive_file_id, remote_path, filetype, size_bytes, gdrive_md5, status, "
        "archive_reason FROM file_locations WHERE drive_file_id IS NOT NULL"
    ).fetchall()

    total_files = len(rows)
    total_bytes = sum(r["size_bytes"] or 0 for r in rows)

    by_type = Counter()
    bytes_by_type = Counter()
    by_class = Counter()
    bytes_by_class = Counter()
    for r in rows:
        ft = r["filetype"]
        by_type[ft] += 1
        bytes_by_type[ft] += r["size_bytes"] or 0
        k = _klass(ft)
        by_class[k] += 1
        bytes_by_class[k] += r["size_bytes"] or 0

    # Exact-dup rate, and the number that actually matters: bytes we now never download.
    excluded = [r for r in rows if r["archive_reason"] == "exact_dup"]
    excluded_bytes = sum(r["size_bytes"] or 0 for r in excluded)

    no_md5 = sum(1 for r in rows if not r["gdrive_md5"])

    # Duplicate basenames (Drive allows them; §6.3 routes collisions to review rather than guessing)
    basenames = Counter(Path(r["remote_path"] or "").name.lower() for r in rows)
    dup_basenames = {n: c for n, c in basenames.items() if c > 1}

    # Zip prevalence (§5.3). Member counts need the zips' central directories, which needs
    # bytes — out of scope for Stage 0, so this is a count and a size, not an estimate.
    zips = [r for r in rows if r["filetype"] in CONTAINER_EXT]

    # MP3+CDG provisional pairing signal, visible in the remote listing with no downloads.
    stems: dict[str, set[str]] = {}
    for r in rows:
        p = Path(r["remote_path"] or "")
        if r["filetype"] in ("mp3", "cdg"):
            stems.setdefault(str(p.parent) + "/" + p.stem.lower(), set()).add(r["filetype"])
    paired = sum(1 for v in stems.values() if v == {"mp3", "cdg"})
    orphan_mp3 = sum(1 for v in stems.values() if v == {"mp3"})
    orphan_cdg = sum(1 for v in stems.values() if v == {"cdg"})

    sizes = sorted(r["size_bytes"] or 0 for r in rows)

    def pct(p: float) -> int:
        if not sizes:
            return 0
        return sizes[min(int(len(sizes) * p), len(sizes) - 1)]

    rng = random.Random(SAMPLE_SEED)
    pool = [r["remote_path"] for r in rows if r["remote_path"]]
    sample = rng.sample(pool, min(SAMPLE_SIZE, len(pool)))

    return {
        "totals": {
            "files": total_files,
            "bytes": total_bytes,
            "gib": round(total_bytes / 1024**3, 1),
        },
        "by_class": {
            k: {"files": by_class[k], "gib": round(bytes_by_class[k] / 1024**3, 1)}
            for k in sorted(by_class, key=lambda x: -by_class[x])
        },
        "by_filetype": {
            ft: {"files": by_type[ft], "gib": round(bytes_by_type[ft] / 1024**3, 1)}
            for ft in sorted(by_type, key=lambda x: -by_type[x])
        },
        "exact_dup": {
            "locations_excluded": len(excluded),
            "rate_pct": round(100 * len(excluded) / total_files, 2) if total_files else 0,
            "bytes_saved": excluded_bytes,
            "gib_saved": round(excluded_bytes / 1024**3, 1),
            "download_gib_after_dedup": round((total_bytes - excluded_bytes) / 1024**3, 1),
        },
        "md5_coverage": {
            "missing_md5": no_md5,
            "pct_with_md5": round(100 * (total_files - no_md5) / total_files, 2)
            if total_files
            else 0,
        },
        "duplicate_basenames": {
            "distinct_names_colliding": len(dup_basenames),
            "files_involved": sum(dup_basenames.values()),
            "worst": sorted(dup_basenames.items(), key=lambda kv: -kv[1])[:10],
        },
        "zips": {
            "count": len(zips),
            "gib": round(sum(z["size_bytes"] or 0 for z in zips) / 1024**3, 1),
            "note": "member counts require reading central directories (needs bytes; not Stage 0)",
        },
        "mp3g_pairing_provisional": {
            "paired": paired,
            "orphan_mp3": orphan_mp3,
            "orphan_cdg": orphan_cdg,
        },
        "size_distribution_bytes": {
            "min": sizes[0] if sizes else 0,
            "p50": pct(0.50),
            "p90": pct(0.90),
            "p99": pct(0.99),
            "max": sizes[-1] if sizes else 0,
        },
        "filename_sample": sample,
    }
