"""Stage 6 §9.2 — Organize: project the index onto the filesystem, and fsck it back.

Staging is content-addressed and unreadable: 51,008 files named by Drive file ID, sharded into
256 hex directories. Every fact about what any of them IS lives in the index. Organize is the
one pass that writes that knowledge back onto the filesystem, so a human with a file manager
sees a library instead of a hash dump.

Layout (§9.2, with the 2026-08-22 sharding decision):

    active/<shard>/<Artist> - <Title> [<item_id>]/<Artist> - <Title> [<item_id>].<ext>

ONE DIRECTORY PER COPY, not per song (sha-yol, 2026-08-22). 28,907 active items across 21,576
songs, so ~7.3k directories are alternate copies sitting beside the one they duplicate,
distinguished by `[item_id]`. That id is not decoration: it is the join key between what a
person sees in the tree and what `files.relpath` says in the runtime DB, and it is what makes
renames safe and fsck possible.

SHARDING by the artist's first character, because 28,907 directories in one parent is fine for
ext4 and painful in every file manager and in the Drive web UI. Measured distribution over the
live index: 51 buckets, largest T at 3,076 — but 1,627 of those are a leading "The". The
article is therefore stripped FOR THE SHARD KEY ONLY (the directory name keeps it), which drops
T to ~1,435 and leaves B (~2,150) as the largest bucket. Rare buckets are NOT merged: ו holds 6
items and that costs nothing, while "first letter, always" is a rule a human can apply without
being taught one.

MOVES, NEVER COPIES. Staging and active are the same filesystem, so each placement is a
rename(2): instant, and zero additional bytes. This is not an optimization — the host has ~30 GB
free against ~432 GB of content, so any copy-then-delete design cannot run at all.

SHARED BLOBS get hardlinks. 742 blobs (all `.cdg` graphics — one lyric file paired against
several different mp3s) belong to more than one active item, 821 extra copies, 1.4 GB. The
lowest-numbered item owning a blob gets the rename; every other item gets `os.link` into its
own directory, plus its own `file_locations` row. That row is not a hack: `file_locations` is
the physical-copy table, its `content_hash` is documented as deliberately non-unique, and a
hardlink is exactly a second physical location of one content. Both directories end up
self-contained and playable, which is the point — a folder that is missing its .cdg is not a
karaoke track. (Drive has no hardlinks, so the upload pays the 1.4 GB once. Cheap.)

BROKEN media is archived, not shipped. 59 active items have a broken *defining* (av/audio)
blob; they go to `archive/broken/` and the item is marked `status='archived'`. This aligns the
tree with what `export_runtime.py` already does — it ships only versions whose audio/av blob is
`probed_ok`/`decoded_ok` — so what a human browses matches what the app serves. A broken
GRAPHICS blob (79 items) is deliberately NOT archived: the mp3 still plays, and export ships
it. `--keep-broken-active` opts out. Reversible in both directions: replace the file, re-run.

ORPHANS: 509 staged files belong to no active item (420 stray `.cdg` plus 89 assorted `.db`,
`.jpg`, `.txt` junk that came along with the media). They go to `archive/orphans/`.

Idempotent and resumable like every other pass (§13). Destinations are pure functions of the
index, every placement is checked against the filesystem before it is attempted, and the whole
thing commits per item, so a second run back-to-back does nothing and Ctrl-C costs at most one
item.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db

# --- Naming ------------------------------------------------------------------------------

# Bidi control characters. Hebrew titles arriving from Drive, from ID3 tags and from the
# catalogue sources carry these; they are invisible, they survive into filenames, and they make
# two visually identical names compare unequal. Strip them and let the renderer do its job.
_BIDI = "".join(chr(c) for c in
                (0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                 0x2066, 0x2067, 0x2068, 0x2069))
_BIDI_RE = re.compile(f"[{_BIDI}]")

# Windows' forbidden set, not just POSIX's `/`. The event may well run off an exFAT or NTFS
# external disk, and a name that cannot be copied there is a name that fails at the worst
# possible moment. Cheap insurance.
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}

# Stripped for the SHARD KEY only — never from the displayed name. Filing half the English
# catalogue under T is filing it under a word nobody searches by.
SHARD_ARTICLES = ("the ", "a ", "an ")

HEBREW_FIRST, HEBREW_LAST = 0x05D0, 0x05EA   # א .. ת

SHARD_UNNAMED = "_unnamed"
SHARD_OTHER = "_other"
SHARD_DIGIT = "0-9"

# ext4 caps a single name at 255 BYTES, and Hebrew costs 2 bytes per character. The directory
# name is reused as the file stem plus an extension, so cap below the limit and leave headroom.
MAX_NAME_BYTES = 180


def strip_bidi(s: str) -> str:
    return _BIDI_RE.sub("", s)


def _truncate_bytes(s: str, limit: int) -> str:
    """Truncate to `limit` UTF-8 bytes without splitting a character."""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", errors="ignore").rstrip()


def sanitize_component(s: str | None) -> str:
    """One path component: safe on ext4, exFAT, NTFS and Drive, and still readable."""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", strip_bidi(s))
    s = _UNSAFE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Trailing dots and spaces are silently dropped by Windows/exFAT, which turns a unique name
    # into a colliding one. Strip them here where it is visible instead.
    s = s.rstrip(". ")
    if s.upper() in _WIN_RESERVED:
        s = f"_{s}"
    return s


def shard_key(name: str | None) -> str:
    """Bucket a display name by its first character, article-insensitively."""
    n = sanitize_component(name).lstrip("\"'([")
    low = n.lower()
    for art in SHARD_ARTICLES:
        if low.startswith(art):
            n = n[len(art):].lstrip()
            break
    if not n:
        return SHARD_UNNAMED
    c = n[0]
    if c.isascii() and c.isalpha():
        return c.upper()
    if c.isdigit():
        return SHARD_DIGIT
    if HEBREW_FIRST <= ord(c) <= HEBREW_LAST:
        return c
    return SHARD_OTHER


def item_dirname(item_id: int, artist: str | None, title: str | None,
                 fallback: str | None) -> tuple[str, str]:
    """(directory name, name_source). §9.2: names from v_metadata, fallback raw filename."""
    a, t = sanitize_component(artist), sanitize_component(title)
    suffix = f" [{item_id}]"
    room = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    if a and t:
        return _truncate_bytes(f"{a} - {t}", room) + suffix, "metadata"
    if t:
        return _truncate_bytes(t, room) + suffix, "title_only"
    if a:
        return _truncate_bytes(a, room) + suffix, "artist_only"
    fb = sanitize_component(fallback)
    if fb:
        return _truncate_bytes(fb, room) + suffix, "filename"
    return f"item{suffix}", "none"


def shard_for(artist: str | None, title: str | None, fallback: str | None) -> str:
    """Shard on the artist — the thing a person browses by — falling back the same way the
    directory name does, so a directory is never filed under a letter it does not show."""
    for candidate in (artist, title, fallback):
        if sanitize_component(candidate):
            return shard_key(candidate)
    return SHARD_UNNAMED


# --- Planning ----------------------------------------------------------------------------

DEFINING_ROLES = ("av", "audio")


@dataclass
class FilePlan:
    role: str
    content_hash: str
    loc_id: int | None
    src: Path | None
    dst: Path
    action: str          # move | link | noop | adopt | missing
    owned: bool = True   # this item gets the rename; others hardlink off it
    size_bytes: int = 0


@dataclass
class ItemPlan:
    item_id: int
    format: str
    shard: str
    dirname: str
    dest_dir: Path
    name_source: str
    archive_reason: str | None = None
    files: list[FilePlan] = field(default_factory=list)


@dataclass
class OrphanPlan:
    loc_id: int
    src: Path
    dst: Path
    filetype: str
    size_bytes: int = 0


_PLAN_SQL = """
WITH meta AS (
    SELECT media_item_id AS id,
           MAX(CASE WHEN field='artist' THEN value END) AS artist,
           MAX(CASE WHEN field='title'  THEN value END) AS title
    FROM v_metadata GROUP BY 1
)
SELECT i.id            AS item_id,
       i.format        AS format,
       m.artist        AS artist,
       m.title         AS title,
       f.role          AS role,
       f.content_hash  AS content_hash,
       b.integrity_status AS integrity,
       b.size_bytes    AS size_bytes,
       l.id            AS loc_id,
       l.local_path    AS local_path,
       l.remote_path   AS remote_path,
       l.filetype      AS filetype
FROM media_items i
JOIN media_item_files f ON f.media_item_id = i.id
JOIN blobs b           ON b.content_hash = f.content_hash
LEFT JOIN meta m       ON m.id = i.id
LEFT JOIN file_locations l ON l.id = (
        SELECT MIN(x.id) FROM file_locations x
         WHERE x.content_hash = f.content_hash AND x.local_path IS NOT NULL)
WHERE i.status = 'active'
ORDER BY i.id, f.role
"""


def _extension(row: sqlite3.Row) -> str:
    """Extension for the placed file. `filetype` is the lowercased extension the enumerator
    recorded; fall back to the staged path, which is authoritative for what is on disk."""
    ft = (row["filetype"] or "").strip().lstrip(".").lower()
    if ft and ft != "none" and re.fullmatch(r"[a-z0-9]{1,8}", ft):
        return f".{ft}"
    if row["local_path"]:
        suf = Path(row["local_path"]).suffix.lower()
        if suf:
            return suf
    return ""


def build_plan(conn: sqlite3.Connection, *, archive_broken: bool = True) -> tuple[
        list[ItemPlan], list[OrphanPlan], dict]:
    """Pure planning: reads the index and the filesystem, writes nothing."""
    rows = conn.execute(_PLAN_SQL).fetchall()

    by_item: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_item.setdefault(r["item_id"], []).append(r)

    # Which item OWNS each blob (gets the rename; the rest get hardlinks). Lowest item id,
    # preferring an item that is staying in active/ — linking out of archive/ into active/
    # would be correct but reads as a mistake to anyone looking at the tree.
    broken_items: set[int] = set()
    if archive_broken:
        for item_id, frows in by_item.items():
            if any(r["role"] in DEFINING_ROLES and r["integrity"] == "broken" for r in frows):
                broken_items.add(item_id)
    owner: dict[str, int] = {}
    for item_id in sorted(by_item):
        for r in by_item[item_id]:
            h = r["content_hash"]
            cur = owner.get(h)
            if cur is None:
                owner[h] = item_id
            elif cur in broken_items and item_id not in broken_items:
                owner[h] = item_id

    # Every path the index already knows about. A destination that is ALREADY indexed is
    # settled — the previous run finished it — and must not be re-derived from the blob's
    # primary location row, which by then points at whichever item owns the blob. Without
    # this, a re-run rewrote the primary row of every shared .cdg to the *sharer's* hardlink
    # and orphaned the owner's copy. Caught by test_rerun_is_a_noop.
    path_to_loc: dict[str, int] = {
        r["local_path"]: r["id"] for r in conn.execute(
            "SELECT id, local_path FROM file_locations WHERE local_path IS NOT NULL")}

    plans: list[ItemPlan] = []
    for item_id in sorted(by_item):
        frows = by_item[item_id]
        defining = next((r for r in frows if r["role"] in DEFINING_ROLES), frows[0])
        fallback = None
        if defining["remote_path"]:
            fallback = Path(defining["remote_path"]).stem
        artist, title = defining["artist"], defining["title"]

        dirname, name_source = item_dirname(item_id, artist, title, fallback)
        reason = "broken" if item_id in broken_items else None
        if reason:
            shard = ""
            dest_dir = config.ARCHIVE_DIR / "broken" / dirname
        else:
            shard = shard_for(artist, title, fallback)
            dest_dir = config.ACTIVE_DIR / shard / dirname

        plan = ItemPlan(item_id=item_id, format=defining["format"], shard=shard,
                        dirname=dirname, dest_dir=dest_dir, name_source=name_source,
                        archive_reason=reason)

        for r in frows:
            dst = dest_dir / f"{dirname}{_extension(r)}"
            src = Path(r["local_path"]) if r["local_path"] else None
            owned = owner.get(r["content_hash"]) == item_id
            loc_id = r["loc_id"]
            settled = path_to_loc.get(str(dst))
            if settled is not None:
                action, loc_id = "noop", settled
            elif src is None:
                action = "missing"
            elif src == dst:
                action = "noop"
            elif dst.exists():
                # A previous run placed the bytes and was interrupted before the commit.
                # Adopt what is already there rather than moving over it.
                action = "adopt"
            elif owned:
                action = "move"
            else:
                action = "link"
            plan.files.append(FilePlan(role=r["role"], content_hash=r["content_hash"],
                                       loc_id=loc_id, src=src, dst=dst, action=action,
                                       owned=owned, size_bytes=r["size_bytes"] or 0))
        plans.append(plan)

    orphans = _plan_orphans(conn)

    stats = _summarize(plans, orphans)
    return plans, orphans, stats


_ORPHAN_SQL = """
SELECT l.id AS loc_id, l.local_path, l.filetype, l.size_bytes
FROM file_locations l
WHERE l.local_path IS NOT NULL
  AND l.status <> 'archived'
  AND l.content_hash NOT IN (
        SELECT f.content_hash FROM media_item_files f
        JOIN media_items i ON i.id = f.media_item_id
        WHERE i.status = 'active')
ORDER BY l.id
"""


def _plan_orphans(conn: sqlite3.Connection) -> list[OrphanPlan]:
    out: list[OrphanPlan] = []
    for r in conn.execute(_ORPHAN_SQL):
        src = Path(r["local_path"])
        ft = (r["filetype"] or "none").strip().lstrip(".").lower() or "none"
        dst = config.ARCHIVE_DIR / "orphans" / ft / src.name
        if src == dst:
            continue
        out.append(OrphanPlan(loc_id=r["loc_id"], src=src, dst=dst, filetype=ft,
                              size_bytes=r["size_bytes"] or 0))
    return out


def _summarize(plans: list[ItemPlan], orphans: list[OrphanPlan]) -> dict:
    actions: dict[str, int] = {}
    shards: dict[str, int] = {}
    names: dict[str, int] = {}
    bytes_moved = 0
    for p in plans:
        names[p.name_source] = names.get(p.name_source, 0) + 1
        if not p.archive_reason:
            shards[p.shard] = shards.get(p.shard, 0) + 1
        for f in p.files:
            actions[f.action] = actions.get(f.action, 0) + 1
            if f.action in ("move", "link"):
                bytes_moved += f.size_bytes
    archived = sum(1 for p in plans if p.archive_reason)
    return {
        "items": len(plans),
        "items_to_active": len(plans) - archived,
        "items_archived_broken": archived,
        "files": sum(len(p.files) for p in plans),
        "actions": dict(sorted(actions.items())),
        "shards": dict(sorted(shards.items(), key=lambda kv: -kv[1])),
        "shard_count": len(shards),
        "largest_shard": max(shards.items(), key=lambda kv: kv[1]) if shards else None,
        "name_source": dict(sorted(names.items(), key=lambda kv: -kv[1])),
        "orphans": len(orphans),
        "orphan_bytes": sum(o.size_bytes for o in orphans),
        "bytes_to_place": bytes_moved,
    }


def songs_losing_last_copy(conn: sqlite3.Connection, plans: list[ItemPlan]) -> int:
    """How many songs are left with no active item once broken items are archived. Reported
    loudly rather than discovered later: archiving is correct, but it silently shrinks the
    catalogue, and that is the operator's call to accept."""
    broken = [p.item_id for p in plans if p.archive_reason == "broken"]
    if not broken:
        return 0
    qmarks = ",".join("?" * len(broken))
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT cluster_id FROM media_items
             WHERE status='active' AND cluster_id IS NOT NULL
             GROUP BY cluster_id
            HAVING SUM(CASE WHEN id IN ({qmarks}) THEN 0 ELSE 1 END) = 0)
        """, broken).fetchone()
    return row[0]


# --- Execution ---------------------------------------------------------------------------

def write_plan_file(plans: list[ItemPlan], orphans: list[OrphanPlan], path: Path) -> Path:
    """§9.2 requires a dry run to print EVERY planned move. At 51k files that belongs in a
    file the operator can grep and diff, not in a terminal scrollback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("action\titem_id\trole\tsrc\tdst\n")
        for p in plans:
            for f in p.files:
                fh.write(f"{f.action}\t{p.item_id}\t{f.role}\t{f.src or ''}\t{f.dst}\n")
        for o in orphans:
            fh.write(f"orphan\t\t{o.filetype}\t{o.src}\t{o.dst}\n")
    return path


def _place(fp: FilePlan, owner_dst: dict[str, Path]) -> None:
    """Do the one filesystem operation this file needs. Raises on failure."""
    fp.dst.parent.mkdir(parents=True, exist_ok=True)
    if fp.action == "move":
        os.rename(fp.src, fp.dst)
        owner_dst[fp.content_hash] = fp.dst
    elif fp.action == "link":
        source = owner_dst.get(fp.content_hash) or fp.src
        try:
            os.link(source, fp.dst)
        except OSError:
            # Cross-device or a filesystem without hardlinks: a copy is correct, just costlier.
            shutil.copy2(source, fp.dst)
    elif fp.action == "adopt":
        owner_dst.setdefault(fp.content_hash, fp.dst)


def organize(*, dry_run: bool = True, archive_broken: bool = True,
             limit: int | None = None, progress=None) -> dict:
    """Stage 6 §9.2. Returns the run report."""
    conn = db.connect()
    try:
        plans, orphans, stats = build_plan(conn, archive_broken=archive_broken)
        stats["songs_losing_last_copy"] = songs_losing_last_copy(conn, plans)
        stats["dry_run"] = dry_run
        stats["archive_broken"] = archive_broken
        if limit is not None:
            plans = plans[:limit]
            orphans = orphans[:limit]
            stats["limited_to"] = limit

        ts = db.utcnow().replace(":", "").replace("-", "")
        plan_path = write_plan_file(plans, orphans,
                                    config.LOGS_DIR / f"organize-plan-{ts}.tsv")
        stats["plan_file"] = str(plan_path)
        if dry_run:
            return stats

        owner_dst: dict[str, Path] = {}
        placed = failed = 0
        for n, p in enumerate(plans, 1):
            try:
                with conn:
                    for fp in p.files:
                        if fp.action in ("noop", "missing"):
                            if fp.action == "noop":
                                owner_dst.setdefault(fp.content_hash, fp.dst)
                            continue
                        _place(fp, owner_dst)
                        _record_location(conn, fp)
                    if p.archive_reason:
                        conn.execute(
                            "UPDATE media_items SET status='archived', updated_at=? WHERE id=?",
                            (db.utcnow(), p.item_id))
                placed += 1
            except Exception as exc:            # noqa: BLE001 — one bad item must not stop 28k
                failed += 1
                stats.setdefault("errors", []).append(f"item {p.item_id}: {exc}")
            if progress and n % 500 == 0:
                progress(n, len(plans))

        orph_done = 0
        orph_absent = 0
        for o in orphans:
            try:
                with conn:
                    if o.src.exists():
                        o.dst.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(o.src, o.dst)
                        conn.execute(
                            "UPDATE file_locations SET local_path=?, status='archived', "
                            "archive_reason='orphan', updated_at=? WHERE id=?",
                            (str(o.dst), db.utcnow(), o.loc_id))
                        orph_done += 1
                    else:
                        # The staged file is already gone — an interrupted download's `.part`,
                        # or something cleaned up between staging and now. NEVER write the
                        # destination path in this branch: that would point the index at a file
                        # this run did not create, and fsck would (correctly) call it missing
                        # forever. local_path=NULL is the honest statement "no local copy";
                        # drive_file_id survives, so the content is still recoverable.
                        conn.execute(
                            "UPDATE file_locations SET local_path=NULL, status='archived', "
                            "archive_reason='orphan_missing', updated_at=? WHERE id=?",
                            (db.utcnow(), o.loc_id))
                        orph_absent += 1
            except Exception as exc:            # noqa: BLE001
                failed += 1
                stats.setdefault("errors", []).append(f"orphan {o.loc_id}: {exc}")

        stats["items_placed"] = placed
        stats["orphans_moved"] = orph_done
        stats["orphans_absent"] = orph_absent
        stats["failed"] = failed
        stats["staging_dirs_removed"] = _prune_empty_staging()
        if "errors" in stats:
            stats["errors"] = stats["errors"][:50]
        return stats
    finally:
        conn.close()


def _record_location(conn: sqlite3.Connection, fp: FilePlan) -> None:
    """Point the index at where the file now is. A hardlink is a NEW physical location of the
    same content, so it gets its own row — `file_locations.content_hash` is documented as
    deliberately non-unique for exactly this."""
    now = db.utcnow()
    if not fp.owned:
        existing = conn.execute(
            "SELECT id FROM file_locations WHERE local_path=?", (str(fp.dst),)).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO file_locations (drive_file_id, remote_path, local_path, filetype, "
            "size_bytes, content_hash, role, status, first_seen_at, updated_at) "
            "VALUES (NULL, NULL, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (str(fp.dst), fp.dst.suffix.lstrip(".").lower(), fp.size_bytes,
             fp.content_hash, fp.role, now, now))
    else:
        conn.execute(
            "UPDATE file_locations SET local_path=?, status='active', updated_at=? WHERE id=?",
            (str(fp.dst), now, fp.loc_id))


def _prune_empty_staging() -> int:
    removed = 0
    if not config.STAGING_DIR.exists():
        return 0
    for d in sorted(config.STAGING_DIR.iterdir()):
        if d.is_dir():
            try:
                d.rmdir()
                removed += 1
            except OSError:
                pass
    return removed


# --- fsck --------------------------------------------------------------------------------

def fsck(*, check_strays: bool = True, hash_sample: int = 0) -> dict:
    """Index ↔ filesystem reconciliation (§9.2: runs after every Organize/archive batch and
    must pass). Read-only: it reports, it never repairs."""
    import hashlib
    import random

    conn = db.connect()
    try:
        report: dict = {"missing_files": [], "size_mismatch": [], "incomplete_items": [],
                        "strays": [], "hash_mismatch": []}

        indexed: set[str] = set()
        checked = 0
        for r in conn.execute(
                "SELECT l.id, l.local_path, b.size_bytes FROM file_locations l "
                "JOIN blobs b ON b.content_hash=l.content_hash "
                "WHERE l.local_path IS NOT NULL AND l.status IN ('staged','active','archived')"):
            p = Path(r["local_path"])
            indexed.add(str(p))
            checked += 1
            try:
                st = p.stat()
            except OSError:
                report["missing_files"].append(str(p))
                continue
            if r["size_bytes"] is not None and st.st_size != r["size_bytes"]:
                report["size_mismatch"].append(f"{p} ({st.st_size} != {r['size_bytes']})")
        report["locations_checked"] = checked

        for r in conn.execute(
                """SELECT i.id, i.format, COUNT(*) AS n,
                          SUM(CASE WHEN EXISTS (SELECT 1 FROM file_locations l
                                                 WHERE l.content_hash=f.content_hash
                                                   AND l.local_path IS NOT NULL)
                                   THEN 1 ELSE 0 END) AS present
                     FROM media_items i JOIN media_item_files f ON f.media_item_id=i.id
                    WHERE i.status='active' GROUP BY i.id"""):
            if r["present"] != r["n"]:
                report["incomplete_items"].append(f"item {r['id']} ({r['format']}): "
                                                  f"{r['present']}/{r['n']} files")

        if check_strays and config.ACTIVE_DIR.exists():
            for root, _dirs, files in os.walk(config.ACTIVE_DIR):
                for name in files:
                    p = str(Path(root) / name)
                    if p not in indexed:
                        report["strays"].append(p)

        if hash_sample:
            rows = conn.execute(
                "SELECT l.local_path, l.content_hash FROM file_locations l "
                "WHERE l.local_path IS NOT NULL AND l.status='active'").fetchall()
            for r in random.sample(rows, min(hash_sample, len(rows))):
                p = Path(r["local_path"])
                if not p.exists():
                    continue
                h = hashlib.sha256()
                with p.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != r["content_hash"]:
                    report["hash_mismatch"].append(str(p))
            report["hashed"] = min(hash_sample, len(rows))

        report["counts"] = {k: len(v) for k, v in report.items() if isinstance(v, list)}
        report["ok"] = all(n == 0 for n in report["counts"].values())
        for k in ("missing_files", "size_mismatch", "incomplete_items", "strays",
                  "hash_mismatch"):
            report[k] = report[k][:50]
        return report
    finally:
        conn.close()
