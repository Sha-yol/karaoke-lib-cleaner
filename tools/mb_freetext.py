#!/usr/bin/env python3
"""§9.1 extension: unqualified free-text MusicBrainz search for items with no song_mbid.

WHY THIS EXISTS
---------------
5,839 winner/sole_copy items with Latin-script metadata carry no `song_mbid`. They already
went through the §9.1 text search and missed. That search builds a FIELD-QUALIFIED Lucene
query (`stage5.mb_search_url` -> `recording:"..." AND artist:"..."`), which fails whenever our
artist/title split is wrong, or our strings carry noise the qualified fields won't tolerate.

MusicBrainz also accepts an UNQUALIFIED free-text query. That is a different question, and --
importantly -- a different `mb_cache` key, so it reaches the network rather than replaying the
cached miss. Re-running the qualified search over these items would be an expensive no-op;
this is not.

THE RANKER (measured, n=200 labeled items, one query each, re-ranked offline)
----------------------------------------------------------------------------
    recall ceiling (right answer anywhere in top-8)   90.5%
    artist-substring only                             87.0%   <- what this uses
    artist gate + title tiebreak                      86.5%
    artist + title, ADDITIVE                          78.5%   <- actively worse
    MB's own relevance score                          76.0%
    title-substring only                              67.0%

Two things that follow, and that the code below depends on:
  * MB's `score` collapses on free-text queries -- candidates routinely all return 100,
    wrong artists included. `config.MB_AUTO_ACCEPT_SCORE` (92) is meaningless here and is
    deliberately NOT used. Score is a tiebreak of last resort, nothing more.
  * Adding title to artist ADDITIVELY costs ~8.5 points, because title tokens dilute the
    artist constraint and let a wrong-artist candidate outrank a right-artist one on token
    count. Title is only ever a subordinate tiebreak below the artist gate.

THE ACCEPTANCE RULE, AND WHY IT IS SAFE
---------------------------------------
Gate candidates on artist-token overlap, then require the pick to agree with BOTH of our
existing fields at >= FIELD_FLOOR. We are not asking MusicBrainz what this song is -- we are
asking it for a canonical id for something we already believe. A candidate that would
REDIRECT us is by construction rejected, which is what kills the failure mode that free-text
otherwise invites (an unrelated artist matching on title words alone).

Probe of this exact population (n=60, 2026-07-31): 25.0% accepted (15/60), and all 15 were
correct on inspection -- several CORRECTING our text ("The Airbourne Toxic Even" ->
"Airborne Toxic Event"; "John Secada" -> "Jon Secada"). Of the 45 rejected: 41.7% failed the
artist gate outright (a Tony Hawk soundtrack compilation, a magazine, a parody act -- all
correctly refused) and 33.3% passed the gate but fell below the agreement floor.

Hebrew is excluded (`--script latn`): MB's coverage of Israeli artists is thin, and Hebrew
order ambiguity is Spotify's job (the since-retired spotify_order pass). See docs/history/HISTORY.md.

    tools/mb_freetext.py --dry-run          # plan only, writes nothing (§13)
    tools/mb_freetext.py --limit 200        # bounded live run
    tools/mb_freetext.py                    # the full pass; Ctrl-C-safe, resumable

Idempotent: an item that already has a `musicbrainz_freetext` row is not re-queried, so a
second back-to-back run makes zero changes (§13). Every response lands in `mb_cache`, so
re-tuning the floors never needs a refetch (§11).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from karaokemp import config, db, stage5  # noqa: E402

SOURCE = "musicbrainz_freetext"

# Both of our fields must agree with the candidate this well. Mirrors config.MB_FIELD_AGREEMENT
# (0.85) deliberately: this is the same "do we already believe this" question the §9.1 pass
# asks, so it should not answer it with a different number.
FIELD_FLOOR = config.MB_FIELD_AGREEMENT

# Decoration tokens are stripped from the QUERY (they poison Lucene scoring) and from the
# gate's token set. Kept in sync with stage1's decoration vocabulary by intent, not by import:
# this is a query-shaping list, not a parse rule.
DECOR = {
    "karaoke", "instrumental", "playback", "backing", "hd", "lyrics", "official",
    "video", "version", "cdg", "mp3", "mp4", "avi", "mpg", "track",
}

MB_SEARCH_LIMIT = 8


def toks(s: str | None) -> set[str]:
    """Lowercased alphanumeric tokens, singles dropped. Hebrew survives (it just won't be
    queried -- see --script)."""
    return {t for t in stage5.norm_name(s).split() if len(t) > 1}


def freetext_url(artist: str, title: str, *, limit: int = MB_SEARCH_LIMIT) -> str:
    """UNQUALIFIED query: no `recording:`/`artist:` prefixes. That is the whole point --
    it is a different question from stage5.mb_search_url, and a different cache key."""
    words = [w for w in f"{artist} {title}".split() if w.lower().strip("()[[]") not in DECOR]
    q = " ".join(words).strip()
    return (f"{config.MB_API_ROOT}/recording?query={urllib.parse.quote(q)}"
            f"&fmt=json&limit={limit}")


def parse_candidates(payload: dict) -> list[dict]:
    out = []
    for rec in payload.get("recordings", []) or []:
        credit = ", ".join(
            a["name"] for a in rec.get("artist-credit", []) or []
            if isinstance(a, dict) and "name" in a
        )
        out.append({
            "mbid": rec.get("id"),
            "score": rec.get("score") or 0,
            "artist": credit,
            "title": rec.get("title") or "",
            "year": (rec.get("first-release-date") or "")[:4] or None,
        })
    return out


def choose(our_artist: str, our_title: str, cands: list[dict]) -> tuple[dict | None, str]:
    """The ranker + the acceptance rule. Pure, so it is testable and retunable.

    Returns (accepted_candidate_or_None, reason)."""
    if not cands:
        return None, "no_results"
    ours = toks(our_artist) | (toks(our_title) - DECOR)
    gated = [c for c in cands if toks(c["artist"]) & ours]
    if not gated:
        return None, "no_artist_gate"
    # Artist gate already applied; title is a SUBORDINATE tiebreak, never additive with it.
    best = max(gated, key=lambda c: (len(toks(c["title"]) & toks(our_title)), c["score"]))
    a_sim = stage5.field_agreement(our_artist, best["artist"])
    t_sim = stage5.title_agreement(our_title, best["title"])
    if a_sim >= FIELD_FLOOR and t_sim >= FIELD_FLOOR and best["mbid"]:
        best = dict(best, artist_sim=a_sim, title_sim=t_sim)
        return best, "accept"
    return None, "below_floor"


"""--- TIER 2: order-free acceptance ------------------------------------------------------

Tier 1 (`choose`) compares FIELD TO FIELD: our artist vs their artist, our title vs their
title. That is what makes it unable to redirect an item -- and it is also why it rejects every
item whose artist/title are SWAPPED in our parse. Such an item passes the artist gate (which
tests the union of both our fields) and then dies at the field-wise comparison.

Tier 2 drops the field alignment but keeps the containment discipline: the candidate must be
EXPLAINABLE BY OUR TEXT as a bag of words -- its artist tokens and its title tokens must each
be covered by the union of our two fields. If it is, MusicBrainz has also told us WHICH of our
strings is the artist. That is the segmentation oracle, and it is the same move
the Spotify order pass made for Hebrew order ambiguity (§6.1).

The disjointness requirement is load-bearing, not a nicety. Without it, candidates whose TITLE
is simply the artist's name pass trivially -- the same tokens satisfy both coverage checks.
Measured on the 4,068 remaining items: coverage alone recovered 542, but among them
"All That Jazz | Catherine Zeta Jones" -> `Catherine Zeta-Jones | Catherine Zeta Jones`,
"The Boomtown Rats | I Don't Like Mondays" -> `The Boomtown Rats | The Boomtown Rats`, and a
Hebrew item matched to an unrelated Tuvan band. Requiring the title to be covered by tokens the
ARTIST did not already consume drops the yield to 415 and rejects all of those, while keeping
every good recovery (flips, `[<site>.com]` junk, `(In the style of ...)`, `01.Madonna`).

Writing a tier-2 accept therefore CHANGES OUR FIELD ORDER. §3.6's identity ladder permits this
explicitly -- an external catalogue may correct spelling AND order -- and it does not change
which song the item is; the candidate had to be explainable by our own text to get here.
"""

# Fraction of a candidate field's tokens that our combined text must contain.
ORDER_FREE_COVER = 0.80
# Tier-2 confidence is mapped into a band strictly BELOW tier-1's floor so the two tiers stay
# distinguishable by confidence alone (they share a source, and §11 retune must be able to
# tell them apart without re-querying).
ORDER_FREE_CONF_LO, ORDER_FREE_CONF_HI = 0.60, 0.84


def _cover(cand_tokens: set[str], ours: set[str]) -> float:
    return len(cand_tokens & ours) / len(cand_tokens) if cand_tokens else 0.0


def choose_order_free(our_artist: str, our_title: str,
                      cands: list[dict]) -> tuple[dict | None, str]:
    """Tier 2. Pure, so it is testable and retunable. Returns (candidate_or_None, reason)."""
    if not cands:
        return None, "no_results"
    ours = toks(our_artist) | (toks(our_title) - DECOR)
    if not ours:
        return None, "no_tokens"
    scored = []
    for c in cands:
        ca = toks(c["artist"])
        ct = toks(c["title"]) - DECOR
        if not ca or not ct or not c["mbid"]:
            continue
        # Self-titled degenerate case: artist and title explaining the SAME tokens.
        if ca <= ct or ct <= ca:
            continue
        # The title must be covered by what the artist did not already consume.
        rest = ours - ca
        if not rest or _cover(ct, rest) < ORDER_FREE_COVER:
            continue
        a_cov, t_cov = _cover(ca, ours), _cover(ct, ours)
        if a_cov >= ORDER_FREE_COVER and t_cov >= ORDER_FREE_COVER:
            scored.append((a_cov + t_cov, c, min(a_cov, t_cov)))
    if not scored:
        return None, "order_free_reject"
    _, best, worst_cov = max(scored, key=lambda x: (x[0], x[1]["score"]))
    span = ORDER_FREE_CONF_HI - ORDER_FREE_CONF_LO
    conf = ORDER_FREE_CONF_LO + span * ((worst_cov - ORDER_FREE_COVER) / (1.0 - ORDER_FREE_COVER))
    best = dict(best, artist_sim=worst_cov, title_sim=worst_cov,
                order_free_conf=round(min(max(conf, ORDER_FREE_CONF_LO), ORDER_FREE_CONF_HI), 4))
    return best, "accept_order_free"


def worklist(conn, *, script: str, limit: int | None, stride: int = 1) -> list[sqlite3.Row]:
    """Winners + sole copies (§9.1 runs on survivors), no song_mbid from ANY source, both
    fields present, requested script, and not already handled by this pass (idempotence)."""
    sql = """
      SELECT mi.id AS id,
             (SELECT value FROM v_metadata WHERE media_item_id=mi.id AND field='artist') AS artist,
             (SELECT value FROM v_metadata WHERE media_item_id=mi.id AND field='title')  AS title
      FROM media_items mi
      WHERE mi.quality_verdict IN ('winner','sole_copy')
        AND mi.status = 'active'
        AND NOT EXISTS (SELECT 1 FROM song_metadata s
                        WHERE s.media_item_id=mi.id AND s.field='song_mbid')
        AND NOT EXISTS (SELECT 1 FROM song_metadata s
                        WHERE s.media_item_id=mi.id AND s.source=?)
        AND COALESCE((SELECT value FROM song_metadata l WHERE l.media_item_id=mi.id
                      AND l.field='language' LIMIT 1), '?') = ?
      ORDER BY mi.id
    """
    params: list = [SOURCE, script]
    rows = [r for r in conn.execute(sql, params).fetchall() if r["artist"] and r["title"]]
    # --stride samples EVENLY across the id range. Plain `LIMIT n` is not a representative
    # sample here: bad source batches cluster by id (the lowest ids are swapped artist/title,
    # unstripped [<site>.com] site tags, and transliterated Russian misfiled as latn), so a
    # head slice reads far worse than the population. Stride is for estimating only; the
    # real pass takes everything.
    if stride > 1:
        rows = rows[::stride]
    return rows[:limit] if limit else rows


def write_accept(conn, item_id: int, cand: dict) -> None:
    """Write only this source's rows (§3.6: each pass owns its own provenance). song_mbid is
    SONG-level identity, never acoustic truth (§3.6/§3.4) -- nothing here touches
    fingerprints.acoustid_recording_mbid."""
    # Tier-2 accepts carry a confidence in a band strictly below tier-1's floor, so the two
    # remain distinguishable by confidence alone despite sharing a source.
    conf = cand.get("order_free_conf") or round(min(cand["artist_sim"], cand["title_sim"]), 4)
    ts = db.utcnow() if hasattr(db, "utcnow") else stage5.utcnow()
    rows = [("song_mbid", cand["mbid"]), ("artist", cand["artist"]), ("title", cand["title"])]
    if cand.get("year"):
        rows.append(("year", cand["year"]))
    for field, value in rows:
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
            "updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(media_item_id, field, source) DO UPDATE SET "
            "value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at",
            (item_id, field, value, SOURCE, conf, ts),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the full planned action list, write nothing (§13)")
    ap.add_argument("--limit", type=int, default=None, help="cap items considered")
    ap.add_argument("--script", default="latn",
                    help="language/script to run on (default latn; Hebrew is Spotify's job)")
    ap.add_argument("--show", type=int, default=25, help="how many decisions to print")
    ap.add_argument("--stride", type=int, default=1,
                    help="sample every Nth item for a REPRESENTATIVE estimate (bad source "
                         "batches cluster by id, so a head slice is not representative)")
    ap.add_argument("--no-order-free", action="store_true",
                    help="tier 1 only: skip the order-free acceptance pass (§9.1.1 tier 2)")
    args = ap.parse_args()

    conn = db.connect()
    items = worklist(conn, script=args.script, limit=args.limit, stride=args.stride)
    print(f"worklist: {len(items)} items "
          f"(winner/sole_copy, no song_mbid, script={args.script}, not already done)")
    if not items:
        return 0

    client = stage5.MbClient()
    counters: dict = {}
    tally = {"accept": 0, "accept_order_free": 0, "no_results": 0, "no_artist_gate": 0,
             "below_floor": 0, "order_free_reject": 0, "no_tokens": 0, "error": 0}
    shown = 0

    try:
        for n, row in enumerate(items, 1):
            url = freetext_url(row["artist"], row["title"])
            try:
                payload = stage5.cached_fetch(conn, client, url, counters=counters)
            except stage5.EnrichNetworkError as exc:
                print(f"  network stop after {n-1} items: {exc}", file=sys.stderr)
                tally["error"] += 1
                break
            cands = parse_candidates(payload)
            cand, reason = choose(row["artist"], row["title"], cands)
            # Tier 2 only ever sees what tier 1 refused, so it can add accepts but never
            # overrule a field-aligned one.
            if cand is None and not args.no_order_free:
                cand, reason = choose_order_free(row["artist"], row["title"], cands)
            tally[reason] = tally.get(reason, 0) + 1
            if cand is not None:
                swapped = reason == "accept_order_free"
                if shown < args.show:
                    tag = "ACCEPT*" if swapped else "ACCEPT "
                    print(f"  {tag} ours: {row['artist']!r} | {row['title']!r}")
                    print(f"          mb  : {cand['artist']!r} | {cand['title']!r} "
                          f"({'cov' if swapped else 'sim'} "
                          f"a={cand['artist_sim']:.2f} t={cand['title_sim']:.2f}) {cand['mbid']}")
                    shown += 1
                if not args.dry_run:
                    write_accept(conn, row["id"], cand)
                    conn.commit()          # per-item: Ctrl-C leaves a consistent partial run
            if n % 100 == 0:
                print(f"  ... {n}/{len(items)}  tier1={tally.get('accept',0)} "
                      f"tier2={tally.get('accept_order_free',0)}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted — committed work is intact, re-run to resume", file=sys.stderr)

    total = sum(tally.values()) or 1
    print(f"\n--- {'DRY RUN — nothing written' if args.dry_run else 'RESULTS'} ---")
    for k in ("accept", "accept_order_free", "no_artist_gate", "below_floor",
              "order_free_reject", "no_tokens", "no_results", "error"):
        print(f"  {k:16} {tally.get(k,0):5}  ({100*tally.get(k,0)/total:5.1f}%)")
    print(f"  cache hits: {counters.get('cache_hits',0)}  fetched: {counters.get('fetched',0)}")

    if not args.dry_run:
        conn.execute(
            "INSERT INTO pipeline_runs (stage, started_at, finished_at, host, items_processed, "
            "items_failed, tool_versions, report, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            ("stage5_mb_freetext", None, stage5.utcnow(), __import__("socket").gethostname(),
             total, tally.get("error", 0), json.dumps(config.tool_versions()),
             json.dumps(tally), f"free-text MB pass, script={args.script}, source={SOURCE}"),
        )
        conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
