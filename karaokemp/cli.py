"""CLI for the karaokemp pipeline. Plain commands, no daemons (§13)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, db, stage0, stage1, stage2, stage3, stage5


def _fmt_report(rep: dict) -> str:
    return json.dumps(rep, indent=2, ensure_ascii=False)


def cmd_init(args: argparse.Namespace) -> int:
    path = db.init_db()
    print(f"library root : {config.LIBRARY_ROOT}")
    print(f"database     : {path}")
    with db.connect() as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        views = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
        ]
    print(f"tables ({len(tables)}) : {', '.join(tables)}")
    print(f"views  ({len(views)}) : {', '.join(views)}")
    return 0


def cmd_enumerate(args: argparse.Namespace) -> int:
    """Stage 0 §5.1 — enumerate Drive, upsert file_locations. No downloads, no Drive writes."""
    db.init_db()
    if args.from_dump:
        dump = Path(args.from_dump)
        print(f"reusing dump : {dump}")
    else:
        print(f"enumerating  : {config.RCLONE_REMOTE}: folder {config.DRIVE_ROOT_FOLDER_ID}")
        print("(rclone lsjson -R --hash --files-only; this takes a while over ~53k files)")
        dump = stage0.enumerate_drive()
        print(f"dump written : {dump} ({dump.stat().st_size / 1024**2:.1f} MiB)")

    entries = stage0.load_enumeration(dump)
    print(f"entries      : {len(entries):,}")

    with db.pipeline_run("stage0_enumerate", notes=f"dump={dump.name}") as run:
        with db.connect() as conn:
            stats = stage0.upsert_locations(conn, entries)
            conn.commit()
            removals = stage0.detect_removals(conn, entries)
            run["items_processed"] = len(entries)
            run["items_failed"] = stats.get("missing_id", 0)
            run["report"] = {
                "dump": str(dump),
                "upsert": stats,
                "removed_from_drive": {"count": len(removals), "ids": removals[:100]},
                "review_queue_sizes": db.review_queue_sizes(conn),
            }
            print(_fmt_report(run["report"]))
    if removals:
        print(f"\nNOTE: {len(removals)} indexed file(s) no longer on Drive — reported, not mutated.")
    return 0


def cmd_dedup(args: argparse.Namespace) -> int:
    """Stage 0 §5.2 — logical exact-dup pass. Index-only; nothing on Drive moves."""
    with db.connect() as conn:
        if args.dry_run:
            res = stage0.exact_dup_pass(conn, dry_run=True)
            print("DRY RUN — no changes written\n")
            print(_fmt_report({k: v for k, v in res.items() if k != "plan"}))
            print(f"\nplanned actions ({len(res['plan'])}):")
            for action, fid, path in res["plan"]:
                print(f"  {action:18} {fid}  {path}")
            return 0

    with db.pipeline_run("stage0_dedup") as run:
        with db.connect() as conn:
            res = stage0.exact_dup_pass(conn, dry_run=False)
            run["items_processed"] = res["duplicate_groups"]
            run["report"] = {**res, "review_queue_sizes": db.review_queue_sizes(conn)}
            print(_fmt_report({k: v for k, v in res.items() if k != "plan"}))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Stage 0 §5.3 — inventory report. PHASE GATE: review before finalizing Stage 1–3."""
    with db.pipeline_run("stage0_report") as run:
        with db.connect() as conn:
            rep = stage0.inventory_report(conn)
            run["items_processed"] = rep["totals"]["files"]
            run["report"] = {**rep, "review_queue_sizes": db.review_queue_sizes(conn)}
    out = config.LOGS_DIR / f"inventory-report-{db.utcnow().replace(':', '')}.json"
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    trimmed = {k: v for k, v in rep.items() if k != "filename_sample"}
    print(_fmt_report(trimmed))
    print(f"\nfull report (incl. {len(rep['filename_sample'])} filename sample): {out}")
    return 0


def cmd_parse_stats(args: argparse.Namespace) -> int:
    """Stage 1 §6.1 — dry parse of every filename. Writes nothing; this is the §6.2 evidence."""
    with db.connect() as conn:
        stats = stage1.parse_stats(conn)
    print(_fmt_report(stats))
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    """Stage 1 §6.1 — parse every location into location_parses. Gated on §6.2 (PASSED)."""
    if args.dry_run:
        with db.connect() as conn:
            res = stage1.write_parses(conn, dry_run=True)
        print("DRY RUN — no rows written\n")
        print(_fmt_report(res))
        return 0

    with db.pipeline_run("stage1_parse", notes=f"parser={stage1.parser_version()}") as run:
        with db.connect() as conn:
            res = stage1.write_parses(conn)
            run["items_processed"] = res["locations"]
            run["report"] = {**res, "review_queue_sizes": db.review_queue_sizes(conn)}
            print(_fmt_report(res))
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    """Stage 1 §6.3 — provisional MP3+CDG pairing, computed over the full pre-exclusion listing
    and stored at content level so §5.2's pair-blind survivor rule cannot split a pair."""
    if args.dry_run:
        with db.connect() as conn:
            res = stage1.pair_mp3g(conn, dry_run=True)
        print("DRY RUN — no changes written\n")
        print(_fmt_report({k: v for k, v in res.items() if k != "plan"}))
        print(f"\nplanned location changes (first {len(res['plan'])}):")
        for action, path in res["plan"]:
            print(f"  {action:8} {path}")
        return 0

    with db.pipeline_run("stage1_pair") as run:
        with db.connect() as conn:
            res = stage1.pair_mp3g(conn)
            run["items_processed"] = res["pairs"]["content_pairs"]
            run["report"] = {**res, "review_queue_sizes": db.review_queue_sizes(conn)}
            print(_fmt_report({k: v for k, v in res.items() if k != "plan"}))
    if res["over_budget"]:
        # §11: exceeding the budget is a stop condition, not a speed bump.
        print(
            f"\nSTOPPED — pair_mismatch review would exceed the §11 budget "
            f"({config.REVIEW_QUEUE_BUDGET}/kind). Nothing was written. Retune before proceeding."
        )
        return 1
    return 0


def cmd_resolve_pair(args: argparse.Namespace) -> int:
    """§6.3 / §10 — record a reviewer's verdict on open pair_mismatch rows.

    The verdict is stored in review_queue.resolution (the durable fact); run `pair` afterwards
    to materialize it into provisional_pairs / orphan-cdg exclusion.
    """
    with db.connect() as conn:
        for rq_id in args.ids:
            stage1.resolve_pair_mismatch(conn, rq_id, args.verdict, args.source, args.note)
            print(f"  #{rq_id} -> {args.verdict}  (source: {args.source})")
    print(f"\n{len(args.ids)} verdict(s) recorded. Run `pair` to materialize them.")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Stage 2 §7.1 — download every non-excluded location to staging/, keyed by drive_file_id.

    Resumable and Ctrl-C-safe: re-run to pick up deferred/interrupted files. The long pole
    (~403 GiB); expect it to span days and to hit per-file Drive quota errors that resolve on a
    later re-run.
    """
    if args.dry_run:
        with db.connect() as conn:
            worklist = stage2.build_worklist(conn, args.limit)
            total_bytes = sum(r["size_bytes"] or 0 for r in worklist)
            free = stage2.free_bytes()
        print("DRY RUN — nothing downloaded\n")
        print(_fmt_report({
            "worklist": len(worklist),
            "gib_to_download": round(total_bytes / 2**30, 2),
            "gib_free_on_staging_fs": round(free / 2**30, 2),
            "limit": args.limit,
        }))
        print(f"\nfirst {min(10, len(worklist))} in download order:")
        for r in worklist[:10]:
            print(f"  {r['drive_file_id']}  {(r['size_bytes'] or 0)/2**20:8.2f} MiB  {r['remote_path']}")
        return 0

    def _progress(rep: stage2.DownloadReport) -> None:
        done = rep.staged + rep.already_staged
        print(
            f"  [{done}/{rep.worklist}] staged={rep.staged} adopted={rep.already_staged} "
            f"deferred={rep.deferred} verify_failed={rep.verify_failed} "
            f"{rep.bytes_downloaded / 2**30:.2f} GiB  {rep.elapsed_sec:.0f}s",
            flush=True,
        )

    note = f"limit={args.limit}" if args.limit else "full"
    if args.batched:
        note += f" batched(size={args.batch_size}, from_end={args.from_end})"
    with db.pipeline_run("stage2_download", notes=note) as run:
        with db.connect() as conn:
            if args.batched:
                report = stage2.download_batched(
                    conn, batch_size=args.batch_size, limit=args.limit,
                    from_end=args.from_end, min_free=args.min_free_gib * 2**30,
                    on_progress=_progress,
                )
            else:
                report = stage2.download_all(
                    conn, limit=args.limit, min_free=args.min_free_gib * 2**30,
                    on_progress=_progress,
                )
            run["items_processed"] = report.staged + report.already_staged
            run["items_failed"] = report.deferred + report.verify_failed
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            printable = {k: v for k, v in report.as_dict().items() if k != "failures"}
            print("\n" + _fmt_report(printable))
    if report.stopped_reason == "disk_low":
        print("\nSTOPPED — free disk fell below the floor. Downloaded so far is intact; "
              "re-run after freeing space to resume.")
        return 1
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-file commits mean staging is consistent. Re-run to resume.")
    if report.deferred or report.verify_failed:
        print(f"\n{report.deferred} deferred (quota/error) + {report.verify_failed} verify-failed "
              f"remain remote_only; re-run to retry them.")
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    """Stage 3 §7.2.1 — SHA-256 every staged file into blobs; link locations; md5 tripwire.

    Pure local I/O (no Drive). Resumable: re-run to pick up where an interrupt left off.
    """
    if args.dry_run:
        with db.connect() as conn:
            worklist = stage3.hash_worklist(conn, args.limit)
            total_bytes = sum(r["size_bytes"] or 0 for r in worklist)
        print("DRY RUN — nothing hashed\n")
        print(_fmt_report({
            "worklist": len(worklist),
            "gib_to_hash": round(total_bytes / 2**30, 2),
            "limit": args.limit,
        }))
        return 0

    def _progress(rep: stage3.HashReport) -> None:
        print(
            f"  [{rep.hashed}/{rep.worklist}] blobs+{rep.new_blobs} shared={rep.shared_blobs} "
            f"drift={rep.md5_drift} errors={rep.read_errors} "
            f"{rep.bytes_hashed / 2**30:.2f} GiB  {rep.elapsed_sec:.0f}s",
            flush=True,
        )

    with db.pipeline_run("stage3_hash", notes=f"limit={args.limit}" if args.limit else "full") as run:
        with db.connect() as conn:
            report = stage3.hash_all(conn, limit=args.limit, on_progress=_progress)
            run["items_processed"] = report.hashed
            run["items_failed"] = report.md5_drift + report.read_errors
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            printable = {k: v for k, v in report.as_dict().items() if k != "failures"}
            print("\n" + _fmt_report(printable))
    if report.md5_drift or report.read_errors:
        print(f"\nWARNING: {report.md5_drift} md5-drift + {report.read_errors} read-error file(s) "
              f"were NOT linked to blobs — local bytes are suspect; see the run report's failures.")
        return 1
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-file commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Stage 3 §7.2.3 — ffprobe every unchecked media blob; record streams; classify integrity.

    No full decode (that is Stage 4, survivors only). Resumable: re-run picks up unchecked blobs.
    """
    if args.dry_run:
        with db.connect() as conn:
            worklist = stage3.probe_worklist(conn, args.limit)
            skipped = stage3.count_skipped_non_media(conn)
        print("DRY RUN — nothing probed\n")
        print(_fmt_report({
            "worklist": len(worklist),
            "skipped_non_media_blobs": skipped,
            "limit": args.limit,
        }))
        return 0

    def _progress(rep: stage3.ProbeReport) -> None:
        print(
            f"  [{rep.probed}/{rep.worklist}] ok={rep.probed_ok} broken={rep.broken} "
            f"suspect={rep.suspect}  {rep.elapsed_sec:.0f}s",
            flush=True,
        )

    with db.pipeline_run("stage3_probe", notes=f"limit={args.limit}" if args.limit else "full") as run:
        with db.connect() as conn:
            report = stage3.probe_all(conn, limit=args.limit, workers=args.workers,
                                      on_progress=_progress)
            run["items_processed"] = report.probed
            run["items_failed"] = report.broken
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            printable = {k: v for k, v in report.as_dict().items() if k != "failures"}
            print("\n" + _fmt_report(printable))
    if report.broken or report.suspect:
        print(f"\n{report.broken} broken + {report.suspect} suspect blob(s) — see the run "
              f"report's failures sample; these never become dedup winners uncontested.")
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-blob commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_items(args: argparse.Namespace) -> int:
    """Stage 3 §7.2.4 — promote blobs + pairs into media_items; CDG check; filename metadata.

    Runs after `hash` and `probe`. Idempotent by natural key: re-run reports 0 created.
    """
    if args.dry_run:
        with db.connect() as conn:
            rep = stage3.build_items(conn, dry_run=True)
            meta = stage3.promote_filename_metadata(conn, dry_run=True)
        print("DRY RUN — nothing written\n")
        print(_fmt_report({**rep.as_dict(), "metadata": meta}))
        return 0

    with db.pipeline_run("stage3_items") as run:
        with db.connect() as conn:
            rep = stage3.build_items(conn)
            if not rep.over_budget:
                rep.metadata = stage3.promote_filename_metadata(conn)
            run["items_processed"] = sum(rep.created.values()) + rep.existing + rep.updated
            run["items_failed"] = rep.cdg_failed + rep.unresolvable_pairs
            run["report"] = {**rep.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            print(_fmt_report(run["report"]))
    if rep.over_budget:
        print(
            f"\nSTOPPED — pair_mismatch review would exceed the §11 budget "
            f"({config.REVIEW_QUEUE_BUDGET}/kind). Nothing was written. Retune before proceeding."
        )
        return 1
    return 0


def cmd_id3(args: argparse.Namespace) -> int:
    """Stage 3 §7.2.2 — read ID3 tags into song_metadata (source 'id3'). Runs after `items`."""
    if args.dry_run:
        with db.connect() as conn:
            worklist = stage3.id3_worklist(conn, args.limit)
        print("DRY RUN — nothing read\n")
        print(_fmt_report({"worklist": len(worklist), "limit": args.limit}))
        return 0

    def _progress(rep: stage3.Id3Report) -> None:
        print(
            f"  [{rep.tagged + rep.untagged}/{rep.worklist}] tagged={rep.tagged} "
            f"untagged={rep.untagged} truncated={rep.truncated_skipped} "
            f"disagreements={rep.disagreements}  {rep.elapsed_sec:.0f}s",
            flush=True,
        )

    with db.pipeline_run("stage3_id3", notes=f"limit={args.limit}" if args.limit else "full") as run:
        with db.connect() as conn:
            report = stage3.id3_all(conn, limit=args.limit, on_progress=_progress)
            run["items_processed"] = report.tagged + report.untagged
            run["items_failed"] = report.disagreements
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            print("\n" + _fmt_report(run["report"]))
    if report.over_budget:
        print(
            f"\nSTOPPED — metadata_match review exceeded the §11 budget "
            f"({config.REVIEW_QUEUE_BUDGET}/kind). Drain or retune, then re-run to resume."
        )
        return 1
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-item commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    """Stage 3 §7.2.5 — fpcalc every audio-carrying blob into fingerprints.

    `--benchmark` is the §7.3 PHASE GATE: fingerprint a 1k sample, then report timing
    projections and candidate-pair quality for review BEFORE committing to the full run.
    """
    if args.benchmark:
        with db.pipeline_run("stage3_fp_benchmark", notes=f"sample={args.benchmark}") as run:
            with db.connect() as conn:
                rep = stage3.fingerprint_benchmark(conn, sample=args.benchmark)
                run["items_processed"] = rep["sample_fingerprints"]
                run["report"] = rep
        out = config.LOGS_DIR / f"fp-benchmark-{db.utcnow().replace(':', '')}.json"
        out.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        print(_fmt_report(rep))
        print(f"\nbenchmark report: {out}")
        print("PHASE GATE (§7.3): review timing + pair quality before the full run.")
        return 0

    if args.dry_run:
        with db.connect() as conn:
            worklist = stage3.fingerprint_worklist(conn, args.limit)
            skipped = stage3.count_skipped_broken(conn)
        print("DRY RUN — nothing fingerprinted\n")
        print(_fmt_report({"worklist": len(worklist), "skipped_broken": skipped,
                           "limit": args.limit}))
        return 0

    def _progress(rep: stage3.FpReport) -> None:
        print(
            f"  [{rep.fingerprinted + rep.failed}/{rep.worklist}] ok={rep.fingerprinted} "
            f"failed={rep.failed}  {rep.elapsed_sec:.0f}s",
            flush=True,
        )

    with db.pipeline_run("stage3_fingerprint",
                         notes=f"limit={args.limit}" if args.limit else "full") as run:
        with db.connect() as conn:
            report = stage3.fingerprint_all(conn, limit=args.limit, workers=args.workers,
                                            on_progress=_progress)
            run["items_processed"] = report.fingerprinted
            run["items_failed"] = report.failed
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            printable = {k: v for k, v in report.as_dict().items() if k != "failures"}
            print("\n" + _fmt_report(printable))
    if report.failed:
        print(f"\n{report.failed} blob(s) failed fpcalc and were marked suspect (§7.2.5).")
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-blob commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_cluster(args: argparse.Namespace) -> int:
    """Stage 3 §7.3 — candidate generation, batched similarity, edges, union-find clusters.

    Deterministic and fully reconciling: a second run over unchanged inputs reports zero
    db_* changes. --dry-run computes everything and reports what would change.
    """
    from . import cluster as cluster_mod
    with db.pipeline_run("stage3_cluster",
                         notes="dry-run" if args.dry_run else "full",
                         backup=not args.dry_run) as run:
        with db.connect() as conn:
            report = cluster_mod.cluster_all(conn, dry_run=args.dry_run)
            run["items_processed"] = report.clustered_items
            run["report"] = report.as_dict()
    if args.dry_run:
        print("DRY RUN — nothing written\n")
    print(_fmt_report(report.as_dict()))
    return 0


def cmd_verdicts(args: argparse.Namespace) -> int:
    """Stage 3 §7.4 — rank the copies of each song per (song, format) + grouped reviews."""
    from . import verdicts as verdicts_mod
    notes = "dry-run" if args.dry_run else ("partial-reviews" if args.partial_reviews
                                            else "full")
    with db.pipeline_run("stage3_verdicts", notes=notes, backup=not args.dry_run) as run:
        with db.connect() as conn:
            report = verdicts_mod.run_verdicts(conn, dry_run=args.dry_run,
                                               partial=args.partial_reviews)
            run["items_processed"] = report.items_seen
            run["report"] = {**report.as_dict(),
                             "review_queue_sizes": db.review_queue_sizes(conn)}
    if args.dry_run:
        print("DRY RUN — nothing written\n")
    print(_fmt_report(run["report"]))
    if report.over_budget:
        # §11 must be impossible to miss: the failure mode this replaces was a full silent
        # rollback that looked like a clean run in every counter the operator reads.
        print("\n" + "=" * 78)
        print("STOPPED (§11 REVIEW BUDGET)" if not report.partial
              else "PARTIAL (§11 REVIEW BUDGET)")
        for kind in report.over_budget_kinds:
            p = report.budget_projection[kind]
            print(f"  {kind}: {p['open']} open + {p['new']} new = {p['projected']} "
                  f"> budget {p['budget']}  (over by {p['over_by']})")
        print(f"\n  {report.stopped_reason}")
        print("=" * 78)
        return 1
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    """Stage 4 §8 — full ffmpeg decode of winner/sole-copy defining blobs.

    `--benchmark` is the small phase gate: decode a stratified sample (writes results —
    resumable work is never wasted) and report the timing projection before the full run.
    `--until-stable` runs the §8 promotion loop (decode → verdicts → decode …).
    """
    from . import stage4
    if args.benchmark:
        with db.pipeline_run("stage4_benchmark", notes=f"sample={args.benchmark}") as run:
            with db.connect() as conn:
                rep = stage4.decode_benchmark(conn, sample=args.benchmark)
                run["items_processed"] = rep["sample"]
                run["report"] = rep
        print(_fmt_report(rep))
        print("\nPHASE GATE (§8): review the projection before the full run.")
        return 0

    if args.dry_run:
        with db.connect() as conn:
            worklist = stage4.decode_worklist(conn, args.limit)
        print("DRY RUN — nothing decoded\n")
        print(_fmt_report({"worklist": len(worklist), "limit": args.limit}))
        return 0

    def _progress(rep: stage4.DecodeReport) -> None:
        print(f"  [{rep.decoded_ok + rep.failed}/{rep.worklist}] ok={rep.decoded_ok} "
              f"failed={rep.failed}  {rep.elapsed_sec:.0f}s", flush=True)

    with db.pipeline_run("stage4_decode",
                         notes="until-stable" if args.until_stable else
                               (f"limit={args.limit}" if args.limit else "one-pass")) as run:
        with db.connect() as conn:
            if args.until_stable:
                result = stage4.decode_until_stable(conn, workers=args.workers,
                                                    on_progress=_progress)
                run["report"] = result
                print("\n" + _fmt_report(result))
                return 0 if result["stable"] else 1
            report = stage4.decode_all(conn, limit=args.limit, workers=args.workers,
                                       on_progress=_progress)
            run["items_processed"] = report.decoded_ok
            run["items_failed"] = report.failed
            run["report"] = report.as_dict()
            printable = {k: v for k, v in report.as_dict().items() if k != "failures"}
            print("\n" + _fmt_report(printable))
    if report.failed:
        print(f"\n{report.failed} blob(s) failed decode → broken. Re-run `verdicts` to "
              "promote replacements, then `decode` again (or use --until-stable).")
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-blob commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    """Stage 5 §9.1 — MusicBrainz text search into song_metadata (source 'musicbrainz_text').

    `--benchmark` is the phase gate (house style): enrich a stratified sample first, review
    the projected review-queue burden vs the §11 budget, retune if needed, THEN run full.
    """
    if args.retune:
        with db.pipeline_run("stage5_retune", notes="drop mb_text rows + open mb reviews") as run:
            with db.connect() as conn:
                rep = stage5.retune_enrichment(conn)
                run["report"] = rep
        print(_fmt_report(rep))
        print("\nRe-run `enrich` to re-derive from mb_cache with current thresholds.")
        return 0

    if args.benchmark:
        def _bench_progress(rep: stage5.EnrichReport) -> None:
            n = rep.accepted + rep.reviewed + rep.filename_stands
            print(f"  [{n}] accepted={rep.accepted} review={rep.reviewed} "
                  f"stands={rep.filename_stands}", flush=True)

        with db.pipeline_run("stage5_enrich_benchmark", notes=f"sample={args.benchmark}") as run:
            with db.connect() as conn:
                rep = stage5.enrich_benchmark(conn, sample=args.benchmark,
                                              on_progress=_bench_progress)
                run["items_processed"] = rep["sample"]
                run["report"] = {**rep, "review_queue_sizes": db.review_queue_sizes(conn)}
        out = config.LOGS_DIR / f"enrich-benchmark-{db.utcnow().replace(':', '')}.json"
        out.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        print(_fmt_report(rep))
        print(f"\nbenchmark report: {out}")
        print("PHASE GATE (§9.1/§11): review acceptance bands + projected queue before the full run.")
        return 0

    if args.dry_run:
        with db.connect() as conn:
            stage5.materialize_metadata(conn)
            worklist = stage5.enrich_worklist(conn, args.limit)
            unsearchable = stage5.count_unsearchable(conn)
        print("DRY RUN — nothing fetched, nothing written\n")
        print(_fmt_report({"worklist": len(worklist), "unsearchable_no_title": unsearchable,
                           "limit": args.limit}))
        return 0

    def _progress(rep: stage5.EnrichReport) -> None:
        n = rep.accepted + rep.reviewed + rep.filename_stands
        print(f"  [{n}/{rep.worklist}] accepted={rep.accepted} review={rep.reviewed} "
              f"stands={rep.filename_stands} fetched={rep.fetched} "
              f"cache={rep.cache_hits}  {rep.elapsed_sec:.0f}s", flush=True)

    with db.pipeline_run("stage5_enrich",
                         notes=f"limit={args.limit}" if args.limit else "full") as run:
        with db.connect() as conn:
            report = stage5.enrich_all(conn, limit=args.limit, budget=args.budget,
                                       on_progress=_progress)
            run["items_processed"] = report.accepted + report.reviewed + report.filename_stands
            run["items_failed"] = 0
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            print("\n" + _fmt_report(run["report"]))
    if report.over_budget:
        print(f"\nSTOPPED — metadata_match review exceeded the §11 budget "
              f"({config.REVIEW_QUEUE_BUDGET}/kind). Retune (`enrich --retune`) or drain, "
              f"then re-run to resume.")
        return 1
    if report.stopped_reason and report.stopped_reason.startswith("network"):
        print(f"\nSTOPPED on persistent network failure — {report.stopped_reason}. "
              f"Re-run to resume; everything fetched so far is cached.")
        return 1
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-item commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_acoustid(args: argparse.Namespace) -> int:
    """Stage 5 §9.1 — AcoustID lookup per fingerprinted winner/sole blob (acoustic identity)."""
    if not config.ACOUSTID_API_KEY:
        print("BLOCKED: no AcoustID application key. Register one at "
              "https://acoustid.org/new-application and export KARAOKEMP_ACOUSTID_KEY.\n"
              "This pass is corroboration only (§9.1) — text enrichment does not wait for it.")
        return 1
    if args.dry_run:
        with db.connect() as conn:
            worklist = stage5.acoustid_worklist(conn, args.limit)
        print("DRY RUN — nothing fetched\n")
        print(_fmt_report({"worklist": len(worklist), "limit": args.limit}))
        return 0

    def _progress(rep: stage5.AcoustidReport) -> None:
        print(f"  [{rep.checked}/{rep.worklist}] hits={rep.hits} conflicts={rep.conflicts} "
              f"junk={rep.junk} misses={rep.misses}  {rep.elapsed_sec:.0f}s", flush=True)

    with db.pipeline_run("stage5_acoustid",
                         notes=f"limit={args.limit}" if args.limit else "full") as run:
        with db.connect() as conn:
            report = stage5.acoustid_all(conn, limit=args.limit, on_progress=_progress)
            run["items_processed"] = report.checked
            run["report"] = {**report.as_dict(), "review_queue_sizes": db.review_queue_sizes(conn)}
            print("\n" + _fmt_report(run["report"]))
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — per-blob commits mean the index is consistent. Re-run to resume.")
    return 0


def cmd_titlecard(args: argparse.Namespace) -> int:
    """Stage 5 — read the on-screen title card of title-less video items (source
    'title_card_ocr'), so §9.1's `title IS NOT NULL` worklist can finally see them.

    This stage identifies nothing; it unblocks the MB search that already exists. Run
    `enrich` afterwards — the items it titled enter that worklist through the front door.

    Export → external process → apply, the §10 shape (`review export` / `review apply`):
    `extract` writes the frames + manifest an operator's Claude Code session reads, `ingest`
    applies the results.jsonl it produces. Nothing here makes a network call.
    """
    from . import titlecard

    if args.retune or args.tc_cmd == "retune":
        with db.pipeline_run("stage5_titlecard_retune",
                             notes="re-derive from title_cards evidence") as run:
            with db.connect() as conn:
                rep = titlecard.retune_titlecards(conn)
                run["report"] = rep
        print(_fmt_report(rep))
        print("\nRe-derived from stored title_cards evidence — no re-OCR, no operator time.")
        return 0

    if args.tc_cmd == "ingest":
        return _titlecard_ingest(args, titlecard)
    if args.tc_cmd == "extract":
        return _titlecard_extract(args, titlecard)
    print("titlecard: pick a subcommand — `extract` (write the work packet), "
          "`ingest <results.jsonl>` (apply it), or `retune`.")
    return 2


def _titlecard_extract(args: argparse.Namespace, titlecard) -> int:
    """§9.1 — frames + manifest into ARTIFACTS_DIR/titlecards (§3.9). Reads the DB, writes
    only artifacts."""
    if args.dry_run:
        # No pipeline_run: a dry run writes nothing, so there is no state to snapshot and
        # a §13 backup would only rotate a real restore point out of the keep window.
        with db.connect() as conn:
            report = titlecard.extract_work_packet(
                conn, limit=args.limit, model_override=args.model_override, dry_run=True)
        print("DRY RUN — frames extracted and classified; nothing written\n")
        print(_fmt_report(report.as_dict()))
        return 0

    def _progress(rep) -> None:
        print(f"  [{rep.processed}/{rep.worklist}] frames={rep.frames_written} "
              f"failed={rep.failed}  {rep.elapsed_sec:.0f}s", flush=True)

    # No pipeline_run either: extraction touches no DB state at all, it only reads the
    # worklist. The durable record of what was asked of the operator is the manifest itself.
    with db.connect() as conn:
        report = titlecard.extract_work_packet(
            conn, limit=args.limit, model_override=args.model_override,
            on_progress=_progress)
    print("\n" + _fmt_report(report.as_dict()))
    if report.layouts.get("unknown"):
        print(f"\nNOTE: {report.layouts['unknown']} item(s) matched no known producer "
              f"layout — routed to `{titlecard.MODEL_SONNET}`. A spike here means a layout "
              f"we have not seen; eyeball a few before trusting the results.")
    if report.stopped_reason == "interrupted":
        print("\nINTERRUPTED — the manifest still describes only the items actually extracted.")
    print(f"\nNext: follow {titlecard.titlecards_dir() / 'INSTRUCTIONS.md'} to read the "
          f"cards, then `titlecard ingest <results.jsonl>`.")
    return 0


def _titlecard_ingest(args: argparse.Namespace, titlecard) -> int:
    """§9.1 — apply an operator's results.jsonl through `promote()`. Per-item commit."""
    try:
        if args.dry_run:
            with db.connect() as conn:
                rep = titlecard.ingest_results(conn, args.results, manifest=args.manifest,
                                               dry_run=True)
            print("DRY RUN — nothing written\n")
            print(_fmt_report(rep.as_dict()))
            return 1 if rep.rejected else 0

        with db.pipeline_run("stage5_titlecard_ingest",
                             notes=f"results={Path(args.results).name}") as run:
            with db.connect() as conn:
                rep = titlecard.ingest_results(conn, args.results, manifest=args.manifest)
                run["items_processed"] = rep.applied
                run["items_failed"] = rep.rejected
                run["report"] = rep.as_dict()
                print(_fmt_report(run["report"]))
    except titlecard.TitleCardError as exc:
        print(f"BLOCKED: {exc}")
        return 1

    if rep.rejections:
        print("\nREJECTED lines (nothing was applied for any of these):")
        for r in rep.rejections:
            print(f"  line {r['line']} (item {r['media_item_id']}): {r['reason']}")
    print("\nNext: `enrich` — items that gained a title are now in the §9.1 worklist.")
    return 1 if rep.rejected else 0


def cmd_organize(args: argparse.Namespace) -> int:
    """Stage 6 §9.2 — move staged content into `active/<shard>/<Artist> - <Title> [id]/`.

    Placements are renames within one filesystem, so this costs no disk. A `--dry-run` writes
    the full plan to `logs/organize-plan-*.tsv` (every move, one per line) and prints the
    summary; §9.2 requires the first real run to follow a reviewed dry run.
    """
    from . import stage6

    def _progress(n: int, total: int) -> None:
        print(f"  [{n}/{total}] items placed", flush=True)

    if args.dry_run:
        # backup=False: a dry run writes nothing, and snapshotting 450 MB would rotate a real
        # restore point out of the §13 keep window for no reason.
        with db.pipeline_run("stage6_organize_dryrun", notes="dry-run", backup=False) as run:
            rep = stage6.organize(dry_run=True, archive_broken=not args.keep_broken_active,
                                  limit=args.limit)
            run["items_processed"] = rep["items"]
            run["report"] = rep
        print("DRY RUN — nothing moved\n")
        print(_fmt_report({k: v for k, v in rep.items() if k != "shards"}))
        print("\nshard sizes:")
        for shard, n in list(rep["shards"].items())[:12]:
            print(f"  {shard:<10} {n}")
        if len(rep["shards"]) > 12:
            print(f"  ... {len(rep['shards']) - 12} more")
        print(f"\nfull plan: {rep['plan_file']}")
        if rep.get("songs_losing_last_copy"):
            print(f"\n⚠  {rep['songs_losing_last_copy']} song(s) lose their last active copy "
                  "when broken items are archived. Review before the real run "
                  "(`--keep-broken-active` leaves them in place).")
        print("\nPHASE GATE (§9.2): review the plan before running for real.")
        return 0

    with db.pipeline_run("stage6_organize",
                         notes=f"limit={args.limit}" if args.limit else "full") as run:
        rep = stage6.organize(dry_run=False, archive_broken=not args.keep_broken_active,
                              limit=args.limit, progress=_progress)
        run["items_processed"] = rep.get("items_placed", 0)
        run["items_failed"] = rep.get("failed", 0)
        run["report"] = rep
    print("\n" + _fmt_report({k: v for k, v in rep.items() if k != "shards"}))
    print("\n§9.2 requires fsck after every organize batch — running it now.\n")
    return cmd_fsck(argparse.Namespace(no_strays=False, hash_sample=0))


def cmd_fsck(args: argparse.Namespace) -> int:
    """Index ↔ filesystem reconciliation (§9.2). Read-only; reports, never repairs."""
    from . import stage6
    rep = stage6.fsck(check_strays=not args.no_strays, hash_sample=args.hash_sample)
    print(_fmt_report(rep))
    if rep["ok"]:
        print("\nfsck OK — index and filesystem agree.")
        return 0
    print("\nfsck FAILED — see counts above (lists truncated to 50).")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="karaokemp", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create storage layout + database").set_defaults(func=cmd_init)

    e = sub.add_parser("enumerate", help="Stage 0 §5.1: enumerate Drive into file_locations")
    e.add_argument("--from-dump", help="reuse an existing lsjson dump instead of re-fetching")
    e.set_defaults(func=cmd_enumerate)

    d = sub.add_parser("dedup", help="Stage 0 §5.2: logical exact-dup pass (index-only)")
    d.add_argument("--dry-run", action="store_true", help="print the full planned action list")
    d.set_defaults(func=cmd_dedup)

    sub.add_parser("report", help="Stage 0 §5.3: inventory report (phase gate)").set_defaults(
        func=cmd_report
    )

    sub.add_parser(
        "parse-stats", help="Stage 1 §6.1: dry parse of all filenames; writes nothing"
    ).set_defaults(func=cmd_parse_stats)

    pa = sub.add_parser("parse", help="Stage 1 §6.1: persist parse payloads to location_parses")
    pa.add_argument("--dry-run", action="store_true", help="report the diff without writing")
    pa.set_defaults(func=cmd_parse)

    pr = sub.add_parser("pair", help="Stage 1 §6.3: provisional MP3+CDG pairing (content-level)")
    pr.add_argument("--dry-run", action="store_true", help="print the planned changes")
    pr.set_defaults(func=cmd_pair)

    rp = sub.add_parser("resolve-pair", help="§6.3: record confirm/reject verdicts on pair_mismatch rows")
    rp.add_argument("ids", type=int, nargs="+", help="review_queue row id(s)")
    rp.add_argument("--verdict", required=True, choices=["confirm", "reject"])
    rp.add_argument("--source", required=True,
                    help="who decided (e.g. 'sha-yol', 'claude-session-YYYY-MM-DD') — stored for audit")
    rp.add_argument("--note", default="", help="free-text rationale")
    rp.set_defaults(func=cmd_resolve_pair)

    dl = sub.add_parser("download", help="Stage 2 §7.1: download non-excluded locations to staging")
    dl.add_argument("--limit", type=int, default=None,
                    help="download at most N files (for a supervised sample run)")
    dl.add_argument("--min-free-gib", type=int, default=stage2.MIN_FREE_BYTES // 2**30,
                    help="stop gracefully when free disk drops below this (default 15)")
    dl.add_argument("--dry-run", action="store_true",
                    help="print the worklist size, byte total, and first rows; download nothing")
    dl.add_argument("--batched", action="store_true",
                    help="bulk path-keyed rclone copy per batch, by-ID fallback for misses")
    dl.add_argument("--batch-size", type=int, default=500)
    dl.add_argument("--from-end", action="store_true",
                    help="walk the worklist tail-first (coexists with an in-flight sequential run)")
    dl.set_defaults(func=cmd_download)

    h = sub.add_parser("hash", help="Stage 3 §7.2.1: SHA-256 staged files into blobs")
    h.add_argument("--limit", type=int, default=None, help="hash at most N files")
    h.add_argument("--dry-run", action="store_true", help="print worklist size; hash nothing")
    h.set_defaults(func=cmd_hash)

    pb = sub.add_parser("probe", help="Stage 3 §7.2.3: ffprobe unchecked media blobs")
    pb.add_argument("--limit", type=int, default=None, help="probe at most N blobs")
    pb.add_argument("--workers", type=int, default=stage3.PROBE_WORKERS)
    pb.add_argument("--dry-run", action="store_true", help="print worklist size; probe nothing")
    pb.set_defaults(func=cmd_probe)

    it = sub.add_parser("items", help="Stage 3 §7.2.4: build media_items + promote metadata")
    it.add_argument("--dry-run", action="store_true", help="report the plan without writing")
    it.set_defaults(func=cmd_items)

    i3 = sub.add_parser("id3", help="Stage 3 §7.2.2: read ID3 tags into song_metadata")
    i3.add_argument("--limit", type=int, default=None, help="process at most N items")
    i3.add_argument("--dry-run", action="store_true", help="print worklist size; read nothing")
    i3.set_defaults(func=cmd_id3)

    fp = sub.add_parser("fingerprint", help="Stage 3 §7.2.5: fpcalc audio-carrying blobs")
    fp.add_argument("--limit", type=int, default=None, help="fingerprint at most N blobs")
    fp.add_argument("--workers", type=int, default=stage3.PROBE_WORKERS)
    fp.add_argument("--benchmark", type=int, nargs="?", const=1000, default=None,
                    help="§7.3 phase gate: fingerprint an N-sample (default 1000) and report "
                         "timing + candidate-pair quality instead of running in full")
    fp.add_argument("--dry-run", action="store_true", help="print worklist size; run nothing")
    fp.set_defaults(func=cmd_fingerprint)

    cl = sub.add_parser("cluster", help="Stage 3 §7.3: candidate edges + union-find clusters")
    cl.add_argument("--dry-run", action="store_true",
                    help="compute everything, report the diff, write nothing")
    cl.set_defaults(func=cmd_cluster)

    vd = sub.add_parser("verdicts", help="Stage 3 §7.4: dedup verdicts within clusters")
    vd.add_argument("--dry-run", action="store_true",
                    help="compute all verdicts + reviews, report, write nothing")
    vd.add_argument("--partial-reviews", action="store_true",
                    help="§11: on a budget overrun apply the verdicts anyway and withhold "
                         "only the over-budget kind's review rows (default: roll back "
                         "everything). Never raises the budget.")
    vd.set_defaults(func=cmd_verdicts)

    dc = sub.add_parser("decode", help="Stage 4 §8: full ffmpeg decode of winners + sole copies")
    dc.add_argument("--limit", type=int, default=None, help="decode at most N blobs")
    dc.add_argument("--workers", type=int, default=4)
    dc.add_argument("--benchmark", type=int, nargs="?", const=100, default=None,
                    help="§8 phase gate: decode a stratified N-sample (default 100), report projection")
    dc.add_argument("--until-stable", action="store_true",
                    help="§8 promotion loop: decode → verdicts → decode until zero failures")
    dc.add_argument("--dry-run", action="store_true", help="print worklist size; run nothing")
    dc.set_defaults(func=cmd_decode)

    en = sub.add_parser("enrich", help="Stage 5 §9.1: MusicBrainz text search on winners + sole copies")
    en.add_argument("--limit", type=int, default=None, help="enrich at most N items")
    en.add_argument("--budget", type=int, default=None,
                    help="metadata_match review budget (default: config §11)")
    en.add_argument("--benchmark", type=int, nargs="?", const=200, default=None,
                    help="phase gate: enrich a stratified N-sample (default 200), report projection")
    en.add_argument("--retune", action="store_true",
                    help="§11: drop musicbrainz_text rows + OPEN mb reviews; next run re-derives "
                         "from mb_cache with current thresholds (no refetch)")
    en.add_argument("--dry-run", action="store_true", help="print worklist size; fetch nothing")
    en.set_defaults(func=cmd_enrich)

    ac = sub.add_parser("acoustid", help="Stage 5 §9.1: AcoustID lookup (needs KARAOKEMP_ACOUSTID_KEY)")
    ac.add_argument("--limit", type=int, default=None, help="check at most N blobs")
    ac.add_argument("--dry-run", action="store_true", help="print worklist size; fetch nothing")
    ac.set_defaults(func=cmd_acoustid)

    tc = sub.add_parser("titlecard",
                        help="Stage 5: read the on-screen title card of title-less video "
                             "items so §9.1 can search them (export/apply, no API calls)")
    # `--retune` stays a flag on the parent, unchanged, so `titlecard --retune` keeps working
    # exactly as it did before extract/ingest existed; `retune` is also a subcommand for
    # symmetry. Hence required=False on the subparsers.
    tc.add_argument("--retune", action="store_true",
                    help="§11: re-derive title_card_ocr rows from the stored title_cards "
                         "evidence with the current promotion rules (no re-OCR)")
    tc.set_defaults(func=cmd_titlecard, tc_cmd=None, limit=None, model_override=None,
                    dry_run=False, results=None, manifest=None)
    tcsub = tc.add_subparsers(dest="tc_cmd", required=False)

    tce = tcsub.add_parser("extract",
                           help="write the work packet: frames + manifest.jsonl under "
                                "ARTIFACTS_DIR/titlecards (§3.9)")
    tce.add_argument("--limit", type=int, default=None, help="pack at most N items")
    tce.add_argument("--model-override", default=None, choices=["haiku", "sonnet"],
                     help="force one model for the whole packet instead of routing on layout")
    tce.add_argument("--dry-run", action="store_true",
                     help="classify only: report the layout histogram and the model routing "
                          "split; write no frames and no manifest")

    tci = tcsub.add_parser("ingest", help="apply an operator's results.jsonl via promote()")
    tci.add_argument("results", help="JSONL file, one extraction result per line")
    tci.add_argument("--manifest", default=None,
                     help="work-packet manifest (default: ARTIFACTS_DIR/titlecards/manifest.jsonl)")
    tci.add_argument("--dry-run", action="store_true",
                     help="validate and report; write nothing")

    tcsub.add_parser("retune", help="same as --retune")

    og = sub.add_parser(
        "organize",
        help="Stage 6 §9.2: move staged files into active/<shard>/<Artist> - <Title> [id]/")
    og.add_argument("--dry-run", action="store_true",
                    help="plan only; writes logs/organize-plan-*.tsv and moves nothing")
    og.add_argument("--limit", type=int, help="place only the first N items (smoke test)")
    og.add_argument(
        "--keep-broken-active",
        action="store_true",
        help="do NOT archive items whose defining blob is broken. Default is to archive them, "
             "matching what export_runtime already refuses to ship",
    )
    og.set_defaults(func=cmd_organize)

    fs = sub.add_parser("fsck", help="§9.2: reconcile the index against the filesystem")
    fs.add_argument("--no-strays", action="store_true",
                    help="skip the walk of active/ that finds unindexed files")
    fs.add_argument("--hash-sample", type=int, default=0,
                    help="also SHA-256 this many random active files (slow, strong)")
    fs.set_defaults(func=cmd_fsck)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
