"""Stage 3 §7.3 — clustering. **A SONG IS (ARTIST, TITLE), and the name is the ONLY key.**

2026-07-31, second reframe of the day (spec §7.3/§7.4 rewritten, changelog §14.19). This
SUPERSEDES the arrangement model of §14.18: fingerprint and prefix_fingerprint edges are gone
from the union-find entirely. They are still computed and still persisted — §3.5's "persist
all edges" is untouched, and §11 must be able to retune from them without recomputation — but
they no longer decide what a cluster IS.

Why. §14.18 kept audio edges as a second merge key alongside names, and the two are logically
incompatible with "cluster == song". An audio edge glues items together regardless of what
they are called, so a cluster built partly from audio has no well-defined name — and a song
that cannot be named cannot be searched, which is the only thing a user ever does with this
library. Measured on the live index under §14.18: **2,424 clusters contain more than one
distinct artist+title string** (crude lower+trim key, so this overstates true ambiguity, but
the mechanism is real). The reverse error is rare — 41 names split across songs — so the
audio key was buying precision the corpus did not need while costing coherence it did.

Audio evidence keeps exactly two jobs, both secondary and neither of them a merge:

  (a) **cross-song review.** Two DIFFERENT name-clusters with a high audio similarity are
      worth a human's attention — usually a parse artifact (a typo, `AC/DC` vs `ACDC`, a
      Hebrew spelling variant). §7.4 queues one `possible_song_merge` row per cluster pair.
      Evidence to look at, never an automatic merge: if it merged, we would be back to
      clusters with no name.
  (b) **no-name fallback.** An item whose parse yields no usable artist+title has no key at
      all. Those items group by audio among themselves, and a no-name audio component
      attaches to a named song when its strong edges point at EXACTLY ONE named component —
      ambiguity leaves it standalone and is reported. This is the one place audio creates
      grouping, and it is constructed so it can never join two named songs together.

Consequently nothing is archived because two filenames agreed, and nothing is archived
because two fingerprints agreed either: §7.4 RANKS the copies of a song and exposes a
default. Only exact-content duplicates (same content_hash) are true duplicates, and Stage 0
already collapsed those.

**Every active item gets a cluster**, singletons included (sha-yol: "singletons have their own
cluster, that's fine"). `cluster_id` is therefore non-NULL for every active item and every
downstream query loses its NULL branch — see `v_songs`, one row per cluster.

The key is the BEST AVAILABLE name, not a bootstrap-gated one: whatever `v_metadata` serves,
i.e. the canonical value where MusicBrainz/Spotify resolved it and the filename parse
otherwise. The MB bootstrap reaches only ~66% of items (18,992/28,907 carry a song_mbid) and
fails worst on Hebrew, so gating on it would strand exactly the population that needs the
grouping most. Better names simply flow through and produce better keys; a re-run re-keys the
affected items and reshapes their clusters with no re-architecture (`_sync_db` is a full diff).

Candidate generation (union of all three, per spec):
  (a) duration blocking ±10s;
  (b) normalized name identity regardless of duration — catches truncated copies AND the
      re-encodes whose audio has nothing in common with their twin. ORDER-INSENSITIVE (the
      artist/title boundary is a parse artifact in this library — §6.1's deferred order
      question — so the key is the token set of artist+title combined) and CONTAINMENT-aware
      (one side a proper subset of the other: featured-artist supersets, appended
      transliterations);
  (c) prefix-fingerprint comparison for (b)-pairs with unequal durations.

Scale: 51.4M (a)-pairs at full library size, so the (a) pass is two-tier: a numpy-batched
COARSE similarity (offset 0, first CLUSTER_COARSE_WORDS uint32 words, zero-pad-corrected)
collects candidates at ≥ CLUSTER_COARSE_FLOOR, and only those get the exact fp_similarity
(±3 offsets, full length). The floor sits below the 0.65 review threshold and above the noise
band's edge (benchmark: 29/60,240 pairs ≥0.6; true duplicates start at 0.96) — a real
duplicate cannot coarse-score under it, because offset-0-on-the-prefix is within a few
percent of the best-offset score for same-recording re-encodes.

Edges persisted (§3.5 "persist all edges"): every (a)-candidate whose REFINED (full-length)
similarity is ≥ CLUSTER_EDGE_FLOOR — the sub-threshold 0.60–0.65 band is kept for
auditability and retuning, but coarse hits whose full-length sim collapses below 0.60 are
prefix-similarity artifacts (karaoke intros are low-entropy: silence and count-ins read
alike) and are dropped, histogrammed in the report — every (b) pair as `title_match`
(similarity = name-evidence strength, see below), plus exact `fingerprint` sims for (b) pairs
within the duration block and `prefix_fingerprint` sims beyond it. The ~51M noise pairs are
NOT persisted — a table of 0.5-similarity rows audits nothing.

Merging (`cluster_all`), in this order and no other:
  1. **name-identity edges** from a group small enough to be a real song rather than a junk
     key — vetoed wherever a reviewer has already called the two components `different` (§10
     records that as a resolution, not as an edge; a human who played both files outranks the
     key), and skipped on an explicit `is_instrumental` yes/no conflict. That gate can only
     ever BLOCK, never require: measured 2026-07-31 over active items, 15,077 are 'yes',
     13,797 'unknown', 33 NULL, and **'no' does not occur — not on one item, not on one
     location parse**. So it currently fires zero times, and a gate demanding positive
     agreement would instead block ~48% of all merges. It is kept because it costs nothing
     and becomes real the moment a full-vocal original is ingested. The 33 NULLs are handled
     by set membership, not by a truthiness test (see `instrumental_conflict`).
  2. **§10 manual edges**, unconditionally. An operator saying "these two are the same song"
     is the one thing allowed to join two named songs, because it is a statement about the
     song, not about the audio.
  3. **the no-name fallback** (see (b) above), which by construction can only attach a
     nameless component to an existing song or leave it alone.

Audio edges never merge. A prefix_fingerprint edge ≥ CLUSTER_AUTO_MERGE still marks the
shorter item's quality_attrs.truncation_suspect — that is a measurement about one file
("same audio prefix, less of it"), independent of who ends up clustered with whom, and §7.4.4
uses it to keep a truncated file from being ranked the default copy.

`title_match.similarity` is NAME-EVIDENCE STRENGTH, not audio similarity: 1.0 for an
identical token set, |subset|/|superset| for a containment match. Consumers MUST branch on
edge_type — comparing 0.85 audio similarity against 0.7 name evidence is meaningless (§3.5,
§7.4's cross-song query filters on edge_type for this reason).

Sync is a full diff against the DB (edges of the three §7.3 types, clusters, cluster_id
assignments): deterministic inputs => second run is a no-op. No review rows are queued here;
audio edges ≥ VERDICT_REVIEW_FLOOR that cross a song boundary surface at §7.4 verdict time
with cluster context, one row per cluster pair rather than per edge.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field

from .stage3 import ID3_JUNK_VALUES, decode_fp, fp_similarity

# Placeholder metadata seen in this library's karaoke-template files; a shared junk key would
# weld unrelated songs into one (b)-group.
CLUSTER_JUNK_KEYS = ID3_JUNK_VALUES | {"tartist", "tsongtitle", "track", "demo", "sample"}
MIN_OVERLAP_WORDS = 16  # same guard as fp_similarity: less overlap means nothing
# Group-size guards live in config (§11 retune): CLUSTER_NAME_GROUP_MAX_MERGE /
# CLUSTER_NAME_GROUP_MAX_EDGES. The old module-level MAX_TITLE_GROUP=30 conflated "too big to
# merge" with "too big to even look at" and silently dropped the evidence; see config.py.


def norm_key(s: str | None) -> str | None:
    """Aggressive matching key (NOT display): NFKD, strip diacritics, casefold, alnum+Hebrew
    only, collapsed whitespace. None for empty or junk."""
    if not s:
        return None
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = re.sub(r"[^0-9a-z֐-ת ]+", " ", s)
    s = " ".join(s.split())
    if not s or s in CLUSTER_JUNK_KEYS:
        return None
    return s


def name_signature(artist_key: str | None, title_key: str | None) -> tuple[str, ...] | None:
    """§7.3(b) identity key: the sorted, de-duplicated TOKEN SET of artist+title combined.

    Order-insensitive by construction — the artist/title boundary is a parse artifact in this
    library, not a fact about the item. §6.1 explicitly defers the Hebrew "which side is the
    artist" question, and the sampled residue of disagreeing clusters is dominated by exactly
    that class (order swaps, one-letter typos, appended transliterations, featured-artist
    supersets), not by fingerprint false merges. Keying on the ordered pair discards those
    matches for a distinction the data does not support: measured on the live DB, the
    order-insensitive key finds 2,119 merge groups against 1,989 for the ordered key.

    De-duplicated because a repeated word ("Love Love") is a spelling of the same identity
    claim, not a stronger one. None when the combined name is too thin to key on.

    ARTIST == TITLE YIELDS NO KEY (2026-08-24). De-duplication is what makes this dangerous:
    when both fields carry the same string the combined set collapses to the tokens of ONE
    field, so the signature is half the size the key assumes -- and a short token set is a
    SUBSET of every longer name built from the same words. `containment_pairs` then merges the
    item into all of them at once.

    Measured on the live index the day it landed: item 365 parsed to artist "Let It Go" /
    title "Let It Go", collapsing to {go, it, let}, and pulled fifteen items into one cluster --
    Idina Menzel's song, four unrelated songs that merely share the title (James Bay,
    Alexandra Burke, Will Young, Cavo), and, because the key is also order-insensitive, two
    with DIFFERENT titles: Oasis "Go Let It Out" and Afrodite "Never Let It Go", both of which
    contain those three tokens in some order. Item 297 ("The Phantom of the Opera" in both
    fields) did the same to three separate songs from that musical. 25 items library-wide hold
    artist == title; 4 clusters larger than two items were affected.

    Such an item is not merely oddly named -- we cannot tell which field is the artist, so it
    has no artist/title identity to key on at all. Returning None routes it to the no-name
    fallback, where audio groups it among other nameless items. A singleton is the correct
    outcome for a name we cannot read; merging six songs is not.
    """
    from . import config as _config
    if not artist_key or not title_key:
        return None
    if artist_key == title_key:
        return None
    toks = sorted(set((artist_key + " " + title_key).split()))
    if len(toks) < _config.CLUSTER_NAME_MIN_TOKENS:
        return None
    return tuple(toks)


# --- corpus -------------------------------------------------------------------------------


@dataclass
class Corpus:
    ids: list          # item ids WITH a usable fingerprint+duration, one per row below
    durs: list         # duration_sec (float, never None here)
    fps: dict          # item_id -> full decoded numpy uint32 array
    keys: dict         # item_id -> (artist_key, title_key) for items with both
    sigs: dict = field(default_factory=dict)   # item_id -> §7.3(b) name signature (token set)
    inst: dict = field(default_factory=dict)   # item_id -> is_instrumental yes/no/unknown/None
    # EVERY active item, fingerprint or not, name or not. `ids` is a subset. Clustering is
    # universal now — a song with one nameless, unfingerprinted copy is still a song — so the
    # assignment pass iterates this, and only the similarity passes iterate `ids`.
    all_ids: list = field(default_factory=list)
    no_fp_items: int = 0
    no_dur_items: int = 0

    def named(self) -> set:
        """Items that have a §7.3(b) key. Its complement drives the no-name fallback."""
        return set(self.sigs)


def load_corpus(conn) -> Corpus:
    """Active items with their audio-carrying fingerprint and v_metadata (artist, title).

    The name comes from `v_metadata`, i.e. the BEST AVAILABLE value: the MusicBrainz/Spotify
    canonical form where enrichment resolved it, the filename parse otherwise. Deliberately
    not gated on having a song_mbid — see the module docstring.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.duration_sec, i.is_instrumental, fp.chromaprint, fp.fp_duration_sec,
               (SELECT value FROM v_metadata m
                 WHERE m.media_item_id = i.id AND m.field = 'artist') AS artist,
               (SELECT value FROM v_metadata m
                 WHERE m.media_item_id = i.id AND m.field = 'title')  AS title
        FROM media_items i
        LEFT JOIN media_item_files f
               ON f.media_item_id = i.id AND f.role IN ('audio', 'av')
        LEFT JOIN fingerprints fp ON fp.content_hash = f.content_hash
        WHERE i.status = 'active'
        ORDER BY i.id
        """
    ).fetchall()
    corpus = Corpus(ids=[], durs=[], fps={}, keys={})
    for r in rows:
        ak, tk = norm_key(r["artist"]), norm_key(r["title"])
        corpus.all_ids.append(r["id"])
        corpus.inst[r["id"]] = r["is_instrumental"]
        if ak and tk:
            corpus.keys[r["id"]] = (ak, tk)
            sig = name_signature(ak, tk)
            if sig is not None:
                corpus.sigs[r["id"]] = sig
        dur = r["duration_sec"] if r["duration_sec"] is not None else r["fp_duration_sec"]
        if r["chromaprint"] is None:
            corpus.no_fp_items += 1
            continue
        if dur is None:
            corpus.no_dur_items += 1
            continue
        corpus.fps[r["id"]] = decode_fp(r["chromaprint"])
        corpus.ids.append(r["id"])
        corpus.durs.append(float(dur))
    return corpus


# --- (a) coarse batched pass ---------------------------------------------------------------


def coarse_candidates(corpus: Corpus, *, floor: float | None = None,
                      words: int | None = None, block_sec: float | None = None,
                      progress_every: int = 5000, log=print) -> list[tuple[int, int, float]]:
    """Duration-blocked pairwise coarse similarity; returns (item_a, item_b, coarse_sim)
    for pairs scoring ≥ floor at offset 0 over the first `words` uint32 words.

    Zero-padded matrix + per-item cumulative popcounts make the truncation exact:
    popcount over the overlap prefix m = popcount over all `words` minus each side's
    bits beyond m (where the other side is zero padding).
    """
    import numpy as np
    from . import config as _config
    floor = _config.CLUSTER_COARSE_FLOOR if floor is None else floor
    words = _config.CLUSTER_COARSE_WORDS if words is None else words
    block = _config.CLUSTER_DURATION_BLOCK_SEC if block_sec is None else block_sec

    n = len(corpus.ids)
    if n < 2:
        return []
    order = sorted(range(n), key=lambda i: corpus.durs[i])
    ids = [corpus.ids[i] for i in order]
    durs = np.array([corpus.durs[i] for i in order])

    mat = np.zeros((n, words), dtype="<u4")
    lens = np.empty(n, dtype=np.int32)
    for row, item_id in enumerate(ids):
        fp = corpus.fps[item_id][:words]
        mat[row, : len(fp)] = fp
        lens[row] = len(fp)

    lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    bytes_view = mat.view(np.uint8).reshape(n, words * 4)
    # cum[row, k] = popcount of the row's first k words; cum[row, words] is the row total
    cum = np.zeros((n, words + 1), dtype=np.int32)
    wordpop = lut[bytes_view].reshape(n, words, 4).sum(axis=2, dtype=np.int32)
    np.cumsum(wordpop, axis=1, out=cum[:, 1:])

    out: list[tuple[int, int, float]] = []
    t0 = time.time()
    for i in range(n - 1):
        j_end = int(np.searchsorted(durs, durs[i] + block, side="right"))
        if j_end <= i + 1:
            continue
        js = np.arange(i + 1, j_end)
        m = np.minimum(lens[i], lens[js])
        ok = m >= MIN_OVERLAP_WORDS
        if not ok.any():
            continue
        js, m = js[ok], m[ok]
        xor = np.bitwise_xor(mat[i], mat[js]).view(np.uint8)
        pop_all = lut[xor].sum(axis=1, dtype=np.int32)
        pop_m = pop_all - (cum[i, words] - cum[i, m]) - (cum[js, words] - cum[js, m])
        sim = 1.0 - pop_m / (32.0 * m)
        hits = np.nonzero(sim >= floor)[0]
        for h in hits:
            a, b = ids[i], ids[int(js[h])]
            out.append((min(a, b), max(a, b), float(sim[h])))
        if progress_every and (i + 1) % progress_every == 0:
            log(f"  coarse [{i + 1}/{n}] candidates={len(out)}  {time.time() - t0:.0f}s")
    return out


# --- (b)/(c) title groups ------------------------------------------------------------------


def title_groups(corpus: Corpus) -> tuple[dict[tuple, list[int]], dict[tuple, list[int]],
                                           list[tuple]]:
    """name signature -> sorted item ids, multi-member only, split into three buckets:

      * `mergeable`  — ≤ CLUSTER_NAME_GROUP_MAX_MERGE members: a plausible set of copies of
                       one song; these edges carry merge weight;
      * `edge_only`  — bigger than that but ≤ CLUSTER_NAME_GROUP_MAX_EDGES: the key is more
                       likely a junk template value than 13 copies of one song, so the pairs
                       are persisted as evidence and left for §11/review, not merged;
      * `skipped`    — beyond that: n² pairs of a key we already disbelieve. Reported.
    """
    from . import config as _config
    groups: dict[tuple, list[int]] = {}
    for item_id, sig in corpus.sigs.items():
        groups.setdefault(sig, []).append(item_id)
    merge_max = _config.CLUSTER_NAME_GROUP_MAX_MERGE
    edge_max = _config.CLUSTER_NAME_GROUP_MAX_EDGES
    mergeable, edge_only, skipped = {}, {}, []
    for k, v in groups.items():
        if len(v) < 2:
            continue
        if len(v) <= merge_max:
            mergeable[k] = sorted(v)
        elif len(v) <= edge_max:
            edge_only[k] = sorted(v)
        else:
            skipped.append((k, len(v)))
    return mergeable, edge_only, skipped


def _extra_tokens_are_artist_side(a_keys, b_keys) -> bool:
    """§7.3(b) containment guard: A ⊂ B is admissible only when B's EXTRA tokens sit in the
    ARTIST field, never the title.

    This is the rule stage5 already pins for enrichment -- "title containment is not
    agreement: 'Crazy' vs 'Crazy in Love' must not match; containment is allowed for the
    ARTIST field only" -- applied to the identity key, where it had been missing.
    `name_signature` combines artist+title into ONE token set, so raw set containment cannot
    tell a featured-artist superset from a longer title by the same artist. Both look
    identical to `a_set < set(b_sig)`.

    Measured live 2026-08-24: item 5914 "Shania Twain / Don't" is a proper subset of the same
    artist's "Don't Be Stupid" and "That Don't Impress Me Much", and containment merged four
    distinct Shania Twain songs into one cluster. No degenerate parse involved -- just a short
    title contained in longer ones.

    TWO ALIGNMENTS ARE TRIED, not four. The artist/title boundary is a parse artifact here
    (§6.1 defers the Hebrew order question), so B may be stored in the opposite order to A --
    but each item's own two fields are still a PAIR, and re-reading them independently is what
    lets a wrong answer through. Reassigning A's roles on their own turns "Shania Twain /
    Don't" into artist='Don't', title='Shania Twain', which then reads as an artist superset of
    "Don't Be Stupid" -- exactly the merge this guard exists to refuse. (That is not
    hypothetical: the first cut of this function tried all four and the regression test caught
    it.) So the comparison is either ALIGNED (A.title vs B.title) or CROSSED (A.title vs
    B.artist), and never a mix.

    "Pink" ⊂ "Pink & Nate Ruess" on an identical title still merges, and an order-swapped
    superset still merges, while a same-artist longer title does not.
    """
    aa, at = set(a_keys[0].split()), set(a_keys[1].split())
    ba, bt = set(b_keys[0].split()), set(b_keys[1].split())
    return (at == bt and aa < ba) or (at == ba and aa < bt)


def containment_pairs(corpus: Corpus) -> tuple[list[tuple[int, int, float]], int]:
    """§7.3(b) containment: item A's token set a PROPER SUBSET of item B's.

    Catches what strict equality cannot: featured-artist supersets, an appended
    transliteration, a dropped middle initial, a parenthetical the other side kept. Returns
    (a, b, strength) with strength = |A|/|B| (= Jaccard, since A ⊂ B), plus the number of
    items whose candidate generation was capped.

    Candidates come from an inverted token index, probed on the RAREST token of A — every
    superset of A must contain that token, so this is exact, not heuristic, as long as the
    posting list is short enough to scan. Items whose rarest token is still library-wide
    common (CLUSTER_NAME_CONTAINMENT_MAX_POSTINGS) are skipped and counted rather than
    silently costing O(n²): a name built entirely from common words is also the name most
    likely to produce a bogus subset match.
    """
    from . import config as _config
    if not _config.CLUSTER_NAME_CONTAINMENT:
        return [], 0
    min_tokens = _config.CLUSTER_NAME_CONTAINMENT_MIN_TOKENS
    ratio = _config.CLUSTER_NAME_CONTAINMENT_RATIO
    max_postings = _config.CLUSTER_NAME_CONTAINMENT_MAX_POSTINGS

    postings: dict[str, list[int]] = {}
    for item_id, sig in corpus.sigs.items():
        for tok in sig:
            postings.setdefault(tok, []).append(item_id)

    out: list[tuple[int, int, float]] = []
    capped = 0
    for item_id in sorted(corpus.sigs):
        sig = corpus.sigs[item_id]
        if len(sig) < min_tokens:
            continue
        rarest = min(sig, key=lambda t: len(postings[t]))
        cands = postings[rarest]
        if len(cands) > max_postings:
            capped += 1
            continue
        a_set = set(sig)
        for other in cands:
            if other == item_id:
                continue
            b_sig = corpus.sigs[other]
            if len(b_sig) <= len(sig):
                continue          # equality is the exact-group case; ⊃ is seen from the other side
            if len(sig) / len(b_sig) < ratio:
                continue
            if a_set < set(b_sig):
                # The extra tokens must be an ARTIST superset, not a longer title.
                if not _extra_tokens_are_artist_side(
                        corpus.keys[item_id], corpus.keys[other]):
                    continue
                a, b = min(item_id, other), max(item_id, other)
                out.append((a, b, round(len(sig) / len(b_sig), 4)))
    return out, capped


def operator_split_pairs(conn) -> set[tuple[int, int]]:
    """§10 NEGATIVE identity facts: pairs a reviewer explicitly called `different`.

    The `duplicates` review tab records both directions of the judgement — a `same` decision
    becomes a manual edge (a positive fact, re-applied every run), while a `different`
    decision has until now had nowhere to go, because nothing ever merged those pairs anyway.
    Name identity changes that: two items called "Dup - Song" now merge on their names, and
    without this they would merge straight back over an operator who has already looked at
    both and said no. A reviewed pair is stronger evidence than any rule in this module.

    Pair-level, not transitive: if a–b and b–c both merge on names while a–c was called
    different, the component still forms. Recording that as a hard three-way split needs a
    negative-edge type in the schema; it is deliberately out of scope here, and the honest
    consequence is that an operator's `different` blocks the direct merge only.
    """
    out: set[tuple[int, int]] = set()
    for r in conn.execute(
            "SELECT media_item_id, resolution FROM review_queue "
            "WHERE kind='dedup_verdict' AND resolution IS NOT NULL AND media_item_id IS NOT NULL"):
        try:
            res = json.loads(r["resolution"]) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        for v in res.get("verdicts") or []:
            other = v.get("other")
            if v.get("verdict") == "different" and isinstance(other, int):
                out.add((min(r["media_item_id"], other), max(r["media_item_id"], other)))
    return out


def instrumental_conflict(corpus: Corpus, a: int, b: int) -> bool:
    """§7.3 name-merge gate: block ONLY on an explicit yes/no disagreement.

    This is the rule the old "title-only equality never auto-merges" prohibition was really
    reaching for — keep a full-vocal original from being merged into its karaoke cover.
    `is_instrumental` states that directly; a fingerprint cannot.

    'unknown'/NULL never blocks: measured on the live DB 2026-07-31, active items are 15,077
    'yes', 13,797 'unknown', 33 NULL — and **'no' does not occur anywhere in the index**,
    neither on an item nor on a location parse. Two consequences worth stating plainly:

      * the gate can only ever BLOCK, never require. It is a guard for a case this data does
        not contain yet, so today it fires zero times. That is not a reason to delete it —
        it is the correct shape for the rule, and it costs one set comparison.
      * a gate demanding positive AGREEMENT would be a disaster here: it would refuse ~48% of
        merges purely because we never determined whether the copy has a guide vocal, which
        is also why §7.4 ranks copies instead of archiving them (13,797 'unknown' means we
        cannot tell two karaoke productions apart, so we must keep both).

    Set membership, not truthiness — the 33 NULLs land in the set as `None` and simply never
    make it equal to {'yes','no'}.
    """
    return {corpus.inst.get(a), corpus.inst.get(b)} == {"yes", "no"}


def prefix_similarity(fp_a, fp_b, dur_a: float, dur_b: float,
                      fp_window_sec: float = 120.0) -> float | None:
    """§7.3(c): compare the first min(dur)−10s of both fingerprints. None if too short."""
    short = min(dur_a, dur_b) - 10.0
    if short <= 0:
        return None
    w = None
    for fp, dur in ((fp_a, dur_a), (fp_b, dur_b)):
        covered = min(dur, fp_window_sec)
        if covered <= 0:
            return None
        n = int(len(fp) * min(short / covered, 1.0))
        w = n if w is None else min(w, n)
    if w is None or w < MIN_OVERLAP_WORDS:
        return None
    return fp_similarity(fp_a[:w], fp_b[:w])


# --- union-find + sync ---------------------------------------------------------------------


class DSU:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class ClusterReport:
    items_with_fp: int = 0
    items_no_fp: int = 0
    coarse_pairs: int = 0
    refined_pairs: int = 0
    refined_dropped: int = 0            # full-length sim < CLUSTER_EDGE_FLOOR: not persisted
    refined_histogram: dict = field(default_factory=dict)   # 0.05 bins of refined (a)-sims
    title_groups: int = 0
    title_pairs: int = 0
    oversize_groups_skipped: list = field(default_factory=list)
    # §7.3(b) name identity
    name_groups_edge_only: int = 0        # too big to merge, still persisted as evidence
    name_containment_pairs: int = 0
    name_containment_capped: int = 0      # items whose rarest token was too common to probe
    name_merge_pairs: int = 0             # name pairs that carried merge weight
    name_merge_blocked_instrumental: int = 0
    name_merge_blocked_operator: int = 0   # §10 reviewer already called the pair 'different'
    # -- no-name fallback (the ONLY grouping audio still does) --
    items_without_name: int = 0           # no usable artist+title => no §7.3(b) key at all
    noname_audio_merges: int = 0          # nameless<->nameless strong-audio unions
    noname_attached: int = 0              # nameless components adopted by exactly one song
    noname_ambiguous: int = 0             # strong audio into >1 song: left standalone, reported
    noname_standalone: int = 0            # nameless and no strong audio anywhere: own song
    # -- audio evidence that did NOT merge (§7.4 turns this into review rows) --
    cross_song_audio_pairs: int = 0       # strong audio edges spanning two different songs
    cross_song_pairs_by_cluster: int = 0  # ...collapsed to distinct cluster pairs
    clusters_by_method: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)          # edge_type -> count
    band_below_review: int = 0    # 0.60–0.65: persisted, no action
    band_review: int = 0          # 0.65–0.85: candidate edges (§7.4 surfaces them)
    band_auto_merge: int = 0      # ≥0.85
    clusters: int = 0                     # == songs. Every active item is in exactly one.
    clustered_items: int = 0
    singleton_clusters: int = 0           # songs with exactly one copy
    multi_item_clusters: int = 0
    largest_cluster: int = 0
    cluster_size_histogram: dict = field(default_factory=dict)
    songs_multi_name: int = 0             # songs holding >1 distinct raw artist+title string
    names_split_across_songs: int = 0     # one raw name appearing in >1 song
    truncation_suspects: int = 0
    db_edges_added: int = 0
    db_edges_updated: int = 0
    db_edges_removed: int = 0
    db_clusters_created: int = 0
    db_clusters_kept: int = 0
    db_clusters_deleted: int = 0
    db_items_reassigned: int = 0
    dry_run: bool = False
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def compute_edges(conn, *, log=print) -> tuple[Corpus, dict, set, ClusterReport]:
    """Pure computation. Returns (corpus, edges, name_merges, report), where `edges` is
    {(a, b, edge_type): similarity_or_None} and `name_merges` is the subset of title_match
    pairs that carry merge weight (small enough group, no is_instrumental conflict)."""
    from . import config as _config
    report = ClusterReport()
    t0 = time.time()
    corpus = load_corpus(conn)
    report.items_with_fp = len(corpus.ids)
    report.items_no_fp = corpus.no_fp_items
    log(f"corpus: {len(corpus.ids)} items with fp, {corpus.no_fp_items} without, "
        f"{len(corpus.keys)} with (artist,title)")

    edges: dict[tuple, float | None] = {}

    coarse = coarse_candidates(corpus, log=log)
    report.coarse_pairs = len(coarse)
    log(f"coarse candidates: {len(coarse)}")
    edge_floor = _config.CLUSTER_EDGE_FLOOR
    for a, b, _c in coarse:
        sim = fp_similarity(corpus.fps[a], corpus.fps[b])
        bin_key = f"{int(sim * 20) / 20:.2f}"
        report.refined_histogram[bin_key] = report.refined_histogram.get(bin_key, 0) + 1
        if sim < edge_floor:
            report.refined_dropped += 1
            continue
        edges[(a, b, "fingerprint")] = sim
    report.refined_pairs = len(coarse)

    # -- §7.3(b) name identity: exact-signature groups + containment ------------------------
    groups, edge_only, oversize = title_groups(corpus)
    report.title_groups = len(groups)
    report.name_groups_edge_only = len(edge_only)
    report.oversize_groups_skipped = [
        {"key": " ".join(k)[:120], "members": n} for k, n in oversize
    ]
    # (a, b) -> name-evidence strength; `mergeable` marks the ones allowed to union.
    name_strength: dict[tuple[int, int], float] = {}
    mergeable: set[tuple[int, int]] = set()
    for members in groups.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                pair = (members[x], members[y])
                name_strength[pair] = 1.0
                mergeable.add(pair)
    for members in edge_only.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                name_strength.setdefault((members[x], members[y]), 1.0)

    contained, capped = containment_pairs(corpus)
    report.name_containment_capped = capped
    for a, b, strength in contained:
        if strength > name_strength.get((a, b), 0.0):
            name_strength[(a, b)] = strength
        if (a, b) not in mergeable and _config.CLUSTER_NAME_CONTAINMENT:
            report.name_containment_pairs += 1
            mergeable.add((a, b))

    if not _config.CLUSTER_NAME_MERGE:
        mergeable.clear()   # §11 escape hatch: keep the evidence, drop the merge weight
    name_merges: set[tuple[int, int]] = set()
    for pair in sorted(mergeable):
        if instrumental_conflict(corpus, *pair):
            report.name_merge_blocked_instrumental += 1
            continue
        name_merges.add(pair)
    # §10 operator "different" verdicts are applied at UNION time (cluster_all), not here:
    # they constrain components, not pairs, and the component a pair belongs to is only known
    # once the audio and manual merges have been applied.
    report.name_merge_pairs = len(name_merges)

    # (c) prefix / same-block fingerprint comparison for every name pair, merged or not. It no
    # longer decides anything about clustering; it is kept because it is the only measurement
    # that tells a TRUNCATED copy (same audio, less of it => §7.4.4 must not rank it first)
    # from two genuinely different karaoke productions of one song (rank both, archive
    # neither). Persisted either way, so §11 can retune the distinction without recomputing.
    dur_of = dict(zip(corpus.ids, corpus.durs))
    block = _config.CLUSTER_DURATION_BLOCK_SEC
    for (a, b), strength in sorted(name_strength.items()):
        report.title_pairs += 1
        edges[(a, b, "title_match")] = strength
        fa, fb = corpus.fps.get(a), corpus.fps.get(b)
        if fa is None or fb is None:
            continue
        if abs(dur_of[a] - dur_of[b]) <= block:
            if (a, b, "fingerprint") not in edges:
                edges[(a, b, "fingerprint")] = fp_similarity(fa, fb)
        else:
            sim = prefix_similarity(fa, fb, dur_of[a], dur_of[b])
            if sim is not None:
                edges[(a, b, "prefix_fingerprint")] = sim

    auto = _config.CLUSTER_AUTO_MERGE
    review = _config.CLUSTER_CANDIDATE
    for (a, b, etype), sim in edges.items():
        report.edges[etype] = report.edges.get(etype, 0) + 1
        if etype == "title_match" or sim is None:
            continue
        if sim >= auto:
            report.band_auto_merge += 1
        elif sim >= review:
            report.band_review += 1
        else:
            report.band_below_review += 1
    report.elapsed_sec = time.time() - t0
    return corpus, edges, name_merges, report


# §3.5: the cluster methods §7.3 OWNS and fully reconciles. 'exact_hash' clusters (if anything
# ever creates them) are somebody else's rows and are never touched here — measured on the
# live DB there are none, so this is a guard, not a workaround.
#
# NULL is now an owned value and means SINGLETON: a song with one copy, held together by
# nothing because there is nothing to hold. It is a real answer, in the same way
# location_parses.layout='opaque' is, and it is what universal cluster assignment made
# necessary — 'title_match' on a cluster where no title ever matched would be a lie.
DERIVED_CLUSTER_METHODS = ("fingerprint", "title_match", "manual")
OWNED_METHODS_SQL = (
    "(method IN ('fingerprint','title_match','manual') OR method IS NULL)"
)


def _cluster_method(audio_sims: list | None, name_sims: list | None,
                    manual_sims: list | None) -> str | None:
    """§3.5 cluster method — WHAT MADE THIS A SONG.

    The precedence inverted with the name-only reframe, and the inversion is the point. Under
    the arrangement model 'fingerprint' was the headline because audio identity was the
    stronger claim. Audio no longer makes identity claims at all, so:

      'title_match'  the names agree — the only ordinary way a song acquires a second copy.
      'manual'       no name evidence; an operator said "same song" (§10). The one non-name
                     merge, because it is a human talking about the song, not about audio.
      'fingerprint'  no name evidence and no operator: the no-name fallback grouped items
                     that have no key at all by their audio.
      None           singleton — one copy, nothing merged, nothing to explain.

    The full composition still goes in `notes`, so a song assembled from more than one kind of
    evidence stays auditable without inventing a schema value the CHECK constraint rejects.
    """
    if name_sims:
        return "title_match"
    if manual_sims:
        return "manual"
    if audio_sims:
        return "fingerprint"
    return None


def edges_sim(edges: dict, a: int, b: int) -> float:
    """The audio similarity recorded for a pair, whichever audio edge type carries it."""
    sim = edges.get((a, b, "fingerprint"))
    if sim is None:
        sim = edges.get((a, b, "prefix_fingerprint"))
    return 1.0 if sim is None else sim


def _noname_fallback(dsu: DSU, strong_audio: list, named: set, report: ClusterReport) -> None:
    """§7.3(b-fallback): group items that have NO usable name, by audio.

    626 locations parse to layout='opaque' and more items simply have no artist or no title,
    so `name_signature` returns None for them. Under a name-only model those items have no key
    whatsoever — every one of them would be its own song, including the ones that are provably
    the same audio as an item we CAN name. This is the one place audio still creates grouping.

    Two steps, and the second is deliberately conservative:

      1. nameless <-> nameless strong audio edges union freely. Neither side has a name to
         contradict, so there is no coherence to lose.
      2. a nameless component adopts the name of a named song only when its strong audio edges
         point at EXACTLY ONE named component. Pointing at two would mean merging two songs
         through a nameless bridge — precisely the failure this whole reframe exists to remove
         — so an ambiguous component is left standalone and counted in `noname_ambiguous`.

    Step 2 reads `dsu.find` on the NAMED side after step 1, and applies its unions afterwards
    from a snapshot, so the targets it decided on cannot be perturbed mid-loop. Sorted
    iteration keeps it deterministic.
    """
    from . import config as _config
    if not _config.CLUSTER_NONAME_FALLBACK:
        report.noname_standalone = report.items_without_name
        return

    for a, b in strong_audio:
        if a not in named and b not in named:
            if dsu.find(a) != dsu.find(b):
                report.noname_audio_merges += 1
            dsu.union(a, b)

    targets: dict[int, set[int]] = {}
    for a, b in strong_audio:
        for x, y in ((a, b), (b, a)):
            if x not in named and y in named:
                targets.setdefault(dsu.find(x), set()).add(dsu.find(y))

    # Count BEFORE applying the unions. Afterwards `dsu.find` on an attached component returns
    # the NAMED root, which is not one of `targets`' keys, so every attached component would
    # be miscounted as standalone.
    noname_roots = {dsu.find(i) for i in dsu.parent if i not in named}
    report.noname_standalone = len(noname_roots - set(targets))

    for root, roots in sorted(targets.items()):
        if len(roots) == 1:
            dsu.union(root, next(iter(roots)))
            report.noname_attached += 1
        else:
            report.noname_ambiguous += 1


def _name_coherence(conn, components: dict, report: ClusterReport) -> None:
    """The two numbers that say whether "cluster == song" actually holds.

    `songs_multi_name`   — songs holding more than one distinct raw artist+title string. This
                           is the metric the reframe exists to drive down (2,424 under the
                           previous model). It will not reach zero and should not: an
                           order-insensitive, containment-aware key deliberately unites
                           "Pink" with "Pink & Nate Ruess", which ARE two distinct strings.
                           Reported so ambiguity stays visible rather than being collapsed
                           behind one representative — `v_songs.distinct_names` exposes the
                           same fact per row.
    `names_split_across_songs` — the reverse error: one raw name appearing in more than one
                           song. Was 41 under the previous model, on a song=cluster-else-own-id
                           reading. Kept honest with the same crude lower+trim key the operator
                           measured with, so the numbers are comparable run to run.
    """
    names = {
        r["id"]: r["k"] for r in conn.execute(
            """
            SELECT i.id AS id,
                   lower(trim(COALESCE(MAX(CASE WHEN v.field='artist' THEN v.value END),'')))
                   || ' | ' ||
                   lower(trim(COALESCE(MAX(CASE WHEN v.field='title'  THEN v.value END),''))) AS k
            FROM media_items i LEFT JOIN v_metadata v ON v.media_item_id = i.id
            WHERE i.status='active' GROUP BY i.id
            """)
    }
    per_name: dict[str, set[int]] = {}
    for root, members in components.items():
        distinct = {names.get(m, " | ") for m in members} - {" | "}
        if len(distinct) > 1:
            report.songs_multi_name += 1
        for k in distinct:
            per_name.setdefault(k, set()).add(root)
    report.names_split_across_songs = sum(1 for roots in per_name.values() if len(roots) > 1)


def cluster_all(conn, *, dry_run: bool = False, log=print) -> ClusterReport:
    """§7.3 end-to-end: compute edges, union-find on NAMES, sync DB. Idempotent —
    identical inputs produce zero db_* changes on the second run."""
    from . import config as _config
    t0 = time.time()
    corpus, edges, name_merges, report = compute_edges(conn, log=log)
    report.dry_run = dry_run
    auto = _config.CLUSTER_AUTO_MERGE

    dsu = DSU()
    merge_sims: dict[int, list[float]] = {}      # no-name audio evidence, per component root
    name_sims: dict[int, list[float]] = {}       # name evidence, per component root
    manual_sims: dict[int, list[float]] = {}     # §10 operator edges, per component root
    # Seed the DSU with EVERY active item. A song with one nameless copy is still a song, and
    # seeding here is what makes cluster_id universally non-NULL without a special case later.
    for item_id in corpus.all_ids:
        dsu.find(item_id)

    # Truncation is a measurement about ONE FILE — "the same audio as its sibling, but less of
    # it" — so it is read off every strong prefix_fingerprint edge, merged or not. Under the
    # old model only merging edges could mark it, which tied a fact about a file to a decision
    # about a pair. §7.4.4 uses it to keep a truncated copy from being ranked the default.
    truncation: set[int] = set()
    dur_of = dict(zip(corpus.ids, corpus.durs))
    strong_audio: list[tuple[int, int]] = []
    for (a, b, etype), sim in sorted(edges.items()):
        if etype == "title_match" or sim is None or sim < auto:
            continue
        strong_audio.append((a, b))
        if etype == "prefix_fingerprint":
            truncation.add(a if dur_of[a] < dur_of[b] else b)

    # -- 1. NAME identity: the sole cluster-forming key ------------------------------------
    # Under the operator's cannot-link constraints. A reviewer's `different` verdict separates
    # COMPONENTS, not just the two rows they were shown: reviewing a against c and calling them
    # different has to keep c out even when b, already merged with a, shares c's name. Greedy
    # over sorted pairs, so it is deterministic; a name pair that would join two components a
    # human separated is dropped, not deferred.
    forbidden = operator_split_pairs(conn)
    applied_name_merges: set[tuple[int, int]] = set()
    for a, b in sorted(name_merges):
        ra, rb = dsu.find(a), dsu.find(b)
        if ra == rb:
            applied_name_merges.add((a, b))
            continue
        blocked = any({dsu.find(x), dsu.find(y)} == {ra, rb} for x, y in forbidden)
        if blocked:
            report.name_merge_blocked_operator += 1
            continue
        dsu.union(a, b)
        applied_name_merges.add((a, b))
    name_merges = applied_name_merges
    report.name_merge_pairs = len(name_merges)

    # -- 2. §10 manual edges: the one non-name merge ---------------------------------------
    # An operator duplicate verdict ("these two items are the same song"). Not in `edges` (that
    # is pure fingerprint computation), so read from the DB and unioned unconditionally.
    # Re-applied every run, exactly resolve_pair_mismatch's discipline: the durable fact is the
    # edge, the merge is re-derived from it. This CAN join two named songs — deliberately, and
    # only here, because a human is asserting something about the song and not about the audio.
    manual_edges = list(conn.execute(
        "SELECT e.item_a, e.item_b, e.similarity FROM cluster_edges e "
        "JOIN media_items a ON a.id = e.item_a AND a.status='active' "
        "JOIN media_items b ON b.id = e.item_b AND b.status='active' "
        "WHERE e.edge_type='manual' ORDER BY e.item_a, e.item_b"))
    for e in manual_edges:
        dsu.union(e["item_a"], e["item_b"])

    # -- 3. no-name fallback: the only grouping audio still does ---------------------------
    named = corpus.named()
    report.items_without_name = sum(1 for i in corpus.all_ids if i not in named)
    _noname_fallback(dsu, strong_audio, named, report)

    # Audio evidence that spans two DIFFERENT songs after all merging. It never merges (that
    # is the whole reframe) — §7.4 turns it into `possible_song_merge` review rows so a human
    # can look. Counted here so `cluster`'s own report projects the review burden before
    # `verdicts` runs.
    cross_pairs: set[tuple[int, int]] = set()
    for a, b in strong_audio:
        ra, rb = dsu.find(a), dsu.find(b)
        if ra != rb:
            report.cross_song_audio_pairs += 1
            cross_pairs.add((min(ra, rb), max(ra, rb)))
    report.cross_song_pairs_by_cluster = len(cross_pairs)

    # -- components: EVERY item, singletons included ---------------------------------------
    components: dict[int, list[int]] = {}
    for x in corpus.all_ids:
        components.setdefault(dsu.find(x), []).append(x)
    components = {r: sorted(m) for r, m in components.items()}
    for a, b in strong_audio:
        ra, rb = dsu.find(a), dsu.find(b)
        if ra == rb and (a not in named or b not in named):
            merge_sims.setdefault(ra, []).append(edges_sim(edges, a, b))
    for e in manual_edges:
        manual_sims.setdefault(dsu.find(e["item_a"]), []).append(
            e["similarity"] if e["similarity"] is not None else 1.0)
    for a, b in name_merges:
        name_sims.setdefault(dsu.find(a), []).append(edges.get((a, b, "title_match"), 1.0))

    report.clusters = len(components)
    report.clustered_items = sum(len(m) for m in components.values())
    sizes = [len(m) for m in components.values()]
    report.singleton_clusters = sum(1 for n in sizes if n == 1)
    report.multi_item_clusters = sum(1 for n in sizes if n > 1)
    report.largest_cluster = max(sizes, default=0)
    for n in sizes:
        bucket = str(n) if n <= 5 else ("6-10" if n <= 10 else ("11-20" if n <= 20 else "21+"))
        report.cluster_size_histogram[bucket] = report.cluster_size_histogram.get(bucket, 0) + 1
    _name_coherence(conn, components, report)
    report.truncation_suspects = len(truncation)
    for root in components:
        method = _cluster_method(merge_sims.get(root), name_sims.get(root),
                                 manual_sims.get(root))
        key = method or "singleton"
        report.clusters_by_method[key] = report.clusters_by_method.get(key, 0) + 1

    _sync_db(conn, components, merge_sims, name_sims, manual_sims, truncation, edges,
             report, dry_run)
    report.elapsed_sec = time.time() - t0
    return report


def _sync_db(conn, components, merge_sims, name_sims, manual_sims, truncation, edges,
             report, dry_run):
    """Diff computed state against the DB. All §7.3-owned rows (edge types fingerprint /
    prefix_fingerprint / title_match, clusters whose method is owned per OWNED_METHODS_SQL,
    cluster_id assignments) are derived data and fully reconciled; 'exact_hash' clusters are
    never touched."""
    # -- edges --
    want = {(a, b, t): (None if s is None else round(s, 4))
            for (a, b, t), s in edges.items()}
    have = {
        (r["item_a"], r["item_b"], r["edge_type"]): r["similarity"]
        for r in conn.execute(
            "SELECT item_a, item_b, edge_type, similarity FROM cluster_edges "
            "WHERE edge_type IN ('fingerprint','prefix_fingerprint','title_match')"
        )
    }
    for k, sim in want.items():
        if k not in have:
            report.db_edges_added += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO cluster_edges (item_a, item_b, edge_type, similarity) "
                    "VALUES (?,?,?,?)", (*k, sim))
        elif have[k] != sim:
            report.db_edges_updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE cluster_edges SET similarity=? "
                    "WHERE item_a=? AND item_b=? AND edge_type=?", (sim, *k))
    for k in have:
        if k not in want:
            report.db_edges_removed += 1
            if not dry_run:
                conn.execute(
                    "DELETE FROM cluster_edges "
                    "WHERE item_a=? AND item_b=? AND edge_type=?", k)

    # -- clusters + assignments --
    current_cluster: dict[int, int | None] = {
        r["id"]: r["cluster_id"]
        for r in conn.execute("SELECT id, cluster_id FROM media_items WHERE status='active'")
    }
    fp_clusters = {
        r["id"]: r for r in conn.execute(
            f"SELECT id, method, confidence, notes FROM clusters WHERE {OWNED_METHODS_SQL}")
    }
    members_of: dict[int, list[int]] = {}
    for item_id, cid in current_cluster.items():
        if cid is not None:
            members_of.setdefault(cid, []).append(item_id)

    assigned: dict[int, int] = {}   # item -> cluster id (existing or negative placeholder)
    used_clusters: set[int] = set()
    next_placeholder = -1
    for root, members in sorted(components.items()):
        sims = merge_sims.get(root, [])
        nsims = name_sims.get(root, [])
        msims = manual_sims.get(root, [])
        method = _cluster_method(sims, nsims, msims)
        # §3.5 confidence is the WEAKEST link that holds the component together, on the scale
        # named by confidence_basis — name-evidence strength for a name-formed song, audio
        # similarity for a no-name fallback group. The two are not comparable and are
        # deliberately never mixed into one number. A singleton has no link and no basis:
        # confidence NULL, basis 'none'. Reading `confidence` without `confidence_basis`
        # remains meaningless.
        basis_sims = {"title_match": nsims, "manual": msims,
                      "fingerprint": sims}.get(method or "", [])
        confidence = round(min(basis_sims), 4) if basis_sims else None
        notes = json.dumps({
            "items": len(members), "merge_edges": len(sims) + len(nsims) + len(msims),
            "evidence": {"audio": len(sims), "name": len(nsims), "manual": len(msims)},
            "confidence_basis": {"title_match": "name_evidence",
                                 "manual": "operator",
                                 "fingerprint": "fingerprint_similarity"}.get(
                                     method or "", "none"),
        })
        existing = {current_cluster.get(m) for m in members}
        reuse = None
        if len(existing) == 1:
            (cid,) = existing
            if cid in fp_clusters and sorted(members_of.get(cid, [])) == members:
                reuse = cid
        if reuse is not None:
            used_clusters.add(reuse)
            report.db_clusters_kept += 1
            row = fp_clusters[reuse]
            if row["method"] != method or row["confidence"] != confidence \
                    or row["notes"] != notes:
                if not dry_run:
                    conn.execute(
                        "UPDATE clusters SET method=?, confidence=?, notes=? WHERE id=?",
                        (method, confidence, notes, reuse))
            for m in members:
                assigned[m] = reuse
        else:
            report.db_clusters_created += 1
            if dry_run:
                cid = next_placeholder
                next_placeholder -= 1
            else:
                cid = conn.execute(
                    "INSERT INTO clusters (method, confidence, notes) "
                    "VALUES (?, ?, ?)", (method, confidence, notes)).lastrowid
            used_clusters.add(cid)
            for m in members:
                assigned[m] = cid

    for item_id, cid in current_cluster.items():
        target = assigned.get(item_id)
        if target is None and cid is not None and cid not in fp_clusters:
            continue   # manual/exact_hash assignment: not ours to clear
        if target != cid:
            report.db_items_reassigned += 1
            if not dry_run:
                conn.execute(
                    "UPDATE media_items SET cluster_id=?, updated_at=datetime('now') "
                    "WHERE id=?", (target, item_id))

    for cid in fp_clusters:
        if cid not in used_clusters:
            referenced = conn.execute(
                "SELECT 1 FROM review_queue WHERE cluster_id=? "
                "UNION ALL SELECT 1 FROM media_items WHERE cluster_id=? LIMIT 1",
                (cid, cid)).fetchone()
            if referenced:
                continue   # a review row or non-active item still points here — keep it
            report.db_clusters_deleted += 1
            if not dry_run:
                conn.execute("DELETE FROM clusters WHERE id=?", (cid,))

    # -- truncation suspects (idempotent JSON merge) --
    for item_id in sorted(truncation):
        row = conn.execute(
            "SELECT quality_attrs FROM media_items WHERE id=?", (item_id,)).fetchone()
        try:
            attrs = json.loads(row["quality_attrs"]) if row and row["quality_attrs"] else {}
        except json.JSONDecodeError:
            attrs = {}
        if attrs.get("truncation_suspect"):
            continue
        attrs["truncation_suspect"] = True
        if not dry_run:
            conn.execute(
                "UPDATE media_items SET quality_attrs=?, updated_at=datetime('now') "
                "WHERE id=?", (json.dumps(attrs, ensure_ascii=False), item_id))

    if not dry_run:
        conn.commit()
