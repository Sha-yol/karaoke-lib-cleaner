"""§9.1.2 catalogue enrichment sources: Wikidata, MusicBrainz's artist index, iTunes, Deezer.

Shared plumbing only — the query builders, the response parsers, the Hebrew-aware
normalization, and the `enrich_cache` fetch path. The acceptance RULES live in the tools that
own each pass (`tools/heb_enrich.py`, `tools/catalogue_enrich.py`), because those are what a
§11 retune re-derives; everything here is meant to be boring and stable.

WHY THESE SOURCES, AND WHAT THEY ARE FOR
----------------------------------------
Measured 2026-07-31 on evenly-strided samples of the live index (see migration 006 for the
full table). The short version, because it determines how each one is used:

  Hebrew items (n=40)            found it    Hebrew script
    Wikidata                       82.5%        100%
    MusicBrainz artist index       62.5%        100%
    iTunes                          5.0%          —
    Deezer                          2.5%          —

MusicBrainz stores Israeli artists under a HEBREW primary name with a Latin sort-name
("שרית חדד" / "Hadad, Sarit"), which is the opposite of Spotify and iTunes, where Israeli
artists sit under Latin names ~90% of the time. So MB and Wikidata are the Hebrew sources and
iTunes/Deezer are not run on Hebrew at all.

The catch: an arid-scoped recording search using an artist MBID we had just confirmed yielded
a song_mbid for only 4/25 (16.0%). **MusicBrainz knows the Israeli artists but not their
recordings.** Hebrew enrichment therefore buys correct artist/title SEGMENTATION and canonical
Hebrew spelling — not identifiers. That is the accepted outcome, not a bug to fix.

  Latin items (n=40)             found it    carried year+genre
    iTunes                         67.5%        67.5%
    Deezer                         55.0%          —

iTunes corrects errors the free-text MB pass could not match through ("John Secada" -> "Jon
Secada", "Eric Carmen & BB King" -> "Eric Clapton & B.B. King"). Deezer is measurably noisier
— it matched "THE BEATLES | HELP!" to the cover act "Blues Beatles" — so it is only ever
corroboration, never a lone accept.

NO SOURCE HERE MAY REDIRECT AN ITEM (§3.6 identity ladder). Acceptance always requires either
agreement with our existing fields, or that the candidate be explainable by our own text as a
bag of words. These passes correct spelling and order and add year/genre; they never change
which song an item is.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse

from . import config, stage5

# --- rate limits -----------------------------------------------------------------------
# Wikidata and MusicBrainz both ask for a descriptive User-Agent and roughly 1 req/s.
# iTunes publishes no limit but is widely documented to throttle around 20 req/min, so it
# gets the slowest pace here — it is the reason a Latin pass is measured in hours. Deezer
# allows ~50 req/5s and is genuinely fast.
RATE_SEC = {
    "wikidata": 0.4,
    "mb_artist": 1.1,
    "itunes": 3.0,
    "deezer": 0.25,
}

# --- Hebrew-aware normalization ---------------------------------------------------------
# stage5.norm_name is Latin-shaped: it casefolds and strips punctuation but knows nothing
# about niqqud (Hebrew vowel points), which appear inconsistently in this library's filenames
# and would otherwise make two spellings of one name compare as different. Same job, one
# extra step. Kept here rather than in stage5 so the Latin passes' measured behaviour does
# not shift underneath them.
_NIQQUD_RE = re.compile(r"[֑-ׇ]")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_HEB_RE = re.compile(r"[֐-׿]")
_LAT_RE = re.compile(r"[A-Za-z]")


def norm_he(s: str | None) -> str:
    """Casefolded, niqqud-free, punctuation-free token string. Comparison only — never
    stored. Hebrew has no case, so casefold is a no-op there and this stays safe for the
    mixed Hebrew/Latin strings this library is full of."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = _NIQQUD_RE.sub("", s)
    s = unicodedata.normalize("NFKC", s).casefold()
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(t for t in _WS_RE.split(s) if t)


def toks_he(s: str | None) -> set[str]:
    return {t for t in norm_he(s).split() if len(t) > 1}


def has_hebrew(s: str | None) -> bool:
    return bool(_HEB_RE.search(s or ""))


def script_of(s: str | None) -> str:
    """'heb' / 'lat' / 'none'."""
    if _HEB_RE.search(s or ""):
        return "heb"
    return "lat" if _LAT_RE.search(s or "") else "none"


def sim_he(a: str | None, b: str | None) -> float:
    """Agreement between two names, niqqud-insensitive. Token overlap OR sequence ratio,
    whichever is kinder — same shape as stage5.field_agreement, different normalizer."""
    import difflib

    na, nb = norm_he(a), norm_he(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(jacc, difflib.SequenceMatcher(None, na, nb).ratio())


# --- enrich_cache (§3.10 contract, keyed by source) --------------------------------------

def cache_get(conn, source: str, url: str) -> dict | None:
    row = conn.execute(
        "SELECT response_json FROM enrich_cache WHERE source=? AND query_hash=?",
        (source, stage5.cache_key(url)),
    ).fetchone()
    if row is None:
        return None
    return json.loads(stage5._cache_decode(row["response_json"]))


def cache_put(conn, source: str, url: str, response_text: str) -> None:
    conn.execute(
        "INSERT INTO enrich_cache (source, query_hash, request, response_json, fetched_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(source, query_hash) DO UPDATE "
        "SET response_json=excluded.response_json, fetched_at=excluded.fetched_at",
        (source, stage5.cache_key(url), url, stage5._cache_encode(response_text),
         stage5.utcnow()),
    )


def cached_fetch(conn, client: stage5.MbClient, source: str, url: str,
                 *, counters: dict | None = None) -> dict:
    """Cache-first fetch. Only what PARSES is cached — a garbled body is weather, and
    caching it would make the failure permanent and invisible."""
    cached = cache_get(conn, source, url)
    if cached is not None:
        if counters is not None:
            counters["cache_hits"] = counters.get("cache_hits", 0) + 1
        return cached
    text = client.fetch(url)
    parsed = json.loads(text)
    cache_put(conn, source, url, text)
    conn.commit()
    if counters is not None:
        counters["fetched"] = counters.get("fetched", 0) + 1
    return parsed


def _q(s: str) -> str:
    return urllib.parse.quote(s)


_BRACKETED_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")


def merge_title(ours: str, theirs: str | None) -> str:
    """Canonical spelling from the catalogue, version qualifier from US.

    Both enrichment passes match titles with `stage5.title_agreement`, which strips bracketed
    qualifiers so that "Boston (SC)" and "Boston (Live from the Grove)" compare as equal. That
    is right for DECIDING they are the same song and badly wrong for STORING the result:
    taking the candidate's title wholesale relabels our file with a version we have no
    evidence for, and unlike a song_mbid this is presentation text a human will read.

    Taking the candidate's BASE title and re-appending our own qualifier gets both halves
    right, on every shape measured live:

        ours 'Boston (SC)'              theirs 'Boston (Live from the Grove)'  -> 'Boston (SC)'
        ours 'Only Time (2001 Radio Mix)' theirs 'Only Time'      -> 'Only Time (2001 Radio Mix)'
        ours 'Caribbean Qqueen'         theirs 'Caribbean Queen (No More Love On the Run)'
                                                                  -> 'Caribbean Queen'
        ours 'Homecoming'               theirs 'Homecoming (feat. Chris Martin)' -> 'Homecoming'
    """
    if not theirs:
        return ours
    base = _BRACKETED_RE.sub(" ", theirs)
    base = " ".join(base.split()) or theirs
    ours_qual = _BRACKETED_RE.findall(ours or "")
    if ours_qual:
        return f"{base} {' '.join(ours_qual)}".strip()
    return base


# --- Wikidata ----------------------------------------------------------------------------
# Two endpoints: wbsearchentities finds candidate QIDs from a label (fuzzy, language-scoped),
# wbgetentities then fetches labels + claims for up to 50 of them in ONE request. The second
# call is what makes this cheap: a whole batch of candidates costs one round trip.

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Classes we recognize directly. Deliberately short: the authoritative test below is
# EVIDENCE-BASED (does the entity carry artist-ish or work-ish claims?), because Wikidata's
# class hierarchy is deep and enumerating it would need a transitive-subclass query per hit.
_ARTIST_CLASSES = {
    "Q5",         # human
    "Q215380",    # musical group
    "Q5741069",   # rock band
    "Q2088357",   # musical ensemble
    "Q9212979",   # musical duo
}
_WORK_CLASSES = {
    "Q7366",       # song
    "Q134556",     # single
    "Q105543609",  # musical work/composition
    "Q2894096",    # (seen live on Hebrew folk songs)
    "Q7302866",    # hymn / liturgical piece
}

P_INSTANCE_OF = "P31"
P_PERFORMER = "P175"
P_COMPOSER = "P86"
P_LYRICIST = "P676"
P_MB_ARTIST = "P434"
P_MB_WORK = "P435"
P_MB_RECORDING = "P4404"
P_PUBLICATION_DATE = "P577"
P_INCEPTION = "P571"


def wikidata_search_url(term: str, *, lang: str = "he", limit: int = 8) -> str:
    return (f"{WIKIDATA_API}?action=wbsearchentities&search={_q(term)}"
            f"&language={lang}&uselang={lang}&type=item&limit={limit}&format=json")


def wikidata_entities_url(qids: list[str], *, langs: str = "he|en") -> str:
    return (f"{WIKIDATA_API}?action=wbgetentities&ids={_q('|'.join(qids))}"
            f"&props=labels|claims|descriptions&languages={langs}&format=json")


def wikidata_search_hits(payload: dict) -> list[dict]:
    return [{"qid": h.get("id"), "label": h.get("label") or "",
             "description": h.get("description") or "",
             "matched": ((h.get("match") or {}).get("text") or "")}
            for h in (payload.get("search") or []) if h.get("id")]


def claim_values(entity: dict, prop: str) -> list:
    """Flatten a claim to its values: QIDs as 'Q123', external ids/strings as-is, times as
    their raw '+YYYY-MM-DD...' string. Snaks with no value (novalue/somevalue) are skipped."""
    out = []
    for c in (entity.get("claims") or {}).get(prop, []) or []:
        snak = c.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        dv = (snak.get("datavalue") or {}).get("value")
        if isinstance(dv, dict):
            if "id" in dv:
                out.append(dv["id"])
            elif "time" in dv:
                out.append(dv["time"])
        elif dv is not None:
            out.append(dv)
    return out


def label_of(entity: dict, *, lang: str = "he") -> str | None:
    return ((entity.get("labels") or {}).get(lang) or {}).get("value")


def is_artist_entity(entity: dict) -> bool:
    """Evidence-based, not class-list-based. Carrying a MusicBrainz ARTIST id is the single
    strongest signal an entity is a performing act, and it is exactly the thing we came for;
    the class check is the fallback for artists Wikidata has not linked to MB."""
    if claim_values(entity, P_MB_ARTIST):
        return True
    return bool(set(claim_values(entity, P_INSTANCE_OF)) & _ARTIST_CLASSES)


def is_work_entity(entity: dict) -> bool:
    """A thing with a performer, composer or lyricist IS a musical work, whatever class
    Wikidata filed it under — that covers the long tail of Hebrew folk songs whose P31 is
    some class this module has never heard of."""
    if set(claim_values(entity, P_INSTANCE_OF)) & _WORK_CLASSES:
        return True
    return any(claim_values(entity, p) for p in (P_PERFORMER, P_COMPOSER, P_LYRICIST))


def year_of(entity: dict) -> str | None:
    """First 4-digit year from publication date, else inception. Wikidata times look like
    '+1965-00-00T00:00:00Z' — precision may be year-only, so the month/day can be 00."""
    for prop in (P_PUBLICATION_DATE, P_INCEPTION):
        for t in claim_values(entity, prop):
            m = re.match(r"^[+-](\d{4})", str(t))
            if m and m.group(1) != "0000":
                return m.group(1)
    return None


# --- MusicBrainz artist index ------------------------------------------------------------
# A different question from stage5.mb_search_url (recordings) and from tools/mb_freetext.py
# (unqualified recordings), hence a different cache key rather than a replay of either miss.

def mb_artist_search_url(name: str, *, limit: int = 5) -> str:
    return f"{config.MB_API_ROOT}/artist?query={_q(name)}&fmt=json&limit={limit}"


def mb_artist_lookup_url(mbid: str) -> str:
    """Aliases are NOT returned by the search endpoint — only by a lookup with inc=aliases.
    This is the call that recovers a Hebrew alias for an artist MB happens to file under a
    Latin primary name."""
    return f"{config.MB_API_ROOT}/artist/{mbid}?inc=aliases&fmt=json"


def mb_artist_candidates(payload: dict) -> list[dict]:
    out = []
    for a in (payload.get("artists") or []):
        if not a.get("id"):
            continue
        out.append({
            "mbid": a["id"],
            "name": a.get("name") or "",
            "sort_name": a.get("sort-name") or "",
            "score": a.get("score") or 0,
            "country": a.get("country"),
            "type": a.get("type"),
            # present only on a lookup with inc=aliases; empty from a search
            "aliases": [x.get("name") or "" for x in (a.get("aliases") or [])],
        })
    return out


def mb_arid_recording_url(arid: str, title: str, *, limit: int = 5) -> str:
    """Recordings BY a known artist. Far more precise than a free-text search, and the only
    way this project ever gets a Hebrew song_mbid — when MB happens to hold the recording."""
    query = f'arid:{arid} AND recording:"{title}"'
    return (f"https://musicbrainz.org/ws/2/recording?query={_q(query)}"
            f"&fmt=json&limit={limit}")


def mb_recording_candidates(payload: dict) -> list[dict]:
    out = []
    for rec in (payload.get("recordings") or []):
        if not rec.get("id"):
            continue
        out.append({
            "mbid": rec["id"],
            "title": rec.get("title") or "",
            "score": rec.get("score") or 0,
            "artist": stage5._credit_name(rec),
            "year": (rec.get("first-release-date") or "")[:4] or None,
        })
    return out


# --- iTunes Search API --------------------------------------------------------------------
# No key, no auth. `country` picks the storefront; it changes which catalogue is searched, so
# an Israeli release may only be visible with country=IL. Latin items use the default US
# storefront, which is where this library's Anglo-American bulk lives.

def itunes_search_url(term: str, *, country: str | None = None, limit: int = 10) -> str:
    url = (f"https://itunes.apple.com/search?term={_q(term)}"
           f"&media=music&entity=song&limit={limit}")
    if country:
        url += f"&country={country}"
    return url


def itunes_candidates(payload: dict) -> list[dict]:
    out = []
    for r in (payload.get("results") or []):
        if not (r.get("trackName") or r.get("artistName")):
            continue
        out.append({
            "artist": r.get("artistName") or "",
            "title": r.get("trackName") or "",
            "album": r.get("collectionName"),
            "year": (r.get("releaseDate") or "")[:4] or None,
            "genre": r.get("primaryGenreName"),
        })
    return out


# --- Deezer Search API ---------------------------------------------------------------------
# No key, no auth. Returns no year and no genre on the search endpoint (both would need
# further album/artist calls), so Deezer contributes corroboration and spelling only.

def deezer_search_url(term: str, *, limit: int = 10) -> str:
    return f"https://api.deezer.com/search?q={_q(term)}&limit={limit}"


def deezer_candidates(payload: dict) -> list[dict]:
    out = []
    for r in (payload.get("data") or []):
        title = r.get("title") or ""
        artist = ((r.get("artist") or {}).get("name")) or ""
        if not (title or artist):
            continue
        out.append({
            "artist": artist,
            "title": title,
            "album": ((r.get("album") or {}).get("title")),
            "year": None,
            "genre": None,
        })
    return out
