"""Stage 3 — Hash, probe, pair, fingerprint (spec §7.2), clustering (§7.3), dedup (§7.4).

Everything here is pure local compute + DB writes: Stage 2 already put all 51,008 non-excluded
locations on local disk, MD5-verified. Nothing in this module talks to Drive.

§7.2.1 hashing — the identity step that unlocks the rest of the pipeline:

* SHA-256 every staged file into `blobs` (content identity) and link the location. Everything
  Stage 1 recorded at md5 level (`location_parses` via locations, `provisional_pairs`) becomes
  promotable to item-level facts only once this md5 → content_hash mapping exists.

* **The same read also recomputes MD5** and checks it against `gdrive_md5`. Stage 2 verified
  every byte at download time, so a mismatch here means the file changed *on local disk* since
  (bit-rot, truncation, a stray write). Such a row gets no blob link — hashing corrupt bytes
  would poison content identity — and is reported for re-download; `content_hash` stays NULL so
  a re-run after recovery picks it up. Expected count: 0.

* Locations sharing a blob beyond what Drive MD5 caught are marked `exact_dup` (§7.2.1).
  Since Stage 2 verified bytes-match-md5 for every staged file, two staged files can only share
  a sha256 if they also share an md5 — which §5.2 already deduplicated on. So this pass is a
  safety net that should find 0; if it fires, the survivor rule is §5.2's (smallest
  drive_file_id). Losers keep status='staged' — their bytes are local, unlike §5.2's
  never-downloaded 'excluded' losers — and carry archive_reason='exact_dup' for §9.2 to archive.

Resumable/idempotent: the worklist is exactly "staged rows with no content_hash", recomputed per
run; each file commits its own row, so an interrupt loses at most one in-flight hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .db import utcnow

HASH_CHUNK = 1024 * 1024


# --- pure helpers ---------------------------------------------------------------------------


def sha256_and_md5(path: Path) -> tuple[str, str, int]:
    """One pass over the file: (sha256, md5, size). The md5 is the local-corruption tripwire —
    computing it costs nothing extra next to the disk read that dominates this stage."""
    sha, md5 = hashlib.sha256(), hashlib.md5()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size


@dataclass
class HashReport:
    worklist: int = 0
    hashed: int = 0
    bytes_hashed: int = 0
    new_blobs: int = 0
    shared_blobs: int = 0          # locations that linked to an already-existing blob
    md5_drift: int = 0             # local bytes no longer match gdrive_md5 — corruption, no link
    read_errors: int = 0
    new_exact_dups: int = 0        # staged locations sharing a blob beyond §5.2's md5 dedup
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    failures: list[dict] = field(default_factory=list)  # capped sample

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        d["gib_hashed"] = round(self.bytes_hashed / 2**30, 2)
        return d


def _record_failure(report: HashReport, loc, kind: str, detail: str) -> None:
    if len(report.failures) < 200:
        report.failures.append(
            {"location_id": loc["id"], "kind": kind,
             "local_path": loc["local_path"], "detail": detail[:300]}
        )


def hash_worklist(conn, limit: int | None = None) -> list:
    """Staged locations not yet linked to a blob. Deterministic order; resume = re-run."""
    rows = conn.execute(
        "SELECT id, local_path, gdrive_md5, size_bytes FROM file_locations "
        "WHERE status='staged' AND content_hash IS NULL AND local_path IS NOT NULL "
        "ORDER BY id"
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def hash_one(conn, loc, report: HashReport, *,
             hasher: Callable[[Path], tuple[str, str, int]] = sha256_and_md5) -> bool:
    """Hash one staged file and link it to its blob. Commits its own row. Returns success."""
    path = Path(loc["local_path"])
    try:
        sha, md5, size = hasher(path)
    except OSError as exc:
        report.read_errors += 1
        _record_failure(report, loc, "read_error", str(exc))
        return False

    if loc["gdrive_md5"] and md5 != loc["gdrive_md5"]:
        # The bytes changed after Stage 2 verified them. Linking a blob to corrupt bytes would
        # poison content identity, so the row is left unhashed and surfaced for re-download.
        report.md5_drift += 1
        _record_failure(report, loc, "md5_drift",
                        f"local md5 {md5} != gdrive_md5 {loc['gdrive_md5']}")
        return False

    cur = conn.execute(
        "INSERT INTO blobs (content_hash, size_bytes, first_hashed_at) VALUES (?,?,?) "
        "ON CONFLICT(content_hash) DO NOTHING",
        (sha, size, utcnow()),
    )
    if cur.rowcount:
        report.new_blobs += 1
    else:
        report.shared_blobs += 1
    conn.execute(
        "UPDATE file_locations SET content_hash=?, updated_at=? WHERE id=?",
        (sha, utcnow(), loc["id"]),
    )
    conn.commit()
    report.hashed += 1
    report.bytes_hashed += size
    return True


def mark_new_exact_dups(conn, dry_run: bool = False) -> list[tuple[str, str]]:
    """§7.2.1: staged locations sharing a content_hash beyond what §5.2's md5 dedup caught.

    Survivor rule is §5.2's own (smallest drive_file_id), so verdicts are deterministic and
    consistent with Stage 0. Losers keep status='staged' (their bytes are on disk — 'excluded'
    means never-downloaded) and get archive_reason='exact_dup' for §9.2 to archive. Never
    touches a row that already carries an archive_reason. Expected to find nothing (see module
    docstring); existing is what makes that an invariant rather than an assumption.
    """
    losers = conn.execute(
        "SELECT id, drive_file_id, remote_path FROM ("
        "  SELECT id, drive_file_id, remote_path,"
        "         RANK() OVER (PARTITION BY content_hash ORDER BY drive_file_id) AS rnk"
        "  FROM file_locations"
        "  WHERE status='staged' AND content_hash IS NOT NULL AND archive_reason IS NULL"
        ") WHERE rnk > 1"
    ).fetchall()
    plan = [("mark_exact_dup", r["remote_path"]) for r in losers]
    if not dry_run and losers:
        conn.executemany(
            "UPDATE file_locations SET archive_reason='exact_dup', updated_at=? WHERE id=?",
            [(utcnow(), r["id"]) for r in losers],
        )
        conn.commit()
    return plan


def hash_all(conn, *, limit: int | None = None, progress_every: int = 500,
             hasher: Callable[[Path], tuple[str, str, int]] = sha256_and_md5,
             on_progress: Callable[[HashReport], None] | None = None) -> HashReport:
    """§7.2.1 over the whole staging tree. Ctrl-C-safe; per-file commits; re-run to resume."""
    worklist = hash_worklist(conn, limit)
    report = HashReport(worklist=len(worklist))
    started = time.monotonic()
    try:
        for i, loc in enumerate(worklist, 1):
            hash_one(conn, loc, report, hasher=hasher)
            if on_progress and (i % progress_every == 0 or i == len(worklist)):
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    report.new_exact_dups = len(mark_new_exact_dups(conn))
    report.elapsed_sec = time.monotonic() - started
    return report


# --- §7.2.3 probe ----------------------------------------------------------------------------
#
# ffprobe every media blob: parse failure / zero duration / missing audio => 'broken'; else the
# stream facts are recorded in blobs.integrity_detail and the blob becomes 'probed_ok'. No full
# decode here (§7.2.3) — fingerprinting decodes the audio anyway, and full A/V decode is Stage 4,
# survivors only.
#
# Empirical notes from the live library (2026-07-18), which shaped the rules:
#   * ffprobe reads .cdg natively (codec 'cdgraphics', 300x216, real duration) — so cdg blobs go
#     through the same probe. A cdg has NO audio stream by nature, so the missing-audio=>broken
#     rule must not apply to it; its audio lives in the paired mp3 (§7.2.4 checks the pairing).
#   * The two `*.mp4.part` truncated downloads PROBE CLEAN — an intact moov atom happily claims
#     208s over 714 KB of file. Container metadata cannot be trusted about completeness, so a
#     location whose *remote name* ends '.part' is capped at 'suspect' even when the probe
#     succeeds. Stage 4's full decode is the real arbiter for those.
#   * Known non-media detritus (thumbs.db, jpgs, docs — §5.3's ~115 files) is skipped, not
#     probed: 'broken' on a jpg would be noise, and no media item is ever built from one.
#     Anything unknown IS probed — ffprobe is the content sniffer that catches `.mp333` and the
#     extensionless real videos.

NON_MEDIA_EXTS = frozenset({
    "jpg", "jpeg", "png", "gif", "bmp", "gif-c200", "djp",
    "txt", "doc", "docx", "pdf", "rtf", "log", "htm", "html", "url",
    "ini", "db", "bat", "jar", "sfk", "ots", "pptx", "odp", "lnk", "tmp",
})

PROBE_TIMEOUT_SEC = 120
PROBE_WORKERS = 4


def ffprobe_file(path: Path) -> dict:
    """Run ffprobe and return its parsed JSON. Never raises on probe failure — returns
    {'error': ...} so classification is uniform for rc!=0, bad JSON, and timeouts."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_SEC, check=False)
    except subprocess.TimeoutExpired:
        return {"error": f"ffprobe timeout after {PROBE_TIMEOUT_SEC}s"}
    except OSError as exc:
        return {"error": f"ffprobe spawn failed: {exc}"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or f"ffprobe rc={proc.returncode}").strip()[:500]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "ffprobe emitted unparseable JSON"}


def summarize_probe(raw: dict) -> dict:
    """Compact, DB-worthy projection of ffprobe output (full output is huge and re-derivable)."""
    fmt = raw.get("format", {}) or {}
    streams = []
    for s in raw.get("streams", []) or []:
        streams.append({k: s.get(k) for k in
                        ("codec_type", "codec_name", "sample_rate", "channels",
                         "bit_rate", "width", "height", "duration")
                        if s.get(k) is not None})
    out = {"format_name": fmt.get("format_name"), "streams": streams}
    for k in ("duration", "bit_rate"):
        if fmt.get(k) is not None:
            out[k] = fmt[k]
    return out


def probe_duration(summary: dict) -> float | None:
    """format duration, falling back to the longest stream duration."""
    vals = []
    try:
        if summary.get("duration") is not None:
            vals.append(float(summary["duration"]))
    except (TypeError, ValueError):
        pass
    for s in summary.get("streams", []):
        try:
            if s.get("duration") is not None:
                vals.append(float(s["duration"]))
        except (TypeError, ValueError):
            continue
    return max(vals) if vals else None


def classify_probe(raw: dict, filetype: str, remote_name: str) -> tuple[str, dict]:
    """(integrity_status, detail) for one probed blob. Pure; unit-tested against real shapes."""
    if "error" in raw:
        return "broken", {"probe_error": raw["error"]}
    summary = summarize_probe(raw)
    if not summary["streams"]:
        return "broken", {**summary, "probe_error": "no streams"}
    dur = probe_duration(summary)
    if not dur or dur <= 0:
        return "broken", {**summary, "probe_error": "zero/missing duration"}
    has_audio = any(s.get("codec_type") == "audio" for s in summary["streams"])
    # cdg is graphics-only by nature; its audio is the paired mp3 (§7.2.4), so the
    # missing-audio rule applies to everything except it.
    if not has_audio and filetype != "cdg":
        return "broken", {**summary, "probe_error": "no audio stream"}
    if remote_name.lower().endswith(".part"):
        # Truncated download whose container header still probes clean (see module notes).
        return "suspect", {**summary, "probe_note": "incomplete download (.part); "
                                                    "container probes but bytes may be truncated"}
    return "probed_ok", summary


@dataclass
class ProbeReport:
    worklist: int = 0
    probed: int = 0
    probed_ok: int = 0
    broken: int = 0
    suspect: int = 0
    skipped_non_media: int = 0
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    failures: list[dict] = field(default_factory=list)  # capped sample of broken/suspect

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def probe_worklist(conn, limit: int | None = None) -> list:
    """Unchecked blobs, each with one representative staged location for path/filetype.

    Keyed by blob (a probe is a property of the bytes); the representative location is the
    smallest location id, deterministically. Known non-media extensions are excluded here so
    the report's skip count is visible up front.
    """
    placeholders = ",".join("?" for _ in NON_MEDIA_EXTS)
    rows = conn.execute(
        f"""
        SELECT b.content_hash, MIN(l.id) AS loc_id,
               (SELECT l2.local_path FROM file_locations l2
                 WHERE l2.content_hash = b.content_hash AND l2.status='staged'
                 ORDER BY l2.id LIMIT 1) AS local_path,
               (SELECT l2.filetype FROM file_locations l2
                 WHERE l2.content_hash = b.content_hash AND l2.status='staged'
                 ORDER BY l2.id LIMIT 1) AS filetype,
               (SELECT l2.remote_path FROM file_locations l2
                 WHERE l2.content_hash = b.content_hash AND l2.status='staged'
                 ORDER BY l2.id LIMIT 1) AS remote_path
        FROM blobs b
        JOIN file_locations l ON l.content_hash = b.content_hash AND l.status='staged'
        WHERE b.integrity_status = 'unchecked'
          AND lower(
                (SELECT l2.filetype FROM file_locations l2
                  WHERE l2.content_hash = b.content_hash AND l2.status='staged'
                  ORDER BY l2.id LIMIT 1)
              ) NOT IN ({placeholders})
        GROUP BY b.content_hash
        ORDER BY loc_id
        """,
        tuple(NON_MEDIA_EXTS),
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def count_skipped_non_media(conn) -> int:
    placeholders = ",".join("?" for _ in NON_MEDIA_EXTS)
    return conn.execute(
        f"SELECT COUNT(DISTINCT content_hash) AS n FROM file_locations "
        f"WHERE status='staged' AND content_hash IS NOT NULL "
        f"AND lower(filetype) IN ({placeholders})",
        tuple(NON_MEDIA_EXTS),
    ).fetchone()["n"]


def probe_all(conn, *, limit: int | None = None, workers: int = PROBE_WORKERS,
              progress_every: int = 500,
              prober: Callable[[Path], dict] = ffprobe_file,
              on_progress: Callable[[ProbeReport], None] | None = None) -> ProbeReport:
    """§7.2.3 over all unchecked media blobs. ffprobe runs in a small thread pool (it is a
    subprocess, so the GIL is idle); all DB writes happen on this thread, one commit per blob —
    Ctrl-C-safe, resumable by re-run exactly like hashing."""
    worklist = probe_worklist(conn, limit)
    report = ProbeReport(worklist=len(worklist),
                         skipped_non_media=count_skipped_non_media(conn))
    started = time.monotonic()

    def _probe(row) -> tuple:
        raw = prober(Path(row["local_path"]))
        name = Path(row["remote_path"] or row["local_path"]).name
        status, detail = classify_probe(raw, (row["filetype"] or "").lower(), name)
        return row, status, detail

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (row, status, detail) in enumerate(pool.map(_probe, worklist), 1):
                detail["probed_at"] = utcnow()
                conn.execute(
                    "UPDATE blobs SET integrity_status=?, integrity_detail=? WHERE content_hash=?",
                    (status, json.dumps(detail, ensure_ascii=False), row["content_hash"]),
                )
                conn.commit()
                report.probed += 1
                if status == "probed_ok":
                    report.probed_ok += 1
                elif status == "suspect":
                    report.suspect += 1
                else:
                    report.broken += 1
                if status != "probed_ok" and len(report.failures) < 200:
                    report.failures.append(
                        {"content_hash": row["content_hash"], "status": status,
                         "remote_path": row["remote_path"],
                         "detail": str(detail.get("probe_error") or detail.get("probe_note"))[:200]}
                    )
                if on_progress and (i % progress_every == 0 or i == len(worklist)):
                    report.elapsed_sec = time.monotonic() - started
                    on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    report.elapsed_sec = time.monotonic() - started
    return report


# --- §7.2.4 media items — the blob->item promotion ------------------------------------------
#
# Everything Stage 1 stored at md5 level becomes item-level here:
#   * confirmed provisional pairs        -> mp3g items (roles audio+graphics)
#   * blobs whose probe shows a video stream -> video items (role av)
#   * orphan audio blobs                 -> audio_only items (Demucs input; never archived here)
#   * location_parses hints              -> song_metadata rows, source 'filename'
#
# Classification is by PROBE CONTENT first, extension second: `.mp333` is an audio blob because
# it probes as one, the literal `.mp4`-named file is a video because it probes as one, and cdg
# "video" streams (codec cdgraphics) are graphics, never av. Extension is the fallback only for
# broken blobs that yielded no streams — a broken file still deserves an item: per sha-yol's
# 2026-07-19 correction of §7.4.5 it will be ARCHIVED even as a sole copy (unplayable; to be
# replaced instead), and the item row is what tracks it and puts the song on the
# replacement list rather than letting it vanish silently.
#
# CDG check (§7.2.4): abs(cdg_size/7200 - mp3_duration) <= 3s. cdg is CBR subcode — 24-byte
# packets at 300/s — so size/7200 IS its duration; the check is really pair coherence. A failing
# pair still becomes an item (the pairing was §6.2/§6.3-confirmed evidence; silently dropping it
# would be the destructive guess §1.2 forbids) but the graphics blob goes 'suspect' and the item
# is queued as pair_mismatch, exactly as the spec words it.
#
# 15 audio blobs pair with >1 graphics blob (measured). The item takes ONE graphics half:
# candidates that pass the CDG check outrank ones that don't, then most witnesses, then smallest
# hash — deterministic. The losers are recorded in quality_attrs.graphics_alternates, so nothing
# is silently forgotten and §9.2 can archive them as dedup extras.
#
# Idempotent by natural key, not by rebuild: an item is identified by its (format, role->blob)
# composition via media_item_files. Unlike provisional_pairs (which `pair` rebuilds wholesale),
# items are REFERENCED (review_queue, song_metadata, clusters), so delete+recreate would orphan
# those references; re-runs upsert instead and never delete an item.

VIDEO_EXTS = frozenset({"mp4", "avi", "mpg", "mpeg", "vob", "dat", "wmv", "mkv", "webm", "part"})
AUDIO_EXTS = frozenset({"mp3", "mp333", "wav", "flac", "ogg", "wma", "m4a"})

CDG_BYTES_PER_SEC = 7200.0   # 24-byte subcode packets at 300/s (CBR by format definition)


def blob_media_class(filetype: str, detail: dict | None) -> str | None:
    """'graphics' / 'av' / 'audio' / None(non-media), by probe content first, extension second."""
    ft = (filetype or "").lower()
    if ft == "cdg":
        return "graphics"   # probes as a video stream (cdgraphics) but is a graphics overlay
    streams = (detail or {}).get("streams", [])
    if any(s.get("codec_type") == "video" for s in streams):
        return "av"
    if any(s.get("codec_type") == "audio" for s in streams):
        return "audio"
    # Broken/unprobed blobs: no streams to look at; classify by extension so a broken copy
    # still gets an item and can lose a dedup fairly (or surface as a flagged sole copy).
    if ft in AUDIO_EXTS:
        return "audio"
    if ft in VIDEO_EXTS:
        return "av"
    return None


def probe_item_fields(detail: dict | None) -> dict:
    """media_items columns derivable from a stored probe summary. Missing pieces stay None."""
    out = {"duration_sec": None, "audio_codec": None, "audio_bitrate_kbps": None,
           "sample_rate": None, "video_codec": None, "width": None, "height": None}
    if not detail:
        return out
    out["duration_sec"] = probe_duration(detail)
    for s in detail.get("streams", []):
        if s.get("codec_type") == "audio" and out["audio_codec"] is None:
            out["audio_codec"] = s.get("codec_name")
            try:
                if s.get("bit_rate") is not None:
                    out["audio_bitrate_kbps"] = int(round(float(s["bit_rate"]) / 1000))
            except (TypeError, ValueError):
                pass
            try:
                if s.get("sample_rate") is not None:
                    out["sample_rate"] = int(s["sample_rate"])
            except (TypeError, ValueError):
                pass
        elif s.get("codec_type") == "video" and s.get("codec_name") != "cdgraphics" \
                and out["video_codec"] is None:
            out["video_codec"] = s.get("codec_name")
            out["width"], out["height"] = s.get("width"), s.get("height")
    # An mp3's format-level bit_rate covers files whose stream entry lacks one.
    if out["audio_codec"] is not None and out["audio_bitrate_kbps"] is None:
        try:
            if detail.get("bit_rate") is not None:
                out["audio_bitrate_kbps"] = int(round(float(detail["bit_rate"]) / 1000))
        except (TypeError, ValueError):
            pass
    return out


def cdg_check(cdg_size_bytes: int | None, mp3_duration_sec: float | None) -> dict:
    """§7.2.4 pair-coherence check, asymmetric per the §11 retune (see config for the evidence).

    delta = cdg_duration − mp3_duration. Positive beyond the LONGER tolerance (graphics
    outliving the audio) or negative beyond the SHORTER tolerance (whole-side recordings / dead air)
    fails; the normal graphics-stop-at-last-lyric gap passes. Never raises.
    """
    from . import config as _config
    if not cdg_size_bytes or not mp3_duration_sec:
        return {"status": "skipped", "reason": "missing cdg size or mp3 duration"}
    cdg_dur = cdg_size_bytes / CDG_BYTES_PER_SEC
    delta = cdg_dur - mp3_duration_sec
    ok = (-_config.CDG_TOLERANCE_GRAPHICS_SHORTER_SEC <= delta
          <= _config.CDG_TOLERANCE_GRAPHICS_LONGER_SEC)
    return {
        "status": "ok" if ok else "failed",
        "cdg_duration_sec": round(cdg_dur, 2),
        "mp3_duration_sec": round(mp3_duration_sec, 2),
        "delta_sec": round(delta, 2),
    }


def choose_graphics(candidates: list[tuple[str, int, dict]]) -> tuple[str, dict, list[str]]:
    """Pick the one graphics blob for an mp3g item from [(hash, witnesses, check_verdict)].

    Order: CDG check pass beats fail/skip, then most witnesses, then smallest hash. Returns
    (chosen_hash, its_verdict, alternate_hashes). Deterministic; alternates are recorded, never
    silently dropped.
    """
    ranked = sorted(
        candidates,
        key=lambda c: (0 if c[2].get("status") == "ok" else 1, -c[1], c[0]),
    )
    chosen = ranked[0]
    return chosen[0], chosen[2], [c[0] for c in ranked[1:]]


@dataclass
class ItemsReport:
    created: dict = field(default_factory=lambda: {"mp3g": 0, "video": 0, "audio_only": 0})
    existing: int = 0
    updated: int = 0
    format_upgraded: int = 0        # audio_only -> mp3g after a later-confirmed pair
    cdg_ok: int = 0
    cdg_failed: int = 0
    cdg_skipped: int = 0
    pair_mismatch_queued: int = 0
    unresolvable_pairs: int = 0     # pair halves with no staged blob (expected 0)
    unclassified_blobs: int = 0     # staged non-media blobs — no item
    graphics_blobs: int = 0
    roles_set: int = 0
    metadata: dict = field(default_factory=lambda: {"inserted": 0, "updated": 0, "unchanged": 0})
    over_budget: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _load_staged_blobs(conn) -> dict[str, dict]:
    """content_hash -> {filetype, size, integrity, detail, md5, remote_path} for staged rows."""
    blobs: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT l.gdrive_md5, l.content_hash, l.filetype, l.remote_path, "
        "       b.size_bytes, b.integrity_status, b.integrity_detail "
        "FROM file_locations l JOIN blobs b ON b.content_hash = l.content_hash "
        "WHERE l.status='staged' ORDER BY l.id"
    )
    for r in rows:
        if r["content_hash"] in blobs:
            continue  # first (smallest-id) location represents the blob
        detail = None
        if r["integrity_detail"]:
            try:
                detail = json.loads(r["integrity_detail"])
            except json.JSONDecodeError:
                detail = None
        blobs[r["content_hash"]] = {
            "filetype": (r["filetype"] or "").lower(),
            "size": r["size_bytes"],
            "integrity": r["integrity_status"],
            "detail": detail,
            "md5": r["gdrive_md5"],
            "remote_path": r["remote_path"],
        }
    return blobs


def _find_item_by_blob(conn, role: str, content_hash: str):
    row = conn.execute(
        "SELECT media_item_id FROM media_item_files WHERE role=? AND content_hash=?",
        (role, content_hash),
    ).fetchone()
    return row["media_item_id"] if row else None


def _upsert_item(conn, report: ItemsReport, fmt: str, files: dict[str, str],
                 fields: dict, attrs: dict, dry_run: bool) -> int | None:
    """Create or update the item identified by its defining blob (audio for mp3g/audio_only,
    av for video). Never deletes; never touches quality_verdict once the dedup pass owns it."""
    defining_role = "av" if fmt == "video" else "audio"
    item_id = _find_item_by_blob(conn, defining_role, files[defining_role])
    now = utcnow()
    attrs_json = json.dumps(attrs, ensure_ascii=False, sort_keys=True) if attrs else None
    if item_id is None:
        report.created[fmt] += 1
        if dry_run:
            return None
        cur = conn.execute(
            "INSERT INTO media_items (format, duration_sec, audio_codec, audio_bitrate_kbps, "
            "sample_rate, video_codec, width, height, quality_attrs, quality_verdict, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (fmt, fields["duration_sec"], fields["audio_codec"], fields["audio_bitrate_kbps"],
             fields["sample_rate"], fields["video_codec"], fields["width"], fields["height"],
             attrs_json, now, now),
        )
        item_id = cur.lastrowid
        for role, ch in files.items():
            conn.execute(
                "INSERT INTO media_item_files (media_item_id, content_hash, role) VALUES (?,?,?)",
                (item_id, ch, role),
            )
        return item_id

    # Existing item: bring format/files/fields up to date without disturbing verdicts.
    old = conn.execute("SELECT * FROM media_items WHERE id=?", (item_id,)).fetchone()
    old_files = {
        r["role"]: r["content_hash"]
        for r in conn.execute(
            "SELECT role, content_hash FROM media_item_files WHERE media_item_id=?", (item_id,)
        )
    }
    changed = old["format"] != fmt or old_files != files or old["quality_attrs"] != attrs_json
    for k in ("duration_sec", "audio_codec", "audio_bitrate_kbps", "sample_rate",
              "video_codec", "width", "height"):
        if old[k] != fields[k]:
            changed = True
    if not changed:
        report.existing += 1
        return item_id
    report.updated += 1
    if old["format"] == "audio_only" and fmt == "mp3g":
        report.format_upgraded += 1
    if dry_run:
        return item_id
    conn.execute(
        "UPDATE media_items SET format=?, duration_sec=?, audio_codec=?, audio_bitrate_kbps=?, "
        "sample_rate=?, video_codec=?, width=?, height=?, quality_attrs=?, updated_at=? "
        "WHERE id=?",
        (fmt, fields["duration_sec"], fields["audio_codec"], fields["audio_bitrate_kbps"],
         fields["sample_rate"], fields["video_codec"], fields["width"], fields["height"],
         attrs_json, now, item_id),
    )
    conn.execute("DELETE FROM media_item_files WHERE media_item_id=?", (item_id,))
    for role, ch in files.items():
        conn.execute(
            "INSERT INTO media_item_files (media_item_id, content_hash, role) VALUES (?,?,?)",
            (item_id, ch, role),
        )
    return item_id


def _queue_pair_mismatch(conn, report: ItemsReport, item_id: int | None, payload: dict,
                         dry_run: bool) -> None:
    """One open/answered pair_mismatch per item — never re-ask a question a reviewer settled."""
    if item_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM review_queue WHERE kind='pair_mismatch' AND media_item_id=?",
            (item_id,),
        ).fetchone()
        if exists:
            return
    report.pair_mismatch_queued += 1
    if dry_run or item_id is None:
        return
    conn.execute(
        "INSERT INTO review_queue (kind, media_item_id, payload, created_at) "
        "VALUES ('pair_mismatch', ?, ?, ?)",
        (item_id, json.dumps(payload, ensure_ascii=False), utcnow()),
    )


def _mark_graphics_suspect(conn, graphics_hash: str, verdict: dict, dry_run: bool) -> None:
    """§7.2.4: a failed CDG check marks the graphics blob suspect (merged into its detail)."""
    if dry_run:
        return
    row = conn.execute(
        "SELECT integrity_status, integrity_detail FROM blobs WHERE content_hash=?",
        (graphics_hash,),
    ).fetchone()
    if row is None or row["integrity_status"] == "broken":
        return  # broken is worse than suspect; never upgrade
    try:
        detail = json.loads(row["integrity_detail"]) if row["integrity_detail"] else {}
    except json.JSONDecodeError:
        detail = {}
    detail["cdg_check"] = verdict
    conn.execute(
        "UPDATE blobs SET integrity_status='suspect', integrity_detail=? WHERE content_hash=?",
        (json.dumps(detail, ensure_ascii=False), graphics_hash),
    )


def build_items(conn, *, dry_run: bool = False, budget: int | None = None) -> ItemsReport:
    """§7.2.4: promote staged blobs + provisional pairs into media_items. Idempotent by natural
    key (defining blob); re-run after nothing changed reports 0 created / 0 updated."""
    from . import config as _config
    budget = _config.REVIEW_QUEUE_BUDGET if budget is None else budget
    report = ItemsReport()
    open_before = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue WHERE kind='pair_mismatch' AND resolution IS NULL"
    ).fetchone()["n"]
    blobs = _load_staged_blobs(conn)
    md5_to_hash = {b["md5"]: h for h, b in blobs.items() if b["md5"]}

    # Resolve pairs md5 -> content_hash. Both halves staged is a Stage 1 invariant; count misses.
    audio_pairs: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute("SELECT audio_md5, graphics_md5, witnesses FROM provisional_pairs"):
        ah, gh = md5_to_hash.get(r["audio_md5"]), md5_to_hash.get(r["graphics_md5"])
        if ah is None or gh is None:
            report.unresolvable_pairs += 1
            continue
        audio_pairs.setdefault(ah, []).append((gh, r["witnesses"]))

    role_of_hash: dict[str, str] = {}
    for ch, blob in blobs.items():
        cls = blob_media_class(blob["filetype"], blob["detail"])
        if cls is None:
            report.unclassified_blobs += 1
            continue
        if cls == "graphics":
            report.graphics_blobs += 1
            role_of_hash[ch] = "graphics"
            continue

        fields = probe_item_fields(blob["detail"])
        if cls == "av":
            role_of_hash[ch] = "av"
            _upsert_item(conn, report, "video", {"av": ch}, fields, {}, dry_run)
            continue

        # audio blob: mp3g when paired, audio_only otherwise
        role_of_hash[ch] = "audio"
        candidates = audio_pairs.get(ch)
        if not candidates:
            _upsert_item(conn, report, "audio_only", {"audio": ch}, fields, {}, dry_run)
            continue
        checked = [
            (gh, wit, cdg_check(blobs[gh]["size"], fields["duration_sec"]))
            for gh, wit in candidates
        ]
        chosen, verdict, alternates = choose_graphics(checked)
        if verdict["status"] == "ok":
            report.cdg_ok += 1
        elif verdict["status"] == "failed":
            report.cdg_failed += 1
        else:
            report.cdg_skipped += 1
        attrs: dict = {"cdg_check": verdict}
        if alternates:
            attrs["graphics_alternates"] = alternates
        item_id = _upsert_item(conn, report, "mp3g", {"audio": ch, "graphics": chosen},
                               fields, attrs, dry_run)
        if verdict["status"] == "failed":
            _mark_graphics_suspect(conn, chosen, verdict, dry_run)
            _queue_pair_mismatch(conn, report, item_id, {
                "reason": "cdg_duration_check_failed", "audio_hash": ch,
                "graphics_hash": chosen, "check": verdict,
                "example_path": blob["remote_path"],
            }, dry_run)

    # §11: stop rather than grind through an oversized queue. Projected from the count taken
    # BEFORE this run's own inserts, plus what this run queued — so a dry-run projects the
    # same number the real run would hit, instead of only seeing rows already in the DB.
    if open_before + report.pair_mismatch_queued > budget:
        report.over_budget = True
        if not dry_run:
            conn.rollback()
            return report

    # Roles on staged locations (bookkeeping §3.2): every staged location of a classified blob.
    if not dry_run:
        for ch, role in role_of_hash.items():
            cur = conn.execute(
                "UPDATE file_locations SET role=? "
                "WHERE content_hash=? AND status='staged' AND role IS NOT ?",
                (role, ch, role),
            )
            report.roles_set += cur.rowcount
        conn.commit()
    return report


# --- filename-hint promotion (location_parses -> song_metadata, source 'filename') ----------
#
# Stage 1 parsed EVERY location, including §5.2-excluded duplicates, precisely so this step can
# merge a hash group's hints onto the survivor's item — the duplicate is often the better-
# labelled copy. For each item: gather the parses of every location (any status) sharing a
# gdrive_md5 with any of the item's blobs, then take the best-confidence value per field
# (tie -> smallest location id, deterministic). mp3g items draw from both halves' groups.
#
# Writes only source='filename' rows (§3.6: each pass owns its source) and never touches
# id3/musicbrainz/manual rows. Idempotent: identical values are left untouched and counted.

PROMOTED_FIELDS = ("artist", "title", "language", "disc_id", "disc_series")


def _best_parse_values(parses: list) -> dict[str, tuple[str, float]]:
    """field -> (value, confidence) from a hash group's parses, best confidence first."""
    best: dict[str, tuple[str, float, int]] = {}
    for p in parses:
        vals = {
            "artist": p["artist"],
            "title": p["title"],
            "language": p["language"],
            "disc_series": p["disc_series"],
            "disc_id": (f"{p['disc_id']}-{p['disc_track']}"
                        if p["disc_id"] and p["disc_track"] else p["disc_id"]),
        }
        for f, v in vals.items():
            if v is None or v == "":
                continue
            cur = best.get(f)
            cand = (v, p["confidence"], p["location_id"])
            if cur is None or (cand[1], -cand[2]) > (cur[1], -cur[2]):
                best[f] = cand
    return {f: (v, c) for f, (v, c, _) in best.items()}


def promote_filename_metadata(conn, *, dry_run: bool = False) -> dict:
    """Write each item's best filename-derived hints into song_metadata (source 'filename')."""
    md5s_of_hash: dict[str, set] = {}
    for r in conn.execute(
        "SELECT DISTINCT content_hash, gdrive_md5 FROM file_locations "
        "WHERE content_hash IS NOT NULL AND gdrive_md5 IS NOT NULL"
    ):
        md5s_of_hash.setdefault(r["content_hash"], set()).add(r["gdrive_md5"])

    parses_by_md5: dict[str, list] = {}
    for r in conn.execute(
        "SELECT p.location_id, p.artist, p.title, p.language, p.disc_series, p.disc_id, "
        "       p.disc_track, p.is_instrumental, p.confidence, l.gdrive_md5 "
        "FROM location_parses p JOIN file_locations l ON l.id = p.location_id "
        "WHERE l.gdrive_md5 IS NOT NULL AND p.layout NOT IN ('non_media', 'unparsed')"
    ):
        parses_by_md5.setdefault(r["gdrive_md5"], []).append(r)

    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "items_without_hints": 0,
             "is_instrumental_set": 0}
    items = conn.execute(
        "SELECT i.id, i.is_instrumental, GROUP_CONCAT(f.content_hash) AS hashes "
        "FROM media_items i JOIN media_item_files f ON f.media_item_id = i.id "
        "WHERE i.status='active' GROUP BY i.id"
    ).fetchall()
    now = utcnow()
    for item in items:
        group: list = []
        for ch in (item["hashes"] or "").split(","):
            for md5 in md5s_of_hash.get(ch, ()):
                group.extend(parses_by_md5.get(md5, ()))
        if not group:
            stats["items_without_hints"] += 1
            continue
        best = _best_parse_values(group)
        for fld, (value, confidence) in best.items():
            old = conn.execute(
                "SELECT value, confidence FROM song_metadata "
                "WHERE media_item_id=? AND field=? AND source='filename'",
                (item["id"], fld),
            ).fetchone()
            if old is None:
                stats["inserted"] += 1
                if not dry_run:
                    conn.execute(
                        "INSERT INTO song_metadata (media_item_id, field, value, source, "
                        "confidence, updated_at) VALUES (?,?,?,'filename',?,?)",
                        (item["id"], fld, value, confidence, now),
                    )
            elif old["value"] != value or old["confidence"] != confidence:
                stats["updated"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE song_metadata SET value=?, confidence=?, updated_at=? "
                        "WHERE media_item_id=? AND field=? AND source='filename'",
                        (value, confidence, now, item["id"], fld),
                    )
            else:
                stats["unchanged"] += 1
        # is_instrumental is a media_items column, not a metadata field; best-confidence
        # non-null parse wins, and an existing non-null value is only ever refined, not cleared.
        inst = next(
            (p["is_instrumental"] for p in sorted(group, key=lambda p: -p["confidence"])
             if p["is_instrumental"]),
            None,
        )
        if inst and inst != item["is_instrumental"]:
            stats["is_instrumental_set"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE media_items SET is_instrumental=?, updated_at=? WHERE id=?",
                    (inst, now, item["id"]),
                )
    if not dry_run:
        conn.commit()
    return stats


# --- §7.2.2 ID3 (runs AFTER items exist — see deviation note) -------------------------------
#
# DEVIATION FROM SPEC (documented, deliberate): §7.2 lists ID3 as step 2, before item creation
# (step 4). But ID3 results go to song_metadata, which keys on media_item_id — a table that step
# 4 populates. Same schema gap as location_parses (deviation #9), resolved the same way the spec
# itself resolves it: the pass runs once items exist. Nothing is lost by the reorder; ID3 bytes
# are local and re-readable at any time.
#
# What a 400-file sample of the real library showed (2026-07-18), and what each fact changes:
#   * 68% of mp3s have real artist+title tags — far better than the investigation's guess.
#   * Many are ID3v1, whose 30-byte fields TRUNCATE values ("Don't Think I Don't Think Abou").
#     §3.6 trusts id3 over filename, so writing a truncated tag would have v_metadata serve the
#     truncated title over the filename's full one. A tag value that is a proper prefix of the
#     filename value is therefore the SAME fact, damaged in transit — skipped, counted.
#   * Titles carry "[Karaoke]"-style decorations; artists come comma-inverted ("Jepsen, Carly
#     Rae"). Stage 1's §6.2-gated cleaners handle both; reusing them keeps one set of rules.
#   * "Large ID3<->filename disagreement => review" (§7.2.2): zero token overlap on both artist
#     and title. Disagreeing values are still written (spec) but at low confidence, and the item
#     is queued as metadata_match, once.

ID3_JUNK_VALUES = frozenset({
    "unknown", "unknown artist", "unknown title", "various", "various artists",
    "artist", "title", "no artist", "audiotrack", "track", "untitled", "none",
})
ID3_CONFIDENCE_AGREE = 0.8      # tag and filename tell the same story
ID3_CONFIDENCE_ALONE = 0.7      # no filename value to compare against
ID3_CONFIDENCE_DISAGREE = 0.4   # queued for review; low confidence, but written (§7.2.2)


# ID3 sort-order artists invert with multi-token given names ("Jepsen, Carly Rae"), which
# Stage 1's LASTNAME_FIRST deliberately rejects (filename commas are order *evidence* there).
# Guard: real band names carry commas too ("Earth, Wind & Fire"), so no swap when either side
# has an ampersand/'and' or the tail runs past two tokens.
_ID3_INVERTED = re.compile(r"^([^,&]{2,30}),\s+([^,&]{1,30})$")


def _unswap_id3_artist(name: str) -> str:
    m = _ID3_INVERTED.match(name.strip())
    if not m:
        return name.strip()
    last, given = m.group(1).strip(), m.group(2).strip()
    if len(given.split()) > 2 or re.search(r"\band\b", given, re.I) \
            or re.search(r"\band\b", last, re.I):
        return name.strip()
    return f"{given} {last}"


def clean_id3_value(value: str | None, *, is_artist: bool = False) -> str | None:
    """Normalize one tag value with Stage 1's gated cleaners; None for junk."""
    from . import stage1
    if not value:
        return None
    v = value.strip().strip("\x00").strip()
    if not v or v.lower() in ID3_JUNK_VALUES or re.fullmatch(r"track\s*\d+", v, re.I) \
            or re.fullmatch(r"\d+", v):
        return None
    v, _flags = stage1.extract_decorations(v)
    if is_artist:
        v = _unswap_id3_artist(v)
    v = v.strip()
    return v or None


def _tokens(s: str | None) -> set[str]:
    return set(re.findall(r"[^\W_]+", (s or "").lower(), re.UNICODE))


def id3_vs_filename(id3_val: str | None, file_val: str | None) -> str:
    """'agree' / 'truncated_prefix' / 'disagree' / 'alone' for one field."""
    if id3_val is None:
        return "absent"
    if not file_val:
        return "alone"
    a, b = id3_val.lower().strip(), file_val.lower().strip()
    if a == b or _tokens(id3_val) & _tokens(file_val):
        # ID3v1's 30-byte fields truncate: a strictly shorter tag that prefixes the filename
        # value is the same fact damaged in transit, not new information.
        if len(a) < len(b) and b.startswith(a):
            return "truncated_prefix"
        return "agree"
    return "disagree"


def read_id3(path: Path) -> dict | None:
    """Raw easy-tag values for one file, or None when unreadable/untagged. Never raises."""
    from mutagen import File as MFile
    try:
        f = MFile(str(path), easy=True)
    except Exception:
        return None
    if f is None or not f.tags:
        return None
    def first(key):
        vals = f.tags.get(key)
        return vals[0] if vals else None
    return {"artist": first("artist"), "title": first("title"),
            "date": first("date"), "genre": first("genre")}


@dataclass
class Id3Report:
    worklist: int = 0
    tagged: int = 0
    untagged: int = 0
    fields_written: int = 0
    fields_unchanged: int = 0
    truncated_skipped: int = 0
    junk_skipped: int = 0
    disagreements: int = 0
    metadata_match_queued: int = 0
    over_budget: bool = False
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def id3_worklist(conn, limit: int | None = None) -> list:
    """Items with an audio blob and no id3 metadata yet, plus path + filename values."""
    rows = conn.execute(
        """
        SELECT i.id AS item_id, f.content_hash,
               (SELECT l.local_path FROM file_locations l
                 WHERE l.content_hash = f.content_hash AND l.status='staged'
                 ORDER BY l.id LIMIT 1) AS local_path,
               (SELECT m.value FROM song_metadata m
                 WHERE m.media_item_id = i.id AND m.field='artist' AND m.source='filename')
                 AS file_artist,
               (SELECT m.value FROM song_metadata m
                 WHERE m.media_item_id = i.id AND m.field='title' AND m.source='filename')
                 AS file_title
        FROM media_items i
        JOIN media_item_files f ON f.media_item_id = i.id AND f.role = 'audio'
        WHERE i.status = 'active'
          AND NOT EXISTS (SELECT 1 FROM song_metadata m2
                          WHERE m2.media_item_id = i.id AND m2.source = 'id3')
          AND NOT EXISTS (SELECT 1 FROM review_queue r
                          WHERE r.kind='metadata_match' AND r.media_item_id = i.id)
        ORDER BY i.id
        """
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def id3_all(conn, *, limit: int | None = None, budget: int | None = None,
            progress_every: int = 2000,
            reader: Callable[[Path], dict | None] = read_id3,
            on_progress: Callable[[Id3Report], None] | None = None) -> Id3Report:
    """§7.2.2 over every audio item. Resumable (worklist = items with no id3 rows yet);
    per-item commits; §11 budget stop on the metadata_match queue."""
    from . import config as _config
    budget = _config.REVIEW_QUEUE_BUDGET if budget is None else budget
    worklist = id3_worklist(conn, limit)
    report = Id3Report(worklist=len(worklist))
    started = time.monotonic()
    open_reviews = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue "
        "WHERE kind='metadata_match' AND resolution IS NULL"
    ).fetchone()["n"]
    try:
        for i, row in enumerate(worklist, 1):
            if open_reviews > budget:
                report.over_budget = True
                report.stopped_reason = "review_budget"
                break
            raw = reader(Path(row["local_path"])) if row["local_path"] else None
            if raw is None:
                report.untagged += 1
                # A no-tag marker row keeps the item out of future worklists (resumability
                # without rescanning 23k untagged files every run). value NULL is never
                # served by v_metadata.
                conn.execute(
                    "INSERT OR IGNORE INTO song_metadata (media_item_id, field, value, source, "
                    "confidence, updated_at) VALUES (?, 'title', NULL, 'id3', NULL, ?)",
                    (row["item_id"], utcnow()),
                )
                conn.commit()
                continue

            artist = clean_id3_value(raw.get("artist"), is_artist=True)
            title = clean_id3_value(raw.get("title"))
            if (raw.get("artist") or raw.get("title")) and not (artist or title):
                report.junk_skipped += 1

            verdicts = {"artist": id3_vs_filename(artist, row["file_artist"]),
                        "title": id3_vs_filename(title, row["file_title"])}
            disagree = (verdicts["artist"] == "disagree" and verdicts["title"] != "agree") or \
                       (verdicts["title"] == "disagree" and verdicts["artist"] != "agree")
            if disagree and \
                    id3_vs_filename(artist, row["file_title"]) in ("agree", "truncated_prefix") and \
                    id3_vs_filename(title, row["file_artist"]) in ("agree", "truncated_prefix"):
                # Cross-match: the filename parse has artist/title in the other order (§6.1's
                # order ambiguity) and the tag is telling us which order is right. That is a
                # resolution, not a disagreement.
                disagree = False
                verdicts = {"artist": "agree", "title": "agree"}
            values: list[tuple[str, str, float]] = []
            for fld, val in (("artist", artist), ("title", title)):
                v = verdicts[fld]
                if v == "truncated_prefix":
                    report.truncated_skipped += 1
                    continue
                if v == "absent":
                    continue
                conf = (ID3_CONFIDENCE_DISAGREE if disagree
                        else ID3_CONFIDENCE_ALONE if v == "alone"
                        else ID3_CONFIDENCE_AGREE)
                values.append((fld, val, conf))
            year = (raw.get("date") or "")[:4]
            if re.fullmatch(r"(19|20)\d\d", year):
                values.append(("year", year, ID3_CONFIDENCE_ALONE))
            genre = (raw.get("genre") or "").strip()
            if genre and genre.lower() not in ("other", "unknown", "genre", "misc", "karaoke"):
                values.append(("genre", json.dumps([genre], ensure_ascii=False),
                               ID3_CONFIDENCE_ALONE))

            if not values:
                report.untagged += 1
                conn.execute(
                    "INSERT OR IGNORE INTO song_metadata (media_item_id, field, value, source, "
                    "confidence, updated_at) VALUES (?, 'title', NULL, 'id3', NULL, ?)",
                    (row["item_id"], utcnow()),
                )
                conn.commit()
                continue

            report.tagged += 1
            now = utcnow()
            for fld, val, conf in values:
                conn.execute(
                    "INSERT INTO song_metadata (media_item_id, field, value, source, "
                    "confidence, updated_at) VALUES (?,?,?,'id3',?,?) "
                    "ON CONFLICT(media_item_id, field, source) DO UPDATE "
                    "SET value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at",
                    (row["item_id"], fld, val, conf, now),
                )
                report.fields_written += 1
            if disagree:
                report.disagreements += 1
                conn.execute(
                    "INSERT INTO review_queue (kind, media_item_id, payload, created_at) "
                    "VALUES ('metadata_match', ?, ?, ?)",
                    (row["item_id"], json.dumps({
                        "reason": "id3_filename_disagreement",
                        "id3": {"artist": artist, "title": title},
                        "filename": {"artist": row["file_artist"], "title": row["file_title"]},
                    }, ensure_ascii=False), now),
                )
                report.metadata_match_queued += 1
                open_reviews += 1
            conn.commit()
            if on_progress and (i % progress_every == 0 or i == len(worklist)):
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    report.elapsed_sec = time.monotonic() - started
    return report


# --- §7.2.5 fingerprints (+ the §7.3 benchmark gate) ----------------------------------------
#
# `fpcalc -raw -json` per blob -> `fingerprints`. Worklist: every audio-carrying blob of an
# active item — the audio blob of mp3g/audio_only items and the av blob of video items (fpcalc
# demuxes the audio stream itself; the fingerprint stays keyed by the video blob's hash, §3.4).
# cdg blobs carry no audio; broken blobs are skipped (fpcalc failure is certain and means
# nothing new). fpcalc failure on a probed-ok blob => 'suspect' (§7.2.5).
#
# Chosen encoding (documented per §3.4): `zb64:` + base64(zlib(little-endian uint32 array)) of
# the RAW fingerprint. Raw (not AcoustID-compressed) so similarity math is a straight XOR+
# popcount away; zlib+base64 keeps ~23k rows at ~4 KB each instead of ~20 KB of JSON. fpcalc's
# default 120 s analysis window is kept — it is what AcoustID itself matches against, and §7.3's
# prefix-comparison for truncation detection only ever compares overlapping windows anyway.

FP_LENGTH_SEC = 120           # fpcalc default; recorded here so it is a decision, not an accident
FPCALC_TIMEOUT_SEC = 300
FP_PREFIX = "zb64:"


def fpcalc_file(path: Path) -> dict:
    """Run fpcalc; return {'duration': float, 'fingerprint': [uint32...]} or {'error': str}."""
    cmd = ["fpcalc", "-raw", "-json", "-length", str(FP_LENGTH_SEC), str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=FPCALC_TIMEOUT_SEC, check=False)
    except subprocess.TimeoutExpired:
        return {"error": f"fpcalc timeout after {FPCALC_TIMEOUT_SEC}s"}
    except OSError as exc:
        return {"error": f"fpcalc spawn failed: {exc}"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or f"fpcalc rc={proc.returncode}").strip()[:500]}
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "fpcalc emitted unparseable JSON"}
    if not out.get("fingerprint"):
        return {"error": "fpcalc returned an empty fingerprint"}
    return out


def encode_fp(ints: list[int]) -> str:
    import base64
    import struct
    import zlib
    raw = struct.pack(f"<{len(ints)}I", *(i & 0xFFFFFFFF for i in ints))
    return FP_PREFIX + base64.b64encode(zlib.compress(raw, 6)).decode("ascii")


def decode_fp(text: str):
    """-> numpy uint32 array. numpy is a §13 dependency; imported lazily like the other tools."""
    import base64
    import zlib
    import numpy as np
    if not text.startswith(FP_PREFIX):
        raise ValueError("unknown fingerprint encoding")
    raw = zlib.decompress(base64.b64decode(text[len(FP_PREFIX):]))
    return np.frombuffer(raw, dtype="<u4")


def fp_similarity(a, b, *, max_offset: int = 3) -> float:
    """Chromaprint bit-error similarity over the overlapping prefix, best of small alignments.

    1.0 = identical; ~0.5 = unrelated (random bits). Offsets beyond a few frames are not
    needed for same-recording re-encodes, which start at the same audio (±container padding).
    """
    import numpy as np
    best = 0.0
    for off in range(-max_offset, max_offset + 1):
        aa = a[off:] if off >= 0 else a
        bb = b[-off:] if off < 0 else b
        n = min(len(aa), len(bb))
        if n < 16:   # too little overlap to mean anything
            continue
        x = np.bitwise_xor(aa[:n], bb[:n])
        # vectorized popcount over the packed uint32s, via the uint8 view
        bits = np.unpackbits(x.view(np.uint8)).sum()
        best = max(best, 1.0 - bits / (32.0 * n))
    return best


@dataclass
class FpReport:
    worklist: int = 0
    fingerprinted: int = 0
    failed: int = 0
    marked_suspect: int = 0
    skipped_broken: int = 0
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def fingerprint_worklist(conn, limit: int | None = None) -> list:
    """Audio-carrying blobs of active items with no fingerprint yet, non-broken, with a path."""
    rows = conn.execute(
        """
        SELECT DISTINCT f.content_hash, b.integrity_status,
               (SELECT l.local_path FROM file_locations l
                 WHERE l.content_hash = f.content_hash AND l.status='staged'
                 ORDER BY l.id LIMIT 1) AS local_path
        FROM media_item_files f
        JOIN media_items i ON i.id = f.media_item_id AND i.status = 'active'
        JOIN blobs b ON b.content_hash = f.content_hash
        WHERE f.role IN ('audio', 'av')
          AND b.integrity_status != 'broken'
          AND NOT EXISTS (SELECT 1 FROM fingerprints fp WHERE fp.content_hash = f.content_hash)
        ORDER BY f.content_hash
        """
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def count_skipped_broken(conn) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT f.content_hash) AS n FROM media_item_files f "
        "JOIN blobs b ON b.content_hash = f.content_hash "
        "WHERE f.role IN ('audio','av') AND b.integrity_status='broken'"
    ).fetchone()["n"]


def _mark_fp_suspect(conn, content_hash: str, error: str) -> None:
    row = conn.execute(
        "SELECT integrity_status, integrity_detail FROM blobs WHERE content_hash=?",
        (content_hash,),
    ).fetchone()
    if row is None or row["integrity_status"] == "broken":
        return
    try:
        detail = json.loads(row["integrity_detail"]) if row["integrity_detail"] else {}
    except json.JSONDecodeError:
        detail = {}
    detail["fpcalc_error"] = error[:300]
    conn.execute(
        "UPDATE blobs SET integrity_status='suspect', integrity_detail=? WHERE content_hash=?",
        (json.dumps(detail, ensure_ascii=False), content_hash),
    )


def fingerprint_all(conn, *, limit: int | None = None, workers: int = PROBE_WORKERS,
                    progress_every: int = 200,
                    fper: Callable[[Path], dict] = fpcalc_file,
                    on_progress: Callable[[FpReport], None] | None = None) -> FpReport:
    """§7.2.5 over all unfingerprinted audio-carrying blobs. Same envelope as probe_all:
    subprocess work in a small thread pool, DB writes on this thread, one commit per blob."""
    worklist = fingerprint_worklist(conn, limit)
    report = FpReport(worklist=len(worklist), skipped_broken=count_skipped_broken(conn))
    started = time.monotonic()

    def _fp(row) -> tuple:
        return row, fper(Path(row["local_path"])) if row["local_path"] else {"error": "no local path"}

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (row, out) in enumerate(pool.map(_fp, worklist), 1):
                if "error" in out:
                    report.failed += 1
                    _mark_fp_suspect(conn, row["content_hash"], out["error"])
                    report.marked_suspect += 1
                    if len(report.failures) < 200:
                        report.failures.append({"content_hash": row["content_hash"],
                                                "detail": out["error"][:200]})
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO fingerprints "
                        "(content_hash, chromaprint, fp_duration_sec) VALUES (?,?,?)",
                        (row["content_hash"], encode_fp(out["fingerprint"]),
                         float(out.get("duration") or 0) or None),
                    )
                    report.fingerprinted += 1
                conn.commit()
                if on_progress and (i % progress_every == 0 or i == len(worklist)):
                    report.elapsed_sec = time.monotonic() - started
                    on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    report.elapsed_sec = time.monotonic() - started
    return report


def fingerprint_benchmark(conn, sample: int = 1000) -> dict:
    """§7.3 PHASE GATE evidence: timing + candidate-pair quality on a sample.

    Reuses whatever fingerprints already exist (the pass is resumable, so benchmark work is a
    head start on the full run, not throwaway). Reports: fpcalc timing projections, the
    similarity distribution over ±10s duration-blocked candidate pairs, and the highest-scoring
    pairs with their filename metadata for human eyeballing.
    """
    existing = conn.execute("SELECT COUNT(*) AS n FROM fingerprints").fetchone()["n"]
    t0 = time.monotonic()
    fp_report = None
    if existing < sample:
        fp_report = fingerprint_all(conn, limit=sample - existing)
    fp_elapsed = time.monotonic() - t0

    rows = conn.execute(
        """
        SELECT fp.content_hash, fp.chromaprint, i.id AS item_id, i.duration_sec,
               (SELECT value FROM v_metadata m WHERE m.media_item_id=i.id AND m.field='artist')
                   AS artist,
               (SELECT value FROM v_metadata m WHERE m.media_item_id=i.id AND m.field='title')
                   AS title
        FROM fingerprints fp
        JOIN media_item_files f ON f.content_hash = fp.content_hash AND f.role IN ('audio','av')
        JOIN media_items i ON i.id = f.media_item_id AND i.status='active'
        ORDER BY fp.content_hash LIMIT ?
        """,
        (sample,),
    ).fetchall()

    fps = {r["content_hash"]: decode_fp(r["chromaprint"]) for r in rows}
    by_dur = sorted((r for r in rows if r["duration_sec"]), key=lambda r: r["duration_sec"])
    candidates = []
    for ai in range(len(by_dur)):
        for bi in range(ai + 1, len(by_dur)):
            if by_dur[bi]["duration_sec"] - by_dur[ai]["duration_sec"] > 10.0:
                break
            candidates.append((by_dur[ai], by_dur[bi]))

    t1 = time.monotonic()
    scored = []
    for a, b in candidates:
        sim = fp_similarity(fps[a["content_hash"]], fps[b["content_hash"]])
        scored.append((sim, a, b))
    cmp_elapsed = time.monotonic() - t1
    scored.sort(key=lambda s: -s[0])

    from . import config as _config
    hist: dict[str, int] = {}
    for sim, _, _ in scored:
        bucket = f"{int(sim * 10) / 10:.1f}"
        hist[bucket] = hist.get(bucket, 0) + 1
    total_blobs = conn.execute(
        "SELECT COUNT(DISTINCT f.content_hash) AS n FROM media_item_files f "
        "JOIN media_items i ON i.id=f.media_item_id AND i.status='active' "
        "WHERE f.role IN ('audio','av')"
    ).fetchone()["n"]
    per_file = (fp_report.elapsed_sec / max(1, fp_report.fingerprinted)) if fp_report and fp_report.fingerprinted else None
    return {
        "sample_fingerprints": len(rows),
        "fpcalc": {
            "newly_fingerprinted": fp_report.fingerprinted if fp_report else 0,
            "failed": fp_report.failed if fp_report else 0,
            "sec_per_file": round(per_file, 2) if per_file else None,
            "projected_hours_for_all": round(per_file * total_blobs / 3600, 1) if per_file else None,
            "total_audio_carrying_blobs": total_blobs,
        },
        "comparison": {
            "candidate_pairs_in_sample": len(candidates),
            "compare_sec": round(cmp_elapsed, 2),
            "pairs_per_sec": round(len(candidates) / cmp_elapsed, 0) if cmp_elapsed > 0 else None,
            "similarity_histogram": dict(sorted(hist.items())),
            "auto_merge_threshold": _config.CLUSTER_AUTO_MERGE,
            "candidate_threshold": _config.CLUSTER_CANDIDATE,
            "would_auto_merge": sum(1 for s, _, _ in scored if s >= _config.CLUSTER_AUTO_MERGE),
            "would_review": sum(1 for s, _, _ in scored
                                if _config.CLUSTER_CANDIDATE <= s < _config.CLUSTER_AUTO_MERGE),
        },
        "top_pairs": [
            {"similarity": round(sim, 3),
             "a": {"artist": a["artist"], "title": a["title"], "dur": a["duration_sec"]},
             "b": {"artist": b["artist"], "title": b["title"], "dur": b["duration_sec"]}}
            for sim, a, b in scored[:25]
        ],
    }
