"""Stage 1 parser invariant tests (spec §6.1).

Every case here is a real filename from the library (or a minimal reduction of one), not an
invented example. The parser is the pipeline's highest-variance component (§6.2) and it is
about to run over 53k files, so the patterns it claims to handle are pinned here.

The §6.2 golden-set gate is the *human* check on parser accuracy. These are the machine check
on parser regressions — they are complementary, not substitutes.

Run: python3 -m pytest tests/ -q     (or: python3 tests/test_stage1.py)
"""

from __future__ import annotations

from karaokemp.stage1 import (
    parse_location,
    detect_script,
    extract_decorations,
    folder_hints_for,
    normalize,
    parse_stem,
    strip_hebrew_diacritics,
    _looks_like_video_id,
)


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


# --- §6.1 Hebrew: strip marks, but NOT the punctuation that lives in the same range --------


def test_hebrew_maqaf_survives():
    """§6.1 is explicit: maqaf U+05BE is a hyphen. Stripping it glues words together.

    This is the spec's single most emphatic parser instruction, so it gets a test even though
    the census found zero affected files today — the cost of the test is nil and the cost of
    regressing it is two words silently fused inside a title.
    """
    s = "על־האש"  # על־האש  (maqaf between the words)
    check("־" in strip_hebrew_diacritics(s), "maqaf U+05BE must survive stripping")


def test_hebrew_punctuation_survives():
    for cp, name in (("׀", "paseq"), ("׃", "sof pasuq"), ("־", "maqaf")):
        check(cp in strip_hebrew_diacritics(f"א{cp}ב"), f"{name} must survive")


def test_hebrew_marks_are_stripped():
    # שָׁלוֹם with qamats/shin-dot/holam → bare שלום
    s = "שָׁלוֹם"
    out = strip_hebrew_diacritics(s)
    check(out == "שלום", f"marks not stripped: {out!r}")


def test_non_hebrew_marks_untouched():
    """§6.1 scopes stripping to the Hebrew block; a Latin combining accent is not ours to touch."""
    s = "café"  # café (combining acute)
    check(strip_hebrew_diacritics(s) == s, "non-Hebrew combining mark must be left alone")


# --- §6.1 normalization -------------------------------------------------------------------


def test_video_id_suffix_stripped():
    p = parse_stem("Tender_-_Blur_Karaoke_Version-3iagGi5zAYI")
    check("3iagGi5zAYI" not in (p.title or "") + (p.artist or ""), f"video ID leaked: {p}")
    check(p.artist == "Tender", f"artist={p.artist!r}")
    check(p.title == "Blur", f"title={p.title!r}")


def test_video_id_lookalike_word_is_kept():
    """'tobewithyou' and 'wet_wet_wet' are 11 chars of [A-Za-z0-9_-] — same shape as a video ID.

    Length alone would eat a real title word. Requiring a digit and an uppercase letter is what
    separates a random ID from lowercase prose.
    """
    check(not _looks_like_video_id("tobewithyou"), "lowercase word must not read as a video ID")
    check(not _looks_like_video_id("wet_wet_wet"), "lowercase word must not read as a video ID")
    check(_looks_like_video_id("VZwiiKF3F7Y"), "real video ID must be detected")
    check(_looks_like_video_id("t4oEtf3NJi4"), "real video ID must be detected")


def test_html_entities_decoded():
    p = parse_stem("You Oughta Know in the Style of &quot;Alanis Morissette&quot; with lyrics")
    check("&quot;" not in (p.artist or ""), f"entity leaked: {p.artist!r}")
    check(p.artist == "Alanis Morissette", f"artist={p.artist!r}")


def test_site_tag_stripped():
    p = parse_stem("Jack+Johnson+-+Holes+to+Heaven(music.naij.com)")
    check("naij" not in (p.title or ""), f"site tag leaked: {p.title!r}")


def test_embedded_stale_extension_stripped():
    p = parse_stem("SC8327-15 - Dixon, Willie - I'm Your Hoochie Coochie Man.mpg [KARAOKE]")
    check(".mpg" not in (p.title or ""), f"stale ext leaked: {p.title!r}")
    check(p.title == "I'm Your Hoochie Coochie Man", f"title={p.title!r}")


def test_underscores_as_spaces_only_without_real_spaces():
    check(normalize("Angie_-_The_Rolling_Stones") == "Angie - The Rolling Stones")
    # A name that already has spaces keeps its underscores — they may be meaningful.
    check("_" in normalize("Track_01 - Real Title"), "underscore removed from spaced name")


# --- §6.1 decorations & instrumental hints ------------------------------------------------


def test_decorations_move_to_flags():
    body, flags = extract_decorations("Madonna - Rain KARAOKE WITH BACKING VOCALS HD")
    check("karaoke" not in body.lower(), f"decoration left in body: {body!r}")
    check("backing_vocals" in flags and "hd" in flags, f"flags={flags}")
    check(body == "Madonna - Rain", f"body={body!r}")


def test_bare_video_is_protected_but_karaoke_video_is_not():
    """'Video Killed The Radio Star' is a real title here (4 rows), so bare 'video' stays.
    The 'karaoke video' compound (18 rows) is unambiguous and does become a flag."""
    p = parse_stem("DK54-15 - Video Killed The Radio Star - Buggles", batch="Unorginized")
    check(p.title == "Video Killed The Radio Star", f"title={p.title!r}")
    check(p.artist == "Buggles", f"artist={p.artist!r}")
    p2 = parse_stem("Disturbed Sound of Silence Karaoke Video")
    check("video" in p2.flags, f"flags={p2.flags}")
    check("Video" not in (p2.title or ""), f"title={p2.title!r}")


def test_instrumental_yes_from_karaoke_tag():
    check(parse_stem("Platters - Great Pretender [karaoke]").is_instrumental == "yes")
    check(parse_stem("Slow blues Backing Track in C").is_instrumental == "yes")


def test_instrumental_hints_non_english():
    """§6.1 lists פלייבק/קריוקי; the census added караоке (Russian)."""
    for name in ("קריוקי שיר", "Some Song (караоке)"):
        check(parse_stem(name).is_instrumental == "yes", f"missed instrumental hint: {name!r}")


def test_con_voz_is_negative_evidence_not_instrumental():
    """'con voz' = 'with voice'. It must not be read as an instrumental hint."""
    p = parse_stem("Some Song (con voz)")
    check(p.is_instrumental == "no", f"con voz misread: {p.is_instrumental}")


def test_unknown_when_filename_says_nothing():
    check(parse_stem("Jimmy Eat World - The Middle").is_instrumental == "unknown")


# --- regressions: decorations that are also real names ------------------------------------
# Each of these was a live corruption caught by the §6.2 census, not a hypothetical.


def test_bare_weak_decoration_does_not_eat_a_band_name():
    """'Clean Bandit' is a band; 'clean' bare must not be stripped (24 real rows)."""
    p = parse_stem("SF336-06 - Clean Bandit Feat. Jess Glynne - Rather Be")
    check(p.artist == "Clean Bandit Feat. Jess Glynne", f"artist={p.artist!r}")
    check(p.title == "Rather Be", f"title={p.title!r}")


def test_bare_weak_decoration_does_not_eat_a_title_word():
    """'Do They Know It's Christmas' — 'christmas' bare is the title (234 real rows)."""
    p = parse_stem("MRH124-01 - Band Aid 30 - Do They Know It's Christmas 2014")
    check("Christmas" in (p.title or ""), f"title={p.title!r}")
    p2 = parse_stem("LeAnn Rimes - How Do I Live")
    check(p2.title == "How Do I Live", f"title={p2.title!r}")


def test_bracketed_weak_decoration_is_still_stripped():
    """Inside brackets the intent is unambiguous, so 'clean'/'live' do become flags there."""
    p = parse_stem("Some Artist - Some Song (Clean)")
    check(p.title == "Some Song", f"title={p.title!r}")
    check("clean" in p.flags, f"flags={p.flags}")


def test_parenthetical_title_survives_intact():
    """1,093 titles were being truncated mid-paren ('Guerrilla Radio (Pixel')."""
    p = parse_stem("BS5417 - 13 Lisa Loeb & Nine Stories - Stay (I Missed You)")
    check(p.title == "Stay (I Missed You)", f"title={p.title!r}")
    check(p.artist == "Lisa Loeb & Nine Stories", f"artist={p.artist!r}")


def test_partly_decorative_bracket_keeps_its_remainder():
    p = parse_stem("Destiny's Child - Say My Name (Karaoke Piano)")
    check(p.title == "Say My Name (Piano)", f"title={p.title!r}")
    check("karaoke" in p.flags, f"flags={p.flags}")


def test_english_word_plus_numbers_is_not_a_disc_id():
    """'Slow 12-8 Blues Backing Track in E' was parsed as series SLOW, disc 12, track 8."""
    p = parse_stem("Slow 12-8 Blues Backing Track in E (Extra Long)")
    check(p.disc_series is None, f"misread as disc series {p.disc_series!r}")
    check("Blues" in (p.title or ""), f"title={p.title!r}")


def test_decoration_that_is_the_whole_name_still_parses():
    """'Christmas - Christmas [Karaoke]' parsed to nothing when 'christmas' was strippable."""
    p = parse_stem("Christmas - Christmas [Karaoke]")
    check(p.title is not None and p.artist is not None, f"name eaten entirely: {p}")
    check(p.layout != "unparsed", f"layout={p.layout}")


# --- §6.1 disc IDs -------------------------------------------------------------


def test_sf_order_from_verified_batch():
    p = parse_stem("SF222-06 - Duran Duran - Sunrise", batch="New")
    check((p.disc_series, p.disc_id, p.disc_track) == ("SF", "SF222", "06"), f"{p}")
    check(p.artist == "Duran Duran" and p.title == "Sunrise", f"artist={p.artist!r} title={p.title!r}")
    check(p.layout == "disc_artist_title", f"layout={p.layout}")
    check("order_from_batch" in p.notes, f"notes={p.notes}")


def test_dk_order_from_verified_batch():
    p = parse_stem("DK30-15 - Venus - Frankie Avalon", batch="Unorginized")
    check(p.title == "Venus" and p.artist == "Frankie Avalon", f"artist={p.artist!r} title={p.title!r}")
    check(p.layout == "disc_title_artist", f"layout={p.layout}")


def test_same_series_different_batch_flips_order():
    """The crux: SF is Artist-Title under New/ but Title-Artist under Unorginized/. Order is a
    property of the source batch, not the series — a series-keyed table gets this row backwards."""
    p = parse_stem("SF014-02 - Crazy - Patsy Cline", batch="Unorginized")
    check(p.confidence <= 0.5, f"unverified batch must not claim certainty: {p.confidence}")
    check("order_unresolved" in p.notes, f"notes={p.notes}")


def test_comma_pins_order_per_row_over_batch():
    """'Murs, Olly' is unmistakably an artist. That per-row fact beats any batch generalisation,
    and it is what separates the two mixed sub-batches inside Unorginized/SF."""
    p = parse_stem("SF314-08 - Oh My Goodness - Murs, Olly", batch="Unorginized")
    check(p.title == "Oh My Goodness", f"title={p.title!r}")
    check(p.artist == "Olly Murs", f"artist={p.artist!r}")
    check("order_from_comma" in p.notes, f"notes={p.notes}")
    check(p.confidence >= 0.9, f"comma evidence should be high confidence: {p.confidence}")

    p2 = parse_stem("sf019-03 - crow, sheryl - all i wanna do", batch="Unorginized")
    check(p2.artist == "sheryl crow", f"artist={p2.artist!r}")
    check(p2.title == "all i wanna do", f"title={p2.title!r}")


def test_comma_only_when_exactly_one_side_matches():
    """'Hello, Goodbye' is a title, not an inverted name — the tail must be a single token."""
    from karaokemp.stage1 import LASTNAME_FIRST
    check(LASTNAME_FIRST.match("Murs, Olly") is not None, "real inverted name must match")
    check(LASTNAME_FIRST.match("Wanted, The") is not None, "article tail must match")
    check(LASTNAME_FIRST.match("Hello, Goodbye Cruel World") is None, "multi-word tail must not match")


def test_disc_id_case_insensitive():
    """'sf012-08-…' and 'dk089-01_-_…' are real rows; §6.1's [A-Z]-only form drops them."""
    p = parse_stem("dk089-01_-_love_is_all_around_-_wet_wet_wet", batch="Unorginized")
    check(p.disc_series == "DK", f"series={p.disc_series!r}")
    check(p.title == "love is all around", f"title={p.title!r}")
    check(p.artist == "wet wet wet", f"artist={p.artist!r}")


def test_disc_id_space_separated_house_style():
    """'AMS1060 08   Pras & Mya   Ghetto Superstar' — separator is a space run, not a dash."""
    p = parse_stem("AMS1060 08   Pras & Mya   Ghetto Superstar [karaoke]")
    check(p.disc_series == "AMS", f"series={p.disc_series!r}")
    check(p.artist == "Pras & Mya", f"artist={p.artist!r}")
    check(p.title == "Ghetto Superstar", f"title={p.title!r}")


def test_lastname_first_unswapped():
    p = parse_stem("BHK027-03 - Mastin, Reece - Good Night")
    check(p.artist == "Reece Mastin", f"artist={p.artist!r}")
    check("lastname_first_unswapped" in p.notes, f"notes={p.notes}")


def test_unverified_batch_downgrades_confidence():
    """Disc structure alone does not license an order claim; §6.1 defers that to MB scoring."""
    p = parse_stem("ZZZ999-01 - Someone - Something", batch="Nowhere")
    check(p.confidence <= 0.5, f"unverified order must not claim high confidence: {p.confidence}")
    check("order_unresolved" in p.notes, f"notes={p.notes}")


# --- §6.1 layout ladder -------------------------------------------------------------------


def test_style_of_layout():
    p = parse_stem("Me And Bobby McGee in the style of Janis Joplin karaoke video with lyrics")
    check(p.title == "Me And Bobby McGee", f"title={p.title!r}")
    check(p.artist == "Janis Joplin", f"artist={p.artist!r}")
    check(p.layout == "style_of", f"layout={p.layout}")


def test_plain_artist_title():
    p = parse_stem("Janet Jackson - Someone To Call My Lover [Karaoke]")
    check(p.artist == "Janet Jackson" and p.title == "Someone To Call My Lover", f"{p}")


def test_tight_dash_fallback():
    p = parse_stem("lauren hill-nothing even matters")
    check(p.artist == "lauren hill" and p.title == "nothing even matters", f"{p}")
    check("tight_dash_split" in p.notes, f"notes={p.notes}")


def test_bare_title_only():
    p = parse_stem("JUNGEL")
    check(p.title == "JUNGEL" and p.artist is None, f"{p}")
    check(p.layout == "title_only" and p.confidence < 0.5, f"{p}")


def test_hebrew_order_is_flagged_ambiguous():
    """Hebrew rows in this library are more often Title - Artist than Artist - Title, and both
    occur. §6.1 says score both orders against MB later — so the parser must not claim to know."""
    p = parse_stem("מכל האהבות - עידן רייכל")
    check(p.language == "he", f"language={p.language}")
    check("hebrew_order_ambiguous" in p.notes, f"notes={p.notes}")
    check(p.confidence <= 0.5, f"confidence too high for an ambiguous order: {p.confidence}")


def test_junk_names_stay_low_confidence():
    """VCD-style files and UUID names carry no metadata; §5.3 flagged them. They must not fake a parse."""
    for junk in ("Chapter_13-1113", "Title_0109", "SONG-f215ae21-fb55-44da-b450-da40ae806fcd"):
        p = parse_stem(junk)
        check(p.confidence <= 0.6, f"{junk!r} claimed confidence {p.confidence}: {p}")


def test_junk_does_not_match_disc_id():
    """'Chapter_13-1113' and 'Title_0109' must not be read as disc IDs."""
    for junk in ("Chapter_13-1113", "Title_0109"):
        check(parse_stem(junk).disc_series is None, f"{junk!r} misread as a disc ID")


# --- regressions from sha-yol's §6.2 golden-set review (2026-07-14) -------------------------
# Each case below is a row sha-yol marked 'bad'. These are the *structural* failures — the ones a
# consistent parser can catch. Order-flips with no signal in the name are deliberately NOT
# chased here; §6.1 hands those to MusicBrainz scoring at Stage 5.


def test_non_media_files_are_not_parsed_as_songs():
    """'Thumbs.db' was yielding a confident artist/title out of Windows shell cruft."""
    for path, ft in (
        ("קריוקי בעברית 1/א-ב/Thumbs.db", "db"),
        ("בקשות/סיון וסויסר/desktop.ini", "ini"),
    ):
        p = parse_location(1, path, ft)
        check(p.layout == "non_media", f"{path} -> layout={p.layout}")
        check(p.artist is None and p.title is None, f"{path} -> A={p.artist!r} T={p.title!r}")
        check(p.confidence == 0.0, f"{path} -> confidence={p.confidence}")


def test_appledouble_sidecar_is_not_media():
    """All 29 '._' files are exactly 4096 bytes — macOS resource forks, not media."""
    p = parse_stem("._Green Day - Basket Case (karaoke).")
    check(p.layout == "non_media", f"layout={p.layout}")
    check(p.artist is None, f"artist={p.artist!r}")


def test_opaque_names_yield_no_artist():
    """'SONG-<uuid>' became artist='SONG'; 'Chapter_03-113' became artist='Chapter 03'."""
    for junk in (
        "SONG-5fbf3a01-241f-41a0-acbf-1940468ae768",
        "Chapter_03-113",
        "Chapter_12-2112",
        "Title_1101",
    ):
        p = parse_stem(junk)
        check(p.layout == "opaque", f"{junk!r} -> layout={p.layout}")
        check(p.artist is None and p.title is None, f"{junk!r} -> A={p.artist!r} T={p.title!r}")
        check(p.confidence == 0.0, f"{junk!r} -> confidence={p.confidence}")


def test_opaque_name_still_records_folder_hint():
    """§5.3 predicted the parent folder is the only signal for these. It must survive — but
    §6.1 forbids auto-promoting it, so it stays a hint for Stage 5 to weigh."""
    p = parse_location(
        1, "קריוקי בעברית 1/.-דיסקים קריוקי/שלמה ארצ נרקוד נשכח  151/Chapter_02-112.avi", "avi"
    )
    check(p.layout == "opaque", f"layout={p.layout}")
    check(any("שלמה" in h for h in p.folder_hints), f"folder hint lost: {p.folder_hints}")
    check(p.artist is None, "folder hint must not be auto-promoted to artist")


def test_hebrew_rip_strips_catalogue_code_and_puts_artist_last():
    """'עד סוף הקיץ - רפאל מירילה - 6904' was yielding title='רפאל מירילה 6904' — the catalogue
    number glued to the artist, and the order backwards. 1,026 rows in this batch."""
    p = parse_stem("עד סוף הקיץ - רפאל מירילה - 6904", batch="קריוקי בעברית 1")
    check(p.catalogue_code == "6904", f"catalogue_code={p.catalogue_code!r}")
    check(p.artist == "רפאל מירילה", f"artist={p.artist!r}")
    check(p.title == "עד סוף הקיץ", f"title={p.title!r}")
    check(p.layout == "title_artist", f"layout={p.layout}")


def test_hebrew_rip_three_part_name_artist_still_last():
    p = parse_stem("כאן - שירי ילדות - אורנה ומשה דץ - 7701", batch="קריוקי בעברית 1")
    check(p.artist == "אורנה ומשה דץ", f"artist={p.artist!r}")
    check(p.title == "כאן שירי ילדות", f"title={p.title!r}")


def test_catalogue_code_only_name_has_no_artist():
    """'מחרוזת רוק ישראלי - 6305' is a title plus a code — there is no artist to find."""
    p = parse_stem("מחרוזת רוק ישראלי - 6305", batch="קריוקי בעברית 1")
    check(p.catalogue_code == "6305", f"catalogue_code={p.catalogue_code!r}")
    check(p.artist is None, f"artist invented: {p.artist!r}")
    check(p.title == "מחרוזת רוק ישראלי", f"title={p.title!r}")


def test_one_sided_space_dash_is_a_separator():
    """302 rows use 'X- Y' or 'X -Y'. Requiring spaces on both sides missed all of them."""
    p = parse_stem("יזהר אשדות -יש לך אותי")
    check(p.artist == "יזהר אשדות" and p.title == "יש לך אותי", f"A={p.artist!r} T={p.title!r}")
    p2 = parse_stem("DDT- CHTO TAKOEE LETO")
    check(p2.artist == "DDT" and p2.title == "CHTO TAKOEE LETO", f"A={p2.artist!r} T={p2.title!r}")


def test_tight_dash_is_not_a_separator_without_whitespace():
    """'Spider-Man' must survive; only the tight-dash *fallback* may split a bare name."""
    p = parse_stem("Some Artist - Spider-Man Theme")
    check(p.title == "Spider-Man Theme", f"title={p.title!r}")


def test_leading_number_is_never_stripped():
    """217 stems start with a number, but they include '4 Non Blondes' (a band) and '7 Nation
    Army' (a title). Stripping a leading track number would corrupt both silently — the same
    asymmetry that makes weak decorations bracket-only. '14 להשתטות' keeps its 14; that is
    visible and fixable, unlike an eaten band name."""
    p = parse_stem("4 Non Blondes Whats Up")
    check("4" in (p.title or "") + (p.artist or ""), f"leading number eaten: A={p.artist!r} T={p.title!r}")
    p2 = parse_stem("4 non blondes - whats up")
    check(p2.artist == "4 non blondes", f"artist={p2.artist!r}")


def test_artist_promoted_from_verified_artist_folder():
    """§6.1 says never auto-promote a folder — right for 'M', wrong for a verified artist root.
    sha-yol's review marked all of these bad. Scoped to verified roots AND artist-less rows."""
    p = parse_location(1, "קריוקי בעברית 1/karaoke/אביב גפן/התקוה.avi", "avi")
    check(p.artist == "אביב גפן", f"artist={p.artist!r}")
    check(p.title == "התקוה", f"title={p.title!r}")
    check("artist_from_folder" in p.notes, f"notes={p.notes}")

    p2 = parse_location(1, "קריוקי בעברית 1/קריוקי מרביד השרירי/ים תיכוני/ישי לוי/בשבילי את השער לגן עדן.avi", "avi")
    check(p2.artist == "ישי לוי", f"artist={p2.artist!r}")


def test_folder_artist_never_overrides_a_name_that_has_one():
    """Promotion may only add information, never overwrite what the filename actually said."""
    p = parse_location(1, "קריוקי בעברית 1/karaoke/אייל גולן/חמי רודנר - בואי ניפרד.MPG", "MPG")
    check(p.artist != "אייל גולן", f"folder overrode a filename artist: {p.artist!r}")
    check("artist_from_folder" not in p.notes, f"notes={p.notes}")


def test_unverified_folder_is_not_promoted():
    """Outside the verified roots, §6.1's rule stands: the folder stays a hint."""
    p = parse_location(1, "English karaoke/M/Mariah Carey/JUNGEL.mp3", "mp3")
    check(p.artist is None, f"unverified folder promoted: {p.artist!r}")
    check("Mariah Carey" in p.folder_hints, f"hint lost: {p.folder_hints}")


def test_bracketed_lyrics_stripped_but_bare_lyrics_kept():
    p = parse_stem("Suck My Kiss - Red hot Chilli Peppers [ Lyrics ]")
    check("Lyrics" not in (p.title or ""), f"title={p.title!r}")
    check("lyrics" in p.flags, f"flags={p.flags}")


# --- §6.1 folder hints --------------------------------------------------------------------


def test_folder_hints_drop_shelving_folders():
    """'M' and '01234' are alphabetical shelving, not an artist named 'M'."""
    hints = folder_hints_for("English karaoke/M/Mariah Carey/Mariah Carey - I Don't Want To Cry.mp3")
    check("Mariah Carey" in hints, f"hints={hints}")
    check("M" not in hints, f"shelving folder offered as a hint: {hints}")


def test_folder_hint_is_only_artist_signal_for_bare_titles():
    hints = folder_hints_for("קריוקי בעברית 1/karaoke/אייל גולן/JUNGEL.mp3")
    check("אייל גולן" in hints, f"hints={hints}")
    check("karaoke" not in hints, f"structural folder offered as hint: {hints}")


def test_folder_hints_never_auto_promoted():
    """§6.1: parent-folder names are recorded as low-confidence hints, never auto-promoted."""
    p = parse_stem("JUNGEL", folder_hints=["אייל גולן"])
    check(p.artist is None, f"folder hint was promoted to artist: {p.artist!r}")
    check(p.folder_hints == ["אייל גולן"], f"hint not recorded: {p.folder_hints}")


# --- script detection ---------------------------------------------------------------------


def test_script_detection():
    check(detect_script("שלום") == "he")
    check(detect_script("Hello") == "latn")
    check(detect_script("Кино") == "ru")


# --- determinism (§1.3) -------------------------------------------------------------------


def test_parser_is_deterministic():
    name = "SF222-06 - Duran Duran - Sunrise [Karaoke]"
    check(parse_stem(name).to_json() == parse_stem(name).to_json(), "parser is not deterministic")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passing")
    raise SystemExit(1 if failed else 0)
