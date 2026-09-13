#!/usr/bin/env python3
"""§9.1.2 Hebrew enrichment: Wikidata + the MusicBrainz ARTIST index.

WHY THIS EXISTS
---------------
3,005 Hebrew items carry no `song_mbid`. §9.1.1 deliberately skipped them ("MB's coverage of
Israeli artists is thin; Hebrew order is Spotify's job"). Probing 2026-07-31 showed that
premise is wrong in a specific and useful way:

    Hebrew items (n=40, evenly strided)   found it   Hebrew script
      Wikidata                             82.5%       100%
      MusicBrainz ARTIST index             62.5%       100%
      iTunes                                5.0%         —
      Deezer                                2.5%         —

MusicBrainz files Israeli artists under a HEBREW primary name with a Latin sort-name
("שרית חדד" / "Hadad, Sarit") — the opposite of Spotify and iTunes, which carry them under
Latin names ~90% of the time. MB's Hebrew coverage is thin at the RECORDING level, not the
ARTIST level.

WHAT THIS BUYS, AND WHAT IT DOES NOT
------------------------------------
Not identifiers. An `arid:`-scoped recording search using an artist MBID we had just
CONFIRMED yielded a song_mbid for 4/25 (16.0%): MusicBrainz knows these artists but does not
hold their tracks. `song_mbid` therefore stays missing for most Hebrew items and that is the
accepted outcome (sha-yol, 2026-07-31: "missing would stay missing").

What it does buy is the thing §6.1 actually got wrong — SEGMENTATION. `location_parses` splits
Hebrew items `artist_title` 1,897 vs `title_artist` 1,853, a coin flip, because §6.1 deferred
order to a MusicBrainz match that Hebrew items never got. Two independent catalogues that both
know *which of our two strings names a person* settle it. Plus canonical Hebrew spelling, and
`year` where Wikidata has it.

THE ORDER ORACLE
----------------
Wikidata is a TYPED graph, and that is what makes it decisive here. Each of our two fields is
searched separately; the returned entities are classified as artist-like or work-like from
their claims (not from string shape). If field X resolves to a person/band and field Y to a
song, the order is settled without comparing any text at all. The MB artist index gives the
same answer independently — whichever of our fields matches an artist name IS the artist.

Agreement between the two is what earns the top confidence band; a single source still writes,
one band lower; a genuine disagreement writes nothing.

HEBREW TEXT DISCIPLINE (non-negotiable, see PROGRESS/§6.1)
----------------------------------------------------------
End users search in Hebrew, so a correct-but-transliterated "Eyal Golan" is unusable. A
catalogue's name is written ONLY when it is itself Hebrew script; when our text is Hebrew and
the catalogue's is Latin, ours stands and the catalogue is used purely as a signal about
ORDER. Wikidata's Hebrew labels are the one external source measured good enough to write
(native spellings, not transliterations), and they do correct real errors —
"פאבלו רוזנברג" -> "פבלו רוזנברג".

TITLE-ONLY ITEMS (541 of them, no artist field at all)
-------------------------------------------------------
Wikidata finds a song entity for 20% of these, and every one carried a performer. But only 4%
have that performer's name INSIDE our own string. That distinction is the whole safety rule:

  * performer name IS a substring of our text  -> we have SEGMENTED our own string. Auto-accept.
  * performer name is NOT in our text          -> that is Wikidata's ATTRIBUTION, not ours.
    Covers are the norm in a karaoke library, so this goes to `metadata_match` review.

    tools/heb_enrich.py --dry-run           # plan only, writes nothing (§13)
    tools/heb_enrich.py --stride 40         # representative estimate (see §9.1.1 trap)
    tools/heb_enrich.py                     # the full pass; Ctrl-C-safe, resumable

Idempotent: an item with a row from either source this tool owns is not re-queried, so a
second back-to-back run makes zero changes (§13). Every response lands in `enrich_cache`, so
re-tuning the floors never needs a refetch (§11).
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from karaokemp import config, db, stage5  # noqa: E402
from karaokemp import enrich_sources as es  # noqa: E402

SOURCE_WD = "wikidata"
SOURCE_MB = "musicbrainz_artist"

# --- acceptance bands (§11: retunable from enrich_cache without refetching) ----------------
# A catalogue label must match one of our fields this well before we believe it names the same
# thing. Deliberately strict: Hebrew is a small, dense name space (probe item 8 matched our
# "הוריקן" to a SERBIAN band of the same name), and a loose floor here does not surface as a
# bad string — it surfaces as a confidently wrong ORDER decision.
WD_LABEL_FLOOR = 0.90
MB_ARTIST_FLOOR = 0.90
# Title agreement required before an arid-scoped recording hit becomes a song_mbid.
MB_RECORDING_FLOOR = 0.85

# Confidence bands, chosen so a §11 retune can tell the three cases apart without re-querying.
CONF_BOTH_AGREE = 0.95   # Wikidata and MusicBrainz independently agree on the order
CONF_SINGLE = 0.85       # exactly one catalogue resolved it
CONF_SEGMENTED = 0.80    # title-only, performer name found inside our own string

# Hebrew decorations. §6.1's decoration list is English-only, so these survived parsing and
# reach the catalogues inside the query, where they are poison: Wikidata has an entry for
# 'עומר אדם' but none for 'עומר אדם ישראלי מזרחי' (a GENRE tag, not part of the name).
#
# Measured on the 628 unresolved paired items: 60 of them (9.6%) carry one of these —
# שרים 29, ישראלי מזרחי 12, כוכב נולד 11, ביצוע 8, מברך 1. Worth stripping, and no more than
# that: the dominant failure is 267 items (42.5%) where Wikidata simply has no entry for the
# artist OR the song, which no amount of query shaping reaches.
HEB_DECOR = ("שרים", "שר", "שרה", "בביצוע", "ביצוע", "מארחים את", "מארחים", "מברך",
             "קריוקי", "פלייבק", "ישראלי מזרחי", "מזרחי", "כוכב נולד", "הפקות")

# Decorations are stripped only at the EDGES of a field. A decoration is an affix — a genre
# tag or a verb appended to a credit — whereas the same word mid-string is far more likely to
# be part of a real title. Removing 'שר' from the middle of a sentence-shaped Hebrew title
# would corrupt it, and titles are what we store.
_MAX_DECOR_STRIPS = 3


def strip_query_decor(s: str) -> str:
    """Edge-anchored decoration removal, for QUERY SHAPING and for comparison against a
    catalogue label. Returns the input unchanged if stripping would empty it."""
    if not s:
        return s
    cur = " ".join(s.split())
    for _ in range(_MAX_DECOR_STRIPS):
        low = cur
        for d in HEB_DECOR:
            if low.endswith(" " + d) or low == d:
                cur = cur[: len(cur) - len(d)].strip()
                break
            if low.startswith(d + " "):
                cur = cur[len(d):].strip()
                break
        else:
            break
        if not cur:
            return " ".join(s.split())
    return cur or " ".join(s.split())


# ==========================================================================================
# Pure decision logic — no I/O, so it is testable and §11-retunable.
# ==========================================================================================

@dataclass
class Resolution:
    """Which of our two strings is the artist, and what each catalogue called them."""
    artist_field: str | None = None       # 'a' or 'b' — which INPUT field is the artist
    artist_label: str | None = None       # catalogue's name for the artist (any script)
    title_label: str | None = None        # catalogue's name for the work (any script)
    artist_mbid: str | None = None        # MB artist id, used transiently to scope searches
    year: str | None = None
    reason: str = "unresolved"
    qids: dict = dc_field(default_factory=dict)


def _best_hit(field_text: str, hits: list[dict], entities: dict, floor: float):
    """Best (score, qid, entity) whose LABEL matches this field's text. Wikidata's own search
    ranking is ignored: it ranks by prominence, and a famous unrelated act routinely outranks
    the obscure Israeli one we actually want."""
    best = None
    for h in hits:
        ent = entities.get(h["qid"])
        if not ent:
            continue
        label = es.label_of(ent, lang="he") or h["label"]
        score = max(es.sim_he(label, field_text), es.sim_he(h["label"], field_text))
        if score >= floor and (best is None or score > best[0]):
            best = (score, h["qid"], ent)
    return best


def resolve_wikidata(a: str, b: str, hits_a: list[dict], hits_b: list[dict],
                     entities: dict) -> Resolution:
    """The typed order oracle. If one of our fields resolves to an ARTIST entity and the
    other to a WORK entity, the order is settled by TYPE, with no text comparison at all."""
    ba = _best_hit(a, hits_a, entities, WD_LABEL_FLOOR)
    bb = _best_hit(b, hits_b, entities, WD_LABEL_FLOOR)
    a_art = bool(ba and es.is_artist_entity(ba[2]))
    b_art = bool(bb and es.is_artist_entity(bb[2]))
    a_work = bool(ba and es.is_work_entity(ba[2]))
    b_work = bool(bb and es.is_work_entity(bb[2]))

    # An entity can look like both (a self-titled single, a band named after a song). Only an
    # UNAMBIGUOUS type split decides the order.
    a_only_art, b_only_art = a_art and not a_work, b_art and not b_work
    a_only_work, b_only_work = a_work and not a_art, b_work and not b_art

    if a_only_art and b_only_work:
        art, work, fld, why = ba, bb, "a", "wd_resolved"
    elif b_only_art and a_only_work:
        art, work, fld, why = bb, ba, "b", "wd_resolved"
    # Fallback: an artist matched but the song is simply not in Wikidata — the common Hebrew
    # case. This branch is REAL yield but it is also the one demonstrably dangerous branch,
    # because "no hit on the other field" is not evidence, it is absence of evidence. Live
    # example: item 8, 'הוריקן' | 'Hurricane עדן גולן (גרסת בנות) PIANO l NATI'. Wikidata has
    # a SERBIAN band called הוריקן; our other field is decoration-laden junk that matches
    # nothing, so the fallback fired and confidently declared the TITLE to be the artist. It
    # is marked separately here so `combine` can demand a second vote for it.
    elif a_only_art and not bb:
        art, work, fld, why = ba, None, "a", "wd_resolved_artist_only"
    elif b_only_art and not ba:
        art, work, fld, why = bb, None, "b", "wd_resolved_artist_only"
    else:
        return Resolution(reason="wd_no_hit" if not (ba or bb) else "wd_ambiguous_type")

    mbids = es.claim_values(art[2], es.P_MB_ARTIST)
    return Resolution(
        artist_field=fld,
        artist_label=es.label_of(art[2], lang="he"),
        title_label=es.label_of(work[2], lang="he") if work else None,
        artist_mbid=mbids[0] if mbids else None,
        year=es.year_of(work[2]) if work else None,
        reason=why,
        qids={"artist": art[1], "work": work[1] if work else None},
    )


def _mb_name_match(field_text: str, cands: list[dict], floor: float):
    """Best artist candidate matching this field, comparing the primary name, the sort-name,
    and any aliases. Aliases only arrive from an inc=aliases LOOKUP, so this scores whatever
    it is given and improves silently once the caller enriches a candidate."""
    best = None
    for c in cands:
        names = [c["name"], c.get("sort_name") or ""] + list(c.get("aliases") or [])
        score = max((es.sim_he(n, field_text) for n in names if n), default=0.0)
        if score >= floor and (best is None or score > best[0]):
            best = (score, c)
    return best


def resolve_mb_artist(a: str, b: str, cands_a: list[dict],
                      cands_b: list[dict]) -> Resolution:
    """The second, independent order oracle: whichever of our fields names a real artist in
    MusicBrainz's artist index IS the artist."""
    ma = _mb_name_match(a, cands_a, MB_ARTIST_FLOOR)
    mb_ = _mb_name_match(b, cands_b, MB_ARTIST_FLOOR)
    if ma and mb_:
        # Both look like artists. Take the stronger, but only if it is clearly stronger —
        # otherwise we would be guessing, and a guess here writes a wrong ORDER.
        if abs(ma[0] - mb_[0]) < 0.05:
            return Resolution(reason="mb_ambiguous_both_artists")
        best, fld = (ma, "a") if ma[0] > mb_[0] else (mb_, "b")
    elif ma:
        best, fld = ma, "a"
    elif mb_:
        best, fld = mb_, "b"
    else:
        return Resolution(reason="mb_no_hit")
    return Resolution(artist_field=fld, artist_label=best[1]["name"],
                      artist_mbid=best[1]["mbid"], reason="mb_resolved")


def choose_text(ours: str, theirs: str | None) -> str:
    """Which string actually gets stored.

    The no-transliteration rule lives here: if our text is Hebrew and the catalogue's is not,
    OURS stands and the catalogue was only ever a signal about order. A Hebrew catalogue label
    is preferred because it canonicalizes spelling ("פאבלו רוזנברג" -> "פבלו רוזנברג")."""
    if not theirs:
        return ours
    if es.script_of(ours) == "heb" and es.script_of(theirs) != "heb":
        return ours
    return theirs


def choose_title(ours: str, theirs: str | None) -> str:
    """As choose_text, but keeps OUR version qualifier. Wikidata labels are bare song names,
    so taking one wholesale would silently drop the '(גרסת בנות פסנתר)' / '(גרסת בנות)' that
    distinguishes one copy of a song from another in this library."""
    picked = choose_text(ours, theirs)
    return ours if picked is ours else es.merge_title(ours, picked)


def combine(a: str, b: str, wd: Resolution, mb: Resolution) -> dict | None:
    """Merge the two oracles into one decision, or None if they disagree / neither resolved."""
    wd_ok, mb_ok = wd.artist_field is not None, mb.artist_field is not None
    # The absence-of-evidence branch (§ resolve_wikidata) may not stand alone: it inferred the
    # order from the OTHER field matching nothing, which is exactly what a junk-laden title
    # field does. It needs MusicBrainz to independently name the same field as the artist.
    wd_needs_second_vote = wd.reason == "wd_resolved_artist_only"

    if wd_ok and mb_ok:
        if wd.artist_field != mb.artist_field:
            return {"reason": "conflict", "wd": wd, "mb": mb}
        conf, reason = CONF_BOTH_AGREE, "both_agree"
    elif wd_ok and not wd_needs_second_vote:
        conf, reason = CONF_SINGLE, "wikidata_only"
    elif mb_ok:
        conf, reason = CONF_SINGLE, "mb_only"
    elif wd_ok:
        return {"reason": "wd_artist_only_uncorroborated", "wd": wd, "mb": mb}
    else:
        return {"reason": wd.reason if wd.reason != "wd_no_hit" else mb.reason,
                "wd": wd, "mb": mb}

    fld = wd.artist_field if wd_ok else mb.artist_field
    our_artist, our_title = (a, b) if fld == "a" else (b, a)
    # Prefer Wikidata's Hebrew label for the artist (measured the best Hebrew name source);
    # fall back to MB's, which is also usually Hebrew for Israeli acts.
    cat_artist = wd.artist_label if wd_ok else None
    if es.script_of(cat_artist or "") != "heb" and mb_ok:
        cat_artist = mb.artist_label
    return {
        "reason": reason,
        "confidence": conf,
        "swapped": fld == "b",
        "artist": choose_text(our_artist, cat_artist),
        "title": choose_title(our_title, wd.title_label if wd_ok else None),
        "artist_mbid": wd.artist_mbid or mb.artist_mbid,
        "year": wd.year,
        "wd": wd, "mb": mb,
    }


def strip_decor(text: str) -> str:
    out = text
    for d in HEB_DECOR:
        out = out.replace(d, " ")
    return " ".join(out.split())


def segment_title_only(our_title: str, performer_names: list[str]) -> dict | None:
    """Undelimited `Title Artist` strings, e.g. 'ילדה קטנה משה פרץ ואגם בוחבוט שרים'.

    Accept ONLY when a performer's name is literally present in our own string: that makes
    this SEGMENTATION of text we already had, not an attribution imported from Wikidata. The
    distinction is the safety rule for this population — see the module docstring."""
    norm_title = es.norm_he(our_title)
    for name in performer_names:
        n = es.norm_he(name)
        if not n or n not in norm_title:
            continue
        # Prefer cutting the name out of the ORIGINAL string, which keeps its punctuation and
        # spacing; fall back to the normalized form only when the name is spelled there with
        # niqqud or punctuation we had to strip in order to match it at all.
        if name in our_title:
            remainder = strip_decor(our_title.replace(name, " "))
        else:
            remainder = strip_decor(norm_title.replace(n, " "))
        # If removing the artist leaves nothing, our string was ONLY the artist name — that
        # is not a title, and writing the empty remainder would destroy the item's only text.
        if not remainder:
            return None
        return {"artist": name, "title": remainder, "matched": name}
    return None


# ==========================================================================================
# I/O: worklist, the fetch chain, writes.
# ==========================================================================================

def worklist(conn, *, limit: int | None, stride: int, title_only: bool) -> list:
    """Winners + sole copies (§9.1 runs on survivors), Hebrew, no song_mbid from ANY source,
    and not already handled by this pass (idempotence).

    `title_only` selects the complementary population: items with a title and NO artist."""
    having = ("AND artist IS NULL" if title_only else "AND artist IS NOT NULL")
    sql = f"""
      WITH w AS (SELECT media_item_id, field, value FROM v_metadata
                 WHERE field IN ('artist','title','song_mbid'))
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
                        WHERE s.media_item_id=mi.id AND s.source IN (?, ?))
        AND COALESCE((SELECT value FROM song_metadata l WHERE l.media_item_id=mi.id
                      AND l.field='language' LIMIT 1), '?') = 'he'
      GROUP BY mi.id
      HAVING title IS NOT NULL {having}
      ORDER BY mi.id
    """
    rows = conn.execute(sql, (SOURCE_WD, SOURCE_MB)).fetchall()
    # --stride samples EVENLY across the id range. Bad source batches cluster by id, so a plain
    # head slice is not a representative sample of the population (§9.1.1).
    if stride > 1:
        rows = rows[::stride]
    return rows[:limit] if limit else rows


def wd_lookup(conn, client, term: str, counters: dict) -> tuple[list[dict], dict]:
    """One search + one batched entity fetch. wbgetentities takes up to 50 ids per call, so
    every candidate for a term costs exactly two round trips regardless of how many there are."""
    hits = es.wikidata_search_hits(
        es.cached_fetch(conn, client, "wikidata", es.wikidata_search_url(term),
                        counters=counters))
    if not hits:
        return [], {}
    ents = es.cached_fetch(
        conn, client, "wikidata",
        es.wikidata_entities_url([h["qid"] for h in hits[:8]]), counters=counters)
    return hits, (ents.get("entities") or {})


def mb_artist_lookup(conn, client, term: str, counters: dict) -> list[dict]:
    return es.mb_artist_candidates(
        es.cached_fetch(conn, client, "mb_artist", es.mb_artist_search_url(term),
                        counters=counters))


def find_song_mbid(conn, client, arid: str, title: str, counters: dict) -> dict | None:
    """The arid-scoped recording search. Measured yield 16% — MusicBrainz holds these artists
    but mostly not their recordings — so this is a bonus path, never the point of the pass."""
    cands = es.mb_recording_candidates(
        es.cached_fetch(conn, client, "mb_artist",
                        es.mb_arid_recording_url(arid, title), counters=counters))
    best, score = None, 0.0
    for c in cands:
        s = max(es.sim_he(c["title"], title),
                stage5.title_agreement(title, c["title"]))
        if s > score:
            best, score = c, s
    return dict(best, title_sim=round(score, 4)) if best and score >= MB_RECORDING_FLOOR else None


def write_rows(conn, item_id: int, source: str, rows: list[tuple[str, str]],
               confidence: float) -> int:
    """Write only this source's rows (§3.6: each pass owns, and can rebuild, its own
    provenance). Never touches fingerprints.acoustid_recording_mbid — that is acoustic
    identity and this pass has no acoustic evidence (§3.4)."""
    ts = stage5.utcnow()
    n = 0
    for f, v in rows:
        if v is None or v == "":
            continue
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
            "updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(media_item_id, field, source) DO UPDATE SET "
            "value=excluded.value, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (item_id, f, v, source, confidence, ts))
        n += 1
    return n


def queue_review(conn, item_id: int, payload: dict) -> None:
    conn.execute(
        "INSERT INTO review_queue (kind, media_item_id, payload, created_at) "
        "VALUES ('metadata_match', ?, ?, ?)",
        (item_id, json.dumps(payload, ensure_ascii=False), stage5.utcnow()))


# ==========================================================================================
# Per-item chains
# ==========================================================================================

def process_paired(conn, wd_client, mb_client, row, counters: dict) -> dict:
    """Items with BOTH fields: resolve the order, canonicalize, then try for a song_mbid."""
    # Decorations are stripped BEFORE querying and before every comparison. They are poison in
    # a catalogue query (Wikidata has 'עומר אדם', not 'עומר אדם ישראלי מזרחי'), and comparing a
    # decorated field against an undecorated label would fail the match floor even on a
    # perfect hit. The stripped form is also what gets stored — dropping a genre tag from an
    # artist credit is a correction, not a loss.
    a = strip_query_decor(row["artist"])
    b = strip_query_decor(row["title"])
    hits_a, ents_a = wd_lookup(conn, wd_client, a, counters)
    hits_b, ents_b = wd_lookup(conn, wd_client, b, counters)
    wd = resolve_wikidata(a, b, hits_a, hits_b, {**ents_a, **ents_b})

    mb = resolve_mb_artist(a, b,
                           mb_artist_lookup(conn, mb_client, a, counters),
                           mb_artist_lookup(conn, mb_client, b, counters))

    decision = combine(a, b, wd, mb)
    if decision is None or "confidence" not in decision:
        return decision or {"reason": "unresolved"}

    # Bonus path: a confirmed artist MBID lets us ask MB for the RECORDING. Usually nothing.
    if decision.get("artist_mbid"):
        rec = find_song_mbid(conn, mb_client, decision["artist_mbid"],
                             decision["title"], counters)
        if rec:
            decision["song_mbid"] = rec["mbid"]
            decision["song_mbid_title"] = rec["title"]
            decision["song_mbid_year"] = rec.get("year")
    return decision


def process_title_only(conn, wd_client, row, counters: dict) -> dict:
    """Items with a title and no artist. Auto-accept only a performer name we can find inside
    our own string; anything else is Wikidata's attribution and goes to review."""
    t = row["title"]
    hits, ents = wd_lookup(conn, wd_client, t, counters)
    best = _best_hit(t, hits, ents, WD_LABEL_FLOOR)
    if not best or not es.is_work_entity(best[2]):
        return {"reason": "no_work_entity"}
    work = best[2]
    perf_qids = es.claim_values(work, es.P_PERFORMER)
    if not perf_qids:
        return {"reason": "work_no_performer"}
    perf_ents = es.cached_fetch(
        conn, wd_client, "wikidata",
        es.wikidata_entities_url(perf_qids[:5]), counters=counters).get("entities") or {}
    names, mbids = [], []
    for pe in perf_ents.values():
        nm = es.label_of(pe, lang="he")
        if nm and es.script_of(nm) == "heb":
            names.append(nm)
        mbids += es.claim_values(pe, es.P_MB_ARTIST)

    seg = segment_title_only(t, names)
    common = {"qid": best[1], "performers": names, "artist_mbid": mbids[0] if mbids else None,
              "year": es.year_of(work),
              "description": (work.get("descriptions") or {}).get("he", {}).get("value")}
    if seg:
        return {"reason": "segmented", "confidence": CONF_SEGMENTED, **common, **seg}
    if not names:
        return {"reason": "performer_not_hebrew"}
    return {"reason": "needs_review", **common}


# ==========================================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned action list, write nothing (§13)")
    ap.add_argument("--limit", type=int, default=None, help="cap items considered")
    ap.add_argument("--stride", type=int, default=1,
                    help="sample every Nth item for a REPRESENTATIVE estimate (§9.1.1)")
    ap.add_argument("--show", type=int, default=25, help="how many decisions to print")
    ap.add_argument("--title-only", action="store_true",
                    help="run the title-only population instead of the paired one")
    ap.add_argument("--budget", type=int, default=config.REVIEW_QUEUE_BUDGET,
                    help="stop queueing metadata_match reviews past this many open (§11)")
    args = ap.parse_args()

    conn = db.connect()
    items = worklist(conn, limit=args.limit, stride=args.stride, title_only=args.title_only)
    pop = "title-only" if args.title_only else "paired"
    print(f"worklist: {len(items)} {pop} Hebrew items "
          f"(winner/sole_copy, no song_mbid, not already done)")
    print(f"db      : {config.DB_PATH}")
    if not items:
        return 0

    wd_client = stage5.MbClient(rate_sec=es.RATE_SEC["wikidata"])
    mb_client = stage5.MbClient(rate_sec=es.RATE_SEC["mb_artist"])
    counters: dict = {}
    tally: dict = {}
    written = {"wikidata": 0, "musicbrainz_artist": 0, "song_mbid": 0, "reviews": 0}
    shown = 0
    open_reviews = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue "
        "WHERE kind='metadata_match' AND resolution IS NULL").fetchone()["n"]

    try:
        for n, row in enumerate(items, 1):
            try:
                d = (process_title_only(conn, wd_client, row, counters) if args.title_only
                     else process_paired(conn, wd_client, mb_client, row, counters))
            except stage5.EnrichNetworkError as exc:
                print(f"  network stop after {n-1} items: {exc}", file=sys.stderr)
                tally["error"] = tally.get("error", 0) + 1
                break
            reason = d.get("reason", "unresolved")
            tally[reason] = tally.get(reason, 0) + 1

            if shown < args.show and reason in ("both_agree", "wikidata_only", "mb_only",
                                                "segmented", "needs_review", "conflict"):
                if args.title_only:
                    print(f"  {reason:15} [{row['id']}] {row['title']!r}")
                    if reason == "segmented":
                        print(f"      -> artist={d['artist']!r} title={d['title']!r}")
                    elif reason == "needs_review":
                        print(f"      ?  performers={d['performers']} — {d.get('description')}")
                else:
                    flag = "SWAP" if d.get("swapped") else "keep"
                    print(f"  {reason:15} [{row['id']}] {flag} "
                          f"{row['artist']!r} | {row['title']!r}")
                    if "confidence" in d:
                        print(f"      -> artist={d['artist']!r} title={d['title']!r}"
                              + (f" mbid={d['song_mbid']}" if d.get("song_mbid") else ""))
                shown += 1

            if args.dry_run:
                continue

            # --- writes ---------------------------------------------------------------
            if reason in ("both_agree", "wikidata_only", "mb_only"):
                conf = d["confidence"]
                wd_rows = [("artist", d["artist"]), ("title", d["title"])]
                if d.get("year"):
                    wd_rows.append(("year", d["year"]))
                if d["wd"].artist_field is not None:
                    written["wikidata"] += write_rows(conn, row["id"], SOURCE_WD, wd_rows, conf)
                mb_rows: list[tuple[str, str]] = []
                if d["mb"].artist_field is not None:
                    mb_rows += [("artist", d["artist"]), ("title", d["title"])]
                if d.get("song_mbid"):
                    mb_rows.append(("song_mbid", d["song_mbid"]))
                    if d.get("song_mbid_year"):
                        mb_rows.append(("year", d["song_mbid_year"]))
                    written["song_mbid"] += 1
                if mb_rows:
                    written["musicbrainz_artist"] += write_rows(
                        conn, row["id"], SOURCE_MB, mb_rows, conf)
                conn.commit()
            elif reason == "segmented":
                rows_ = [("artist", d["artist"]), ("title", d["title"])]
                if d.get("year"):
                    rows_.append(("year", d["year"]))
                written["wikidata"] += write_rows(
                    conn, row["id"], SOURCE_WD, rows_, d["confidence"])
                conn.commit()
            elif reason == "needs_review":
                # §11: never grind past the budget — stop queueing, keep enriching.
                if open_reviews < args.budget:
                    queue_review(conn, row["id"], {
                        "reason": "wikidata_title_only_attribution",
                        "query": {"title": row["title"]},
                        "proposal": {"artist": d["performers"][0] if d["performers"] else None,
                                     "all_performers": d["performers"],
                                     "wikidata_qid": d["qid"],
                                     "description": d.get("description"),
                                     "year": d.get("year")},
                        "caveat": ("performer NOT present in our own string — this is "
                                   "Wikidata's attribution, and covers are common"),
                    })
                    conn.commit()
                    open_reviews += 1
                    written["reviews"] += 1
                else:
                    tally["review_over_budget"] = tally.get("review_over_budget", 0) + 1

            if n % 100 == 0:
                print(f"  ... {n}/{len(items)}  "
                      f"wd={written['wikidata']} mb={written['musicbrainz_artist']} "
                      f"mbid={written['song_mbid']}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted — committed work is intact, re-run to resume", file=sys.stderr)

    total = sum(tally.values()) or 1
    print(f"\n--- {'DRY RUN — nothing written' if args.dry_run else 'RESULTS'} ({pop}) ---")
    for k in sorted(tally, key=lambda x: -tally[x]):
        print(f"  {k:24} {tally[k]:5}  ({100*tally[k]/total:5.1f}%)")
    print(f"  rows written: {written}")
    print(f"  cache hits: {counters.get('cache_hits',0)}  fetched: {counters.get('fetched',0)}")

    if not args.dry_run:
        conn.execute(
            "INSERT INTO pipeline_runs (stage, started_at, finished_at, host, "
            "items_processed, items_failed, tool_versions, report, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"stage5_heb_enrich_{pop}", None, stage5.utcnow(), socket.gethostname(),
             total, tally.get("error", 0), json.dumps(config.tool_versions()),
             json.dumps({"tally": tally, "written": written}),
             f"§9.1.2 Hebrew enrichment ({pop}); sources={SOURCE_WD},{SOURCE_MB}"))
        conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
