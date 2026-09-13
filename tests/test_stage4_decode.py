"""Stage 4 §8 full-decode invariant tests. ffmpeg is the impure edge, injected. Pinned:

  * worklist = defining blobs of active winner/sole_copy items only — losers, manual_review
    and already-decoded/broken blobs never re-decode; the worklist self-empties (resumable);
  * clean decode ⇒ decoded_ok and CLEARS a suspect; failure ⇒ broken, stderr recorded;
  * the §8 promotion loop: a failed cluster winner is demoted, `verdicts` crowns the
    runner-up, the loop decodes it, and only then is the cluster stable;
  * a failed sole copy becomes a broken sole copy ⇒ quality_flag replacement row
    (sha-yol's 2026-07-19 correction flows through Stage 4 automatically).

Run: python3 tests/test_stage4_decode.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from karaokemp import config, db, stage0, stage4, verdicts


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


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
    return conn


_seq = [0]


def _add_item(conn, *, verdict="sole_copy", cluster=None, kbps=192,
              integrity="probed_ok", with_path=True):
    _seq[0] += 1
    n = _seq[0]
    fid, sha = f"f{n}", f"sha{n:04d}"
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": f"d/{fid}", "Name": fid + ".mp3",
                "Size": 1000, "Hashes": {"md5": "aa" + fid}}]
    )
    conn.execute(
        "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
        "VALUES (?, 1000, ?)", (sha, integrity))
    if with_path:
        p = config.STAGING_DIR / f"{fid}.mp3"
        p.write_bytes(b"x")
        conn.execute(
            "UPDATE file_locations SET status='staged', local_path=?, content_hash=? "
            "WHERE drive_file_id=?", (str(p), sha, fid))
    cur = conn.execute(
        "INSERT INTO media_items (format, cluster_id, duration_sec, audio_bitrate_kbps, "
        "quality_verdict) VALUES ('mp3g', ?, 200.0, ?, ?)", (cluster, kbps, verdict))
    conn.execute(
        "INSERT INTO media_item_files (media_item_id, content_hash, role) "
        "VALUES (?, ?, 'audio')", (cur.lastrowid, sha))
    conn.commit()
    return cur.lastrowid, sha


def _fake_decoder(broken_names):
    def decoder(path: Path) -> dict:
        if path.stem in broken_names:
            return {"ok": False, "detail": "Invalid data found when processing input"}
        return {"ok": True, "detail": ""}
    return decoder


def _integrity(conn, sha):
    return conn.execute(
        "SELECT integrity_status FROM blobs WHERE content_hash=?", (sha,)).fetchone()[0]


def test_classify_decode_policy():
    """Deviation #15, pinned: null-muxer noise ignored; localized glitches are playable;
    pervasive damage or a decoder give-up is broken."""
    c = stage4.classify_decode(0, "")
    check(c["ok"] and c["glitches"] == 0)
    c = stage4.classify_decode(0, "[null @ 0x1] Application provided invalid, "
                                  "non monotonically increasing dts to muxer\n" * 30)
    check(c["ok"] and c["glitches"] == 0, "muxer-side lines say nothing about the input")
    c = stage4.classify_decode(0, "[mp3float @ 0x1] Header missing\n"
                                  "Error while decoding stream #0:0: Invalid data")
    check(c["ok"] and c["glitches"] == 2, "one bad frame is a glitch, not unplayable")
    c = stage4.classify_decode(1, "[mp3float @ 0x1] Header missing")
    check(not c["ok"], "decoder gave up: broken")
    c = stage4.classify_decode(0, "[mpeg1video @ 0x1] ac-tex damaged\n" * 200)
    check(not c["ok"] and c["glitches"] == 200, "pervasive damage: broken")


def test_glitchy_ok_recorded_in_detail():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _i, sha = _add_item(conn, integrity="suspect")

        def decoder(path):
            return {"ok": True, "glitches": 2, "detail": "[mp3float] Header missing"}
        rep = stage4.decode_all(conn, decoder=decoder, workers=1)
        check(rep.decoded_ok == 1 and rep.glitchy_ok == 1 and rep.suspects_cleared == 1)
        check(_integrity(conn, sha) == "decoded_ok")
        detail = json.loads(conn.execute(
            "SELECT integrity_detail FROM blobs WHERE content_hash=?", (sha,)).fetchone()[0])
        check(detail["decode_glitches"] == 2 and "Header missing" in detail["decode_glitch_sample"])


def test_worklist_survivors_only():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _win, sw = _add_item(conn, verdict="winner")
        _sole, ss = _add_item(conn, verdict="sole_copy")
        _add_item(conn, verdict="alternate")
        _add_item(conn, verdict="manual_review")
        _add_item(conn, verdict="sole_copy", integrity="broken")
        _add_item(conn, verdict="sole_copy", integrity="decoded_ok")
        wl = stage4.decode_worklist(conn)
        check({r["content_hash"] for r in wl} == {sw, ss},
              f"only winner+sole, not-yet-decoded: {[r['content_hash'] for r in wl]}")


def test_decode_ok_clears_suspect_failure_breaks():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _a, sa = _add_item(conn, integrity="suspect")
        _b, sb = _add_item(conn, integrity="suspect")
        rep = stage4.decode_all(conn, decoder=_fake_decoder({f"f{_seq[0]}"}), workers=1)
        check(rep.decoded_ok == 1 and rep.failed == 1, rep.as_dict())
        check(rep.suspects_cleared == 1 and rep.suspects_demoted == 1)
        check(_integrity(conn, sa) == "decoded_ok")
        check(_integrity(conn, sb) == "broken")
        detail = json.loads(conn.execute(
            "SELECT integrity_detail FROM blobs WHERE content_hash=?", (sb,)).fetchone()[0])
        check("Invalid data" in detail["decode_error"])
        check(stage4.decode_worklist(conn) == [], "worklist self-empties")


def test_promotion_loop_crowns_runner_up():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        cid = conn.execute(
            "INSERT INTO clusters (method, confidence) VALUES ('fingerprint', 0.9)").lastrowid
        best, s_best = _add_item(conn, verdict="pending", cluster=cid, kbps=320)
        second, s_second = _add_item(conn, verdict="pending", cluster=cid, kbps=192)
        verdicts.run_verdicts(conn)
        check(conn.execute("SELECT quality_verdict FROM media_items WHERE id=?",
                           (best,)).fetchone()[0] == "winner")
        # the crowned 320k copy fails decode; the loop must demote it and decode the 192k
        result = stage4.decode_until_stable(
            conn, decoder=_fake_decoder({f"f{_seq[0] - 1}"}), workers=1, log=lambda *a: None)
        check(result["stable"], result)
        check(len(result["rounds"]) == 2, "fail round + clean round")
        check(_integrity(conn, s_best) == "broken")
        check(_integrity(conn, s_second) == "decoded_ok")
        vs = {r[0]: r[1] for r in conn.execute(
            "SELECT id, quality_verdict FROM media_items")}
        check(vs[second] == "winner" and vs[best] == "alternate",
              f"runner-up crowned after demotion: {vs}")


def test_failed_sole_copy_joins_replacement_list():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, sha = _add_item(conn, verdict="pending")
        verdicts.run_verdicts(conn)
        result = stage4.decode_until_stable(
            conn, decoder=_fake_decoder({f"f{_seq[0]}"}), workers=1, log=lambda *a: None)
        check(result["stable"], result)
        check(_integrity(conn, sha) == "broken")
        rows = conn.execute(
            "SELECT media_item_id FROM review_queue WHERE kind='quality_flag'").fetchall()
        check([r[0] for r in rows] == [item],
              "Stage 4 failure on a sole copy must flow into sha-yol's replacement list")
        attrs = json.loads(conn.execute(
            "SELECT quality_attrs FROM media_items WHERE id=?", (item,)).fetchone()[0])
        check(attrs.get("broken_sole") is True)


def test_missing_path_is_bookkeeping_not_damage():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _i, sha = _add_item(conn, with_path=False)
        rep = stage4.decode_all(conn, decoder=_fake_decoder(set()), workers=1)
        check(rep.no_path == 1 and rep.failed == 0, rep.as_dict())
        check(_integrity(conn, sha) == "probed_ok",
              "a missing local path must never mark media broken")


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
