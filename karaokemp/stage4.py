"""Stage 4 §8 — full-decode verification, survivors only.

`ffmpeg -v error -i <f> -f null -` on the defining (audio/av) blob of every active item
whose verdict is `winner` or `sole_copy` — ≈24.7k blobs instead of all 51k. `alternate` items
are the lower-ranked copies of a song: they are still ACTIVE and are never archived (§7.4,
2026-07-31), they simply are not the copy served by default, so spending a full decode on them
before anyone asks for one would be the same wasted compute this worklist exists to avoid.
manual_review items wait for the operator (decode them after resolution by just re-running
this pass; the worklist recomputes).

Classification (deviation #15, retuned from the §8 benchmark's failure signatures —
the strict any-stderr policy would have broken ~1,200 PLAYABLE files):
  * `[null @ …]` stderr lines are the null MUXER complaining (non-monotonic dts etc.) —
    they say nothing about the input's decodability and are ignored; the benchmark's .dat
    "failures" had ZERO decode-side errors.
  * nonzero exit ⇒ broken (the decoder gave up).
  * decode-side error lines ≤ DECODE_GLITCH_TOLERANCE_LINES ⇒ `decoded_ok` with
    `decode_glitches` recorded — a single "Header missing" frame or one damaged macroblock
    (measured: 2 lines each) is a momentary artifact, and sha-yol's standard for broken is
    UNPLAYABLE, not imperfect. More lines than that ⇒ pervasive damage ⇒ broken.
A clean decode also ADJUDICATES the fpcalc/probe `suspect`s that survived dedup:
chromaprint's decoder chokes on a truncated final frame, but if ffmpeg decodes the whole
stream the file is playable and the suspicion is cleared.

Failure handling (§8): decode failure ⇒ broken ⇒ the cluster needs a new winner. That is
exactly a `verdicts` re-run (broken ranks last, next-best is crowned; a failed sole copy
becomes a broken sole copy and sha-yol's 2026-07-19 correction queues it for replacement).
`decode_until_stable` alternates decode → verdicts until a pass has zero failures; each
iteration permanently demotes ≥1 blob, so it terminates. Only then are verdicts frozen.

Unattended + resumable like every other pass: worklist = not-yet-decoded, per-blob commits,
ffmpeg in a small thread pool, DB writes on the calling thread, Ctrl-C-safe.
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DECODE_WORKERS = 4          # nproc on penguin; ffmpeg decode is CPU-bound
DECODE_TIMEOUT_SEC = 900    # a 4-minute video decodes in seconds; 15 min means a hang


def classify_decode(returncode: int, stderr: str) -> dict:
    """Pure classification of one decode run (policy above; test-pinned)."""
    from . import config as _config
    lines = [l for l in (stderr or "").splitlines()
             if l.strip() and not l.startswith("[null @")]
    if returncode != 0:
        return {"ok": False, "detail": "\n".join(lines)[:500] or f"rc={returncode}",
                "glitches": len(lines)}
    tolerance = _config.DECODE_GLITCH_TOLERANCE_LINES
    if len(lines) > tolerance:
        return {"ok": False, "glitches": len(lines),
                "detail": f"{len(lines)} decode-error lines (> {tolerance}); "
                          f"first: {lines[0][:200]}"}
    if lines:
        return {"ok": True, "glitches": len(lines), "detail": "\n".join(lines)[:500]}
    return {"ok": True, "glitches": 0, "detail": ""}


def ffmpeg_decode(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=DECODE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"decode timeout >{DECODE_TIMEOUT_SEC}s", "glitches": 0}
    except OSError as exc:
        return {"ok": False, "detail": f"exec error: {exc}", "glitches": 0}
    return classify_decode(proc.returncode, proc.stderr)


@dataclass
class DecodeReport:
    worklist: int = 0
    decoded_ok: int = 0
    glitchy_ok: int = 0     # decoded_ok but with decode_glitches recorded
    failed: int = 0
    suspects_cleared: int = 0
    suspects_demoted: int = 0
    no_path: int = 0
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def decode_worklist(conn, limit: int | None = None) -> list:
    """Defining blobs of active winner/sole_copy items, not yet decoded_ok and not broken."""
    rows = conn.execute(
        """
        SELECT DISTINCT b.content_hash, b.integrity_status, b.integrity_detail,
               (SELECT l.local_path FROM file_locations l
                 WHERE l.content_hash = b.content_hash AND l.status='staged'
                 ORDER BY l.id LIMIT 1) AS local_path
        FROM media_items i
        JOIN media_item_files f ON f.media_item_id = i.id AND f.role IN ('audio', 'av')
        JOIN blobs b ON b.content_hash = f.content_hash
        WHERE i.status = 'active'
          AND i.quality_verdict IN ('winner', 'sole_copy')
          AND b.integrity_status NOT IN ('decoded_ok', 'broken')
        ORDER BY b.content_hash
        """
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def _apply_result(conn, row, out: dict, report: DecodeReport) -> None:
    was_suspect = row["integrity_status"] == "suspect"
    try:
        detail = json.loads(row["integrity_detail"]) if row["integrity_detail"] else {}
    except json.JSONDecodeError:
        detail = {}
    if out["ok"]:
        detail["decoded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        detail.pop("decode_error", None)
        if out.get("glitches"):
            detail["decode_glitches"] = out["glitches"]
            detail["decode_glitch_sample"] = out["detail"][:300]
            report.glitchy_ok += 1
        else:
            detail.pop("decode_glitches", None)
            detail.pop("decode_glitch_sample", None)
        conn.execute(
            "UPDATE blobs SET integrity_status='decoded_ok', integrity_detail=? "
            "WHERE content_hash=?",
            (json.dumps(detail, ensure_ascii=False), row["content_hash"]))
        report.decoded_ok += 1
        if was_suspect:
            report.suspects_cleared += 1
    else:
        detail["decode_error"] = out["detail"]
        conn.execute(
            "UPDATE blobs SET integrity_status='broken', integrity_detail=? "
            "WHERE content_hash=?",
            (json.dumps(detail, ensure_ascii=False), row["content_hash"]))
        report.failed += 1
        if was_suspect:
            report.suspects_demoted += 1
        if len(report.failures) < 200:
            report.failures.append({"content_hash": row["content_hash"],
                                    "detail": out["detail"][:200]})
    conn.commit()


def decode_all(conn, *, limit: int | None = None, workers: int = DECODE_WORKERS,
               progress_every: int = 100,
               decoder: Callable[[Path], dict] = ffmpeg_decode,
               on_progress: Callable[[DecodeReport], None] | None = None) -> DecodeReport:
    """One §8 pass over the current worklist. Same envelope as probe/fingerprint: subprocess
    work in a thread pool, DB writes on this thread, one commit per blob."""
    worklist = decode_worklist(conn, limit)
    report = DecodeReport(worklist=len(worklist))
    started = time.monotonic()

    def _dec(row) -> tuple:
        if not row["local_path"]:
            return row, {"ok": False, "detail": "no staged local path", "_no_path": True}
        return row, decoder(Path(row["local_path"]))

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (row, out) in enumerate(pool.map(_dec, worklist), 1):
                if out.get("_no_path"):
                    # bookkeeping hole, not media damage: leave integrity alone, count it
                    report.no_path += 1
                else:
                    _apply_result(conn, row, out, report)
                if on_progress and (i % progress_every == 0 or i == len(worklist)):
                    report.elapsed_sec = time.monotonic() - started
                    on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    report.elapsed_sec = time.monotonic() - started
    return report


def decode_until_stable(conn, *, workers: int = DECODE_WORKERS,
                        decoder: Callable[[Path], dict] = ffmpeg_decode,
                        on_progress=None, log=print, max_rounds: int = 20) -> dict:
    """§8 promotion loop: decode → (on failures) re-run verdicts to crown replacements →
    decode the newly-promoted → … until a round has zero failures."""
    from .verdicts import run_verdicts
    rounds = []
    for rnd in range(1, max_rounds + 1):
        rep = decode_all(conn, workers=workers, decoder=decoder, on_progress=on_progress)
        rounds.append(rep.as_dict())
        log(f"round {rnd}: worklist={rep.worklist} ok={rep.decoded_ok} failed={rep.failed}")
        if rep.stopped_reason == "interrupted":
            return {"rounds": rounds, "stable": False, "stopped_reason": "interrupted"}
        if rep.failed == 0:
            return {"rounds": rounds, "stable": True, "stopped_reason": None}
        vrep = run_verdicts(conn)
        log(f"  verdicts re-run: changed={vrep.verdicts_changed} "
            f"quality_flag+={vrep.quality_flag_queued}")
        if vrep.over_budget:
            return {"rounds": rounds, "stable": False,
                    "stopped_reason": f"verdicts over budget: {vrep.stopped_reason}"}
    return {"rounds": rounds, "stable": False, "stopped_reason": "max_rounds"}


def decode_benchmark(conn, sample: int = 100) -> dict:
    """Small §8 phase gate: time a stratified sample (audio + video) for a projection.
    Does real decodes and WRITES results (the pass is resumable; work is never wasted)."""
    import random
    worklist = decode_worklist(conn)
    audio = [r for r in worklist if r["local_path"] and not _looks_video(r["local_path"])]
    video = [r for r in worklist if r["local_path"] and _looks_video(r["local_path"])]
    rng = random.Random(8)
    take_v = min(len(video), max(sample // 4, 10))
    take_a = min(len(audio), sample - take_v)
    picked = rng.sample(audio, take_a) + rng.sample(video, take_v)
    report = DecodeReport(worklist=len(picked))
    t0 = time.monotonic()
    per_kind = {"audio": [0, 0.0], "video": [0, 0.0]}   # count, sec
    with ThreadPoolExecutor(max_workers=DECODE_WORKERS) as pool:
        def _one(row):
            k0 = time.monotonic()
            out = ffmpeg_decode(Path(row["local_path"]))
            return row, out, time.monotonic() - k0
        for row, out, sec in pool.map(_one, picked):
            kind = "video" if _looks_video(row["local_path"]) else "audio"
            per_kind[kind][0] += 1
            per_kind[kind][1] += sec
            _apply_result(conn, row, out, report)
    elapsed = time.monotonic() - t0
    a_n, a_s = per_kind["audio"]
    v_n, v_s = per_kind["video"]
    # wall-clock throughput with the pool ≈ elapsed / n; per-kind sec are single-file costs
    proj = {
        "sample": len(picked), "elapsed_sec": round(elapsed, 1),
        "wall_sec_per_file": round(elapsed / max(len(picked), 1), 2),
        "audio": {"n": a_n, "cpu_sec_per_file": round(a_s / a_n, 2) if a_n else None},
        "video": {"n": v_n, "cpu_sec_per_file": round(v_s / v_n, 2) if v_n else None},
        "remaining_audio": len(audio) - take_a, "remaining_video": len(video) - take_v,
        "failed_in_sample": report.failed,
        "failures": report.failures[:10],
    }
    if a_n and v_n:
        est = ((len(audio) - take_a) * (a_s / a_n) + (len(video) - take_v) * (v_s / v_n)) \
              / DECODE_WORKERS
        proj["projected_hours_remaining"] = round(est / 3600, 1)
    return proj


def _looks_video(path: str) -> bool:
    from .stage3 import VIDEO_EXTS
    return Path(path).suffix.lstrip(".").lower() in VIDEO_EXTS
