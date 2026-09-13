"""Stage 5 §9.1 — enrichment: MusicBrainz text search + AcoustID lookup.

Two passes, both over winners + sole copies only (§9.1):

* `enrich_all` — MB recording search on each item's v_metadata artist+title. Responses land
  in `mb_cache` (§3.10) so re-runs and §11 retunes never refetch. Acceptance bands (§9.1,
  thresholds in config):
    - HIGH  (score ≥ MB_AUTO_ACCEPT_SCORE and BOTH fields agree ≥ MB_FIELD_AGREEMENT):
      write `musicbrainz_text` rows (artist/title/song_mbid/year) — v_metadata re-ranks.
    - MEDIUM (best score ≥ MB_REVIEW_SCORE): queue `metadata_match` with top-3 candidates.
    - LOW: nothing written — filename metadata stands, re-derived from cache every run.
  Hebrew is searched as-is; a candidate whose title is in a different script than ours is a
  transliteration and is NEVER auto-accepted (§9.1) — review at most.
  Order ambiguity (§6.1 defers it here): when the straight query's best score is below the
  review band and both fields exist, the SWAPPED query (title↔artist) is tried too and the
  better direction wins. Accepting from the swapped query is safe because the values written
  are MusicBrainz's canonical fields, not our guess — the swap only steers the search.

* `acoustid_all` — AcoustID lookup per fingerprinted winner/sole blob (acoustic identity;
  karaoke covers are EXPECTED to miss, §3.4). Hits write `fingerprints.acoustid_*` and
  `musicbrainz_fp` metadata rows. Gated on ACOUSTID_API_KEY; our fingerprints are stored raw
  (`zb64:`, stage3), so `compress_acoustid` re-packs them into chromaprint's wire format.

Resumability needs no new state column:
  accepted items gain `song_mbid` → out of the worklist; queued items have an open mb-reason
  `metadata_match` row → skipped; LOW items stay in the worklist but every re-run classifies
  them from cache — deterministic, local, and exactly what §11's retune wants. Idempotence:
  a second run fetches nothing and writes nothing.

DEVIATION #16 (documented, deliberate) — mb_cache.response_json is stored compressed
(`zb64:` + base64(zlib(utf-8 JSON))): ~22k search responses ≈ hundreds of MB as plain text,
and every one of the 20 rotating §13 backups would carry a full copy. `cache_get` is the only
reader; plain-text rows (prefixless) are still accepted so the encoding can be revisited.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field

from .db import utcnow

CACHE_PREFIX = "zb64:"
# Retuned 2026-07-20 (5 attempts/62s cap -> 9/~4min): the live run hit the IDENTICAL
# "TLS/SSL connection has been closed (EOF)" failure twice in ~40 minutes, each time
# exhausting every attempt — meaning the outage on THIS host's network path outlasted a full
# minute, not a single dropped packet. curl succeeded seconds after each stop, so it is a
# genuine transient blip, not a block — the same class of intermittent connectivity this
# host already showed during Stage 2 downloads (quota deferrals, 5s->300s backoff). Mirrors
# that proven shape: more attempts, a longer capped backoff, bail only once a multi-minute
# outage is credible rather than a few seconds of bad luck.
FETCH_ATTEMPTS = 9
FETCH_BACKOFF_CAP_SEC = 60.0
FETCH_TIMEOUT_SEC = 30


class EnrichNetworkError(Exception):
    """Persistent network failure — stop the run (resumable), don't mark anything."""


# --- name normalization & agreement --------------------------------------------------------

_ARTICLE_TOKENS = {"the", "a", "an"}
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm_name(s: str | None) -> str:
    """Casefolded, punctuation-free, article-free token string for agreement scoring only.

    Dropping articles is safe HERE because this never feeds stored values — it only decides
    whether two already-existing names describe the same thing ('Beatles' vs 'The Beatles').
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold().replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    toks = [t for t in _WS_RE.split(s) if t and t not in _ARTICLE_TOKENS]
    return " ".join(toks)


def field_agreement(ours: str | None, theirs: str | None, *,
                    allow_containment: bool = False) -> float:
    """0..1 agreement between two names. Token overlap OR sequence ratio, whichever is kinder —
    tokens forgive reordering ('Murs, Olly'), the ratio forgives typos.

    `allow_containment` is for ARTIST only: 'Queen' vs 'Queen feat. David Bowie' is the same
    primary credit. Never for titles — 'Crazy' is contained in 'Crazy in Love' and is a
    different song.
    """
    a, b = norm_name(ours), norm_name(theirs)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    best = max(jacc, seq)
    if allow_containment and ta and tb and (ta <= tb or tb <= ta):
        best = max(best, 0.95)
    return best


_BRACKETED_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")   # balanced (…) or […]
_TRAILING_OPEN_RE = re.compile(r"[\(\[][^\)\]]*$")     # unbalanced tail: '… [karaok'


def base_title(s: str | None) -> str:
    """Title with bracketed qualifiers removed — '(live)', '(radio mix)', and our own
    filenames' truncated '[karaok' tails. The karaoke-purpose identity of 'Celebrity Skin'
    and 'Celebrity Skin (live)' is the same song; measured live (2026-07-20), version
    qualifiers were the #1 source of false AcoustID 'conflicts'. Same insight as stage1's
    weak-decorations-only-inside-brackets rule, applied at comparison time."""
    if not s:
        return ""
    s = _BRACKETED_RE.sub(" ", s)
    s = _TRAILING_OPEN_RE.sub(" ", s)
    return s.strip()


def title_agreement(ours: str | None, theirs: str | None) -> float:
    """field_agreement on titles, forgiving bracketed version qualifiers on either side —
    the kinder of the full and base comparisons."""
    full = field_agreement(ours, theirs)
    base = field_agreement(base_title(ours), base_title(theirs))
    return max(full, base)


def script_of(s: str | None) -> str:
    """'hebrew' / 'latin' / 'other' — the §9.1 transliteration guard's only question."""
    if s:
        for ch in s:
            if "֐" <= ch <= "׿":
                return "hebrew"
        for ch in s:
            if ("a" <= ch.lower() <= "z"):
                return "latin"
    return "other"


# --- MusicBrainz query construction --------------------------------------------------------

def _lucene_phrase(s: str) -> str:
    return '"' + s.replace("\\", r"\\").replace('"', r"\"") + '"'


def mb_search_url(artist: str | None, title: str, *, limit: int | None = None) -> str:
    from . import config
    q = f"recording:{_lucene_phrase(title)}"
    if artist:
        q += f" AND artist:{_lucene_phrase(artist)}"
    return (f"{config.MB_API_ROOT}/recording?query={urllib.parse.quote(q)}"
            f"&fmt=json&limit={limit if limit is not None else config.MB_SEARCH_LIMIT}")


# --- mb_cache (§3.10; deviation #16 encoding) ----------------------------------------------

def _cache_encode(text: str) -> str:
    return CACHE_PREFIX + base64.b64encode(zlib.compress(text.encode("utf-8"), 6)).decode("ascii")


def _cache_decode(stored: str) -> str:
    if stored.startswith(CACHE_PREFIX):
        return zlib.decompress(base64.b64decode(stored[len(CACHE_PREFIX):])).decode("utf-8")
    return stored


def cache_key(url: str, data: bytes | None = None) -> str:
    """POST requests (AcoustID) share one URL — the body is part of the request identity."""
    h = hashlib.sha256(url.encode("utf-8"))
    if data:
        h.update(b"\x00")
        h.update(data)
    return h.hexdigest()


def cache_get(conn, url: str, data: bytes | None = None) -> dict | None:
    row = conn.execute(
        "SELECT response_json FROM mb_cache WHERE query_hash=?", (cache_key(url, data),)
    ).fetchone()
    if row is None:
        return None
    return json.loads(_cache_decode(row["response_json"]))


def cache_put(conn, url: str, response_text: str, data: bytes | None = None) -> None:
    request = url if not data else f"{url} POST {data[:512].decode('ascii', 'replace')}"
    conn.execute(
        "INSERT INTO mb_cache (query_hash, request, response_json, fetched_at) "
        "VALUES (?,?,?,?) ON CONFLICT(query_hash) DO UPDATE "
        "SET response_json=excluded.response_json, fetched_at=excluded.fetched_at",
        (cache_key(url, data), request, _cache_encode(response_text), utcnow()),
    )


# --- rate-limited fetching -----------------------------------------------------------------

class MbClient:
    """One long-lived rate gate for the whole run. 503 = MB says slow down: back off and
    retry; other 4xx = our bug: raise immediately; repeated network failure = stop the run."""

    def __init__(self, rate_sec: float | None = None):
        from . import config
        self.rate_sec = config.MB_RATE_LIMIT_SEC if rate_sec is None else rate_sec
        self._next_ok = 0.0
        self.fetches = 0
        self.waited_sec = 0.0

    def fetch(self, url: str, *, data: bytes | None = None) -> str:
        from . import config
        last_err: str = ""
        for attempt in range(FETCH_ATTEMPTS):
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
                self.waited_sec += wait
            self._next_ok = time.monotonic() + self.rate_sec
            req = urllib.request.Request(
                url, data=data, headers={"User-Agent": config.mb_user_agent()}
            )
            try:
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
                    self.fetches += 1
                    return resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                if exc.code == 503:
                    retry_after = 0.0
                    try:
                        retry_after = float(exc.headers.get("Retry-After") or 0)
                    except ValueError:
                        pass
                    backoff = max(min(2.0 ** (attempt + 1), FETCH_BACKOFF_CAP_SEC), retry_after)
                    last_err = f"HTTP 503 (backoff {backoff:.0f}s)"
                    time.sleep(backoff)
                    continue
                raise  # 4xx = malformed request = a bug here, not weather
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = str(exc)
                time.sleep(min(2.0 ** (attempt + 1), FETCH_BACKOFF_CAP_SEC))
        raise EnrichNetworkError(f"{url}: {last_err}")


def cached_fetch(conn, client: MbClient, url: str, *, data: bytes | None = None,
                 counters: dict | None = None) -> dict:
    cached = cache_get(conn, url, data)
    if cached is not None:
        if counters is not None:
            counters["cache_hits"] = counters.get("cache_hits", 0) + 1
        return cached
    text = client.fetch(url, data=data)
    parsed = json.loads(text)  # cache only what parses — garbage is refetchable weather
    cache_put(conn, url, text, data)
    conn.commit()
    if counters is not None:
        counters["fetched"] = counters.get("fetched", 0) + 1
    return parsed


# --- classification (pure, test-pinned) ----------------------------------------------------

@dataclass
class Candidate:
    mbid: str
    score: int
    artist: str
    title: str
    year: str | None
    artist_sim: float
    title_sim: float
    transliterated: bool

    def as_payload(self) -> dict:
        return {
            "mbid": self.mbid, "score": self.score, "artist": self.artist,
            "title": self.title, "year": self.year,
            "artist_sim": round(self.artist_sim, 3), "title_sim": round(self.title_sim, 3),
            "transliterated": self.transliterated,
        }


def _credit_name(rec: dict) -> str:
    parts = []
    for credit in rec.get("artist-credit") or []:
        if isinstance(credit, dict):
            parts.append(credit.get("name") or "")
            parts.append(credit.get("joinphrase") or "")
        else:  # MB sometimes emits bare joinphrase strings
            parts.append(str(credit))
    return "".join(parts).strip()


def _first_credit(rec: dict) -> str:
    for credit in rec.get("artist-credit") or []:
        if isinstance(credit, dict) and credit.get("name"):
            return credit["name"]
    return ""


def _year_of(rec: dict) -> str | None:
    date = rec.get("first-release-date") or ""
    return date[:4] if re.fullmatch(r"(19|20)\d\d", date[:4]) else None


def evaluate_candidates(our_artist: str | None, our_title: str,
                        recordings: list[dict]) -> list[Candidate]:
    our_script = script_of(our_title)
    out = []
    for rec in recordings:
        if not rec.get("id") or not rec.get("title"):
            continue
        cand_artist = _credit_name(rec)
        # take the kinder of full-credit vs first-credit: 'Queen feat. X' should not sink
        # a correct 'Queen' match. Containment is allowed for the artist field only.
        a_sim = 0.0
        if our_artist:
            a_sim = max(
                field_agreement(our_artist, cand_artist, allow_containment=True),
                field_agreement(our_artist, _first_credit(rec), allow_containment=True),
            )
        t_sim = field_agreement(our_title, rec["title"])
        out.append(Candidate(
            mbid=rec["id"], score=int(rec.get("score") or 0),
            artist=cand_artist, title=rec["title"], year=_year_of(rec),
            artist_sim=a_sim, title_sim=t_sim,
            transliterated=(our_script != script_of(rec["title"])),
        ))
    return out


@dataclass
class Decision:
    kind: str                       # 'accept' / 'review' / 'none'
    reason: str
    accepted: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)


def classify(our_artist: str | None, our_title: str, recordings: list[dict]) -> Decision:
    """§9.1 acceptance bands over one search response. Pure; thresholds from config."""
    from . import config
    cands = evaluate_candidates(our_artist, our_title, recordings)
    if not cands:
        return Decision("none", "no_candidates")
    cands.sort(key=lambda c: (c.score, c.title_sim + c.artist_sim), reverse=True)
    best = cands[0]

    if our_artist:
        for c in cands:
            if (c.score >= config.MB_AUTO_ACCEPT_SCORE
                    and c.artist_sim >= config.MB_FIELD_AGREEMENT
                    and c.title_sim >= config.MB_FIELD_AGREEMENT
                    and not c.transliterated):
                return Decision("accept", "high_score_both_fields", accepted=c,
                                candidates=cands[:3])
        if best.score >= config.MB_REVIEW_SCORE:
            return Decision("review", "medium_band", candidates=cands[:3])
        return Decision("none", "low_score", candidates=cands[:3])

    # title-only: both-field agreement is impossible, so auto-accept is impossible (§9.1);
    # only a near-perfect match is worth an operator's time.
    if best.score >= config.MB_TITLE_ONLY_REVIEW_SCORE \
            and best.title_sim >= config.MB_FIELD_AGREEMENT:
        return Decision("review", "title_only_high", candidates=cands[:3])
    return Decision("none", "title_only_low", candidates=cands[:3])


# --- worklist ------------------------------------------------------------------------------

def materialize_metadata(conn) -> None:
    """v_metadata into a temp table, ONCE per run.

    This docstring used to justify the temp table with "correlated per-item subqueries
    against it measured in HOURS on the live DB". **That does not reproduce, and the claim
    has been withdrawn.** Re-measured 2026-07-27, read-only against the live DB (24,850
    winner/sole items, 206,113 song_metadata rows): the worklist-shaped query — TWO
    correlated v_metadata subqueries per item, both in the WHERE clause so neither can be
    optimised away, across every item with no LIMIT — completes in **0.74s**. A scaling
    sweep (10/50/200 items) came back flat at ~0.5s, i.e. the cost is materialising the
    view once, not per item. Whatever produced the original "hours" observation, it was
    not this query shape.

    The temp table is KEPT regardless: at 1.5s it is not worth the churn to remove, one
    materialisation is still cheaper than many, and every caller below is written against
    it. But do not cite the hours claim as a reason to avoid v_metadata in new code —
    querying the view directly is fine. See karaokemp/titlecard.py's worklist for the
    measurements."""
    conn.execute("DROP TABLE IF EXISTS temp.enrich_meta")
    conn.execute(
        """
        CREATE TEMP TABLE enrich_meta AS
        SELECT i.id AS item_id,
               MAX(CASE WHEN v.field='artist'    THEN v.value END) AS artist,
               MAX(CASE WHEN v.field='title'     THEN v.value END) AS title,
               MAX(CASE WHEN v.field='song_mbid' THEN v.value END) AS song_mbid
        FROM media_items i
        LEFT JOIN v_metadata v ON v.media_item_id = i.id
        WHERE i.status='active' AND i.quality_verdict IN ('winner','sole_copy')
        GROUP BY i.id
        """
    )


def enrich_worklist(conn, limit: int | None = None) -> list:
    """Winner/sole items with a searchable title, no song_mbid yet, and no open mb review.
    Items with an open id3-disagreement review are deliberately IN the worklist — MB is
    exactly the referee that disagreement is waiting for.

    Title-only items sort LAST (benchmark 2026-07-20: ~75% of them queue review — they are
    the review-queue's dominant driver, projecting past the §11 budget on their own). Doing
    them after every artist+title item means a budget stop can only ever cut into the
    title-only tail, never block the well-labelled majority; a re-run after the operator
    drains the queue resumes exactly there."""
    rows = conn.execute(
        """
        SELECT m.item_id, m.artist, m.title FROM temp.enrich_meta m
        WHERE m.title IS NOT NULL AND m.song_mbid IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM review_queue r
              WHERE r.kind='metadata_match' AND r.media_item_id=m.item_id
                AND r.resolution IS NULL
                AND json_extract(r.payload, '$.reason') LIKE 'mb_%')
          AND NOT EXISTS (
              -- §10: an operator who RESOLVED an mb review as none/ours (or filename/id3)
              -- has said "stop asking" — re-queuing it every enrich run would re-page a
              -- settled question. A correction that must re-enter MB search carries
              -- resolution.reenter=true (fixed_* fixes) and is deliberately NOT excluded here.
              SELECT 1 FROM review_queue r
              WHERE r.kind='metadata_match' AND r.media_item_id=m.item_id
                AND r.resolution IS NOT NULL
                AND json_extract(r.payload, '$.reason') LIKE 'mb_%'
                AND json_extract(r.resolution, '$.verdict') IN ('none','ours','filename','id3')
                AND COALESCE(json_extract(r.resolution, '$.reenter'), 0) = 0)
        ORDER BY (m.artist IS NULL), m.item_id
        """
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def count_unsearchable(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM temp.enrich_meta WHERE title IS NULL"
    ).fetchone()["n"]


# --- the MB text-search pass ---------------------------------------------------------------

@dataclass
class EnrichReport:
    worklist: int = 0
    accepted: int = 0
    reviewed: int = 0
    filename_stands: int = 0
    swapped_wins: int = 0
    transliteration_blocked: int = 0   # would have auto-accepted but for the script guard
    unsearchable: int = 0
    cache_hits: int = 0
    fetched: int = 0
    fields_written: int = 0
    over_budget: bool = False
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    reasons: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def _blocked_by_transliteration(cands: list[Candidate]) -> bool:
    """Diagnostic: a candidate cleared every auto-accept bar EXCEPT the script guard."""
    from . import config
    return any(c.score >= config.MB_AUTO_ACCEPT_SCORE
               and c.artist_sim >= config.MB_FIELD_AGREEMENT
               and c.title_sim >= config.MB_FIELD_AGREEMENT
               and c.transliterated for c in cands)


def _accept(conn, item_id: int, cand: Candidate, report: EnrichReport,
            *, swapped: bool) -> None:
    conf = round((cand.score / 100.0) * min(cand.artist_sim, cand.title_sim), 3)
    now = utcnow()
    values = [("artist", cand.artist), ("title", cand.title), ("song_mbid", cand.mbid)]
    if cand.year:
        values.append(("year", cand.year))
    for fld, val in values:
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
            "updated_at) VALUES (?,?,?,'musicbrainz_text',?,?) "
            "ON CONFLICT(media_item_id, field, source) DO UPDATE "
            "SET value=excluded.value, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (item_id, fld, val, conf, now),
        )
        report.fields_written += 1
    report.accepted += 1
    if swapped:
        report.swapped_wins += 1


def _queue_review(conn, item_id: int, our_artist, our_title, decision: Decision,
                  *, swapped: bool) -> None:
    conn.execute(
        "INSERT INTO review_queue (kind, media_item_id, payload, created_at) "
        "VALUES ('metadata_match', ?, ?, ?)",
        (item_id, json.dumps({
            "reason": f"mb_{decision.reason}",
            "query": {"artist": our_artist, "title": our_title, "swapped": swapped},
            "candidates": [c.as_payload() for c in decision.candidates],
        }, ensure_ascii=False), utcnow()),
    )


def enrich_item(conn, client: MbClient, item_id: int, artist: str | None, title: str,
                report: EnrichReport, counters: dict) -> None:
    from . import config
    resp = cached_fetch(conn, client, mb_search_url(artist, title), counters=counters)
    decision = classify(artist, title, resp.get("recordings") or [])
    swapped = False

    # §6.1's order ambiguity lands here: if the straight reading convinced nobody, ask MB
    # about the swapped reading and let the scores referee. Only when straight is below the
    # review band — a straight-medium match is evidence the order was right.
    best_straight = max((c.score for c in decision.candidates), default=0)
    if artist and decision.kind == "none" and best_straight < config.MB_REVIEW_SCORE:
        resp2 = cached_fetch(conn, client, mb_search_url(title, artist), counters=counters)
        decision2 = classify(title, artist, resp2.get("recordings") or [])
        rank = {"accept": 2, "review": 1, "none": 0}
        if rank[decision2.kind] > rank[decision.kind]:
            decision, swapped = decision2, True
            artist, title = title, artist

    if decision.kind == "accept":
        _accept(conn, item_id, decision.accepted, report, swapped=swapped)
    elif decision.kind == "review":
        _queue_review(conn, item_id, artist, title, decision, swapped=swapped)
        report.reviewed += 1
    else:
        report.filename_stands += 1
        if decision.candidates and _blocked_by_transliteration(decision.candidates):
            report.transliteration_blocked += 1
    report.reasons[decision.reason] = report.reasons.get(decision.reason, 0) + 1
    conn.commit()


def enrich_all(conn, *, limit: int | None = None, budget: int | None = None,
               client: MbClient | None = None, progress_every: int = 50,
               on_progress=None) -> EnrichReport:
    """One §9.1 text-search pass. Per-item commits; budget stop on metadata_match; a
    persistent network failure stops the run resumably instead of misclassifying."""
    from . import config
    budget = config.REVIEW_QUEUE_BUDGET if budget is None else budget
    client = client or MbClient()
    materialize_metadata(conn)
    worklist = enrich_worklist(conn, limit)
    report = EnrichReport(worklist=len(worklist), unsearchable=count_unsearchable(conn))
    counters: dict = {}
    open_reviews = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue "
        "WHERE kind='metadata_match' AND resolution IS NULL"
    ).fetchone()["n"]
    started = time.monotonic()
    try:
        for i, row in enumerate(worklist, 1):
            if open_reviews > budget:
                report.over_budget = True
                report.stopped_reason = "review_budget"
                break
            queued_before = report.reviewed
            enrich_item(conn, client, row["item_id"], row["artist"], row["title"],
                        report, counters)
            open_reviews += report.reviewed - queued_before
            if on_progress and (i % progress_every == 0 or i == len(worklist)):
                report.cache_hits = counters.get("cache_hits", 0)
                report.fetched = counters.get("fetched", 0)
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    except EnrichNetworkError as exc:
        report.stopped_reason = f"network: {exc}"
    report.cache_hits = counters.get("cache_hits", 0)
    report.fetched = counters.get("fetched", 0)
    report.elapsed_sec = time.monotonic() - started
    return report


def retune_enrichment(conn) -> dict:
    """§11 retune: drop this pass's own outputs (musicbrainz_text rows and UNRESOLVED
    mb-reason reviews — resolved ones are operator verdicts and are kept), so the next
    `enrich` run re-derives everything from mb_cache with the current thresholds."""
    cur = conn.execute("DELETE FROM song_metadata WHERE source='musicbrainz_text'")
    meta_deleted = cur.rowcount
    cur = conn.execute(
        "DELETE FROM review_queue WHERE kind='metadata_match' AND resolution IS NULL "
        "AND json_extract(payload, '$.reason') LIKE 'mb_%'"
    )
    reviews_deleted = cur.rowcount
    conn.commit()
    return {"musicbrainz_text_rows_deleted": meta_deleted,
            "open_mb_reviews_deleted": reviews_deleted}


# --- benchmark (phase gate, house style §6.2/§7.3) -----------------------------------------

def enrich_benchmark(conn, *, sample: int = 200, client: MbClient | None = None,
                     on_progress=None) -> dict:
    """Enrich a stratified random sample (Hebrew oversampled — its behavior differs most) and
    project the review-queue burden and runtime BEFORE the full run. Writes real results;
    the pass is resumable, so benchmark work is never wasted."""
    import random
    client = client or MbClient()
    materialize_metadata(conn)
    worklist = enrich_worklist(conn)
    hebrew = [r for r in worklist if script_of(r["title"]) == "hebrew"]
    other = [r for r in worklist if script_of(r["title"]) != "hebrew"]
    rng = random.Random(9)
    take_h = min(len(hebrew), max(sample // 4, 10))
    take_o = min(len(other), sample - take_h)
    picked = rng.sample(hebrew, take_h) + rng.sample(other, take_o)

    report = EnrichReport(worklist=len(picked))
    counters: dict = {}
    per_stratum = {"hebrew": {"n": 0, "accepted": 0, "reviewed": 0, "none": 0},
                   "other": {"n": 0, "accepted": 0, "reviewed": 0, "none": 0}}
    t0 = time.monotonic()
    stopped = None
    try:
        for i, row in enumerate(picked, 1):
            before = (report.accepted, report.reviewed, report.filename_stands)
            enrich_item(conn, client, row["item_id"], row["artist"], row["title"],
                        report, counters)
            stratum = "hebrew" if script_of(row["title"]) == "hebrew" else "other"
            s = per_stratum[stratum]
            s["n"] += 1
            s["accepted"] += report.accepted - before[0]
            s["reviewed"] += report.reviewed - before[1]
            s["none"] += report.filename_stands - before[2]
            if on_progress and i % 20 == 0:
                report.elapsed_sec = time.monotonic() - t0
                on_progress(report)
    except (KeyboardInterrupt, EnrichNetworkError) as exc:
        stopped = str(exc) or "interrupted"
    elapsed = time.monotonic() - t0
    fetched = counters.get("fetched", 0)
    n = report.accepted + report.reviewed + report.filename_stands
    remaining = len(worklist) - n
    proj: dict = {
        "sample": n, "elapsed_sec": round(elapsed, 1),
        "sec_per_item": round(elapsed / n, 2) if n else None,
        "fetched": fetched, "cache_hits": counters.get("cache_hits", 0),
        "fetches_per_item": round(fetched / n, 2) if n else None,
        "accepted": report.accepted, "reviewed": report.reviewed,
        "filename_stands": report.filename_stands,
        "transliteration_blocked": report.transliteration_blocked,
        "swapped_wins": report.swapped_wins,
        "reasons": report.reasons, "per_stratum": per_stratum,
        "remaining": remaining, "stopped": stopped,
    }
    if n:
        proj["projected_review_queue"] = round(report.reviewed / n * len(worklist))
        proj["projected_hours_remaining"] = round(remaining * (elapsed / n) / 3600, 1)
    return proj


# --- AcoustID (§9.1 corroboration; §3.4 acoustic identity) ---------------------------------

def compress_acoustid(ints: list[int], algorithm: int = 1) -> str:
    """Raw uint32 fingerprint → chromaprint's compressed wire format (what the AcoustID API
    takes and what `fpcalc` without -raw prints). Round-trip-tested against fpcalc."""
    normal: list[int] = []      # 3-bit stream; 7 = escape to the 5-bit stream
    exceptional: list[int] = []  # 5-bit stream of (value - 7)
    last = 0
    for v in ints:
        x = (v ^ last) & 0xFFFFFFFF
        last = v & 0xFFFFFFFF
        bit, last_bit = 1, 0
        while x:
            if x & 1:
                d = bit - last_bit
                last_bit = bit
                if d >= 7:
                    normal.append(7)
                    exceptional.append(d - 7)
                else:
                    normal.append(d)
            x >>= 1
            bit += 1
        normal.append(0)

    def _pack(values: list[int], width: int) -> bytes:
        out = bytearray()
        buf = size = 0
        for val in values:
            buf |= val << size
            size += width
            while size >= 8:
                out.append(buf & 0xFF)
                buf >>= 8
                size -= 8
        if size:
            out.append(buf & 0xFF)
        return bytes(out)

    n = len(ints)
    payload = (bytes([algorithm & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])
               + _pack(normal, 3) + _pack(exceptional, 5))
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


@dataclass
class AcoustidReport:
    worklist: int = 0
    checked: int = 0
    hits: int = 0
    conflicts: int = 0     # same-artist/different-title hit → review, identity recorded
    junk: int = 0          # high-score hit where NOTHING agrees → polluted fp cluster, ignored
    misses: int = 0
    fields_written: int = 0
    over_budget: bool = False
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def acoustid_worklist(conn, limit: int | None = None) -> list:
    """Fingerprinted defining blobs of winner/sole items, not yet checked. Duration from the
    item (full media length) — fp_duration_sec is capped at fpcalc's analysis window and is
    NOT the track length AcoustID wants. Carries the item's current artist/title (from the
    materialized metadata snapshot) for recording selection and conflict detection."""
    rows = conn.execute(
        """
        SELECT DISTINCT fp.content_hash, fp.chromaprint, i.id AS item_id,
               COALESCE(i.duration_sec, fp.fp_duration_sec) AS duration_sec,
               m.artist, m.title
        FROM media_items i
        JOIN media_item_files f ON f.media_item_id = i.id AND f.role IN ('audio','av')
        JOIN fingerprints fp ON fp.content_hash = f.content_hash
        LEFT JOIN temp.enrich_meta m ON m.item_id = i.id
        WHERE i.status='active' AND i.quality_verdict IN ('winner','sole_copy')
          AND fp.acoustid_checked_at IS NULL
          AND NOT EXISTS (
              -- §10: mirror enrich_worklist. A resolved acoustid_conflict (verdict 'ours'
              -- says keep our metadata) must not re-page even if a retune cleared checked_at.
              SELECT 1 FROM review_queue r
              WHERE r.kind='metadata_match' AND r.media_item_id=i.id
                AND r.resolution IS NOT NULL
                AND json_extract(r.payload, '$.reason') LIKE 'mb_%'
                AND json_extract(r.resolution, '$.verdict') IN ('none','ours','filename','id3')
                AND COALESCE(json_extract(r.resolution, '$.reenter'), 0) = 0)
        ORDER BY fp.content_hash
        """
    ).fetchall()
    return rows[:limit] if limit is not None else rows


def choose_acoustid_recording(result: dict, our_artist: str | None,
                              our_title: str | None) -> tuple[dict | None, float, float]:
    """One AcoustID result routinely lists SEVERAL recordings for the same fingerprint —
    rereleases, compilations, and junk submissions alike. Taking the first is arbitrary;
    take the one that best agrees with our current metadata, and report its agreement so
    the caller can tell a corroborating hit from a contradicting one."""
    best, best_a, best_t, best_key = None, 0.0, 0.0, -1.0
    for rec in result.get("recordings") or []:
        if not rec.get("id"):
            continue
        artists = ", ".join(a.get("name", "") for a in rec.get("artists") or []
                            if a.get("name"))
        a_sim = field_agreement(our_artist, artists, allow_containment=True)
        t_sim = title_agreement(our_title, rec.get("title"))
        if a_sim + t_sim > best_key:
            best, best_a, best_t, best_key = rec, a_sim, t_sim, a_sim + t_sim
    return best, best_a, best_t


def acoustid_all(conn, *, limit: int | None = None, budget: int | None = None,
                 client: MbClient | None = None,
                 progress_every: int = 100, on_progress=None) -> AcoustidReport:
    """AcoustID per fingerprinted winner/sole blob. A hit whose best-agreeing recording still
    CONTRADICTS our metadata (neither field agrees) records the acoustic fact on
    `fingerprints` but writes NO musicbrainz_fp rows — it queues `metadata_match`
    (reason acoustid_conflict) instead. A contradicting hit is either a mislabeled file or an
    AcoustID junk submission, and silently flipping the display metadata on a coin-toss is a
    §1.2 guess; the id3 pass set this precedent for disagreements. Shares the §11
    metadata_match budget with the text pass."""
    from . import config
    from .stage3 import decode_fp
    if not config.ACOUSTID_API_KEY:
        return AcoustidReport(stopped_reason="no_api_key")
    budget = config.REVIEW_QUEUE_BUDGET if budget is None else budget
    client = client or MbClient(rate_sec=config.ACOUSTID_RATE_LIMIT_SEC)
    materialize_metadata(conn)
    worklist = acoustid_worklist(conn, limit)
    report = AcoustidReport(worklist=len(worklist))
    open_reviews = conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue "
        "WHERE kind='metadata_match' AND resolution IS NULL"
    ).fetchone()["n"]
    started = time.monotonic()
    try:
        for i, row in enumerate(worklist, 1):
            if open_reviews > budget:
                report.over_budget = True
                report.stopped_reason = "review_budget"
                break
            fp = compress_acoustid([int(x) for x in decode_fp(row["chromaprint"])])
            body = urllib.parse.urlencode({
                "client": config.ACOUSTID_API_KEY, "format": "json",
                "duration": int(row["duration_sec"] or 0),
                "fingerprint": fp, "meta": "recordings",
            }).encode("ascii")
            resp = cached_fetch(conn, client, config.ACOUSTID_API_ROOT, data=body)
            best_result, best_score = None, 0.0
            for result in resp.get("results") or []:
                score = float(result.get("score") or 0)
                if score > best_score and (result.get("recordings") or []):
                    best_result, best_score = result, score
            now = utcnow()
            rec = a_sim = t_sim = None
            if best_result is not None and best_score >= config.ACOUSTID_MIN_SCORE:
                rec, a_sim, t_sim = choose_acoustid_recording(
                    best_result, row["artist"], row["title"])
            if rec is not None:
                conn.execute(
                    "UPDATE fingerprints SET acoustid_checked_at=? WHERE content_hash=?",
                    (now, row["content_hash"]))
                artists = ", ".join(a.get("name", "") for a in rec.get("artists") or []
                                    if a.get("name"))
                # The TITLE is what decides corroboration. Artist-agreement alone is weak — a
                # band has many songs, and "same artist, different title" (seen live: a
                # Members file where AcoustID's first-listed recording was a different
                # Members song) is precisely the mislabel-vs-junk coin toss that needs ears.
                # A differing ARTIST under an agreeing title is fine: acoustically matching
                # the original recording of a differently-credited cover IS the fp's job.
                has_own_metadata = bool(row["artist"] or row["title"])
                corroborates = (
                    not has_own_metadata
                    or (row["title"] is not None and t_sim >= config.MB_FIELD_AGREEMENT)
                    # title-less item: the artist is the only checkable field left
                    or (row["title"] is None and a_sim >= config.MB_FIELD_AGREEMENT))
                if corroborates:
                    conn.execute(
                        "UPDATE fingerprints SET acoustid_recording_mbid=?, acoustid_score=? "
                        "WHERE content_hash=?",
                        (rec["id"], round(best_score, 3), row["content_hash"]))
                    report.hits += 1
                    values = [("song_mbid", rec["id"])]
                    # The title row is written only on FULL agreement. When only the BASE
                    # titles agree, the MB title carries a version qualifier ('(live)',
                    # '(radio mix)') — identity is corroborated, but replacing a clean
                    # display title with the qualified one would make v_metadata worse.
                    if rec.get("title") and field_agreement(
                            row["title"], rec["title"]) >= config.MB_FIELD_AGREEMENT:
                        values.append(("title", rec["title"]))
                    if artists:
                        values.append(("artist", artists))
                    for fld, val in values:
                        conn.execute(
                            "INSERT INTO song_metadata (media_item_id, field, value, source, "
                            "confidence, updated_at) VALUES (?,?,?,'musicbrainz_fp',?,?) "
                            "ON CONFLICT(media_item_id, field, source) DO UPDATE "
                            "SET value=excluded.value, confidence=excluded.confidence, "
                            "updated_at=excluded.updated_at",
                            (row["item_id"], fld, val, round(best_score, 3), now))
                        report.fields_written += 1
                elif a_sim >= config.MB_FIELD_AGREEMENT:
                    # plausible mislabel: same artist, different song — worth an operator's
                    # ears. The acoustic claim is credible (the artist anchors it), so the
                    # identity columns are recorded alongside the review.
                    conn.execute(
                        "UPDATE fingerprints SET acoustid_recording_mbid=?, acoustid_score=? "
                        "WHERE content_hash=?",
                        (rec["id"], round(best_score, 3), row["content_hash"]))
                    report.conflicts += 1
                    conn.execute(
                        "INSERT INTO review_queue (kind, media_item_id, payload, created_at) "
                        "VALUES ('metadata_match', ?, ?, ?)",
                        (row["item_id"], json.dumps({
                            "reason": "mb_acoustid_conflict",
                            "ours": {"artist": row["artist"], "title": row["title"]},
                            "acoustid": {"recording_mbid": rec["id"], "title": rec.get("title"),
                                         "artist": artists,
                                         "score": round(best_score, 3),
                                         "artist_sim": round(a_sim, 3),
                                         "title_sim": round(t_sim, 3)},
                        }, ensure_ascii=False), now))
                    open_reviews += 1
                else:
                    # NEITHER field agrees ⇒ a polluted AcoustID fingerprint cluster, not a
                    # mislabel. Measured live (2026-07-20): a 64-row sample of these was
                    # audiobook tracks ('Stephen King / Track 9') and mass-submission junk at
                    # scores ≥0.94. Not reviewable by a human (the verdict is always
                    # 'obviously junk'), and recording the mbid would poison acoustic
                    # identity for §12. Claim nothing; the cached response keeps the
                    # evidence, so any retune can revisit for free.
                    report.junk += 1
            else:
                conn.execute(
                    "UPDATE fingerprints SET acoustid_checked_at=? WHERE content_hash=?",
                    (now, row["content_hash"]))
                report.misses += 1
            report.checked += 1
            conn.commit()
            if on_progress and (i % progress_every == 0 or i == len(worklist)):
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"
    except EnrichNetworkError as exc:
        report.stopped_reason = f"network: {exc}"
    report.elapsed_sec = time.monotonic() - started
    return report
