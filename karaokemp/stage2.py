"""Stage 2 — Download (spec §7.1).

Download every non-`excluded` `file_locations` row to `staging/`, keyed by `drive_file_id`
(paths collide on Drive — IDs do not, which is the whole reason §5.1 demotes paths). After each
transfer, verify the bytes against the Drive-reported MD5 (`gdrive_md5`, 100% populated per §5.3);
a mismatch is a corrupt transfer and is retried, never trusted.

Design constraints from §7.1 / the handoff, each load-bearing:

* **Ctrl-C-safe and resumable at any point.** The run spans days. Each file is fetched to a
  `.part` temp, verified, then atomically renamed into place *and* committed in one step. An
  interrupt therefore leaves a row either fully `staged` or still `remote_only` — never a
  half-written staging file masquerading as done. Resume is just "re-run": the worklist is
  recomputed from `status='remote_only'`, so completed files simply drop out.

* **Per-file quota/rate errors are common on shared content.** Exponential backoff retries a file
  inline a few times; if it still fails (Drive download-quota on a shared file can persist for
  hours) the file is *deferred* — left `remote_only`, reported, and picked up by a later re-run
  once quota resets. One stubborn file never blocks the other 51k.

* **Fetch both halves of a pair together** (§6.3's stated purpose): the worklist is ordered so a
  provisional pair's mp3 and cdg are adjacent.

* **Zips: none exist (§5.3 scope cut).** Zip members / extraction are deliberately not built.

The by-ID fetch uses `rclone backend copyid`, which resolves a file by its global Drive ID
regardless of path — verified against a real shared file before this module was written. The
read-only OAuth scope (§0) means no flag here can mutate the shared folder.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config
from .db import utcnow

# --- tunables (kept here, not in config, because they only matter to this stage) -------------

# rclone flags for the by-ID copy. The root-folder pin (config.RCLONE_BASE_FLAGS) is deliberately
# NOT used: copyid resolves a *global* file ID and the pin only confuses it. --drive-acknowledge-
# abuse lets us fetch shared files Drive has flagged (readonly scope still can't write). We do our
# own outer retry loop, so rclone's own --retries is pinned to 1.
RCLONE_COPYID_FLAGS = [
    "--drive-acknowledge-abuse",
    "--low-level-retries", "10",
    "--retries", "1",
    "--timeout", "300s",
]

MAX_ATTEMPTS = 5          # inline attempts per file before deferring it to a later re-run
BACKOFF_BASE_SEC = 5.0    # first backoff; doubles each attempt
BACKOFF_CAP_SEC = 300.0   # ceiling on a single backoff sleep
MIN_FREE_BYTES = 15 * 2**30  # stop the run gracefully below this much free disk (resumable)
MD5_CHUNK = 1024 * 1024

# Outcome tags for a single file.
STAGED = "staged"
ALREADY = "already_staged"
VERIFY_FAILED = "verify_failed"
DEFERRED = "deferred"


# --- pure helpers (unit-tested without touching Drive) --------------------------------------


def ext_of(filetype: str | None) -> str:
    """Extension for the staging filename, or '' when there isn't a clean one.

    `file_locations.filetype` is the lowercased extension from §5.1, but it also holds the
    literal 'none' for extensionless files and occasional garbage from dotted Hebrew names. The
    staging name is cosmetic (the DB's `local_path` is the real handle and Stage 3 sniffs content),
    so anything that isn't a short alnum token becomes no-extension rather than a junk suffix.
    """
    ft = (filetype or "").strip().lower()
    if ft and ft != "none" and re.fullmatch(r"[a-z0-9]{1,5}", ft):
        return ft
    return ""


def staging_path(gdrive_md5: str, drive_file_id: str, filetype: str | None) -> Path:
    """Deterministic staging location for a row: `staging/<md5[:2]>/<file_id>[.<ext>]`.

    Sharded by the md5 prefix (uniformly distributed, §5.3) so no single directory holds 51k
    entries. Fully derived from the row, so resume/skip needs no extra bookkeeping.
    """
    shard = (gdrive_md5 or "00")[:2]
    ext = ext_of(filetype)
    name = f"{drive_file_id}.{ext}" if ext else drive_file_id
    return config.STAGING_DIR / shard / name


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(MD5_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def free_bytes() -> int:
    return shutil.disk_usage(config.STAGING_DIR).free


def pair_order_map(conn) -> dict[str, str]:
    """md5 -> a stable token shared by both halves of its provisional pair.

    Lets the worklist sort so a pair's mp3 and cdg are adjacent (§6.3). A cdg may pair with
    several mp3s (759 cases — see PROGRESS); it is keyed to the smallest audio md5 it pairs with,
    deterministically. md5s in no pair are absent (they sort on their own id).
    """
    token: dict[str, str] = {}
    for r in conn.execute("SELECT audio_md5, graphics_md5 FROM provisional_pairs"):
        a, g = r["audio_md5"], r["graphics_md5"]
        token[a] = a
        # a cdg reused across audio variants keys to the smallest audio md5 (stable)
        if g not in token or a < token[g]:
            token[g] = a
    return token


# --- rclone fetch (the one impure edge; injectable for tests) -------------------------------


def _rclone_copyid(drive_file_id: str, dest: Path) -> subprocess.CompletedProcess:
    """Fetch one file by Drive ID to an exact destination path. Never raises on rclone failure —
    the caller inspects returncode/stderr to classify quota vs. hard error."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rclone", "backend", "copyid",
        f"{config.RCLONE_REMOTE}:", drive_file_id, str(dest),
        *RCLONE_COPYID_FLAGS,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# --- batched fetch (bulk path-keyed transfer; md5 stays the only trust anchor) ---------------
#
# The per-file copyid path spawns one rclone process (plus its API round-trips) per file, which
# measured out at 12-51 s/file on the live run — per-file overhead, not bandwidth, is the
# bottleneck for the ~45k small mp3/cdg files. A batch is one long-lived `rclone copy
# --files-from <manifest>` with parallel transfers, which amortizes all of that.
#
# Batches fetch by *path*, and §5.1 demotes paths for good reason (Drive allows duplicate names
# in one folder; a manifest line can also die on rclone's filter-file parsing, e.g. trailing
# whitespace). That is acceptable here because a path is only ever a *delivery hint*: every
# arrival is hashed and matched against the row's gdrive_md5 before anything is staged or
# committed, exactly like the copyid path. A file that fails to arrive or verify simply stays
# remote_only and falls back to the by-ID path. Wrong bytes can never be staged; the worst a bad
# path can cause is a slow retry.

BATCH_TRANSFERS = 8

RCLONE_BATCH_FLAGS = [
    "--drive-acknowledge-abuse",
    "--low-level-retries", "10",
    "--retries", "1",
    "--timeout", "300s",
]


def _rclone_batch_copy(remote_paths: list[str], batch_dir: Path) -> subprocess.CompletedProcess:
    """Fetch many files by path in one rclone process. Failures are per-file and tolerated —
    the caller stages whatever arrived and verified; the rest stay remote_only."""
    config.require_drive_folder_id()
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest = batch_dir / ".manifest"
    manifest.write_text("".join(p + "\n" for p in remote_paths), encoding="utf-8")
    cmd = [
        "rclone", "copy", f"{config.RCLONE_REMOTE}:", str(batch_dir),
        "--files-from", str(manifest),
        *config.RCLONE_BASE_FLAGS,
        *RCLONE_BATCH_FLAGS,
        "--transfers", str(BATCH_TRANSFERS),
        "--checkers", str(BATCH_TRANSFERS * 2),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def stage_batch_arrivals(
    conn,
    rows,
    batch_dir: Path,
    report: DownloadReport,
    *,
    hasher: Callable[[Path], str] = md5_of,
) -> list:
    """Verify and stage whatever a batch fetch delivered; return the rows it did NOT satisfy.

    Same commit discipline as download_one: hash first, then atomic rename into the canonical
    staging path, then a one-row commit. An unverifiable arrival is discarded and its row is
    left remote_only for the by-ID fallback.
    """
    unsatisfied = []
    for loc in rows:
        src = batch_dir / loc["remote_path"]
        dest = staging_path(loc["gdrive_md5"], loc["drive_file_id"], loc["filetype"])
        if dest.exists() and hasher(dest) == loc["gdrive_md5"]:
            _mark_staged(conn, loc, dest)  # e.g. the concurrent by-ID run got there first
            report.already_staged += 1
            continue
        if not src.exists():
            unsatisfied.append(loc)
            continue
        actual = hasher(src)
        if actual != loc["gdrive_md5"]:
            # Path delivered the wrong bytes (duplicate name in the folder, or a changed file).
            # Never staged; the by-ID path will fetch this row unambiguously.
            src.unlink(missing_ok=True)
            _record_failure(report, loc, VERIFY_FAILED,
                            f"batch arrival md5 {actual} != expected {loc['gdrive_md5']}")
            unsatisfied.append(loc)
            continue
        size = src.stat().st_size
        if loc["size_bytes"] is not None and size != loc["size_bytes"]:
            report.size_claim_mismatches += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)
        _mark_staged(conn, loc, dest)
        report.staged += 1
        report.bytes_downloaded += size
    return unsatisfied


def download_batched(
    conn,
    *,
    batch_size: int = 500,
    limit: int | None = None,
    from_end: bool = False,
    min_free: int = MIN_FREE_BYTES,
    fetch_batch: Callable[[list[str], Path], subprocess.CompletedProcess] = _rclone_batch_copy,
    fallback: bool = True,
    fetch: Callable[[str, Path], subprocess.CompletedProcess] = _rclone_copyid,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[DownloadReport], None] | None = None,
) -> DownloadReport:
    """Batched download: bulk path-keyed fetch, then by-ID fallback for whatever it missed.

    Same safety envelope as download_all — per-file commits, resumable, disk floor. `from_end`
    walks the worklist backwards, which lets a batched run coexist with an in-flight sequential
    run without the two contending for the same files for days.
    """
    config.ensure_layout()
    worklist = build_worklist(conn, None)
    if from_end:
        worklist = list(reversed(worklist))
    if limit is not None:
        worklist = worklist[:limit]
    report = DownloadReport(worklist=len(worklist))
    started = time.monotonic()
    batch_root = config.STAGING_DIR / ".batch-tmp"
    try:
        for i in range(0, len(worklist), batch_size):
            if free_bytes() < min_free:
                report.stopped_reason = "disk_low"
                break
            batch = worklist[i : i + batch_size]
            batch_dir = batch_root / f"b{i}"
            fetch_batch([r["remote_path"] for r in batch], batch_dir)
            unsatisfied = stage_batch_arrivals(conn, batch, batch_dir, report)
            if fallback:
                for loc in unsatisfied:
                    outcome = download_one(conn, loc, report, fetch=fetch, sleep=sleep)
                    if outcome == STAGED:
                        report.staged += 1
                    elif outcome == ALREADY:
                        report.already_staged += 1
            else:
                report.deferred += len(unsatisfied)
            shutil.rmtree(batch_dir, ignore_errors=True)
            if on_progress:
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    shutil.rmtree(batch_root, ignore_errors=True)
    report.elapsed_sec = time.monotonic() - started
    return report


@dataclass
class DownloadReport:
    worklist: int = 0
    staged: int = 0
    already_staged: int = 0
    verify_failed: int = 0
    deferred: int = 0
    bytes_downloaded: int = 0
    size_claim_mismatches: int = 0
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    failures: list[dict] = field(default_factory=list)  # capped sample of non-staged files

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        d["gib_downloaded"] = round(self.bytes_downloaded / 2**30, 2)
        return d


def _record_failure(report: DownloadReport, loc, kind: str, detail: str) -> None:
    if len(report.failures) < 200:
        report.failures.append(
            {"drive_file_id": loc["drive_file_id"], "kind": kind,
             "remote_path": loc["remote_path"], "detail": detail[:300]}
        )


def download_one(
    conn,
    loc,
    report: DownloadReport,
    *,
    fetch: Callable[[str, Path], subprocess.CompletedProcess] = _rclone_copyid,
    hasher: Callable[[Path], str] = md5_of,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Fetch, verify, and stage one location. Commits its own row so an interrupt is safe.

    Returns one of STAGED / ALREADY / VERIFY_FAILED / DEFERRED. `fetch`/`hasher`/`sleep` are
    injectable so the retry/verify logic is testable without Drive.
    """
    fid = loc["drive_file_id"]
    expected_md5 = loc["gdrive_md5"]
    dest = staging_path(expected_md5, fid, loc["filetype"])

    # Already on disk and correct from a prior interrupted run? Adopt it without re-downloading.
    if dest.exists():
        try:
            if hasher(dest) == expected_md5:
                _mark_staged(conn, loc, dest)
                return ALREADY
        except OSError:
            pass
        dest.unlink(missing_ok=True)  # wrong/partial — fall through and re-fetch

    tmp = dest.with_name(dest.name + ".part")
    last_detail = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        tmp.unlink(missing_ok=True)
        proc = fetch(fid, tmp)
        if proc.returncode == 0 and tmp.exists():
            actual = hasher(tmp)
            if actual == expected_md5:
                size = tmp.stat().st_size
                if loc["size_bytes"] is not None and size != loc["size_bytes"]:
                    # md5 matched, so the bytes are correct — Drive's *reported* size at
                    # enumeration was off (deviation #1 keeps both sizes exactly to catch this).
                    report.size_claim_mismatches += 1
                os.replace(tmp, dest)  # atomic within the staging filesystem
                _mark_staged(conn, loc, dest)
                report.bytes_downloaded += size
                return STAGED
            # Bytes arrived but hash is wrong: corrupt transfer. Discard and retry.
            last_detail = f"md5 mismatch: got {actual}, want {expected_md5}"
            tmp.unlink(missing_ok=True)
        else:
            last_detail = (proc.stderr or proc.stdout or f"rclone rc={proc.returncode}").strip()

        if attempt < MAX_ATTEMPTS:
            sleep(min(BACKOFF_BASE_SEC * 2 ** (attempt - 1), BACKOFF_CAP_SEC))

    # Exhausted inline retries. Leave the row remote_only so a later re-run retries it.
    tmp.unlink(missing_ok=True)
    if last_detail.startswith("md5 mismatch"):
        report.verify_failed += 1
        _record_failure(report, loc, VERIFY_FAILED, last_detail)
        return VERIFY_FAILED
    report.deferred += 1
    _record_failure(report, loc, DEFERRED, last_detail)
    return DEFERRED


def _mark_staged(conn, loc, dest: Path) -> None:
    conn.execute(
        "UPDATE file_locations SET status='staged', local_path=?, updated_at=? WHERE id=?",
        (str(dest), utcnow(), loc["id"]),
    )
    conn.commit()


def build_worklist(conn, limit: int | None = None) -> list:
    """Non-excluded, not-yet-staged locations, ordered so pair halves are adjacent (§6.3).

    Excludes zip members (`parent_location_id` — none exist, §5.3) defensively. Sort key is
    (pair token, drive_file_id): paired files cluster; everything else is deterministic.
    """
    rows = conn.execute(
        "SELECT id, drive_file_id, remote_path, gdrive_md5, size_bytes, filetype "
        "FROM file_locations "
        "WHERE status='remote_only' AND parent_location_id IS NULL "
        "ORDER BY drive_file_id"
    ).fetchall()
    token = pair_order_map(conn)
    rows.sort(key=lambda r: (token.get(r["gdrive_md5"], r["gdrive_md5"]), r["drive_file_id"]))
    return rows[:limit] if limit is not None else rows


def download_all(
    conn,
    *,
    limit: int | None = None,
    min_free: int = MIN_FREE_BYTES,
    progress_every: int = 50,
    fetch: Callable[[str, Path], subprocess.CompletedProcess] = _rclone_copyid,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[DownloadReport], None] | None = None,
) -> DownloadReport:
    """Download the whole worklist. Resumable, Ctrl-C-safe, per-file committed."""
    config.ensure_layout()
    worklist = build_worklist(conn, limit)
    report = DownloadReport(worklist=len(worklist))
    started = time.monotonic()
    try:
        for i, loc in enumerate(worklist, 1):
            if free_bytes() < min_free:
                report.stopped_reason = "disk_low"
                break
            outcome = download_one(conn, loc, report, fetch=fetch, sleep=sleep)
            if outcome == STAGED:
                report.staged += 1
            elif outcome == ALREADY:
                report.already_staged += 1
            if on_progress and (i % progress_every == 0 or i == len(worklist)):
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    report.elapsed_sec = time.monotonic() - started
    return report
