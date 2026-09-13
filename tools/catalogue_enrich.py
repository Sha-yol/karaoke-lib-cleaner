#!/usr/bin/env python3
"""§9.1.2 Latin-script enrichment via the iTunes and Deezer search APIs.

WHY THIS EXISTS
---------------
3,541 Latin-script winner/sole_copy items carry no `song_mbid` after §9.1 (field-qualified MB)
and §9.1.1 (free-text MB). Both of those ask MusicBrainz the same underlying question with the
same broken input: when our text says "John Secada" or "Dave Mathews Band", no MB query shape
will match, because the defect is in OUR string, not in the query.

iTunes and Deezer are fuzzy CONSUMER search engines rather than exact catalogue lookups, and
that is precisely why they help — they match through the misspelling and hand back the
canonical form. Measured 2026-07-31 (n=40, evenly strided; see the §9.1.1 sampling trap):

    iTunes   67.5% matched, all of them carrying year AND genre
    Deezer   55.0% matched, no year/genre on the search endpoint

Real corrections from that sample: "John Secada" -> "Jon Secada", "Dave Mathews Band" ->
"Dave Matthews Band", "Red Hot Chilli Peppers" -> "Red Hot Chili Peppers", "herman's hermits |
can't you here my heartbeat" -> "Herman's Hermits | Can't You Hear My Heartbeat", and
"Eric Carmen & BB King" -> "Eric Clapton & B.B. King" (which was a wrong ARTIST, not a typo).

NEITHER API RETURNS A MusicBrainz ID, so this pass writes no `song_mbid` and closes no MBID
gap by itself. It closes it INDIRECTLY, and the follow-on needs no new code:

    tools/catalogue_enrich.py       # corrects the text
    tools/mb_freetext.py            # re-run: now queries MB with the CORRECTED text

`mb_freetext`'s idempotence guard skips items that already have a `musicbrainz_freetext` row,
and it only writes rows on ACCEPT — so every item it previously rejected is retried, and it
retries them with different text, which is a different `mb_cache` key and therefore a real
network query rather than a replayed miss. That is the chain that turns a spelling fix into an
identifier.

DEEZER IS CORROBORATION, NOT AN EQUAL
-------------------------------------
Deezer is measurably noisier: on the same sample it matched "THE BEATLES | HELP!" to the cover
act "Blues Beatles" where iTunes returned The Beatles. It ranks below `itunes_text` in
`v_metadata`, so where both fire, iTunes wins and Deezer is inert; it earns its place only on
the items iTunes missed.

ACCEPTANCE (mirrors §9.1.1's two tiers, and for the same reasons)
-----------------------------------------------------------------
  Tier 1  field-aligned: the candidate must agree with BOTH our fields at >= FIELD_FLOOR.
          Cannot redirect an item, only confirm and canonicalize it (§3.6 identity ladder).
  Tier 2  order-free: runs only on what tier 1 refused. The candidate must be explainable by
          our text as a bag of words -- its artist tokens and its title tokens each covered
          >= ORDER_FREE_COVER by the union of our two fields, and covering DIFFERENT tokens
          (without that, a candidate whose title is just the artist's name passes trivially).
          An accept here also tells us which of our strings is the artist.

    tools/catalogue_enrich.py --dry-run --stride 40    # representative estimate, writes nothing
    tools/catalogue_enrich.py --source itunes          # iTunes only
    tools/catalogue_enrich.py                          # the full pass; Ctrl-C-safe, resumable

Idempotent per source: an item with a row from a given source is not re-queried for it.
Every response lands in `enrich_cache`, so re-tuning the floors never needs a refetch (§11).
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from karaokemp import config, db, stage5  # noqa: E402
from karaokemp import enrich_sources as es  # noqa: E402

SOURCES = {"itunes": "itunes_text", "deezer": "deezer_text"}

# Mirrors config.MB_FIELD_AGREEMENT (0.85) deliberately: this asks the same "do we already
# believe this?" question the MB passes ask, so it should not answer it with a different
# number. Retunable from enrich_cache without refetching (§11).
FIELD_FLOOR = config.MB_FIELD_AGREEMENT
ORDER_FREE_COVER = 0.80
# Tier-2 confidence sits in a band strictly BELOW tier 1's floor. The two tiers share a
# source, so confidence is the only thing that keeps them distinguishable for a §11 retune
# without re-querying (same discipline as §9.1.1).
ORDER_FREE_CONF_LO, ORDER_FREE_CONF_HI = 0.60, 0.84

# Stripped from the QUERY (they poison consumer-search relevance) and from the token sets.
DECOR = {
    "karaoke", "instrumental", "playback", "backing", "hd", "lyrics", "official",
    "video", "version", "cdg", "mp3", "mp4", "avi", "mpg", "track", "with", "sing",
}


def toks(s: str | None) -> set[str]:
    return {t for t in stage5.norm_name(s).split() if len(t) > 1} - DECOR


def query_term(artist: str, title: str) -> str:
    words = [w for w in f"{artist} {title}".split()
             if w.lower().strip("()[]") not in DECOR]
    return " ".join(words).strip()


def _cover(cand: set[str], ours: set[str]) -> float:
    return len(cand & ours) / len(cand) if cand else 0.0


def choose(our_artist: str, our_title: str, cands: list[dict]) -> tuple[dict | None, str]:
    """Tier 1 — field-aligned. Pure, so it is testable and retunable."""
    if not cands:
        return None, "no_results"
    best, best_score = None, 0.0
    for c in cands:
        a_sim = stage5.field_agreement(our_artist, c["artist"], allow_containment=True)
        t_sim = stage5.title_agreement(our_title, c["title"])
        if a_sim >= FIELD_FLOOR and t_sim >= FIELD_FLOOR:
            score = a_sim + t_sim
            if score > best_score:
                best, best_score = dict(c, artist_sim=a_sim, title_sim=t_sim), score
    return (best, "accept") if best else (None, "below_floor")


def choose_order_free(our_artist: str, our_title: str,
                      cands: list[dict]) -> tuple[dict | None, str]:
    """Tier 2 — order-free. Runs only on what tier 1 refused, so it can add accepts but never
    overrule a field-aligned one. An accept here CHANGES OUR FIELD ORDER, which §3.6 permits
    explicitly and which is safe because the candidate had to be explainable by our own text."""
    if not cands:
        return None, "no_results"
    ours = toks(our_artist) | toks(our_title)
    if not ours:
        return None, "no_tokens"
    scored = []
    for c in cands:
        ca, ct = toks(c["artist"]), toks(c["title"])
        if not ca or not ct:
            continue
        # Self-titled degenerate case: artist and title explaining the SAME tokens.
        if ca <= ct or ct <= ca:
            continue
        # The title must be covered by tokens the ARTIST did not already consume.
        rest = ours - ca
        if not rest or _cover(ct, rest) < ORDER_FREE_COVER:
            continue
        a_cov, t_cov = _cover(ca, ours), _cover(ct, ours)
        if a_cov >= ORDER_FREE_COVER and t_cov >= ORDER_FREE_COVER:
            scored.append((a_cov + t_cov, c, min(a_cov, t_cov)))
    if not scored:
        return None, "order_free_reject"
    _, best, worst = max(scored, key=lambda x: x[0])
    span = ORDER_FREE_CONF_HI - ORDER_FREE_CONF_LO
    conf = ORDER_FREE_CONF_LO + span * ((worst - ORDER_FREE_COVER) / (1.0 - ORDER_FREE_COVER))
    return dict(best, artist_sim=worst, title_sim=worst,
                order_free_conf=round(min(max(conf, ORDER_FREE_CONF_LO),
                                          ORDER_FREE_CONF_HI), 4)), "accept_order_free"


def worklist(conn, *, source_col: str, limit: int | None, stride: int) -> list:
    """Winners + sole copies, Latin script, no song_mbid from ANY source, both fields
    present, and not already handled for THIS source (idempotence is per-source, so an
    iTunes run and a Deezer run do not block each other)."""
    sql = """
      WITH w AS (SELECT media_item_id, field, value FROM v_metadata
                 WHERE field IN ('artist','title'))
      SELECT mi.id AS id,
             MAX(CASE WHEN w.field='artist' THEN w.value END) AS artist,
             MAX(CASE WHEN w.field='title'  THEN w.value END) AS title
      FROM media_items mi
      LEFT JOIN w ON w.media_item_id = mi.id
      WHERE mi.quality_verdict IN ('winner','sole_copy')
        AND mi.status = 'active'
        AND NOT EXISTS (SELECT 1 FROM song_metadata s
                        WHERE s.media_item_id=mi.id AND s.field='song_mbid')
        AND NOT EXISTS (SELECT 1 FROM song_metadata s
                        WHERE s.media_item_id=mi.id AND s.source=?)
        AND COALESCE((SELECT value FROM song_metadata l WHERE l.media_item_id=mi.id
                      AND l.field='language' LIMIT 1), '?') = 'latn'
      GROUP BY mi.id
      HAVING artist IS NOT NULL AND title IS NOT NULL
      ORDER BY mi.id
    """
    rows = conn.execute(sql, (source_col,)).fetchall()
    if stride > 1:
        rows = rows[::stride]
    return rows[:limit] if limit else rows


def write_accept(conn, item_id: int, source: str, cand: dict, our_title: str) -> int:
    """Write only this source's rows (§3.6). No song_mbid — neither API returns one, and
    inventing song-level identity from a consumer catalogue is exactly what §3.6 forbids.

    The stored title takes the catalogue's spelling but OUR version qualifier — see
    es.merge_title. Without that, matching "Boston (SC)" against "Boston (Live from the
    Grove)" (which title_agreement does deliberately, to decide they are the same song)
    would relabel our file as a live recording it is not."""
    conf = cand.get("order_free_conf") or round(
        min(cand["artist_sim"], cand["title_sim"]), 4)
    ts = stage5.utcnow()
    rows = [("artist", cand["artist"]), ("title", es.merge_title(our_title, cand["title"]))]
    if cand.get("year"):
        rows.append(("year", cand["year"]))
    if cand.get("genre"):
        # genre is a JSON array per §3.6.
        rows.append(("genre", json.dumps([cand["genre"]], ensure_ascii=False)))
    n = 0
    for f, v in rows:
        if not v:
            continue
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
            "updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(media_item_id, field, source) DO UPDATE SET "
            "value=excluded.value, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (item_id, f, v, source, conf, ts))
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned action list, write nothing (§13)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--stride", type=int, default=1,
                    help="sample every Nth item for a REPRESENTATIVE estimate (§9.1.1)")
    ap.add_argument("--show", type=int, default=25)
    ap.add_argument("--source", choices=("itunes", "deezer", "both"), default="both")
    ap.add_argument("--no-order-free", action="store_true",
                    help="tier 1 only: skip the order-free acceptance pass")
    args = ap.parse_args()

    which = ("itunes", "deezer") if args.source == "both" else (args.source,)
    conn = db.connect()
    print(f"db: {config.DB_PATH}")
    grand: dict = {}

    for api in which:
        source = SOURCES[api]
        items = worklist(conn, source_col=source, limit=args.limit, stride=args.stride)
        print(f"\n=== {api} ({source}) — worklist {len(items)} items ===")
        if not items:
            continue
        client = stage5.MbClient(rate_sec=es.RATE_SEC[api])
        counters: dict = {}
        tally: dict = {}
        written = 0
        shown = 0
        try:
            for n, row in enumerate(items, 1):
                term = query_term(row["artist"], row["title"])
                url = (es.itunes_search_url(term) if api == "itunes"
                       else es.deezer_search_url(term))
                try:
                    payload = es.cached_fetch(conn, client, api, url, counters=counters)
                except stage5.EnrichNetworkError as exc:
                    print(f"  network stop after {n-1} items: {exc}", file=sys.stderr)
                    tally["error"] = tally.get("error", 0) + 1
                    break
                cands = (es.itunes_candidates(payload) if api == "itunes"
                         else es.deezer_candidates(payload))
                cand, reason = choose(row["artist"], row["title"], cands)
                if cand is None and not args.no_order_free:
                    cand, reason = choose_order_free(row["artist"], row["title"], cands)
                tally[reason] = tally.get(reason, 0) + 1

                if cand is not None:
                    if shown < args.show:
                        tag = "ACCEPT*" if reason == "accept_order_free" else "ACCEPT "
                        print(f"  {tag} ours: {row['artist']!r} | {row['title']!r}")
                        print(f"          {api:6}: {cand['artist']!r} | {cand['title']!r}"
                              + (f"  {cand['year']}" if cand.get("year") else "")
                              + (f"  {cand['genre']}" if cand.get("genre") else ""))
                        shown += 1
                    if not args.dry_run:
                        # Which of OUR fields is the title depends on the tier: a tier-2
                        # accept means the fields were swapped in our parse. Pick whichever
                        # actually corresponds to the candidate's title, so merge_title
                        # re-appends the right qualifier.
                        ours_title = max((row["artist"], row["title"]),
                                         key=lambda s: stage5.title_agreement(s, cand["title"]))
                        written += write_accept(conn, row["id"], source, cand, ours_title)
                        conn.commit()
                if n % 100 == 0:
                    print(f"  ... {n}/{len(items)}  "
                          f"acc={tally.get('accept',0)}+{tally.get('accept_order_free',0)}",
                          flush=True)
        except KeyboardInterrupt:
            print("\ninterrupted — committed work is intact, re-run to resume", file=sys.stderr)

        total = sum(tally.values()) or 1
        acc = tally.get("accept", 0) + tally.get("accept_order_free", 0)
        print(f"  --- {'DRY RUN' if args.dry_run else 'RESULTS'} {api}: "
              f"{acc}/{total} accepted ({100*acc/total:.1f}%) ---")
        for k in sorted(tally, key=lambda x: -tally[x]):
            print(f"    {k:20} {tally[k]:5}  ({100*tally[k]/total:5.1f}%)")
        print(f"    rows written: {written}   "
              f"cache hits: {counters.get('cache_hits',0)}  "
              f"fetched: {counters.get('fetched',0)}")
        grand[api] = {"tally": tally, "rows": written}

        if not args.dry_run:
            conn.execute(
                "INSERT INTO pipeline_runs (stage, started_at, finished_at, host, "
                "items_processed, items_failed, tool_versions, report, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"stage5_catalogue_{api}", None, stage5.utcnow(), socket.gethostname(),
                 total, tally.get("error", 0), json.dumps(config.tool_versions()),
                 json.dumps(grand[api]),
                 f"§9.1.2 {api} enrichment, source={source}"))
            conn.commit()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
