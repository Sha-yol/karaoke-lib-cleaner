"""Stage 3 §7.4 — RANKING the copies of a song (DB-only; file moves belong to Stage 6).

2026-07-31, second reframe (changelog §14.19). This module used to pick a winner and declare
everything else a `loser`, which Stage 6 would archive. It no longer does, and the vocabulary
changed with the behaviour: the non-default copies are `alternate` and they stay active.

**Only exact-content duplicates are duplicates.** Same content_hash — and Stage 0 (§5.2)
already collapsed those before an item existed. Everything §7.4 sees is a set of DISTINCT
files, and on this corpus we cannot show that any two of them are interchangeable:

  * formats are not interchangeable (§7.4.1; PRIMARY_FORMAT_PREFERENCE='video', and the
    non-primary format is deliberately kept active);
  * two mp3+cdg copies of one song can be different karaoke PRODUCTIONS — different key,
    different backing, guide vocal present or absent. 13,797 active items are
    `is_instrumental='unknown'`, so for roughly half the library we genuinely cannot tell.
    Archiving one behind the other would be deleting a distinct production on the strength
    of a filename match.

So within a song we RANK the copies and expose a default (`quality_rank` = 1), and nothing is
archived. This is what replaces §14.18's ARRANGEMENT axis: arrangements existed to stop name
merges from manufacturing dedup losers, and once nothing is archived there are no losers to
manufacture. A fingerprint-connected sub-group is no longer a crowning axis at all. It
survives in exactly one place — scoping the duration-outlier check, below — because that check
is a statement about one recording and nothing else.

Groups are per (SONG, FORMAT). Ranking is a TOTAL, deterministic order; the pre-existing keys
are unchanged:

  * mp3g / audio_only (§7.4.2): integrity, then bitrate ≥192 kbps preferred, then raw
    bitrate, tie-broken by smallest audio content_hash.
  * video (§7.4.3): integrity, then height, then audio bitrate, same tie-break. If the
    best-video (height) and best-audio (bitrate) candidates are DIFFERENT items, that is the
    recompressed-reupload trap: both become manual_review + one dedup_verdict row. They are
    still RANKED — a song must always have a default to serve — the verdict just says a human
    should choose.
  * Duration outliers (§7.4.4): a truncation_suspect, or an item >10s from the median of its
    SAME-RECORDING peers, sorts last and gets manual_review + a grouped review row. Scoped to
    audio-connected peers, never to the whole song: across two cuts of one song a 60s spread
    is expected and must not page anyone; within one recording it is a truncation.

Verdicts, now purely "what should happen to this file":

  * `sole_copy`  — the song has exactly ONE active item. Regardless of quality — except a
    BROKEN one, which also gets a quality_flag row (the replacement list) and
    quality_attrs.broken_sole (sha-yol, 2026-07-19: archive, do not serve; replacing it beats
    playing a broken file). `suspect` is NOT broken — Stage 4's decode clears or demotes it.
  * `winner`     — rank 1 of a song that has more than one copy. Per FORMAT, so a song with
    both an mp3g and a video has two winners; formats are peers.
  * `alternate`  — rank ≥ 2. **Stays active. Never archived.** (Was `loser`; migration 006
    renames the value. The old name described an outcome that no longer happens, and Stage 6
    is not written yet — this is the cheap moment to stop lying to it.)
  * `manual_review` — a human is needed (av split, duration outlier). The rank still stands,
    so there is always a default.

Audio edges no longer merge anything (§7.3), so the strongest audio evidence now arrives here
instead: an edge ≥ VERDICT_REVIEW_FLOOR between two DIFFERENT songs becomes ONE
`possible_song_merge` row per cluster pair — evidence to look at, never an automatic merge.
Most of these are parse artifacts (a typo, `AC/DC` vs `ACDC`, a Hebrew spelling variant) and
resolving one is a §10 `manual` edge, which §7.3 then re-applies every run. The floor no
longer has an upper bound: ≥0.85 used to auto-merge and so never needed a human, and that is
exactly the band that needs one now. Deviation #14 (§11) keeps the floor at 0.70 — the
benchmark put the non-duplicate noise ceiling at 0.678, so 0.65–0.70 is sound-alike noise;
those edges stay persisted for retuning, they just do not page anyone.

Idempotent: verdicts are recomputed deterministically and diffed (only changes written);
review rows are queued once per (kind, item/cluster, reason) — existing rows, open or
resolved, are never duplicated. §10 resolution tooling (future) must re-apply operator
verdicts after a re-run, like `resolve-pair` does for pairs.

§11 budget: an overrun is LOUD, never silent. The gate reports a per-kind projection
(open + new vs budget) in `budget_projection`, names the offending kinds in
`over_budget_kinds`, and by default rolls the whole transaction back — including the verdict
updates, which is the trap: a re-cluster that adds thousands of merge groups would otherwise
appear to "run fine" and change nothing. `partial=True` is the deliberate escape: apply the
verdicts and queue every kind that fits, withhold only the over-budget kind, and report
exactly what was withheld. Raising the budget instead is an operator decision recorded in
config.py, never something this module does for itself.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

INTEGRITY_RANK = {"decoded_ok": 0, "probed_ok": 1, "unchecked": 2, "suspect": 3, "broken": 4}
# §7.4.4 duration-outlier half-window. Applied WITHIN a same-recording group, never across a
# whole song: see the module docstring. Across two cuts of one song a large spread is the
# expected signature of two different karaoke productions, and treating it as an anomaly would
# page a human for every one of them.
OUTLIER_SPREAD_SEC = 10.0


@dataclass
class VerdictReport:
    items_seen: int = 0
    songs: int = 0                   # distinct clusters holding at least one active item
    sole_copies: int = 0
    multi_copy_songs: int = 0
    name_twin_songs: int = 0         # a one-copy song with a title_match edge into another
    winners: int = 0
    alternates: int = 0              # rank >= 2. Active, ranked, NOT archived.
    manual_review: int = 0
    overrides_applied: int = 0
    broken_sole: int = 0
    all_broken_groups: int = 0
    video_splits: int = 0
    outliers: int = 0
    verdicts_changed: int = 0
    verdicts_unchanged: int = 0
    ranks_changed: int = 0
    possible_song_merges: int = 0    # cross-song audio evidence, one row per cluster pair
    dedup_verdict_queued: int = 0
    quality_flag_queued: int = 0
    reviews_already_present: int = 0
    over_budget: bool = False
    over_budget_kinds: list = field(default_factory=list)
    budget_projection: dict = field(default_factory=dict)   # kind -> {open,new,projected,budget}
    reviews_withheld: dict = field(default_factory=dict)    # kind -> rows not queued (partial)
    stopped_reason: str | None = None
    partial: bool = False
    dry_run: bool = False
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def _median(values: list[float]) -> float:
    vs = sorted(values)
    n = len(vs)
    return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0


def rank_key_audio(m: dict) -> tuple:
    br = m["audio_bitrate_kbps"] or 0
    return (INTEGRITY_RANK.get(m["integrity"], 9), 0 if br >= 192 else 1, -br, m["hash"])


def rank_key_video(m: dict) -> tuple:
    return (INTEGRITY_RANK.get(m["integrity"], 9), -(m["height"] or 0),
            -(m["audio_bitrate_kbps"] or 0), m["hash"])


def video_split(members: list[dict]) -> tuple[dict, dict] | None:
    """§7.4.3: (best_by_video, best_by_audio) when they're different items and both
    signals actually exist — missing fields never manufacture a split."""
    with_h = [m for m in members if m["height"]]
    with_a = [m for m in members if m["audio_bitrate_kbps"]]
    if not with_h or not with_a:
        return None
    best_v = min(with_h, key=lambda m: (-(m["height"] or 0), m["hash"]))
    best_a = min(with_a, key=lambda m: (-(m["audio_bitrate_kbps"] or 0), m["hash"]))
    if best_v["id"] == best_a["id"]:
        return None
    if (best_v["audio_bitrate_kbps"] or 0) == (best_a["audio_bitrate_kbps"] or 0) \
            or (best_v["height"] or 0) == (best_a["height"] or 0):
        return None   # a tie on one axis is not a conflict, just a tie-break
    return best_v, best_a


def group_outliers(members: list[dict], recording_of: dict | None = None) -> set[int]:
    """§7.4.4: truncation suspects always; plus duration >10s from the median of the item's
    SAME-RECORDING peers, when that peer set is big enough for a median to mean anything
    (≥3 with known durations).

    `recording_of` maps item -> audio-connected component anchor. Passing None treats the whole
    group as one recording (only used when unit-testing this function directly). With it, the
    median is computed per recording — because a duration spread only means TRUNCATION among
    copies of one recording. Across two karaoke productions of one song a 60s spread is
    ordinary and must not page anyone; that distinction is the last surviving job of audio
    connectivity, and it is a scoping device here, never a crowning axis.

    An item MISSING from the map is its own recording, not a member of some shared default
    bucket — `same_recording_map` only lists items that have at least one strong audio edge,
    and "we have never shown this audio to match anything" is not evidence that it does. Get
    this wrong and a song whose copies were never fingerprint-compared reads as one recording
    with a huge spread, and every one of them pages a human.
    """
    out = {m["id"] for m in members if m["truncation_suspect"]}
    buckets: dict[object, list[dict]] = {}
    for m in members:
        key = "all" if recording_of is None else recording_of.get(m["id"], m["id"])
        buckets.setdefault(key, []).append(m)
    for peers in buckets.values():
        durs = [m["duration_sec"] for m in peers if m["duration_sec"] is not None]
        if len(durs) < 3:
            continue
        med = _median(durs)
        for m in peers:
            if m["duration_sec"] is not None \
                    and abs(m["duration_sec"] - med) > OUTLIER_SPREAD_SEC:
                out.add(m["id"])
    return out


def decide_group(members: list[dict], report: VerdictReport, reviews: list[dict],
                 recording_of: dict | None = None,
                 sole: bool = False,
                 prefer: set[int] | None = None) -> dict[int, tuple[str, int]]:
    """Rank + verdict for one (song, format) group. Returns item_id -> (verdict, rank).

    The order is TOTAL and deterministic: outliers sort last (which is what "never auto-
    crowned" always meant — they are simply not eligible to be the default), then the
    pre-existing §7.4.2/§7.4.3 keys, unchanged. Every member gets a rank, including the
    manual_review ones, so a song ALWAYS has a default copy to serve even while a human
    question about it is open.

    `sole` marks a song with exactly one active item — the §7.4.5 `sole_copy` case, which is
    about the SONG having one copy and so is decided by the caller, not here.

    Review rows keep their existing `reason` strings verbatim: §10's sheet tooling and
    resolved_av_overrides() match on them, and rewording one would orphan every resolution
    already recorded against it.
    """
    cluster_id = members[0]["cluster_id"]
    fmt = members[0]["format"]

    outliers = group_outliers(members, recording_of)
    key = rank_key_video if fmt == "video" else rank_key_audio
    # A reviewer who listened to both copies outranks every measured proxy, so `prefer` sorts
    # ahead of the outlier demotion: an explicit "keep this one" is exactly the human judgment
    # the duration heuristic is a stand-in for. Then outliers last, then the untouched ranking
    # key. Still a TOTAL order over the whole group.
    prefer = prefer or set()
    ranked = sorted(members, key=lambda m: (0 if m["id"] in prefer else 1,
                                            1 if m["id"] in outliers else 0, key(m)))
    rank_of = {m["id"]: n for n, m in enumerate(ranked, start=1)}

    if outliers:
        report.outliers += len(outliers)
        reviews.append({
            "kind": "dedup_verdict", "cluster_id": cluster_id, "media_item_id": None,
            "reason": f"duration_outliers_{fmt}",
            "payload": {
                "reason": f"duration_outliers_{fmt}",
                "outlier_items": sorted(outliers),
                "group": [{"id": m["id"], "dur": m["duration_sec"]} for m in members],
            },
        })

    # `not sole` matters: for a one-copy song "every copy is broken" and "the sole copy is
    # broken" are the SAME fact, and §7.4.5 already raises it as `broken_sole_copy` with the
    # item attached. Emitting both would double-charge the §11 quality_flag budget and hand
    # the operator two rows for one file. Before universal cluster assignment a sole copy
    # never reached this function at all, so this guard restores that exactly.
    if not sole and all(m["integrity"] == "broken" for m in members):
        report.all_broken_groups += 1
        reviews.append({
            "kind": "quality_flag", "cluster_id": cluster_id, "media_item_id": None,
            "reason": f"all_broken_{fmt}",
            "payload": {"reason": f"all_broken_{fmt}",
                        "items": [m["id"] for m in members],
                        "action": "find a replacement; every copy is unplayable"},
        })

    split = video_split([m for m in members if m["id"] not in outliers]) \
        if fmt == "video" else None
    # §7.4.3 — config.AV_SPLIT_POLICY. Under 'prefer_video' the split is still COUNTED (it is
    # real, and the telemetry is how we would notice the policy starting to matter), but it
    # asks nothing and parks nothing: rank_key_video already sorts height before bitrate, so
    # "serve the better picture" is what the deterministic order does when left alone.
    from . import config as _config
    ask_split = _config.AV_SPLIT_POLICY == "ask"
    if split is not None:
        report.video_splits += 1
        best_v, best_a = split
        if ask_split:
            reviews.append({
                "kind": "dedup_verdict", "cluster_id": cluster_id, "media_item_id": None,
                "reason": "video_av_split",
                "payload": {
                    "reason": "video_av_split",
                    "best_video": {"id": best_v["id"], "height": best_v["height"],
                                   "audio_kbps": best_v["audio_bitrate_kbps"]},
                    "best_audio": {"id": best_a["id"], "height": best_a["height"],
                                   "audio_kbps": best_a["audio_bitrate_kbps"]},
                },
            })

    split_ids = {best_v["id"], best_a["id"]} if split is not None and ask_split else set()
    out: dict[int, tuple[str, int]] = {}
    for m in ranked:
        rank = rank_of[m["id"]]
        if m["id"] in outliers or m["id"] in split_ids:
            verdict = "manual_review"
        elif sole:
            verdict = "sole_copy"
        elif rank == 1:
            verdict = "winner"
        else:
            verdict = "alternate"
        out[m["id"]] = (verdict, rank)
    return out


def same_recording_map(conn) -> dict[int, int]:
    """active item -> the anchor of its audio-connected component WITHIN its own song.

    All that survives of §14.18's arrangement model, and deliberately demoted to one job:
    scoping the §7.4.4 duration-outlier check. It decides nothing about identity and nothing
    about ranking — a duration spread means TRUNCATION only among copies of one recording, so
    the median has to be taken there and nowhere else.

    Audio-connected means fingerprint/prefix_fingerprint ≥ CLUSTER_AUTO_MERGE, or a §10
    manual edge, between two items of the SAME cluster. An item connected to nobody is its own
    recording — "we have never shown this audio to match anything" is not evidence that it
    does. Derived from persisted edges, so a §11 retune of CLUSTER_AUTO_MERGE re-derives it
    without recomputing a fingerprint, and nothing needs migrating.
    """
    from . import config as _config
    from .cluster import DSU
    dsu = DSU()
    for r in conn.execute(
        """
        SELECT e.item_a, e.item_b FROM cluster_edges e
        JOIN media_items a ON a.id = e.item_a AND a.status='active'
        JOIN media_items b ON b.id = e.item_b AND b.status='active'
        WHERE (e.edge_type = 'manual'
               OR (e.edge_type IN ('fingerprint','prefix_fingerprint')
                   AND e.similarity IS NOT NULL AND e.similarity >= ?))
          AND a.cluster_id IS NOT NULL AND a.cluster_id = b.cluster_id
        """, (_config.CLUSTER_AUTO_MERGE,)):
        dsu.union(r["item_a"], r["item_b"])
    return {i: dsu.find(i) for i in list(dsu.parent)}


def name_twins(conn) -> dict[int, list[int]]:
    """item -> active items linked to it by a §7.3(b) `title_match` edge.

    Read from persisted edges, so it covers name twins §7.3 chose NOT to merge as well as ones
    it did — an oversize/junk-suspect group, an is_instrumental conflict, or an operator veto.
    A one-copy song with a name twin sitting in another song is an unresolved identity
    question, not proof that the song has one copy; §7.4.5 uses this to keep such an item off
    the broken-sole replacement list, where it would ask us to replace a song we may
    well already have twice.
    """
    out: dict[int, list[int]] = {}
    for r in conn.execute(
            "SELECT e.item_a, e.item_b, e.similarity FROM cluster_edges e "
            "JOIN media_items a ON a.id = e.item_a AND a.status='active' "
            "JOIN media_items b ON b.id = e.item_b AND b.status='active' "
            "WHERE e.edge_type = 'title_match' AND a.cluster_id IS NOT b.cluster_id"):
        out.setdefault(r["item_a"], []).append(r["item_b"])
        out.setdefault(r["item_b"], []).append(r["item_a"])
    return {k: sorted(v) for k, v in out.items()}


def _load_items(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.id, i.format, i.cluster_id, i.duration_sec, i.audio_bitrate_kbps,
               i.height, i.quality_attrs, i.quality_verdict, i.quality_rank,
               f.content_hash AS hash, b.integrity_status AS integrity
        FROM media_items i
        JOIN media_item_files f ON f.media_item_id = i.id AND f.role IN ('audio', 'av')
        JOIN blobs b ON b.content_hash = f.content_hash
        WHERE i.status = 'active'
        ORDER BY i.id
        """
    ).fetchall()
    out = []
    for r in rows:
        attrs = {}
        if r["quality_attrs"]:
            try:
                attrs = json.loads(r["quality_attrs"])
            except json.JSONDecodeError:
                pass
        out.append({**dict(r), "attrs": attrs,
                    "truncation_suspect": bool(attrs.get("truncation_suspect"))})
    return out


def _cross_song_reviews(conn, reviews: list[dict], report: VerdictReport) -> None:
    """Audio evidence that two DIFFERENT songs might be one song (§7.3 decision (a)).

    One row per CLUSTER PAIR, never one per edge, so §11 budgets stay meaningful. Same-song
    edges ask nothing — the two items are already together, which is the answer.

    Note the band: `>= VERDICT_REVIEW_FLOOR` with NO upper bound. Under the previous model
    this query stopped at CLUSTER_AUTO_MERGE because anything above it had already been
    merged automatically. Nothing merges on audio now, so the ≥0.85 edges — the ones that
    genuinely are the same audio — are the most important rows this function emits, not the
    ones to skip. Resolving one is a §10 `manual` edge, which §7.3 re-applies every run.

    The old `possible_duplicate_unclustered` kind is gone with the NULL cluster_id it keyed
    on: every item now has a cluster, so a "loose" item is simply a one-copy song and its
    cross-song edges are covered by the cluster-pair rows below.
    """
    from . import config as _config
    floor = _config.VERDICT_REVIEW_FLOOR
    edges = conn.execute(
        """
        SELECT e.item_a, e.item_b, e.edge_type, e.similarity,
               a.cluster_id AS ca, b.cluster_id AS cb
        FROM cluster_edges e
        JOIN media_items a ON a.id = e.item_a
        JOIN media_items b ON b.id = e.item_b
        WHERE e.edge_type IN ('fingerprint', 'prefix_fingerprint')
          AND e.similarity >= ?
          AND a.status = 'active' AND b.status = 'active'
          AND a.cluster_id IS NOT b.cluster_id
        ORDER BY e.item_a, e.item_b
        """, (floor,)).fetchall()

    cross: dict[tuple, list] = {}
    for e in edges:
        cross.setdefault((min(e["ca"], e["cb"]), max(e["ca"], e["cb"])), []).append(e)

    for (c1, c2), es in sorted(cross.items()):
        report.possible_song_merges += 1
        reviews.append({
            "kind": "dedup_verdict", "cluster_id": c1, "media_item_id": None,
            "reason": f"possible_song_merge_{c1}_{c2}",
            "payload": {
                "reason": f"possible_song_merge_{c1}_{c2}", "other_song": c2,
                "action": "Same song under two names, or two different songs that sound "
                          "alike? Audio evidence only — §7.3 never merges on it. If they are "
                          "the same song, record a `manual` cluster edge (§10).",
                "best_similarity": max(e["similarity"] for e in es),
                "edges": [{"a": e["item_a"], "b": e["item_b"],
                           "sim": e["similarity"], "type": e["edge_type"]} for e in es],
            },
        })


def resolved_av_overrides(conn) -> dict[int, str]:
    """§10 operator-override re-apply. A resolved `video_av_split` dedup_verdict row carries the
    reviewer's pick (payload item ids + resolution verdict A/B/both). We force the computed
    verdicts accordingly, EVERY run — the durable fact is the resolution, the verdict is
    re-derived from it, exactly the contract this module's docstring names.

      * A       -> best_video wins,  best_audio becomes an alternate
      * B       -> best_audio wins,  best_video becomes an alternate
      * both    -> both winners (operator wants both served)
      * neither -> both alternates (operator judged both copies unusable; the song may end up
                   with no `winner` in this format — that is the operator's call, and since
                   nothing is archived the files are all still there)
      * unsure/other -> no override (the deterministic manual_review stands)

    'alternate' is what this used to call 'loser' (migration 006). The forced verdict changes
    which copy is presented as the default; it never marks anything for archival.

    Returns item_id -> forced verdict. Idempotent: deterministic, diffed like everything else.
    """
    out: dict[int, str] = {}
    rows = conn.execute(
        "SELECT payload, resolution FROM review_queue "
        "WHERE kind='dedup_verdict' AND resolution IS NOT NULL "
        "AND json_extract(payload, '$.reason')='video_av_split'"
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload"])
        verdict = (json.loads(r["resolution"]) or {}).get("verdict")
        bv = (payload.get("best_video") or {}).get("id")
        ba = (payload.get("best_audio") or {}).get("id")
        if bv is None or ba is None:
            continue
        if verdict == "A":
            out[bv], out[ba] = "winner", "alternate"
        elif verdict == "B":
            out[bv], out[ba] = "alternate", "winner"
        elif verdict == "both":
            out[bv] = out[ba] = "winner"
        elif verdict == "neither":
            out[bv] = out[ba] = "alternate"
    return out


def resolved_dup_preferences(conn) -> set[int]:
    """§10 operator-override re-apply, rank axis. A resolved `duplicates` row may carry a
    per-pair `prefer_item` (from the sheet's `better_copy` column): the reviewer said both
    copies are the same song AND which one should be served.

    Returned as a SET of preferred item ids, deliberately not a pairwise order. Preference is
    recorded per pair, but ranking happens per (song, format) group, and once a `same` verdict
    merges the pair those are not the same population — a group can hold copies the reviewer
    never compared. A tier ("someone picked this one") is the honest projection: preferred
    copies sort ahead, and two preferred copies in one group fall back to the deterministic
    key rather than inventing an order no reviewer stated.

    Re-applied EVERY run, exactly like resolved_av_overrides: the durable fact is the
    resolution, the rank is re-derived from it.
    """
    out: set[int] = set()
    for r in conn.execute(
            "SELECT resolution FROM review_queue "
            "WHERE kind='dedup_verdict' AND resolution IS NOT NULL "
            "AND (json_extract(payload, '$.reason') LIKE 'possible_song_merge_%' "
            "     OR json_extract(payload, '$.reason')='possible_duplicate_unclustered')"):
        res = json.loads(r["resolution"]) or {}
        for v in res.get("verdicts") or []:
            # only a `same` verdict merges the pair, so only then is a rank preference
            # meaningful — the two copies must end up in one group to be ranked against
            # each other at all.
            if v.get("verdict") == "same" and v.get("prefer_item") is not None:
                out.add(int(v["prefer_item"]))
    return out


def run_verdicts(conn, *, dry_run: bool = False, budget: int | None = None,
                 partial: bool = False) -> VerdictReport:
    """§7.4 end-to-end. Single transaction; dry-run computes and reports without writing.

    §11 budget overrun rolls EVERYTHING back (verdicts included) and reports why, in numbers.
    `partial=True` instead applies the verdicts and withholds the over-budget kind's review
    rows — still reported as over_budget, still naming the kind and the projection.
    """
    from . import config as _config
    budget = _config.REVIEW_QUEUE_BUDGET if budget is None else budget
    report = VerdictReport(dry_run=dry_run)
    t0 = time.time()

    items = _load_items(conn)
    report.items_seen = len(items)
    reviews: list[dict] = []
    verdicts: dict[int, str] = {}
    ranks: dict[int, int] = {}

    recording_of = same_recording_map(conn)
    twins = name_twins(conn)
    active_ids = {m["id"] for m in items}
    dup_prefer = resolved_dup_preferences(conn)

    # §7.3 assigns every active item a cluster, so there is no NULL branch here any more.
    # An item with cluster_id NULL can now only be a stale row from before `cluster` last ran;
    # it is keyed on its own id so it behaves as the one-copy song it is, rather than crashing
    # or silently joining a "None" bucket with every other such item.
    by_song: dict[object, list[dict]] = {}
    for m in items:
        by_song.setdefault(m["cluster_id"] if m["cluster_id"] is not None
                           else ("item", m["id"]), []).append(m)
    report.songs = len(by_song)
    report.multi_copy_songs = sum(1 for ms in by_song.values() if len(ms) > 1)

    sole_ids: set[int] = set()
    for song_key in sorted(by_song, key=lambda k: (isinstance(k, tuple), k)):
        members = by_song[song_key]
        sole = len(members) == 1
        by_format: dict[str, list[dict]] = {}
        for m in members:
            by_format.setdefault(m["format"], []).append(m)
        for fmt in sorted(by_format):
            for item_id, (verdict, rank) in decide_group(
                    by_format[fmt], report, reviews, recording_of, sole=sole,
                    prefer=dup_prefer).items():
                verdicts[item_id] = verdict
                ranks[item_id] = rank
        if not sole:
            continue

        # §7.4.5 — the song has exactly one copy. `sole_copy` is now a fact about the SONG,
        # not about an item happening to be unclustered, which is what made the old rule
        # produce 3,572 wrong verdicts.
        m = members[0]
        if verdicts.get(m["id"]) != "sole_copy":
            continue      # an outlier/av-split question outranks the crown; leave it open
        report.sole_copies += 1
        # A name twin in ANOTHER song means we may well already hold this song twice under a
        # slightly different spelling. Looking for a replacement would be wasted work, and the
        # honest answer is the unresolved identity question, so such an item is counted and
        # kept OFF the replacement list even when it is broken.
        tw = [t for t in twins.get(m["id"], []) if t in active_ids]
        if tw:
            report.name_twin_songs += 1
        if m["integrity"] == "broken" and not tw:
            sole_ids.add(m["id"])
            report.broken_sole += 1
            reviews.append({
                "kind": "quality_flag", "cluster_id": m["cluster_id"],
                "media_item_id": m["id"], "reason": "broken_sole_copy",
                "payload": {"reason": "broken_sole_copy", "format": m["format"],
                            "action": "find a replacement; sole copy is unplayable "
                                      "(sha-yol 2026-07-19: archive, do not serve)"},
            })

    # §10 re-apply operator av_split verdicts on top of the deterministic computation, before
    # counting/diffing, so a resolved split releases its manual_review items.
    for item_id, forced in resolved_av_overrides(conn).items():
        if item_id in verdicts and verdicts[item_id] != forced:
            verdicts[item_id] = forced
            report.overrides_applied += 1

    _cross_song_reviews(conn, reviews, report)

    for v in verdicts.values():
        if v == "winner":
            report.winners += 1
        elif v == "alternate":
            report.alternates += 1
        elif v == "manual_review":
            report.manual_review += 1

    # -- review queueing: once per (kind, cluster/item, reason), then the §11 gate --------
    existing = set()
    for r in conn.execute(
            "SELECT kind, cluster_id, media_item_id, "
            "json_extract(payload, '$.reason') AS reason FROM review_queue "
            "WHERE kind IN ('dedup_verdict', 'quality_flag')"):
        existing.add((r["kind"], r["cluster_id"], r["media_item_id"], r["reason"]))
    open_counts = {
        r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM review_queue "
            "WHERE kind IN ('dedup_verdict', 'quality_flag') AND resolution IS NULL "
            "GROUP BY kind")}
    to_queue = []
    for rv in reviews:
        k = (rv["kind"], rv["cluster_id"], rv["media_item_id"], rv["reason"])
        if k in existing:
            report.reviews_already_present += 1
            continue
        existing.add(k)
        to_queue.append(rv)
        if rv["kind"] == "dedup_verdict":
            report.dedup_verdict_queued += 1
        else:
            report.quality_flag_queued += 1

    # -- §11 budget gate. Loud by design: the projection is reported per kind whether or not
    # it trips, so a run that is about to blow the ceiling says so in numbers rather than in a
    # silent rollback. See the module docstring for why `partial` exists.
    for kind, queued in (("dedup_verdict", report.dedup_verdict_queued),
                         ("quality_flag", report.quality_flag_queued)):
        open_now = open_counts.get(kind, 0)
        projected = open_now + queued
        report.budget_projection[kind] = {
            "open": open_now, "new": queued, "projected": projected, "budget": budget,
            "over_by": max(0, projected - budget),
        }
        if projected > budget:
            report.over_budget = True
            report.over_budget_kinds.append(kind)
    if report.over_budget:
        detail = "; ".join(
            f"{k}: {report.budget_projection[k]['open']} open + "
            f"{report.budget_projection[k]['new']} new = "
            f"{report.budget_projection[k]['projected']} > budget {budget} "
            f"(over by {report.budget_projection[k]['over_by']})"
            for k in report.over_budget_kinds)
        if not partial:
            report.stopped_reason = (
                f"§11 review budget exceeded — {detail}. NOTHING was written: verdicts were "
                f"rolled back too, not just the review rows. Drain the queue (§10 review "
                f"tooling), retune the thresholds that generate this kind, or re-run with "
                f"partial=True to apply the verdicts and withhold only the over-budget kind. "
                f"Do not raise REVIEW_QUEUE_BUDGET to make this message go away.")
            conn.rollback()
            report.elapsed_sec = time.time() - t0
            return report
        report.partial = True
        withheld = [rv for rv in to_queue if rv["kind"] in report.over_budget_kinds]
        to_queue = [rv for rv in to_queue if rv["kind"] not in report.over_budget_kinds]
        for rv in withheld:
            report.reviews_withheld[rv["kind"]] = report.reviews_withheld.get(rv["kind"], 0) + 1
            if rv["kind"] == "dedup_verdict":
                report.dedup_verdict_queued -= 1
            else:
                report.quality_flag_queued -= 1
        report.stopped_reason = (
            f"§11 review budget exceeded — {detail}. PARTIAL run: verdicts applied, "
            f"{sum(report.reviews_withheld.values())} review row(s) withheld and recomputed "
            f"(unchanged) on the next run once the queue is drained.")

    # -- apply ----------------------------------------------------------------------------
    now = "datetime('now')"
    for m in items:
        v = verdicts.get(m["id"], "pending")
        r = ranks.get(m["id"])
        if m["quality_verdict"] != v:
            report.verdicts_changed += 1
            if not dry_run:
                conn.execute(
                    f"UPDATE media_items SET quality_verdict=?, updated_at={now} WHERE id=?",
                    (v, m["id"]))
        else:
            report.verdicts_unchanged += 1
        # quality_rank is diffed separately from the verdict: a re-rank with no verdict change
        # is a real change (the default copy moved) and must not be reported as a no-op, and a
        # verdict change with no re-rank must not be double-counted. Idempotency is asserted on
        # both counters.
        if m["quality_rank"] != r:
            report.ranks_changed += 1
            if not dry_run:
                conn.execute(
                    f"UPDATE media_items SET quality_rank=?, updated_at={now} WHERE id=?",
                    (r, m["id"]))
        # independent of verdict churn: Stage 4 can demote a blob to broken later.
        # sole_ids holds only the genuine replacement cases — a one-copy song whose single
        # file is broken AND which has no name twin elsewhere. A broken item WITH a twin is not
        # a replacement candidate: another copy of the song may be sitting right there under a
        # different spelling, which is the question `possible_song_merge`/the twin count raises.
        if m["id"] in sole_ids and m["integrity"] == "broken" \
                and not m["attrs"].get("broken_sole"):
            attrs = {**m["attrs"], "broken_sole": True}
            if not dry_run:
                conn.execute(
                    f"UPDATE media_items SET quality_attrs=?, updated_at={now} WHERE id=?",
                    (json.dumps(attrs, ensure_ascii=False), m["id"]))
    if not dry_run:
        for rv in to_queue:
            conn.execute(
                "INSERT INTO review_queue (kind, cluster_id, media_item_id, payload, created_at) "
                "VALUES (?,?,?,?, datetime('now'))",
                (rv["kind"], rv["cluster_id"], rv["media_item_id"],
                 json.dumps(rv["payload"], ensure_ascii=False)))
        conn.commit()
    report.elapsed_sec = time.time() - t0
    return report
