"""Stage 3 §7.3 clustering invariant tests.

Everything here runs on synthetic fingerprints — no fpcalc, no media files. Pinned:

  * the batched coarse pass agrees with exact offset-0 similarity, including the
    zero-pad correction for unequal fingerprint lengths;
  * THE NAME IS THE SOLE CLUSTER-FORMING KEY (2026-07-31, second reframe). Audio similarity
    never merges two NAMED songs — not at 0.75, not at 0.99 — it is persisted as evidence
    and projected as review burden instead;
  * §7.3(b) name identity is order-insensitive and containment-aware, gated on the operator's
    veto plus an is_instrumental conflict ('unknown' AND NULL must both pass through);
    the group-size guard records the evidence for groups it will not merge;
  * the no-name fallback is the ONE grouping audio still does: nameless items group by audio,
    and a nameless component adopts a named song only when it points at exactly one — an
    ambiguous bridge stays standalone rather than welding two songs together;
  * EVERY active item gets a cluster, singletons included, with method NULL meaning singleton;
  * §7.3(c): a truncated copy is marked truncation_suspect from its prefix_fingerprint edge;
  * junk metadata keys ('tartist'/'tsongtitle' template placeholders) form no groups;
  * sync is a full reconciliation: second run reports zero db_* changes, a changed input
    reshapes clusters instead of duplicating them, and an IMPROVED NAME (enrichment writing a
    higher-trust row) cleanly re-keys the item into the right song on the next run.

Run: python3 tests/test_stage3_cluster.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json as _json
import random
import tempfile
from pathlib import Path

from karaokemp import cluster, config, db, stage0, stage3


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


def _add_item(conn, *, dur, fp=None, artist=None, title=None, fmt="audio_only"):
    """One active item with an audio blob; optional fingerprint and metadata."""
    _seq[0] += 1
    n = _seq[0]
    fid, sha = f"f{n}", f"sha{n}"
    stage0.upsert_locations(
        conn, [{"ID": fid, "Path": f"d/{fid}", "Name": fid,
                "Size": 1000, "Hashes": {"md5": "aa" + fid}}]
    )
    conn.execute(
        "INSERT OR IGNORE INTO blobs (content_hash, size_bytes, integrity_status) "
        "VALUES (?, 1000, 'probed_ok')", (sha,),
    )
    cur = conn.execute(
        "INSERT INTO media_items (format, duration_sec, quality_verdict) "
        "VALUES (?, ?, 'pending')", (fmt, dur),
    )
    item_id = cur.lastrowid
    conn.execute(
        "INSERT INTO media_item_files (media_item_id, content_hash, role) "
        "VALUES (?, ?, 'audio')", (item_id, sha),
    )
    if fp is not None:
        conn.execute(
            "INSERT INTO fingerprints (content_hash, chromaprint, fp_duration_sec) "
            "VALUES (?, ?, ?)", (sha, stage3.encode_fp(fp), dur),
        )
    for fld, val in (("artist", artist), ("title", title)):
        if val is not None:
            conn.execute(
                "INSERT INTO song_metadata (media_item_id, field, value, source, confidence) "
                "VALUES (?, ?, ?, 'filename', 0.6)", (item_id, fld, val),
            )
    conn.commit()
    return item_id


def _rand_fp(seed, n=600):
    rng = random.Random(seed)
    return [rng.getrandbits(32) for _ in range(n)]


def _mutate(fp, frac, seed=99):
    """Flip ~frac of the bits — a same-recording re-encode with codec noise."""
    rng = random.Random(seed)
    out = []
    for w in fp:
        for bit in range(32):
            if rng.random() < frac:
                w ^= 1 << bit
        out.append(w)
    return out


def _quiet(*_args, **_kw):
    pass


# --- coarse math ---------------------------------------------------------------------------


def test_coarse_matches_exact_offset0():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(1)
        _add_item(conn, dur=200.0, fp=base)
        _add_item(conn, dur=201.0, fp=_mutate(base, 0.02))
        _add_item(conn, dur=202.0, fp=_rand_fp(2))
        corpus = cluster.load_corpus(conn)
        cands = cluster.coarse_candidates(corpus, floor=0.0, log=_quiet)
        check(len(cands) == 3, f"floor 0 keeps every blocked pair, got {len(cands)}")
        for a, b, coarse in cands:
            w = config.CLUSTER_COARSE_WORDS
            exact = stage3.fp_similarity(
                corpus.fps[a][:w], corpus.fps[b][:w], max_offset=0)
            check(abs(coarse - exact) < 1e-9,
                  f"batched coarse ({coarse}) must equal exact offset-0 ({exact})")


def test_coarse_zero_pad_correction_unequal_lengths():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(3, n=400)
        _add_item(conn, dur=200.0, fp=base)
        _add_item(conn, dur=200.0, fp=base[:250])   # shorter fp, identical prefix
        corpus = cluster.load_corpus(conn)
        cands = cluster.coarse_candidates(corpus, floor=0.9, log=_quiet)
        check(len(cands) == 1 and cands[0][2] == 1.0,
              f"overlap-only comparison must ignore the padding, got {cands}")


def test_coarse_respects_duration_block():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(4)
        _add_item(conn, dur=200.0, fp=base)
        _add_item(conn, dur=230.0, fp=base)   # identical fp, 30s apart: not (a)-candidates
        corpus = cluster.load_corpus(conn)
        check(cluster.coarse_candidates(corpus, floor=0.0, log=_quiet) == [])


# --- merging rules -------------------------------------------------------------------------


def _cid(conn, item_id):
    return conn.execute(
        "SELECT cluster_id FROM media_items WHERE id=?", (item_id,)).fetchone()[0]


def test_every_item_gets_a_cluster():
    """Universal assignment (sha-yol: "singletons have their own cluster, that's fine").
    cluster_id is non-NULL for every active item, so no downstream query needs a NULL branch,
    and `clusters` is one row per SONG — including the songs with exactly one copy."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        ids = [_add_item(conn, dur=200.0 + 40 * i, fp=_rand_fp(60 + i),
                         artist=f"Artist {i}", title=f"Title {i}") for i in range(4)]
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 4 and rep.singleton_clusters == 4
              and rep.multi_item_clusters == 0, rep.as_dict())
        check(rep.clustered_items == 4, "every item is in exactly one cluster")
        check(all(_cid(conn, i) is not None for i in ids), "cluster_id is universally non-NULL")
        check(len({_cid(conn, i) for i in ids}) == 4, "four songs, four clusters")
        check(rep.clusters_by_method == {"singleton": 4}, rep.clusters_by_method)
        cl = conn.execute("SELECT method, confidence, notes FROM clusters LIMIT 1").fetchone()
        check(cl["method"] is None and cl["confidence"] is None,
              f"a singleton is held together by nothing; method NULL says so: {dict(cl)}")
        check(_json.loads(cl["notes"])["confidence_basis"] == "none", cl["notes"])


def test_audio_never_merges_two_named_songs():
    """THE reframe, in one test. Two items whose audio is all but identical (~0.98, far above
    CLUSTER_AUTO_MERGE) but whose names differ stay in SEPARATE songs. Audio identity is not
    song identity: merging here is what produced 2,424 clusters holding more than one name.
    The evidence is not discarded — the edge is persisted, and §7.4 raises it as a
    `possible_song_merge` review for a human."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(5)
        a = _add_item(conn, dur=210.0, fp=base, artist="Cher", title="Believe")
        b = _add_item(conn, dur=210.5, fp=_mutate(base, 0.02),
                      artist="Nirvana", title="Lithium")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.multi_item_clusters == 0, f"audio must not merge songs: {rep.as_dict()}")
        check(_cid(conn, a) != _cid(conn, b), "two names, two songs")
        check(rep.band_auto_merge == 1, "the ≥0.85 edge is still measured")
        check(rep.cross_song_audio_pairs == 1 and rep.cross_song_pairs_by_cluster == 1,
              f"and projected as review burden: {rep.as_dict()}")
        edge = conn.execute(
            "SELECT similarity FROM cluster_edges WHERE item_a=? AND item_b=? "
            "AND edge_type='fingerprint'", (min(a, b), max(a, b))).fetchone()
        check(edge is not None and edge["similarity"] >= 0.85,
              "§3.5: the edge is persisted regardless — it just does not merge")


def test_noname_items_group_by_audio():
    """The one grouping audio still does. Items with no usable artist+title have no key at
    all, so a strong audio edge is the only evidence they can carry."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(5)
        a = _add_item(conn, dur=210.0, fp=base)                     # no name
        b = _add_item(conn, dur=210.5, fp=_mutate(base, 0.02))      # ~0.98 → fallback merge
        c = _add_item(conn, dur=211.0, fp=_mutate(base, 0.25, 7))   # ~0.75 → below auto-merge
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.items_without_name == 3, rep.as_dict())
        check(rep.noname_audio_merges == 1 and rep.multi_item_clusters == 1, rep.as_dict())
        check(rep.noname_standalone == 2 and rep.noname_attached == 0,
              f"two nameless components ({{a,b}} and {{c}}), neither adopted: {rep.as_dict()}")
        check(_cid(conn, a) == _cid(conn, b), "≥0.85 with no names to contradict → one song")
        check(_cid(conn, c) not in (None, _cid(conn, a)),
              "0.65–0.85 is candidate evidence, never a merge — but c is still its own song")
        check(rep.band_review >= 1, "mid-band edge must be counted for §7.4 review")


def test_noname_bridge_never_joins_two_songs():
    """The constraint that makes the fallback safe. A nameless item with strong audio into TWO
    different named songs would, if attached, weld them together through the one item that
    cannot say which song it is. It is left standalone and counted instead."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(70)
        x = _add_item(conn, dur=200.0, fp=base, artist="Song", title="One")
        y = _add_item(conn, dur=200.4, fp=_mutate(base, 0.02), artist="Song", title="Two")
        n = _add_item(conn, dur=200.2, fp=_mutate(base, 0.01, 8))   # nameless, close to both
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.noname_ambiguous == 1 and rep.noname_attached == 0, rep.as_dict())
        check(len({_cid(conn, x), _cid(conn, y), _cid(conn, n)}) == 3,
              "an ambiguous bridge stays its own song rather than merging two")


def test_noname_item_adopts_its_only_named_match():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(71)
        named = _add_item(conn, dur=200.0, fp=base, artist="Cher", title="Believe")
        blank = _add_item(conn, dur=200.3, fp=_mutate(base, 0.02))
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.noname_attached == 1 and rep.noname_ambiguous == 0, rep.as_dict())
        check(rep.noname_standalone == 0,
              f"an ADOPTED component is not standalone -- counted before the union is "
              f"applied, or find() returns the named root and it is miscounted: {rep.as_dict()}")
        check(_cid(conn, named) == _cid(conn, blank), "unambiguous → adopt the song")
        cl = conn.execute("SELECT method, notes FROM clusters WHERE id=?",
                          (_cid(conn, named),)).fetchone()
        check(cl["method"] == "fingerprint", f"no name merged it, audio did: {dict(cl)}")


def test_name_identity_merges_despite_unrelated_audio():
    """The 2026-07-31 reframe. Equal names + fingerprints that agree on nothing used to be
    the textbook case for NOT merging; it is now a merge, because on this corpus that pattern
    is overwhelmingly two different karaoke cuts of one song, not two different songs. The
    audio disagreement is not discarded — it survives as the persisted fingerprint edge — but
    it no longer splits the song, and §7.4 RANKS the two copies rather than archiving either."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn, dur=200.0, fp=_rand_fp(6), artist="Cher", title="Believe")
        b = _add_item(conn, dur=201.0, fp=_rand_fp(7), artist="CHER!", title="believe")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 1 and rep.clustered_items == 2, rep.as_dict())
        check(rep.name_merge_pairs == 1, rep.as_dict())
        row = conn.execute(
            "SELECT similarity FROM cluster_edges WHERE item_a=? AND item_b=? "
            "AND edge_type='title_match'", (a, b)).fetchone()
        check(row is not None and row["similarity"] == 1.0,
              f"title_match similarity is name-evidence strength, not audio: {dict(row)}")
        cl = conn.execute("SELECT method, confidence, notes FROM clusters").fetchone()
        check(cl["method"] == "title_match" and cl["confidence"] == 1.0, dict(cl))
        notes = _json.loads(cl["notes"])
        check(notes["confidence_basis"] == "name_evidence"
              and notes["evidence"] == {"audio": 0, "name": 1, "manual": 0}, notes)


def test_name_key_is_order_insensitive():
    """artist/title order is a parse artifact here (§6.1 defers it to MusicBrainz), so the
    key must not encode it."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _add_item(conn, dur=200.0, fp=_rand_fp(40), artist="עידן רייכל", title="ממעמקים")
        _add_item(conn, dur=260.0, fp=_rand_fp(41), artist="ממעמקים", title="עידן רייכל")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 1, f"swapped artist/title must share a key: {rep.as_dict()}")
        check(cluster.name_signature("a b", "c") == cluster.name_signature("c", "b a"))


def test_artist_equals_title_yields_no_key():
    """A degenerate parse (artist == title) must not key on names at all.

    De-duplication collapses such a name to the tokens of ONE field, and a short token set is
    a SUBSET of every longer name built from the same words -- so containment merges it into
    all of them at once. Live, item 365 ("Let It Go" in both fields) pulled fifteen items into
    one cluster: Idina Menzel's song, four unrelated songs sharing only the title, and -- the
    key being order-insensitive -- Oasis "Go Let It Out" and Afrodite "Never Let It Go".

    The signature must be None, which routes the item to the no-name fallback. A singleton is
    the right answer for a name we cannot read.
    """
    check(cluster.name_signature("let it go", "let it go") is None)
    check(cluster.name_signature("the phantom of the opera",
                                 "the phantom of the opera") is None)
    # The guard is exact-equality only: a genuine superset is still a real name and keys.
    check(cluster.name_signature("let it go", "let it go frozen") is not None)

    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _add_item(conn, dur=200.0, fp=_rand_fp(70), artist="Let It Go", title="Let It Go")
        _add_item(conn, dur=205.0, fp=_rand_fp(71), artist="Idina Menzel", title="Let It Go")
        _add_item(conn, dur=210.0, fp=_rand_fp(72), artist="Oasis", title="Go Let It Out")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 3,
              f"degenerate name must not merge three distinct songs: {rep.as_dict()}")


def test_containment_never_merges_a_longer_title():
    """A short title contained in a longer one by the SAME artist is a different song.

    stage5 already pins this for enrichment ("'Crazy' vs 'Crazy in Love' must not match;
    containment is allowed for the ARTIST field only"); the identity key had been missing it,
    because name_signature combines artist+title into one set where a featured-artist superset
    and a longer title look identical. Live 2026-08-24: "Shania Twain / Don't" is a subset of
    the same artist's "Don't Be Stupid" and "That Don't Impress Me Much", and containment
    merged four distinct songs.
    """
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _add_item(conn, dur=200.0, fp=_rand_fp(80), artist="Shania Twain", title="Don't")
        _add_item(conn, dur=205.0, fp=_rand_fp(81),
                  artist="Shania Twain", title="Don't Be Stupid")
        _add_item(conn, dur=210.0, fp=_rand_fp(82),
                  artist="Shania Twain", title="That Don't Impress Me Much")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 3,
              f"a longer title by one artist must not absorb the shorter: {rep.as_dict()}")


def test_name_containment_merges_superset():
    """Featured-artist supersets and appended transliterations: one side's tokens are a
    proper subset of the other's."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn, dur=200.0, fp=_rand_fp(42),
                      artist="Pink", title="Just Give Me A Reason")
        b = _add_item(conn, dur=260.0, fp=_rand_fp(43),
                      artist="Pink & Nate Ruess", title="Just Give Me A Reason")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 1 and rep.name_containment_pairs == 1, rep.as_dict())
        row = conn.execute(
            "SELECT similarity FROM cluster_edges WHERE item_a=? AND item_b=? "
            "AND edge_type='title_match'", (min(a, b), max(a, b))).fetchone()
        check(row is not None and 0.6 <= row["similarity"] < 1.0,
              f"containment strength is |subset|/|superset|: {dict(row) if row else None}")


def test_containment_below_ratio_does_not_merge():
    """A short name swallowed by a much longer one is a coincidence, not an identity."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _add_item(conn, dur=200.0, fp=_rand_fp(44), artist="One Two", title="Three")
        _add_item(conn, dur=260.0, fp=_rand_fp(45),
                  artist="One Two Three Four Five", title="Six Seven Eight")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.multi_item_clusters == 0 and rep.clusters == 2,
              f"3/8 tokens is not identity — two songs, not one: {rep.as_dict()}")


def test_instrumental_conflict_blocks_name_merge():
    """The rule the old prohibition was really reaching for: a full-vocal original must not
    be merged into its karaoke cover. `is_instrumental` says that directly.

    Measured 2026-07-31 on the live index: 15,077 active items 'yes', 13,797 'unknown', 33
    NULL, and **'no' occurs nowhere at all** — so this gate can only ever block, never require,
    and today it fires zero times. It is kept because it is the right shape for the rule. Both
    'unknown' and NULL must pass through untouched: a gate demanding positive agreement would
    refuse ~48% of merges over a question we simply never answered."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn, dur=200.0, fp=_rand_fp(46), artist="Queen", title="Bohemian Rap")
        b = _add_item(conn, dur=260.0, fp=_rand_fp(47), artist="Queen", title="Bohemian Rap")
        conn.execute("UPDATE media_items SET is_instrumental='yes' WHERE id=?", (a,))
        conn.execute("UPDATE media_items SET is_instrumental='no'  WHERE id=?", (b,))
        conn.commit()
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.multi_item_clusters == 0 and rep.name_merge_blocked_instrumental == 1,
              rep.as_dict())
        check(rep.edges.get("title_match") == 1, "the evidence is still persisted")
        for value in ("unknown", None):
            conn.execute("UPDATE media_items SET is_instrumental=? WHERE id=?", (value, b))
            conn.commit()
            rep2 = cluster.cluster_all(conn, log=_quiet)
            check(rep2.multi_item_clusters == 1 and rep2.name_merge_blocked_instrumental == 0,
                  f"is_instrumental={value!r} must not block: {rep2.as_dict()}")


def test_operator_different_blocks_name_merge():
    """§10: a reviewer who has played both files and said 'different' outranks the key."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        a = _add_item(conn, dur=200.0, fp=_rand_fp(48), artist="Dup", title="Song Two")
        b = _add_item(conn, dur=260.0, fp=_rand_fp(49), artist="Dup", title="Song Two")
        conn.execute(
            "INSERT INTO review_queue (kind, media_item_id, payload, resolution, created_at) "
            "VALUES ('dedup_verdict', ?, ?, ?, datetime('now'))",
            (a, _json.dumps({"reason": "possible_duplicate_unclustered"}),
             _json.dumps({"verdicts": [{"other": b, "verdict": "different"}]})))
        conn.commit()
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.multi_item_clusters == 0 and rep.name_merge_blocked_operator == 1,
              rep.as_dict())
        check(_cid(conn, a) != _cid(conn, b), "the operator's split must survive re-clustering")


def test_oversize_name_group_keeps_edges_but_does_not_merge():
    """The old MAX_TITLE_GROUP dropped big groups entirely — no edges, no evidence. Now the
    two effects are separate: too big to trust is not too big to record."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        n = config.CLUSTER_NAME_GROUP_MAX_MERGE + 2
        for i in range(n):
            _add_item(conn, dur=100.0 + i, fp=_rand_fp(50 + i),
                      artist="Filler Band", title="Common Take")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.multi_item_clusters == 0 and rep.singleton_clusters == n,
              f"a group that big is a junk key: {rep.as_dict()}")
        check(rep.name_groups_edge_only == 1, rep.as_dict())
        check(rep.edges.get("title_match") == n * (n - 1) // 2, rep.as_dict())


def test_truncated_copy_clusters_via_prefix_and_marked_suspect():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        full = _rand_fp(8, n=900)
        a = _add_item(conn, dur=240.0, fp=full, artist="George Strait", title="Wrapped")
        b = _add_item(conn, dur=90.0, fp=full[:337],   # 90/240 of a 120s-window fp ≈ 337 words
                      artist="George  Strait", title="Wrapped!")
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.clusters == 1 and rep.clustered_items == 2,
              f"§7.3(c) must catch the truncated copy: {rep.as_dict()}")
        check(rep.edges.get("prefix_fingerprint") == 1, rep.as_dict())
        check(rep.truncation_suspects == 1)
        attrs = _json.loads(conn.execute(
            "SELECT quality_attrs FROM media_items WHERE id=?", (b,)).fetchone()[0])
        check(attrs.get("truncation_suspect") is True, "shorter item carries the mark")
        attrs_a = conn.execute(
            "SELECT quality_attrs FROM media_items WHERE id=?", (a,)).fetchone()[0]
        check(attrs_a is None or "truncation_suspect" not in attrs_a)


def test_prefix_artifact_coarse_hit_not_persisted():
    """A pair whose 66s prefix reads ≥0.6 but whose full-length sim collapses below the
    edge floor (the shared-quiet-intro artifact) must be dropped, not stored as an edge."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(30, n=900)
        w = config.CLUSTER_COARSE_WORDS
        other = _mutate(base, 0.38, 31)[:w] + _rand_fp(32, n=900 - w)  # prefix ~0.62, rest ~0.5
        a = _add_item(conn, dur=200.0, fp=base)
        b = _add_item(conn, dur=200.5, fp=other)
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.coarse_pairs == 1 and rep.refined_dropped == 1, rep.as_dict())
        check(conn.execute("SELECT COUNT(*) FROM cluster_edges").fetchone()[0] == 0)
        check(sum(rep.refined_histogram.values()) == 1)


def test_junk_keys_form_no_groups():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        for _ in range(3):
            _add_item(conn, dur=100.0 + 50 * _, fp=_rand_fp(20 + _),
                      artist="TArtist", title="TSongTitle")
        corpus = cluster.load_corpus(conn)
        groups, edge_only, skipped = cluster.title_groups(corpus)
        check(groups == {} and edge_only == {} and skipped == [],
              "template placeholder names must not become a matching key")


# --- sync / idempotency --------------------------------------------------------------------


def test_second_run_is_noop_and_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(9)
        _add_item(conn, dur=180.0, fp=base, artist="ABBA", title="Waterloo")
        _add_item(conn, dur=180.2, fp=_mutate(base, 0.01), artist="Abba", title="waterloo!")
        _add_item(conn, dur=185.0, fp=_rand_fp(10), artist="Other", title="Song")
        # 2 songs: the merged ABBA pair + the singleton. Universal assignment means the
        # singleton is a cluster row too, so "created" counts songs, not merges.
        dry = cluster.cluster_all(conn, dry_run=True, log=_quiet)
        check(dry.db_clusters_created == 2 and dry.db_edges_added >= 1, dry.as_dict())
        check(conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 0,
              "dry run must write nothing")
        check(conn.execute("SELECT COUNT(*) FROM cluster_edges").fetchone()[0] == 0)
        real = cluster.cluster_all(conn, log=_quiet)
        check(real.db_clusters_created == 2, real.as_dict())
        again = cluster.cluster_all(conn, log=_quiet)
        for k, v in again.as_dict().items():
            if k.startswith("db_") and k != "db_clusters_kept":
                check(v == 0, f"second run must be a no-op, {k}={v}")
        check(again.db_clusters_kept == 2)


def test_improved_name_rekeys_and_reshapes():
    """Decision 2: the key is the BEST AVAILABLE name, not a bootstrap-gated one. MB coverage
    is ~66% and worst on Hebrew, so better names must be able to arrive later and simply
    produce better keys. Here enrichment finally resolves a mis-parsed title; the next run
    must re-key that item and move it into the right song, with no orphaned cluster left
    behind and no re-architecture."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        good = _add_item(conn, dur=200.0, fp=_rand_fp(80), artist="Cher", title="Believe")
        bad = _add_item(conn, dur=240.0, fp=_rand_fp(81), artist="Cher", title="Beleive")
        cluster.cluster_all(conn, log=_quiet)
        check(_cid(conn, good) != _cid(conn, bad), "a typo is a different key, correctly")
        # Stage 5 writes a higher-trust row; v_metadata re-ranks it into first place.
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence) "
            "VALUES (?, 'title', 'Believe', 'musicbrainz_text', 0.95)", (bad,))
        conn.commit()
        rep = cluster.cluster_all(conn, log=_quiet)
        check(_cid(conn, good) == _cid(conn, bad),
              f"an improved name must re-key cleanly on re-run: {rep.as_dict()}")
        check(rep.clusters == 1 and rep.db_clusters_deleted >= 1, rep.as_dict())
        check(conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 1,
              "the vacated singleton cluster must be reconciled away, not orphaned")


def test_changed_input_reshapes_instead_of_duplicating():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        base = _rand_fp(11)
        a = _add_item(conn, dur=200.0, fp=base)
        b = _add_item(conn, dur=200.5, fp=_mutate(base, 0.01))
        cluster.cluster_all(conn, log=_quiet)
        old_cid = conn.execute(
            "SELECT cluster_id FROM media_items WHERE id=?", (a,)).fetchone()[0]
        # a third copy arrives (e.g. re-run after new downloads)
        c = _add_item(conn, dur=200.2, fp=_mutate(base, 0.015, 5))
        rep = cluster.cluster_all(conn, log=_quiet)
        cids = {conn.execute(
            "SELECT cluster_id FROM media_items WHERE id=?", (i,)).fetchone()[0]
            for i in (a, b, c)}
        check(len(cids) == 1 and None not in cids, "all three must share one cluster")
        check(conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 1,
              "membership change must not leave an orphaned cluster row")
        check(rep.db_clusters_created == 1 and rep.db_clusters_deleted == 1
              or (old_cid in cids and rep.db_clusters_created == 0), rep.as_dict())


def test_items_without_fingerprint_still_merge_on_name():
    """An item whose fpcalc failed used to be unclusterable by construction — it had no audio
    to compare, so it was crowned a sole copy no matter what its name said. Name identity is
    the only evidence such an item can carry, and it is now enough."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        _add_item(conn, dur=200.0, fp=_rand_fp(12), artist="Cher", title="Believe")
        _add_item(conn, dur=None, fp=None, artist="Cher", title="Believe")  # fp failed
        rep = cluster.cluster_all(conn, log=_quiet)
        check(rep.edges.get("title_match") == 1,
              f"suspect items stay visible via title_match: {rep.as_dict()}")
        check(rep.clusters == 1 and rep.clustered_items == 2, rep.as_dict())


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
