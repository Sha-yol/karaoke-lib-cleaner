"""Stage 5 §9.1 enrichment invariant tests (no live network — a FakeClient serves canned
responses; the only external tool touched is fpcalc, for the AcoustID compressor round-trip,
and that test skips itself if fpcalc or a sample file is unavailable). Pinned:

  * agreement scoring: articles/commas/feat-credits forgiven; title containment NEVER
    accepted ('Crazy' is not 'Crazy in Love');
  * §9.1 bands: high+both-fields ⇒ accept; medium ⇒ review (top-3); low ⇒ filename stands;
  * the transliteration guard: a perfect-scoring Latin match for a Hebrew query is never
    auto-accepted;
  * title-only items can never auto-accept;
  * mb_cache round-trip (compressed + legacy plain), POST body in the cache key;
  * enrich_all end-to-end on a fixture DB: accepted items leave the worklist, second run
    fetches nothing and changes nothing (idempotence), review rows are never duplicated,
    §11 budget stops the run, v_metadata still serves higher-trust sources over mb rows;
  * the swapped-order fallback resolves a Title↔Artist filename;
  * retune deletes ONLY mb outputs (open mb reviews, mb_text rows) — resolved reviews and
    id3-reason reviews survive;
  * acoustid: gated on the key; a hit writes fingerprints.acoustid_* + musicbrainz_fp rows;
  * compress_acoustid matches fpcalc's own compressed output byte-for-byte.

Run: python3 tests/test_stage5_enrich.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
from pathlib import Path

from karaokemp import config, db, stage5


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


def _add_item(conn, *, artist=None, title=None, verdict="sole_copy", fmt="mp3g",
              file_conf=0.8, dur=200.0):
    _seq[0] += 1
    n = _seq[0]
    sha = f"sha{n:04d}"
    conn.execute(
        "INSERT INTO blobs (content_hash, size_bytes, integrity_status) "
        "VALUES (?, 1000, 'decoded_ok')", (sha,))
    cur = conn.execute(
        "INSERT INTO media_items (format, duration_sec, quality_verdict) VALUES (?,?,?)",
        (fmt, dur, verdict))
    item_id = cur.lastrowid
    conn.execute(
        "INSERT INTO media_item_files (media_item_id, content_hash, role) VALUES (?,?,?)",
        (item_id, sha, "av" if fmt == "video" else "audio"))
    now = db.utcnow()
    for fld, val in (("artist", artist), ("title", title)):
        if val is not None:
            conn.execute(
                "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
                "updated_at) VALUES (?,?,?,'filename',?,?)",
                (item_id, fld, val, file_conf, now))
    conn.commit()
    return item_id, sha


def _rec(mbid, score, artist, title, year=None):
    rec = {"id": mbid, "score": score, "title": title,
           "artist-credit": [{"name": artist}]}
    if year:
        rec["first-release-date"] = f"{year}-01-01"
    return rec


class FakeClient:
    """Canned responses keyed on the decoded (artist, title) of the search URL."""

    def __init__(self, responses):
        self.responses = responses   # {(artist_or_None, title): [recording dicts]}
        self.fetches = 0
        self.urls = []

    def fetch(self, url, *, data=None):
        self.fetches += 1
        self.urls.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        # crude un-Lucene: recording:"T" [AND artist:"A"]
        parts = q.split(' AND artist:"')
        title = parts[0][len('recording:"'):-1]
        artist = parts[1][:-1] if len(parts) > 1 else None
        recs = self.responses.get((artist, title), [])
        return json.dumps({"count": len(recs), "recordings": recs})


# --- pure-function invariants ---------------------------------------------------------------

def test_agreement_scoring():
    check(stage5.field_agreement("The Beatles", "Beatles") == 1.0, "article must be forgiven")
    check(stage5.field_agreement("Murs, Olly", "Olly Murs") == 1.0, "comma inversion")
    check(stage5.field_agreement("Queen", "Queen feat. David Bowie",
                                 allow_containment=True) >= 0.95, "feat credit (artist)")
    check(stage5.field_agreement("Crazy", "Crazy in Love") < config.MB_FIELD_AGREEMENT,
          "title containment must NOT read as agreement")
    check(stage5.field_agreement("", "x") == 0.0 and stage5.field_agreement(None, "x") == 0.0)
    check(stage5.field_agreement("שלמה ארצי", "שלמה ארצי") == 1.0, "hebrew exact")


def test_script_of():
    check(stage5.script_of("שיר של יום") == "hebrew")
    check(stage5.script_of("Bohemian Rhapsody") == "latin")
    check(stage5.script_of("1234") == "other" and stage5.script_of(None) == "other")
    check(stage5.script_of("שיר (Live)") == "hebrew", "mixed leans hebrew — hebrew wins")


def test_mb_search_url_escaping():
    url = stage5.mb_search_url('AC/DC', 'She said "no"')
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
    check(r'\"no\"' in q, "quotes must be Lucene-escaped")
    check('artist:"AC/DC"' in q)


def test_classify_bands():
    ours = ("Queen", "Bohemian Rhapsody")
    accept = stage5.classify(*ours, [_rec("m1", 100, "Queen", "Bohemian Rhapsody", 1975)])
    check(accept.kind == "accept" and accept.accepted.mbid == "m1")
    check(accept.accepted.year == "1975")

    medium = stage5.classify(*ours, [_rec("m2", 85, "Queen", "Bohemian Rhapsody")])
    check(medium.kind == "review" and len(medium.candidates) == 1)

    low = stage5.classify(*ours, [_rec("m3", 40, "Somebody", "Something Else")])
    check(low.kind == "none" and low.candidates, "none must still expose candidates")

    # high score but the wrong song: both-field agreement is the gate, not score alone
    wrong = stage5.classify(*ours, [_rec("m4", 99, "Queen", "Killer Queen")])
    check(wrong.kind != "accept", "field disagreement must block auto-accept")


def test_transliteration_guard():
    heb = stage5.classify("שלמה ארצי", "ירח", [_rec("m1", 100, "Shlomo Artzi", "Yareach")])
    check(heb.kind != "accept", "Latin match for Hebrew query must never auto-accept")
    # same-script Hebrew candidate IS acceptable
    heb2 = stage5.classify("שלמה ארצי", "ירח", [_rec("m2", 100, "שלמה ארצי", "ירח")])
    check(heb2.kind == "accept")


def test_title_only_never_accepts():
    d = stage5.classify(None, "Bohemian Rhapsody",
                        [_rec("m1", 100, "Queen", "Bohemian Rhapsody")])
    check(d.kind != "accept", "no artist ⇒ no both-field agreement ⇒ no auto-accept")
    check(d.kind == "review", "a 100-score title match is worth review")
    d2 = stage5.classify(None, "Bohemian Rhapsody",
                         [_rec("m2", 88, "Queen", "Bohemian Rhapsody")])
    check(d2.kind == "none", "title-only below the high bar stands on filename")


# --- retry/backoff (retuned 2026-07-20 after two live TLS-EOF stops) -----------------------

def test_mbclient_retries_and_caps_backoff_then_raises():
    """A sustained network failure must exhaust FETCH_ATTEMPTS with each sleep capped at
    FETCH_BACKOFF_CAP_SEC, then raise EnrichNetworkError (never silently give up early or
    spin forever) — pins the 2026-07-20 retune (5/62s -> 9/~4min) against regression."""
    sleeps = []
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("TLS/SSL connection has been closed (EOF)")

    client = stage5.MbClient(rate_sec=0.0)
    old_sleep, old_urlopen = time.sleep, stage5.urllib.request.urlopen
    old_contact, config.MB_CONTACT = config.MB_CONTACT, "test@example.test"
    stage5.time.sleep = lambda s: sleeps.append(s)
    stage5.urllib.request.urlopen = fake_urlopen
    try:
        try:
            client.fetch("https://example.test/x")
            check(False, "must raise after exhausting attempts")
        except stage5.EnrichNetworkError:
            pass
        check(calls["n"] == stage5.FETCH_ATTEMPTS, calls["n"])
        check(len(sleeps) == stage5.FETCH_ATTEMPTS, len(sleeps))
        check(max(sleeps) <= stage5.FETCH_BACKOFF_CAP_SEC, sleeps)
        check(sleeps[-1] == stage5.FETCH_BACKOFF_CAP_SEC,
              "later attempts must hit the cap, not grow unbounded")
    finally:
        stage5.time.sleep = old_sleep
        stage5.urllib.request.urlopen = old_urlopen
        config.MB_CONTACT = old_contact


# --- cache ----------------------------------------------------------------------------------

def test_cache_roundtrip_and_post_key():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        url = "https://example.test/ws?q=x"
        stage5.cache_put(conn, url, '{"recordings": []}')
        conn.commit()
        check(stage5.cache_get(conn, url) == {"recordings": []})
        stored = conn.execute("SELECT response_json FROM mb_cache").fetchone()[0]
        check(stored.startswith(stage5.CACHE_PREFIX), "cache rows are compressed (deviation #16)")
        # a legacy plain-text row still reads
        conn.execute("UPDATE mb_cache SET response_json='{\"plain\": 1}'")
        check(stage5.cache_get(conn, url) == {"plain": 1})
        # POST body participates in the key: same URL, different bodies, different entries
        check(stage5.cache_key(url, b"a") != stage5.cache_key(url, b"b"))
        check(stage5.cache_get(conn, url, b"body") is None)
        conn.close()


# --- the pass, end to end -------------------------------------------------------------------

def test_enrich_accept_review_none_and_idempotence():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        good, _ = _add_item(conn, artist="Queen", title="Bohemian Rhapsody")
        mid, _ = _add_item(conn, artist="Oasis", title="Wonderwall")
        low, _ = _add_item(conn, artist="Nobody", title="No Such Song")
        loser, _ = _add_item(conn, artist="Queen", title="Bohemian Rhapsody", verdict="alternate")
        client = FakeClient({
            ("Queen", "Bohemian Rhapsody"): [_rec("m1", 100, "Queen", "Bohemian Rhapsody", 1975)],
            ("Oasis", "Wonderwall"): [_rec("m2", 85, "Oasis", "Wonderwall")],
            ("Nobody", "No Such Song"): [],
            ("No Such Song", "Nobody"): [],   # swap fallback also finds nothing
        })
        rep = stage5.enrich_all(conn, client=client)
        check(rep.worklist == 3, f"losers are not enriched (§9.1); got {rep.worklist}")
        check(rep.accepted == 1 and rep.reviewed == 1 and rep.filename_stands == 1, rep.as_dict())

        served = {r["field"]: (r["value"], r["source"]) for r in conn.execute(
            "SELECT field, value, source FROM v_metadata WHERE media_item_id=?", (good,))}
        check(served["song_mbid"][0] == "m1" and served["artist"][1] == "musicbrainz_text")
        check(served["year"][0] == "1975")

        reviews = conn.execute(
            "SELECT media_item_id, payload FROM review_queue WHERE kind='metadata_match'"
        ).fetchall()
        check(len(reviews) == 1 and reviews[0]["media_item_id"] == mid)
        check(json.loads(reviews[0]["payload"])["reason"].startswith("mb_"))

        # idempotence: accepted → out via song_mbid; reviewed → out via open mb row;
        # 'none' re-derives from cache. Zero fetches, zero new rows.
        before = client.fetches
        meta_before = conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0]
        rep2 = stage5.enrich_all(conn, client=client)
        check(client.fetches == before, "second run must be all cache hits")
        check(rep2.accepted == 0 and rep2.reviewed == 0)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == meta_before)
        check(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1,
              "review row must not duplicate")
        conn.close()


def test_swapped_order_resolved_by_mb():
    """A Title↔Artist filename ('R U Mine' as artist): straight query fails, swapped wins —
    §6.1 hands order resolution to MB scoring, and MB's canonical fields fix the order."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, _ = _add_item(conn, artist="R U Mine", title="Arctic Monkeys")
        client = FakeClient({
            ("R U Mine", "Arctic Monkeys"): [],
            ("Arctic Monkeys", "R U Mine"): [_rec("m9", 100, "Arctic Monkeys", "R U Mine?")],
        })
        rep = stage5.enrich_all(conn, client=client)
        check(rep.accepted == 1 and rep.swapped_wins == 1, rep.as_dict())
        served = {r["field"]: r["value"] for r in conn.execute(
            "SELECT field, value FROM v_metadata WHERE media_item_id=?", (item,))}
        check(served["artist"] == "Arctic Monkeys" and served["title"] == "R U Mine?")
        conn.close()


def test_higher_trust_sources_survive():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, _ = _add_item(conn, artist="Queen", title="Bohemian Rhapsody")
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence) "
            "VALUES (?, 'title', 'Operator Says So', 'manual', 1.0)", (item,))
        conn.commit()
        client = FakeClient({
            ("Queen", "Bohemian Rhapsody"): [_rec("m1", 100, "Queen", "Bohemian Rhapsody")]})
        stage5.enrich_all(conn, client=client)
        row = conn.execute(
            "SELECT value, source FROM v_metadata WHERE media_item_id=? AND field='title'",
            (item,)).fetchone()
        check(row["source"] == "manual" and row["value"] == "Operator Says So",
              "manual outranks musicbrainz_text in v_metadata")
        conn.close()


def test_budget_stops_run():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        responses = {}
        for i in range(4):
            _add_item(conn, artist=f"Artist{i}", title=f"Song{i}")
            responses[(f"Artist{i}", f"Song{i}")] = [_rec(f"m{i}", 85, f"Artist{i}", f"Song{i}")]
        rep = stage5.enrich_all(conn, client=FakeClient(responses), budget=2)
        check(rep.over_budget and rep.stopped_reason == "review_budget")
        check(rep.reviewed <= 3, "must stop once the queue exceeds the budget")
        conn.close()


def test_retune_deletes_only_mb_outputs():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, _ = _add_item(conn, artist="Queen", title="Bohemian Rhapsody")
        now = db.utcnow()
        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source) "
                     "VALUES (?, 'title', 'X', 'musicbrainz_text')", (item,))
        for reason, resolved in (("mb_medium_band", None), ("mb_medium_band", '{"ok":1}'),
                                 ("id3_filename_disagreement", None)):
            conn.execute(
                "INSERT INTO review_queue (kind, media_item_id, payload, resolution, created_at) "
                "VALUES ('metadata_match', ?, ?, ?, ?)",
                (item, json.dumps({"reason": reason}), resolved, now))
        conn.commit()
        out = stage5.retune_enrichment(conn)
        check(out["musicbrainz_text_rows_deleted"] == 1)
        check(out["open_mb_reviews_deleted"] == 1, "resolved mb + id3 reviews must survive")
        left = [json.loads(r["payload"])["reason"] for r in
                conn.execute("SELECT payload FROM review_queue")]
        check(sorted(left) == ["id3_filename_disagreement", "mb_medium_band"])
        conn.close()


# --- acoustid -------------------------------------------------------------------------------

def test_acoustid_gated_on_key():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        old = config.ACOUSTID_API_KEY
        try:
            config.ACOUSTID_API_KEY = ""
            rep = stage5.acoustid_all(conn)
            check(rep.stopped_reason == "no_api_key" and rep.checked == 0)
        finally:
            config.ACOUSTID_API_KEY = old
        conn.close()


class FakeAcoustidClient:
    def __init__(self, results):
        self.results = results

    def fetch(self, url, *, data=None):
        return json.dumps({"status": "ok", "results": self.results})


def _add_fingerprint(conn, sha):
    from karaokemp.stage3 import encode_fp
    conn.execute("INSERT INTO fingerprints (content_hash, chromaprint, fp_duration_sec) "
                 "VALUES (?,?,120)", (sha, encode_fp([1, 2, 3, 4])))
    conn.commit()


def test_acoustid_hit_writes_identity_and_fp_rows():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, sha = _add_item(conn, artist="Queen", title="Bohemian Rhapsody")
        _add_fingerprint(conn, sha)
        old = config.ACOUSTID_API_KEY
        try:
            config.ACOUSTID_API_KEY = "testkey"
            hit = [{"score": 0.98, "id": "aid",
                    "recordings": [{"id": "rec-mbid", "title": "Bohemian Rhapsody",
                                    "artists": [{"name": "Queen"}]}]}]
            rep = stage5.acoustid_all(conn, client=FakeAcoustidClient(hit))
            check(rep.checked == 1 and rep.hits == 1, rep.as_dict())
            fp = conn.execute("SELECT * FROM fingerprints WHERE content_hash=?", (sha,)).fetchone()
            check(fp["acoustid_recording_mbid"] == "rec-mbid" and fp["acoustid_score"] == 0.98)
            row = conn.execute(
                "SELECT value FROM song_metadata WHERE media_item_id=? AND field='song_mbid' "
                "AND source='musicbrainz_fp'", (item,)).fetchone()
            check(row and row["value"] == "rec-mbid")
            # second run: checked_at set ⇒ empty worklist
            rep2 = stage5.acoustid_all(conn, client=FakeAcoustidClient(hit))
            check(rep2.worklist == 0 and rep2.checked == 0, "acoustid re-run must be a no-op")
        finally:
            config.ACOUSTID_API_KEY = old
        conn.close()


def test_acoustid_conflict_queues_review_not_metadata():
    """Same artist, different title (the live Members case) = plausible mislabel: record the
    acoustic identity, write NO musicbrainz_fp rows, queue metadata_match for ears."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, sha = _add_item(conn, artist="Members", title="The Sound of the Suburbs")
        _add_fingerprint(conn, sha)
        old = config.ACOUSTID_API_KEY
        try:
            config.ACOUSTID_API_KEY = "testkey"
            hit = [{"score": 0.97, "id": "aid",
                    "recordings": [{"id": "other-rec", "title": "Handling the Big Jets",
                                    "artists": [{"name": "The Members"}]}]}]
            rep = stage5.acoustid_all(conn, client=FakeAcoustidClient(hit))
            check(rep.conflicts == 1 and rep.hits == 0 and rep.junk == 0, rep.as_dict())
            fp = conn.execute("SELECT * FROM fingerprints WHERE content_hash=?", (sha,)).fetchone()
            check(fp["acoustid_recording_mbid"] == "other-rec",
                  "artist-anchored conflict records the acoustic fact")
            check(conn.execute(
                "SELECT COUNT(*) FROM song_metadata WHERE source='musicbrainz_fp'"
            ).fetchone()[0] == 0, "conflict must not write metadata rows")
            r = conn.execute("SELECT payload FROM review_queue WHERE kind='metadata_match'"
                             ).fetchone()
            check(r and json.loads(r["payload"])["reason"] == "mb_acoustid_conflict")
        finally:
            config.ACOUSTID_API_KEY = old
        conn.close()


def test_acoustid_junk_cluster_claims_nothing():
    """High-score hit where NEITHER field agrees = polluted AcoustID fingerprint cluster
    (live sample: audiobooks named 'Track 9' at score ≥0.94). No mbid, no review, no
    metadata — just checked_at so the pass moves on; the cached response keeps the evidence."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, sha = _add_item(conn, artist="The Kinks", title="Waterloo Sunset")
        _add_fingerprint(conn, sha)
        old = config.ACOUSTID_API_KEY
        try:
            config.ACOUSTID_API_KEY = "testkey"
            hit = [{"score": 0.97, "id": "aid",
                    "recordings": [{"id": "junk-rec", "title": "Track 11",
                                    "artists": [{"name": "Saint Augustine of Hippo"}]}]}]
            rep = stage5.acoustid_all(conn, client=FakeAcoustidClient(hit))
            check(rep.junk == 1 and rep.hits == 0 and rep.conflicts == 0, rep.as_dict())
            fp = conn.execute("SELECT * FROM fingerprints WHERE content_hash=?", (sha,)).fetchone()
            check(fp["acoustid_recording_mbid"] is None and fp["acoustid_checked_at"],
                  "junk must not become acoustic identity, but the blob counts as checked")
            check(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0,
                  "junk is not reviewable — the verdict would always be 'obviously junk'")
            check(conn.execute(
                "SELECT COUNT(*) FROM song_metadata WHERE source='musicbrainz_fp'"
            ).fetchone()[0] == 0)
        finally:
            config.ACOUSTID_API_KEY = old
        conn.close()


def test_title_agreement_forgives_version_qualifiers():
    """'(live)' / '(radio mix)' / our truncated '[karaok' tails are qualifiers, not titles —
    they were the #1 source of false AcoustID conflicts (live sample 2026-07-20)."""
    check(stage5.title_agreement("Celebrity Skin", "Celebrity Skin (live)") == 1.0)
    check(stage5.title_agreement("Recover Your Soul", "Recover Your Soul (radio mix)") == 1.0)
    check(stage5.title_agreement("There Must Be An Angel [karaok",
                                 "There Must Be an Angel") == 1.0)
    # the plain comparison must stay strict — the forgiveness lives ONLY in title_agreement
    check(stage5.field_agreement("Celebrity Skin", "Celebrity Skin (live)")
          < config.MB_FIELD_AGREEMENT)
    # base-stripping must not merge genuinely different songs
    check(stage5.title_agreement("Angel Of Mine", "Sensual Man") < 0.3)


def test_acoustid_base_title_hit_skips_qualified_title_row():
    """Base-title agreement corroborates identity, but the '(live)'-qualified MB title must
    NOT replace a clean display title — song_mbid + artist rows only."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, sha = _add_item(conn, artist="Hole", title="Celebrity Skin")
        _add_fingerprint(conn, sha)
        old = config.ACOUSTID_API_KEY
        try:
            config.ACOUSTID_API_KEY = "testkey"
            hit = [{"score": 0.95, "id": "aid",
                    "recordings": [{"id": "live-rec", "title": "Celebrity Skin (live)",
                                    "artists": [{"name": "Hole"}]}]}]
            rep = stage5.acoustid_all(conn, client=FakeAcoustidClient(hit))
            check(rep.hits == 1 and rep.conflicts == 0, rep.as_dict())
            rows = {r["field"]: r["value"] for r in conn.execute(
                "SELECT field, value FROM song_metadata WHERE source='musicbrainz_fp'")}
            check(rows.get("song_mbid") == "live-rec" and rows.get("artist") == "Hole")
            check("title" not in rows, "qualified title must not be written")
        finally:
            config.ACOUSTID_API_KEY = old
        conn.close()


def test_acoustid_picks_best_agreeing_recording():
    """One AcoustID result lists several recordings; the first is junk, a later one agrees —
    the agreeing one must win (first-listed is arbitrary)."""
    result = {"score": 0.95, "recordings": [
        {"id": "junk", "title": "Some Compilation Track", "artists": [{"name": "Various"}]},
        {"id": "right", "title": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]},
    ]}
    rec, a_sim, t_sim = stage5.choose_acoustid_recording(result, "Queen", "Bohemian Rhapsody")
    check(rec["id"] == "right" and t_sim == 1.0 and a_sim == 1.0)


def test_compress_acoustid_matches_fpcalc():
    """Round-trip the wire format against fpcalc itself on any staged mp3. Skips (loudly)
    when fpcalc or a sample file is unavailable — CI-safe, but on `penguin` it runs."""
    if shutil.which("fpcalc") is None:
        print("  (skipped: fpcalc not installed)")
        return
    sample = None
    staging = Path.home() / "karaokemp-library" / "staging"
    if staging.is_dir():
        for p in staging.rglob("*"):
            if p.suffix.lower() == ".mp3" and p.stat().st_size > 100_000:
                sample = p
                break
    if sample is None:
        print("  (skipped: no staged mp3 to fingerprint)")
        return
    raw = json.loads(subprocess.run(
        ["fpcalc", "-raw", "-json", "-length", "120", str(sample)],
        capture_output=True, text=True, check=True).stdout)["fingerprint"]
    compressed = json.loads(subprocess.run(
        ["fpcalc", "-json", "-length", "120", str(sample)],
        capture_output=True, text=True, check=True).stdout)["fingerprint"]
    ours = stage5.compress_acoustid([int(x) for x in raw])
    check(ours == compressed,
          f"wire-format mismatch: ours[:40]={ours[:40]} fpcalc[:40]={compressed[:40]}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passing")
    raise SystemExit(1 if failed else 0)
