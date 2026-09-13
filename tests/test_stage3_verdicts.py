"""Stage 3 §7.4 — ranking invariant tests. Pinned:

  * the RANKING KEYS are unchanged by the 2026-07-31 reframe: ≥192 kbps beats higher-
    integrity-rank ties, content_hash breaks exact ties deterministically, and winner/
    alternate are assigned per FORMAT group (formats are peers, §7.4.1);
  * copies are RANKED, not archived: a song with three copies yields one default and two
    `alternate`s, all three still active. `loser` no longer exists (migration 006);
  * the rank is a TOTAL order, so a song always exposes a default copy even when every
    member is under manual_review;
  * a re-rank with no verdict change is still reported as a change (ranks_changed);
  * video best-video vs best-audio split ⇒ both manual_review + one review row, still ranked;
  * duration outliers sort LAST and are never the default (§7.4.4) — but the median is taken
    over SAME-RECORDING peers, so a spread across two productions of one song flags nothing
    while the same spread inside one recording flags a truncation;
  * sole copies ⇒ sole_copy when the SONG has one copy. BROKEN ones additionally get the
    quality_flag replacement row + attrs.broken_sole (sha-yol's 2026-07-19 correction) —
    suspect does not, and neither does a broken item with a name twin in another song;
  * cross-song audio evidence queues ONE `possible_song_merge` row per cluster pair, with no
    upper bound on similarity — a 0.99 edge between two songs asks rather than merges;
  * §11 budget overrun is loud (per-kind projection + the offending kind by name) and offers
    a partial mode that applies the verdicts instead of silently reverting them;
  * re-run changes nothing — verdicts, ranks and review rows all stable (§13).

Fixtures give every item its own cluster unless told otherwise, because §7.3 assigns every
active item one; a NULL cluster_id is a state the pipeline no longer produces.

Run: python3 tests/test_stage3_verdicts.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path

from karaokemp import config, db, stage0, verdicts


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


@contextlib.contextmanager
def _floor(value: float):
    """Pin VERDICT_REVIEW_FLOOR for tests that assert BEHAVIOUR at the floor rather than the
    production tuning value. That value is explicitly §11-retunable (0.70 -> 0.985 on
    2026-08-05 to fit the review budget); a unit test of "one row per cluster pair" must not
    fail every time it is retuned, so the tests that place fixture edges relative to a floor
    state which floor they mean.
    """
    prev = config.VERDICT_REVIEW_FLOOR
    config.VERDICT_REVIEW_FLOOR = value
    try:
        yield
    finally:
        config.VERDICT_REVIEW_FLOOR = prev


@contextlib.contextmanager
def _policy(value: str):
    """Pin AV_SPLIT_POLICY. Production default became 'prefer_video' on 2026-08-06 (always
    serve the better picture, never ask); the tests that assert the ASK behaviour and the
    operator-override path say so explicitly rather than riding on the default."""
    prev = config.AV_SPLIT_POLICY
    config.AV_SPLIT_POLICY = value
    try:
        yield
    finally:
        config.AV_SPLIT_POLICY = prev


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


def _add_item(conn, *, fmt="mp3g", cluster=None, dur=200.0, kbps=192, height=None,
              integrity="probed_ok", attrs=None, sha=None, own_cluster=True):
    """One active item. If no cluster is given it gets its OWN singleton cluster, because
    that is what §7.3 does to every active item — cluster_id is universally non-NULL, and a
    fixture leaving it NULL would be testing a state the pipeline no longer produces."""
    _seq[0] += 1
    n = _seq[0]
    if cluster is None and own_cluster:
        cluster = conn.execute("INSERT INTO clusters (method) VALUES (NULL)").lastrowid
    fid = f"f{n}"
    sha = sha or f"sha{n:04d}"
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": f"d/{fid}", "Name": fid,
                "Size": 1000, "Hashes": {"md5": "aa" + fid}}]
    )
    conn.execute(
        "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
        "VALUES (?, 1000, ?)", (sha, integrity),
    )
    cur = conn.execute(
        "INSERT INTO media_items (format, cluster_id, duration_sec, audio_bitrate_kbps, "
        "height, quality_attrs, quality_verdict) VALUES (?,?,?,?,?,?, 'pending')",
        (fmt, cluster, dur, kbps, height, json.dumps(attrs) if attrs else None),
    )
    item_id = cur.lastrowid
    role = "av" if fmt == "video" else "audio"
    conn.execute(
        "INSERT INTO media_item_files (media_item_id, content_hash, role) "
        "VALUES (?, ?, ?)", (item_id, sha, role),
    )
    conn.commit()
    return item_id


def _mk_cluster(conn):
    return conn.execute(
        "INSERT INTO clusters (method, confidence) VALUES ('fingerprint', 0.9)").lastrowid


def _verdict(conn, item_id):
    return conn.execute(
        "SELECT quality_verdict FROM media_items WHERE id=?", (item_id,)).fetchone()[0]


def test_mp3g_ranking_bitrate_then_hash():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        lo = _add_item(conn, cluster=c, kbps=128)
        hi = _add_item(conn, cluster=c, kbps=256)
        tie = _add_item(conn, cluster=c, kbps=256, sha="sha0000")  # smallest hash wins ties
        rep = verdicts.run_verdicts(conn)
        check(_verdict(conn, tie) == "winner",
              f"256k + smallest hash must win: {[_verdict(conn, i) for i in (lo, hi, tie)]}")
        check(_verdict(conn, hi) == "alternate" and _verdict(conn, lo) == "alternate")
        check(rep.winners == 1 and rep.alternates == 2, rep.as_dict())


def test_low_bitrate_never_beats_192_class():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        good = _add_item(conn, cluster=c, kbps=192, sha="sha9999")   # worst hash, right class
        loud = _add_item(conn, cluster=c, kbps=190, sha="sha0000")   # best hash, under class
        verdicts.run_verdicts(conn)
        check(_verdict(conn, good) == "winner", "≥192 kbps class outranks hash order")
        check(_verdict(conn, loud) == "alternate")


def test_formats_are_peers_within_cluster():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        m1 = _add_item(conn, cluster=c, fmt="mp3g", kbps=256)
        m2 = _add_item(conn, cluster=c, fmt="mp3g", kbps=128)
        v1 = _add_item(conn, cluster=c, fmt="video", height=720)
        verdicts.run_verdicts(conn)
        check(_verdict(conn, m1) == "winner" and _verdict(conn, m2) == "alternate")
        check(_verdict(conn, v1) == "winner",
              "sole video in a mixed cluster is that format's winner, never a loser")


def test_video_av_split_goes_to_manual_review():
    """AV_SPLIT_POLICY='ask' — the pre-2026-08-06 behaviour, still supported."""
    with tempfile.TemporaryDirectory() as td, _policy("ask"):
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        hd_bad_audio = _add_item(conn, cluster=c, fmt="video", height=1080, kbps=96)
        sd_good_audio = _add_item(conn, cluster=c, fmt="video", height=480, kbps=256)
        third = _add_item(conn, cluster=c, fmt="video", height=480, kbps=96)
        rep = verdicts.run_verdicts(conn)
        check(_verdict(conn, hd_bad_audio) == "manual_review", "§7.4.3 split: no auto-crown")
        check(_verdict(conn, sd_good_audio) == "manual_review")
        check(_verdict(conn, third) == "alternate")
        check(rep.video_splits == 1 and rep.dedup_verdict_queued == 1, rep.as_dict())
        row = conn.execute(
            "SELECT payload FROM review_queue WHERE kind='dedup_verdict'").fetchone()
        check(json.loads(row["payload"])["reason"] == "video_av_split")


def test_av_split_prefer_video_asks_nothing_and_crowns_the_picture():
    """AV_SPLIT_POLICY='prefer_video' (default since 2026-08-06, sha-yol's standing decision):
    a picture/audio split is COUNTED but asks nothing and parks nothing. The better picture
    wins by the ordinary ranking key, and an explicitly resolved override still beats it."""
    with tempfile.TemporaryDirectory() as td, _policy("prefer_video"):
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        hd_bad_audio = _add_item(conn, cluster=c, fmt="video", height=1080, kbps=96)
        sd_good_audio = _add_item(conn, cluster=c, fmt="video", height=480, kbps=256)
        rep = verdicts.run_verdicts(conn)
        check(rep.video_splits == 1, f"the split is still detected+counted: {rep.as_dict()}")
        check(rep.dedup_verdict_queued == 0, f"but nothing is queued: {rep.as_dict()}")
        n = conn.execute("SELECT COUNT(*) FROM review_queue "
                         "WHERE kind='dedup_verdict'").fetchone()[0]
        check(n == 0, f"no video_av_split row may exist under this policy: {n}")
        check(_verdict(conn, hd_bad_audio) == "winner", "the better PICTURE is the default")
        check(_verdict(conn, sd_good_audio) == "alternate")
        check(rep.manual_review == 0, f"neither copy is parked: {rep.as_dict()}")


def test_duration_outlier_never_crowned():
    """§7.4.4. The outlier has the best bitrate, so it would take rank 1 on the ranking key
    alone; being a duration outlier must push it last instead.

    Note the audio edges. They are load-bearing now: they are what says these three files are
    the SAME RECORDING, which is the only reading under which "one is 50s short" means
    truncation. Without them the same three durations are just three karaoke productions of
    one song — see test_duration_spread_across_recordings_is_not_an_outlier. Before name-only
    clustering a cluster implied one recording and this test needed no edges."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        outlier = _add_item(conn, cluster=c, dur=150.0, kbps=320)
        a = _add_item(conn, cluster=c, dur=200.0, kbps=192)
        b = _add_item(conn, cluster=c, dur=201.0, kbps=128)
        _edge(conn, a, b, 0.99)
        _edge(conn, a, outlier, 0.99)
        rep = verdicts.run_verdicts(conn)
        check(_verdict(conn, outlier) == "manual_review", "§7.4.4")
        check(_rank(conn, outlier) == 3, "best bitrate, but an outlier sorts last")
        check(_verdict(conn, a) == "winner" and _verdict(conn, b) == "alternate")
        check(rep.outliers == 1, rep.as_dict())


def test_truncation_suspect_is_outlier_even_in_pair():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        short = _add_item(conn, cluster=c, dur=125.0, kbps=320,
                          attrs={"truncation_suspect": True})
        full = _add_item(conn, cluster=c, dur=210.0, kbps=128)
        verdicts.run_verdicts(conn)
        check(_verdict(conn, short) == "manual_review")
        check(_verdict(conn, full) == "winner",
              "the full copy is the only crownable member")


def test_broken_sole_copy_gets_quality_flag_suspect_does_not():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        broken = _add_item(conn, integrity="broken")
        suspect = _add_item(conn, integrity="suspect")
        ok = _add_item(conn)
        rep = verdicts.run_verdicts(conn)
        for i in (broken, suspect, ok):
            check(_verdict(conn, i) == "sole_copy", "sole copies stay sole_copy (§7.4.5)")
        check(rep.broken_sole == 1 and rep.quality_flag_queued == 1, rep.as_dict())
        rows = conn.execute(
            "SELECT media_item_id, payload FROM review_queue WHERE kind='quality_flag'"
        ).fetchall()
        check(len(rows) == 1 and rows[0]["media_item_id"] == broken,
              "only the BROKEN sole copy joins the replacement list — suspect waits "
              "for Stage 4 (sha-yol 2026-07-19)")
        attrs = json.loads(conn.execute(
            "SELECT quality_attrs FROM media_items WHERE id=?", (broken,)).fetchone()[0])
        check(attrs.get("broken_sole") is True)


def test_cross_song_audio_queues_one_review_per_song_pair():
    """§7.3 decision (a): audio evidence spanning two songs is a QUESTION, never a merge.
    Grouped per cluster pair so §11 budgets stay meaningful — two edges between the same two
    songs are one row, and a same-song edge asks nothing because it is already answered."""
    with tempfile.TemporaryDirectory() as td, _floor(0.70):
        conn = _fresh_env(Path(td))
        c1, c2 = _mk_cluster(conn), _mk_cluster(conn)
        a1 = _add_item(conn, cluster=c1)
        a2 = _add_item(conn, cluster=c1)
        b1 = _add_item(conn, cluster=c2)
        noise = _add_item(conn)
        for x, y, sim in ((a1, b1, 0.72), (a2, b1, 0.74),   # two edges, ONE row (c1,c2)
                          (a1, a2, 0.99),                    # same song: nothing to ask
                          (a1, noise, 0.66)):                # under 0.70: below the floor
            conn.execute(
                "INSERT INTO cluster_edges (item_a, item_b, edge_type, similarity) "
                "VALUES (?,?,?,?)", (min(x, y), max(x, y), "fingerprint", sim))
        conn.commit()
        rep = verdicts.run_verdicts(conn)
        rows = conn.execute(
            "SELECT cluster_id, payload FROM review_queue WHERE kind='dedup_verdict'"
        ).fetchall()
        check(len(rows) == 1 and rep.possible_song_merges == 1,
              f"one row per song pair, floor 0.70: {rep.as_dict()}")
        payload = json.loads(rows[0]["payload"])
        check(payload["reason"] == f"possible_song_merge_{min(c1, c2)}_{max(c1, c2)}", payload)
        check(len(payload["edges"]) == 2, "both ≥0.70 cross-song edges in one payload")
        check(payload["best_similarity"] == 0.74, payload)
        check(_verdict(conn, noise) == "sole_copy")


def test_strong_cross_song_audio_still_asks():
    """The band has no upper bound any more. A ≥0.85 edge used to auto-merge and so never
    needed a human; nothing merges on audio now, so that is exactly the band that does."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn)
        b = _add_item(conn)
        conn.execute(
            "INSERT INTO cluster_edges (item_a, item_b, edge_type, similarity) "
            "VALUES (?,?,?,?)", (min(a, b), max(a, b), "fingerprint", 0.99))
        conn.commit()
        rep = verdicts.run_verdicts(conn)
        rows = conn.execute(
            "SELECT payload FROM review_queue WHERE kind='dedup_verdict'").fetchall()
        check(len(rows) == 1, f"a 0.99 cross-song edge must still ask: {rep.as_dict()}")
        check(json.loads(rows[0]["payload"])["best_similarity"] == 0.99)
        check(_verdict(conn, a) == "sole_copy" and _verdict(conn, b) == "sole_copy",
              "and it must NOT have merged them behind the operator's back")


def test_rerun_is_noop_and_never_requeues():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        _add_item(conn, cluster=c, kbps=256)
        _add_item(conn, cluster=c, kbps=128)
        _add_item(conn, integrity="broken")
        rep1 = verdicts.run_verdicts(conn)
        check(rep1.verdicts_changed == 3 and rep1.quality_flag_queued == 1)
        rep2 = verdicts.run_verdicts(conn)
        check(rep2.verdicts_changed == 0, rep2.as_dict())
        check(rep2.dedup_verdict_queued == 0 and rep2.quality_flag_queued == 0)
        check(rep2.reviews_already_present == 1)
        n = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        check(n == 1, f"re-run must not duplicate reviews, found {n}")


def test_budget_overrun_rolls_back():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for _ in range(3):
            _add_item(conn, integrity="broken")
        rep = verdicts.run_verdicts(conn, budget=2)
        check(rep.over_budget and rep.stopped_reason, rep.as_dict())
        check(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0)
        pend = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE quality_verdict='pending'").fetchone()[0]
        check(pend == 3, "§11 stop must leave verdicts untouched")


def _edge(conn, a, b, sim, etype="fingerprint"):
    conn.execute("INSERT INTO cluster_edges (item_a, item_b, edge_type, similarity) "
                 "VALUES (?,?,?,?)", (min(a, b), max(a, b), etype, sim))
    conn.commit()


def _name_cluster(conn):
    """A song §7.3 built from name evidence — several copies, possibly different recordings."""
    return conn.execute(
        "INSERT INTO clusters (method, confidence, notes) VALUES ('title_match', 1.0, ?)",
        (json.dumps({"items": 2, "merge_edges": 1,
                     "evidence": {"audio": 0, "name": 1, "manual": 0},
                     "confidence_basis": "name_evidence"}),)).lastrowid


def _rank(conn, item_id):
    return conn.execute(
        "SELECT quality_rank FROM media_items WHERE id=?", (item_id,)).fetchone()[0]


def test_copies_are_ranked_and_nothing_is_archived():
    """Decision 3. A song with three copies produces ONE default and two `alternate`s — which
    stay active. There is no archival verdict left to assign: only exact-content duplicates
    are duplicates, and Stage 0 collapsed those before an item existed."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _name_cluster(conn)
        best = _add_item(conn, cluster=c, kbps=320, sha="sha0001")
        mid = _add_item(conn, cluster=c, kbps=256, sha="sha0002")
        worst = _add_item(conn, cluster=c, kbps=128, sha="sha0003")
        rep = verdicts.run_verdicts(conn)
        check([_rank(conn, i) for i in (best, mid, worst)] == [1, 2, 3],
              "a TOTAL order, not just a winner")
        check(_verdict(conn, best) == "winner", rep.as_dict())
        check(_verdict(conn, mid) == "alternate" and _verdict(conn, worst) == "alternate")
        check(rep.winners == 1 and rep.alternates == 2, rep.as_dict())
        still_active = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE status='active'").fetchone()[0]
        check(still_active == 3, "an alternate is NOT archived, superseded or deactivated")


def test_manual_review_item_still_has_a_default_rank():
    """Every song must expose a default copy even while a human question about it is open —
    the old code returned no crown at all when every member was an outlier."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _name_cluster(conn)
        xs = [_add_item(conn, cluster=c, dur=d, attrs={"truncation_suspect": True})
              for d in (200.0, 201.0)]
        verdicts.run_verdicts(conn)
        check(all(_verdict(conn, i) == "manual_review" for i in xs))
        check(sorted(_rank(conn, i) for i in xs) == [1, 2],
              "ranked anyway: there is always something to serve")


def test_duration_spread_across_recordings_is_not_an_outlier():
    """§7.4.4: a 100s spread between two different karaoke productions of one song is
    expected; the same spread inside ONE recording is a truncation. Only the second may page
    a human, so the median is taken over audio-connected peers, never over the whole song."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _name_cluster(conn)
        for dur in (120.0, 220.0, 320.0):        # three recordings, no audio edges
            _add_item(conn, cluster=c, dur=dur)
        rep = verdicts.run_verdicts(conn)
        check(rep.outliers == 0 and rep.dedup_verdict_queued == 0,
              f"cross-recording spread must not queue anything: {rep.as_dict()}")
        check(rep.winners == 1 and rep.alternates == 2,
              f"they are ranked, not flagged: {rep.as_dict()}")


def test_duration_spread_within_one_recording_is_an_outlier():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _name_cluster(conn)
        xs = [_add_item(conn, cluster=c, dur=d) for d in (200.0, 201.0, 320.0)]
        for other in xs[1:]:
            _edge(conn, xs[0], other, 0.99)      # all three are the SAME recording
        rep = verdicts.run_verdicts(conn)
        check(rep.outliers == 1, f"same recording, 120s longer: truncation: {rep.as_dict()}")
        check(_verdict(conn, xs[2]) == "manual_review")
        check(_rank(conn, xs[2]) == 3, "an outlier sorts last — never the default copy")


def test_one_copy_song_with_a_name_twin_is_still_a_sole_copy():
    """§7.4.5 restated for name-only clustering. `sole_copy` is now a fact about the SONG
    having one copy, which is what it always should have meant. A name twin §7.3 declined to
    merge does not change that verdict — it changes what we do about a BROKEN one, because
    another copy may be sitting right there under a different spelling."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn)
        b = _add_item(conn)
        lonely = _add_item(conn)
        _edge(conn, a, b, 1.0, etype="title_match")
        rep = verdicts.run_verdicts(conn)
        for i in (a, b, lonely):
            check(_verdict(conn, i) == "sole_copy", f"item {i} is one copy of its song")
        check(rep.sole_copies == 3 and rep.name_twin_songs == 2, rep.as_dict())
        check(rep.dedup_verdict_queued == 0,
              f"a name twin alone is not worth a review row: {rep.as_dict()}")


def test_broken_item_with_name_twin_is_not_a_replacement_case():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn, integrity="broken")
        b = _add_item(conn)
        _edge(conn, a, b, 1.0, etype="title_match")
        rep = verdicts.run_verdicts(conn)
        check(rep.broken_sole == 0 and rep.quality_flag_queued == 0,
              f"another copy of the song may be right there: {rep.as_dict()}")
        attrs = conn.execute(
            "SELECT quality_attrs FROM media_items WHERE id=?", (a,)).fetchone()[0]
        check(attrs is None or "broken_sole" not in attrs)


def test_reframe_paths_are_idempotent():
    """House rule (§13): any stage run twice back-to-back changes nothing the second time —
    ranks included, and across every path the reframe touched."""
    with tempfile.TemporaryDirectory() as td, _floor(0.70):
        conn = _fresh_env(Path(td))
        c = _name_cluster(conn)
        a1 = _add_item(conn, cluster=c, dur=200.0, kbps=256)
        a2 = _add_item(conn, cluster=c, dur=201.0, kbps=128)
        _edge(conn, a1, a2, 0.99)
        _add_item(conn, cluster=c, dur=320.0)                  # a different recording
        twin_a, twin_b = _add_item(conn), _add_item(conn)
        _edge(conn, twin_a, twin_b, 1.0, etype="title_match")
        cross_a, cross_b = _add_item(conn), _add_item(conn)
        _edge(conn, cross_a, cross_b, 0.88)                    # cross-song audio → one review
        _add_item(conn, integrity="broken")                    # broken sole → quality_flag
        rep1 = verdicts.run_verdicts(conn)
        n1 = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        check(rep1.possible_song_merges == 1 and rep1.broken_sole == 1
              and rep1.name_twin_songs == 2, rep1.as_dict())
        check(n1 == 2, f"one song-merge row + one quality_flag: {n1}")
        rep2 = verdicts.run_verdicts(conn)
        n2 = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        check(rep2.verdicts_changed == 0, rep2.as_dict())
        check(rep2.ranks_changed == 0, f"ranks must be stable too: {rep2.as_dict()}")
        check(rep2.dedup_verdict_queued == 0 and rep2.quality_flag_queued == 0, rep2.as_dict())
        check(n1 == n2, f"re-run duplicated review rows: {n1} -> {n2}")


def test_rank_change_is_reported_even_when_the_verdict_does_not_move():
    """A re-rank with no verdict change is a real change — the default copy moved — and must
    not be silently reported as a no-op. The two counters are diffed independently."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _name_cluster(conn)
        a = _add_item(conn, cluster=c, kbps=256, sha="sha0001")
        b = _add_item(conn, cluster=c, kbps=192, sha="sha0002")
        _add_item(conn, cluster=c, kbps=128, sha="sha0003")
        verdicts.run_verdicts(conn)
        check((_rank(conn, a), _rank(conn, b)) == (1, 2))
        # b's blob decodes clean while a's is merely probed: integrity outranks bitrate,
        # so b takes rank 1 — a and b swap, but 'winner'/'alternate' still exist as before.
        conn.execute("UPDATE blobs SET integrity_status='decoded_ok' WHERE content_hash='sha0002'")
        conn.commit()
        rep = verdicts.run_verdicts(conn)
        check((_rank(conn, b), _rank(conn, a)) == (1, 2), "the default copy moved")
        check(rep.ranks_changed == 2 and rep.verdicts_changed == 2, rep.as_dict())


def test_budget_overrun_is_loud_and_partial_mode_applies_verdicts():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for _ in range(3):
            _add_item(conn, integrity="broken")
        rep = verdicts.run_verdicts(conn, budget=2)
        check(rep.over_budget and rep.over_budget_kinds == ["quality_flag"], rep.as_dict())
        p = rep.budget_projection["quality_flag"]
        check(p == {"open": 0, "new": 3, "projected": 3, "budget": 2, "over_by": 1}, p)
        check("quality_flag" in rep.stopped_reason and "3 > budget 2" in rep.stopped_reason,
              f"the message must name the kind and the numbers: {rep.stopped_reason}")
        check(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0)
        check(conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE quality_verdict='pending'").fetchone()[0] == 3,
            "default stop leaves verdicts untouched — the whole transaction rolls back")

        rep2 = verdicts.run_verdicts(conn, budget=2, partial=True)
        check(rep2.over_budget and rep2.partial, rep2.as_dict())
        check(rep2.reviews_withheld == {"quality_flag": 3}, rep2.as_dict())
        check(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0,
              "the over-budget kind is still withheld")
        check(conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE quality_verdict='sole_copy'").fetchone()[0] == 3,
            "partial mode applies the verdicts instead of silently reverting them")


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        c = _mk_cluster(conn)
        a = _add_item(conn, cluster=c, kbps=256)
        _add_item(conn, cluster=c, kbps=128)
        rep = verdicts.run_verdicts(conn, dry_run=True)
        check(rep.verdicts_changed == 2 and rep.winners == 1, rep.as_dict())
        check(_verdict(conn, a) == "pending", "dry run must not write verdicts")
        check(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0)


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
