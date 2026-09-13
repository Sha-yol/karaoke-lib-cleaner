"""§9.1.2 catalogue enrichment — the pure decision logic.

Every case here is a shape MEASURED on the live index or on the 2026-07-31 probes, not an
invented one. The rules these pin down are the ones a §11 retune would otherwise silently
break: the no-transliteration rule, the version-qualifier merge, the typed order oracle, and
the substring-segmentation safety rule for title-only items.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from karaokemp import enrich_sources as es  # noqa: E402

import catalogue_enrich as cat  # noqa: E402
import heb_enrich as heb  # noqa: E402


# --- normalization -------------------------------------------------------------------

def test_norm_he_strips_niqqud_and_punctuation():
    # Filenames in this library carry niqqud inconsistently; two spellings of one name must
    # compare equal or every Hebrew match floor is meaningless.
    assert es.norm_he("שָׁרִית  חַדָּד!") == es.norm_he("שרית חדד")


def test_sim_he_tolerates_one_letter_spelling_drift():
    # Measured live: our 'פאבלו רוזנברג' vs the catalogues' 'פבלו רוזנברג'.
    assert es.sim_he("פאבלו רוזנברג", "פבלו רוזנברג") >= 0.90


def test_script_of():
    assert es.script_of("שרית") == "heb"
    assert es.script_of("Pink") == "lat"
    assert es.script_of("123") == "none"


# --- merge_title: catalogue spelling, OUR version qualifier --------------------------

def test_merge_title_keeps_our_qualifier_and_drops_theirs():
    # The live defect this was written for: title_agreement strips brackets to decide these
    # are the same song, which would otherwise relabel our file as a live recording.
    assert es.merge_title("Boston (SC)", "Boston (Live from the Grove)") == "Boston (SC)"


def test_merge_title_preserves_our_qualifier_when_theirs_has_none():
    assert es.merge_title("Only Time (2001 Radio Mix)", "Only Time") == \
        "Only Time (2001 Radio Mix)"


def test_merge_title_takes_canonical_spelling_when_we_have_no_qualifier():
    assert es.merge_title("Caribbean Qqueen",
                          "Caribbean Queen (No More Love On the Run)") == "Caribbean Queen"
    assert es.merge_title("Homecoming", "Homecoming (feat. Chris Martin)") == "Homecoming"


def test_merge_title_falls_back_when_candidate_is_all_qualifier():
    # A candidate that is nothing but a bracketed string must not collapse to empty.
    assert es.merge_title("Whatever", "(Live)") == "(Live)"


# --- the no-transliteration rule -----------------------------------------------------

def test_hebrew_text_never_replaced_by_latin():
    # The whole point of §9.1.2 for Hebrew: end users search in Hebrew, so a correct-but-Latin
    # 'Sarit Hadad' is unusable. Ours must stand.
    assert heb.choose_text("שרית חדד", "Sarit Hadad") == "שרית חדד"


def test_hebrew_label_may_canonicalize_hebrew_text():
    assert heb.choose_text("פאבלו רוזנברג", "פבלו רוזנברג") == "פבלו רוזנברג"


def test_choose_title_keeps_our_hebrew_qualifier():
    assert heb.choose_title("יסמין (גרסת בנות פסנתר)", "יסמין") == "יסמין (גרסת בנות פסנתר)"


# --- Hebrew decoration stripping (query shaping) --------------------------------------

def test_strips_trailing_genre_tag():
    # Wikidata has an entry for 'עומר אדם' and none for the genre-tagged form. Measured: 12 of
    # the 628 unresolved paired items carry this exact tag.
    assert heb.strip_query_decor("עומר אדם ישראלי מזרחי") == "עומר אדם"


def test_strips_trailing_verb_decoration():
    assert heb.strip_query_decor("סטטיק ובן אל תבורי שרים") == "סטטיק ובן אל תבורי"


def test_strips_only_at_the_edges():
    # The same token mid-string is far more likely to be part of a real title, and titles are
    # what we store — stripping there would corrupt them.
    s = "מה שרים בבוקר"
    assert heb.strip_query_decor(s) == s


def test_never_empties_a_field():
    # A field that is ONLY a decoration must survive intact: an empty query matches nothing
    # and an empty stored title would destroy the item's only text.
    assert heb.strip_query_decor("שרים") == "שרים"


def test_leaves_undecorated_text_untouched():
    # 90.4% of the unresolved population — this must be a no-op so their cached queries
    # stay cache hits on a re-run.
    assert heb.strip_query_decor("שרית חדד") == "שרית חדד"


# --- Wikidata typed order oracle -----------------------------------------------------

def _ent(qid, label, *, instance=None, mb_artist=None, performer=None, pubdate=None):
    claims = {}

    def _snak(prop, vals, kind="id"):
        claims[prop] = [{"mainsnak": {"snaktype": "value",
                                      "datavalue": {"value": ({"id": v} if kind == "id"
                                                              else {"time": v})
                                                    if kind != "str" else v}}}
                        for v in vals]

    if instance:
        _snak(es.P_INSTANCE_OF, instance)
    if mb_artist:
        _snak(es.P_MB_ARTIST, mb_artist, kind="str")
    if performer:
        _snak(es.P_PERFORMER, performer)
    if pubdate:
        _snak(es.P_PUBLICATION_DATE, pubdate, kind="time")
    return {"id": qid, "labels": {"he": {"value": label}}, "claims": claims}


def _hit(qid, label):
    return {"qid": qid, "label": label, "description": "", "matched": label}


ARTIST = _ent("Q1", "ריקי גל", instance=["Q5"], mb_artist=["mbid-riki"])
WORK = _ent("Q2", "בני ילד רע", instance=["Q7366"], performer=["Q1"],
            pubdate=["+1982-00-00T00:00:00Z"])


def test_wikidata_resolves_order_by_TYPE_not_text():
    # Field a is the TITLE here and field b the ARTIST — the swapped case that is a coin flip
    # in location_parses. Nothing about the strings says which; the entity types do.
    r = heb.resolve_wikidata("בני ילד רע", "ריקי גל",
                             [_hit("Q2", "בני ילד רע")], [_hit("Q1", "ריקי גל")],
                             {"Q1": ARTIST, "Q2": WORK})
    assert r.artist_field == "b"
    assert r.artist_label == "ריקי גל"
    assert r.artist_mbid == "mbid-riki"
    assert r.year == "1982"
    assert r.reason == "wd_resolved"


def test_wikidata_artist_only_branch_is_marked_as_weaker():
    # The dangerous branch: an artist matched, the other field matched NOTHING. Absence of
    # evidence, not evidence — it must be flagged so combine() can demand a second vote.
    r = heb.resolve_wikidata("ריקי גל", "some junk string that matches nothing",
                             [_hit("Q1", "ריקי גל")], [], {"Q1": ARTIST})
    assert r.artist_field == "a"
    assert r.reason == "wd_resolved_artist_only"


def test_wikidata_refuses_when_both_fields_look_like_artists():
    r = heb.resolve_wikidata("ריקי גל", "ריקי גל",
                             [_hit("Q1", "ריקי גל")], [_hit("Q1", "ריקי גל")], {"Q1": ARTIST})
    assert r.artist_field is None
    assert r.reason == "wd_ambiguous_type"


# --- combining the two oracles -------------------------------------------------------

def test_both_agreeing_earns_the_top_band():
    wd = heb.Resolution(artist_field="b", artist_label="ריקי גל", reason="wd_resolved")
    mb = heb.Resolution(artist_field="b", artist_label="ריקי גל", reason="mb_resolved")
    d = heb.combine("בני ילד רע", "ריקי גל", wd, mb)
    assert d["reason"] == "both_agree"
    assert d["confidence"] == heb.CONF_BOTH_AGREE
    assert d["swapped"] is True
    assert d["artist"] == "ריקי גל" and d["title"] == "בני ילד רע"


def test_disagreement_writes_nothing():
    wd = heb.Resolution(artist_field="a", reason="wd_resolved")
    mb = heb.Resolution(artist_field="b", reason="mb_resolved")
    d = heb.combine("x", "y", wd, mb)
    assert d["reason"] == "conflict"
    assert "confidence" not in d


def test_uncorroborated_artist_only_branch_is_refused():
    # This is live item 8: Wikidata has a SERBIAN band called הוריקן, our other field was
    # decoration-laden junk that matched nothing, and the fallback confidently declared our
    # TITLE to be the artist. Without MB agreeing, it must write nothing.
    wd = heb.Resolution(artist_field="a", artist_label="הוריקן",
                        reason="wd_resolved_artist_only")
    mb = heb.Resolution(reason="mb_no_hit")
    d = heb.combine("הוריקן", "Hurricane עדן גולן (גרסת בנות) PIANO l NATI", wd, mb)
    assert d["reason"] == "wd_artist_only_uncorroborated"
    assert "confidence" not in d


def test_artist_only_branch_accepted_once_mb_corroborates():
    wd = heb.Resolution(artist_field="a", artist_label="ריקי גל",
                        reason="wd_resolved_artist_only")
    mb = heb.Resolution(artist_field="a", artist_label="ריקי גל", reason="mb_resolved")
    d = heb.combine("ריקי גל", "בני ילד רע", wd, mb)
    assert d["reason"] == "both_agree"


def test_latin_catalogue_name_never_overwrites_hebrew_even_when_agreeing():
    wd = heb.Resolution(artist_field="a", artist_label="Sarit Hadad", reason="wd_resolved")
    mb = heb.Resolution(artist_field="a", artist_label="Hadad, Sarit", reason="mb_resolved")
    d = heb.combine("שרית חדד", "חופשיה", wd, mb)
    assert d["artist"] == "שרית חדד"


# --- title-only segmentation safety rule ---------------------------------------------

def test_segments_only_when_performer_is_inside_our_own_string():
    # 'ילדה קטנה משה פרץ ואגם בוחבוט שרים' — an undelimited Title+Artist string. Removing the
    # performer we FOUND there is segmentation of our own text.
    out = heb.segment_title_only("ילדה קטנה משה פרץ ואגם בוחבוט שרים", ["משה פרץ"])
    assert out is not None
    assert out["artist"] == "משה פרץ"
    assert "משה פרץ" not in out["title"]
    assert "ילדה קטנה" in out["title"]


def test_refuses_when_performer_is_not_in_our_text():
    # Wikidata says 'טיפת מזל' is performed by זהבה בן. Our string does not say so, and covers
    # are the norm in a karaoke library — this must go to review, never be auto-written.
    assert heb.segment_title_only("טיפת מזל", ["זהבה בן"]) is None


def test_refuses_when_removing_the_artist_leaves_nothing():
    # Our string was ONLY the artist name; the remainder is not a title.
    assert heb.segment_title_only("משה פרץ", ["משה פרץ"]) is None


# --- iTunes/Deezer acceptance --------------------------------------------------------

def test_tier1_accepts_a_spelling_correction():
    cands = [{"artist": "Jon Secada", "title": "Just Another Day",
              "year": "1992", "genre": "Pop"}]
    cand, reason = cat.choose("John Secada", "Just Another Day", cands)
    assert reason == "accept"
    assert cand["artist"] == "Jon Secada"


def test_tier1_refuses_a_candidate_that_would_redirect_us():
    # §3.6: an enrichment source may not change WHICH SONG an item is.
    cands = [{"artist": "Nirvana", "title": "Lithium", "year": None, "genre": None}]
    cand, reason = cat.choose("Britney Spears", "Toxic", cands)
    assert cand is None and reason == "below_floor"


def test_tier2_recovers_a_swapped_parse():
    # Live: ours 'Sugar Shack' | 'Gilmer, Jimmy & The Fireballs' — artist and title swapped.
    cands = [{"artist": "Jimmy Gilmer & The Fireballs", "title": "Sugar Shack",
              "year": "1994", "genre": "Pop"}]
    cand, reason = cat.choose_order_free("Sugar Shack", "Gilmer, Jimmy & The Fireballs", cands)
    assert reason == "accept_order_free"
    assert cand["artist"] == "Jimmy Gilmer & The Fireballs"
    assert cand["order_free_conf"] < cat.FIELD_FLOOR   # strictly below tier 1's band


def test_tier2_rejects_self_titled_degenerate_candidates():
    # Without the disjointness rule, a candidate whose title is merely the artist's name
    # satisfies both coverage checks with the same tokens.
    cands = [{"artist": "The Boomtown Rats", "title": "The Boomtown Rats",
              "year": None, "genre": None}]
    cand, reason = cat.choose_order_free("The Boomtown Rats", "I Don't Like Mondays", cands)
    assert cand is None and reason == "order_free_reject"


def test_no_results_is_distinguishable_from_a_rejection():
    # An empty result must never be silently equivalent to "no match" — that conflation is
    # what made a rate-limited Spotify run write permanent wrong verdicts.
    assert cat.choose("A", "B", [])[1] == "no_results"
    assert cat.choose_order_free("A", "B", [])[1] == "no_results"
