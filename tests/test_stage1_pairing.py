"""Stage 1 §6.1 mass-insert + §6.3 pairing invariant tests.

The headline test here is `test_pair_survives_the_5_2_survivor_split`. It pins the one finding
that reshaped this stage: §5.2's survivor rule is applied per *file*, so for a song present in
two directories the mp3's survivor and the cdg's survivor land in different directories about
half the time. 1,120 real pairs in this library are in exactly that state. Pairing over
post-exclusion rows shreds every one of them into a stray audio file plus a discarded cdg.

That bug is invisible in a unit test that uses tidy fixtures — both halves survive together and
everything passes. So the fixture below is deliberately built to be *split*, with the exclusion
falling on opposite directories for the two halves, which is what the real data looks like.

Run: python3 -m pytest tests/ -q     (or: python3 tests/test_stage1_pairing.py)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from karaokemp import db, stage0, stage1


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


def _fresh_db() -> tuple:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "t.sqlite3"
    conn = db.connect(path)
    conn.executescript((Path(__file__).resolve().parent.parent / "schema.sql").read_text())
    return tmp, conn


def _entry(fid, path, md5, size=1000):
    return {"ID": fid, "Path": path, "Name": Path(path).name, "Size": size, "Hashes": {"md5": md5}}


def _load(conn, entries):
    stage0.upsert_locations(conn, entries)
    conn.commit()


def _pairs(conn):
    return {
        (r["audio_md5"], r["graphics_md5"])
        for r in conn.execute("SELECT audio_md5, graphics_md5 FROM provisional_pairs")
    }


def _statuses(conn):
    return {
        r["remote_path"]: (r["status"], r["archive_reason"])
        for r in conn.execute("SELECT remote_path, status, archive_reason FROM file_locations")
    }


# --- THE BIG ONE: §5.2 splits pairs across directories; §6.3 must not care -----------------


def test_pair_survives_the_5_2_survivor_split():
    """A pair whose two halves survive in DIFFERENT directories must still be found.

    Fixture mirrors the measured reality. One song, two directories, identical bytes in each.
    §5.2 keeps the lexicographically smallest drive_file_id per (md5, size) — applied to each
    file independently — so here the mp3 survives in dirA and the cdg survives in dirB. Neither
    directory ends up holding a complete post-exclusion pair.

    Pairing over survivors would see dirA = mp3-only (orphan) and dirB = cdg-only (orphan), and
    would then exclude the cdg as `orphan_cdg` — destroying a working mp3g track. Pairing over
    the full pre-exclusion listing sees the pair in both directories.
    """
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("aaa", "dirA/song.mp3", "MD5_AUDIO"),   # smallest id for the mp3 -> survives here
        _entry("zzz", "dirB/song.mp3", "MD5_AUDIO"),
        _entry("mmm", "dirA/song.cdg", "MD5_GFX"),
        _entry("bbb", "dirB/song.cdg", "MD5_GFX"),     # smallest id for the cdg -> survives here
    ])
    stage0.exact_dup_pass(conn)

    st = _statuses(conn)
    check(st["dirA/song.mp3"][0] == "remote_only", "fixture: mp3 should survive in dirA")
    check(st["dirB/song.cdg"][0] == "remote_only", "fixture: cdg should survive in dirB")
    check(st["dirA/song.cdg"][0] == "excluded", "fixture: dirA cdg should be an exact_dup")
    check(st["dirB/song.mp3"][0] == "excluded", "fixture: dirB mp3 should be an exact_dup")

    res = stage1.pair_mp3g(conn)
    check(_pairs(conn) == {("MD5_AUDIO", "MD5_GFX")}, f"pair was lost: {_pairs(conn)}")
    check(res["orphan_cdg"]["excluded_locations"] == 0, "a split pair was shredded into orphans")
    check(res["orphan_mp3"]["blobs"] == 0, "a split pair left a stray audio_only")


def test_naive_post_exclusion_pairing_would_have_failed():
    """Guards the guard: proves the fixture above actually exercises the bug.

    If a future refactor made the fixture's halves survive together, the test above would keep
    passing while testing nothing. This asserts the negative directly — restricting the same
    listing to non-excluded rows really does lose the pair — so the fixture cannot rot into a
    tautology unnoticed.
    """
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("aaa", "dirA/song.mp3", "MD5_AUDIO"),
        _entry("zzz", "dirB/song.mp3", "MD5_AUDIO"),
        _entry("mmm", "dirA/song.cdg", "MD5_GFX"),
        _entry("bbb", "dirB/song.cdg", "MD5_GFX"),
    ])
    stage0.exact_dup_pass(conn)
    survivors = conn.execute(
        "SELECT id, remote_path, filetype, gdrive_md5 FROM file_locations WHERE status!='excluded'"
    ).fetchall()
    naive, _ = stage1._pair_over_listing(survivors)
    check(naive == {}, "fixture no longer reproduces the split — the big test above is now vacuous")


# --- §6.3 orphan handling -----------------------------------------------------------------


def test_orphan_cdg_with_no_candidate_is_excluded():
    tmp, conn = _fresh_db()
    _load(conn, [_entry("c1", "d/lonely.cdg", "MD5_GFX")])
    stage1.pair_mp3g(conn)
    check(_statuses(conn)["d/lonely.cdg"] == ("excluded", "orphan_cdg"))


def test_orphan_mp3_is_never_archived():
    """§6.3: orphan MP3s are possible full originals (Demucs input) — do not archive."""
    tmp, conn = _fresh_db()
    _load(conn, [_entry("m1", "d/lonely.mp3", "MD5_AUDIO")])
    res = stage1.pair_mp3g(conn)
    check(_statuses(conn)["d/lonely.mp3"] == ("remote_only", None), "orphan mp3 was archived")
    check(res["orphan_mp3"]["blobs"] == 1)
    rows = list(conn.execute("SELECT remote_path FROM v_provisional_orphan_mp3"))
    check([r["remote_path"] for r in rows] == ["d/lonely.mp3"], "orphan mp3 view is wrong")


def test_orphan_cdg_exclusion_is_reversible():
    """If the missing mp3 later appears on Drive, the cdg must come back.

    Same shape as the §5.2 survivor bug caught in Stage 0: a one-way status write strands a file
    as `excluded` forever, so it is never downloaded and the song is silently gone (§1.2).
    """
    tmp, conn = _fresh_db()
    _load(conn, [_entry("c1", "d/song.cdg", "MD5_GFX")])
    stage1.pair_mp3g(conn)
    check(_statuses(conn)["d/song.cdg"] == ("excluded", "orphan_cdg"), "setup failed")

    _load(conn, [_entry("m1", "d/song.mp3", "MD5_AUDIO")])  # re-enumeration finds the mp3
    res = stage1.pair_mp3g(conn)
    check(_statuses(conn)["d/song.cdg"] == ("remote_only", None), "cdg stranded as excluded")
    check(res["orphan_cdg"]["restored_locations"] == 1)
    check(_pairs(conn) == {("MD5_AUDIO", "MD5_GFX")})


def test_exact_dup_bookkeeping_is_not_clobbered():
    """An exact_dup exclusion belongs to Stage 0 and must survive Stage 1 (§5.2)."""
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("aaa", "dirA/lonely.cdg", "MD5_GFX"),
        _entry("zzz", "dirB/lonely.cdg", "MD5_GFX"),
    ])
    stage0.exact_dup_pass(conn)
    stage1.pair_mp3g(conn)
    st = _statuses(conn)
    check(st["dirB/lonely.cdg"] == ("excluded", "exact_dup"), f"Stage 0's reason was overwritten: {st}")
    check(st["dirA/lonely.cdg"] == ("excluded", "orphan_cdg"), f"survivor not excluded: {st}")


# --- §6.3 "route to review rather than guess" ---------------------------------------------


def test_basename_collision_goes_to_review_not_a_guess():
    """Drive allows two different mp3s under one basename in one folder (§5.1). Which cdg goes
    with which is unknowable from the listing, so §6.3 says review it rather than guess."""
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("m1", "d/song.mp3", "MD5_AUDIO_1"),
        _entry("m2", "d/song.mp3", "MD5_AUDIO_2", size=2000),  # same name, different bytes
        _entry("c1", "d/song.cdg", "MD5_GFX"),
    ])
    res = stage1.pair_mp3g(conn)
    check(_pairs(conn) == set(), "guessed a pair from an ambiguous basename")
    check(res["review_queue"]["basename_collisions"] == 1)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM review_queue")]
    check(kinds == ["pair_mismatch"], f"wrong review kind: {kinds}")


def test_orphan_cdg_with_a_near_name_mp3_is_reviewed_not_excluded():
    """'alesha -lipstick.cdg' / 'alesha-lipstick.mp3' — a real pair, one space apart.

    §6.3's exact-basename rule misses it. Auto-excluding the cdg would lose a working track to a
    typo, so it goes to review and stays downloadable pending a human verdict.
    """
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "d/alesha -lipstick.cdg", "MD5_GFX"),
        _entry("m1", "d/alesha-lipstick.mp3", "MD5_AUDIO"),
    ])
    res = stage1.pair_mp3g(conn)
    check(_statuses(conn)["d/alesha -lipstick.cdg"] == ("remote_only", None), "cdg was excluded")
    check(res["orphan_cdg"]["recoverable_queued"] == 1)
    payload = json.loads(next(conn.execute("SELECT payload FROM review_queue"))["payload"])
    check(payload["matched_on"] == "name", payload)
    check(payload["candidate_audio_md5s"] == ["MD5_AUDIO"], payload)
    check(_pairs(conn) == set(), "a near-miss was auto-paired instead of reviewed")


def test_orphan_cdg_recovered_by_disc_id_when_the_name_order_is_flipped():
    """Real rows: 'SF275-16 - Pink - Sober.cdg' vs 'SF275-16 - Sober - Pink.MP3'.

    The two halves type artist and title in opposite orders, so no name-based rule can match
    them — but a catalogue number identifies exactly one track on exactly one disc.
    """
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "New/SF275-16 - Pink - Sober.cdg", "MD5_GFX"),
        _entry("m1", "New/SF275-16 - Sober - Pink.MP3", "MD5_AUDIO"),
    ])
    res = stage1.pair_mp3g(conn)
    check(res["orphan_cdg"]["recoverable_queued"] == 1, "disc-ID recovery missed a flipped pair")
    check(_statuses(conn)["New/SF275-16 - Pink - Sober.cdg"] == ("remote_only", None))
    payload = json.loads(next(conn.execute("SELECT payload FROM review_queue"))["payload"])
    check(payload["matched_on"] == "disc", payload)


def test_confirmed_pair_verdict_survives_a_pair_rerun():
    """pair_mp3g rebuilds provisional_pairs with DELETE + re-insert, so a confirmed pair must be
    re-derived from its review verdict every run — a row inserted by hand would be wiped."""
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "d/alesha -lipstick.cdg", "MD5_GFX"),
        _entry("m1", "d/alesha-lipstick.mp3", "MD5_AUDIO"),
    ])
    stage1.pair_mp3g(conn)
    rq_id = next(conn.execute("SELECT id FROM review_queue"))["id"]
    stage1.resolve_pair_mismatch(conn, rq_id, "confirm", "test", "same song, one space apart")
    stage1.pair_mp3g(conn)
    check(_pairs(conn) == {("MD5_AUDIO", "MD5_GFX")}, "confirmed pair not materialized")
    check(_statuses(conn)["d/alesha -lipstick.cdg"] == ("remote_only", None))
    # and it survives yet another rebuild
    stage1.pair_mp3g(conn)
    check(_pairs(conn) == {("MD5_AUDIO", "MD5_GFX")}, "rebuild wiped a confirmed pair")
    n = next(conn.execute("SELECT COUNT(*) AS n FROM review_queue"))["n"]
    check(n == 1, f"resolved question was re-asked ({n} rows)")


def test_rejected_pair_verdict_excludes_the_cdg_and_never_reasks():
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "d/alesha -lipstick.cdg", "MD5_GFX"),
        _entry("m1", "d/alesha-lipstick.mp3", "MD5_AUDIO"),
    ])
    stage1.pair_mp3g(conn)
    rq_id = next(conn.execute("SELECT id FROM review_queue"))["id"]
    stage1.resolve_pair_mismatch(conn, rq_id, "reject", "test", "different recordings")
    stage1.pair_mp3g(conn)
    check(_pairs(conn) == set(), "a rejected candidate was paired anyway")
    check(_statuses(conn)["d/alesha -lipstick.cdg"] == ("excluded", "orphan_cdg"),
          "rejected orphan cdg must be excluded per §6.3")
    n = next(conn.execute("SELECT COUNT(*) AS n FROM review_queue"))["n"]
    check(n == 1, f"settled question was re-asked ({n} rows)")


def test_pair_verdicts_are_never_overwritten():
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "d/alesha -lipstick.cdg", "MD5_GFX"),
        _entry("m1", "d/alesha-lipstick.mp3", "MD5_AUDIO"),
    ])
    stage1.pair_mp3g(conn)
    rq_id = next(conn.execute("SELECT id FROM review_queue"))["id"]
    stage1.resolve_pair_mismatch(conn, rq_id, "confirm", "test")
    try:
        stage1.resolve_pair_mismatch(conn, rq_id, "reject", "test")
        raise AssertionError("overwriting a verdict must raise")
    except ValueError:
        pass


def test_review_queue_never_asks_the_same_question_twice():
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "d/alesha -lipstick.cdg", "MD5_GFX"),
        _entry("m1", "d/alesha-lipstick.mp3", "MD5_AUDIO"),
    ])
    first = stage1.pair_mp3g(conn)
    second = stage1.pair_mp3g(conn)
    n = next(conn.execute("SELECT COUNT(*) AS n FROM review_queue"))["n"]
    check(n == 1, f"re-run duplicated a review question ({n} rows)")
    # §11 sizes the review burden from this number, so it must count questions actually asked,
    # not questions re-identified. A re-run adds no burden and must not claim to.
    check(first["review_queue"]["pair_mismatch_newly_queued"] == 1, first["review_queue"])
    check(second["review_queue"]["pair_mismatch_newly_queued"] == 0, second["review_queue"])


def test_a_collision_cdg_is_not_also_queued_as_an_orphan():
    """A cdg in a basename collision has no pair, so it looks like an orphan. Its collision row
    already asks the reviewer about that exact folder+basename — asking again under a second
    framing is noise, and excluding it would pre-empt the verdict being asked for."""
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("m1", "d/song.mp3", "MD5_AUDIO_1"),
        _entry("m2", "d/song.mp3", "MD5_AUDIO_2", size=2000),
        _entry("c1", "d/song.cdg", "MD5_GFX"),
    ])
    res = stage1.pair_mp3g(conn)
    check(res["review_queue"]["pair_mismatch_newly_queued"] == 1, res["review_queue"])
    check(res["orphan_cdg"]["recoverable_queued"] == 0, "queued the same cdg twice")
    check(res["orphan_cdg"]["excluded_locations"] == 0, "excluded a cdg that is under review")
    check(_statuses(conn)["d/song.cdg"] == ("remote_only", None))


def test_over_budget_stops_without_writing():
    """§11: exceeding the per-kind budget is a stop condition, not a speed bump."""
    from karaokemp import config

    tmp, conn = _fresh_db()
    entries = []
    for i in range(4):
        entries += [
            _entry(f"c{i}", f"d{i}/alesha -lipstick.cdg", f"GFX{i}"),
            _entry(f"m{i}", f"d{i}/alesha-lipstick.mp3", f"AUD{i}"),
        ]
    _load(conn, entries)
    original = config.REVIEW_QUEUE_BUDGET
    config.REVIEW_QUEUE_BUDGET = 2
    try:
        res = stage1.pair_mp3g(conn)
    finally:
        config.REVIEW_QUEUE_BUDGET = original
    check(res["over_budget"] is True, "budget overrun not detected")
    check(next(conn.execute("SELECT COUNT(*) AS n FROM review_queue"))["n"] == 0, "wrote anyway")
    check(_pairs(conn) == set(), "wrote pairs despite being over budget")


# --- many-to-many is real, not a conflict -------------------------------------------------


def test_one_cdg_may_pair_with_several_mp3s():
    """759 cdg blobs in this library pair with more than one mp3 blob: the graphics track was
    reused byte-identically across several re-encodes of the same audio. Stage 3's clustering
    collapses the audio variants; Stage 1 must not pick a winner or drop a pair here."""
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("c1", "dirA/under the boardwalk.cdg", "MD5_GFX"),
        _entry("m1", "dirA/under the boardwalk.mp3", "MD5_AUDIO_1"),
        _entry("c2", "dirB/under the boardwalk.cdg", "MD5_GFX"),        # identical graphics
        _entry("m2", "dirB/under the boardwalk.mp3", "MD5_AUDIO_2"),    # different copy
    ])
    stage0.exact_dup_pass(conn)
    stage1.pair_mp3g(conn)
    check(_pairs(conn) == {("MD5_AUDIO_1", "MD5_GFX"), ("MD5_AUDIO_2", "MD5_GFX")}, _pairs(conn))


# --- §1.3 idempotence ---------------------------------------------------------------------


def test_pairing_is_idempotent():
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("m1", "d/song.mp3", "MD5_AUDIO"),
        _entry("c1", "d/song.cdg", "MD5_GFX"),
        _entry("c2", "d/lonely.cdg", "MD5_ORPHAN"),
    ])
    stage1.pair_mp3g(conn)
    before = (_pairs(conn), _statuses(conn))
    res = stage1.pair_mp3g(conn)
    check((_pairs(conn), _statuses(conn)) == before, "second run changed state")
    check(res["orphan_cdg"]["excluded_locations"] == 0, "second run re-excluded")
    check(res["orphan_cdg"]["restored_locations"] == 0, "second run re-restored")


# --- §6.1 mass insert ---------------------------------------------------------------------


def test_parses_are_written_for_excluded_locations_too():
    """§5.2/§6.1: the dedup pass discarded bytes, never evidence.

    A duplicate's filename is often the better-labelled one ('SF018-02 - The Drifters - Under
    The Boardwalk' vs 'drifters-under the boardwalk'), and Stage 3 merges a whole hash group's
    hints onto the survivor's item. Skipping excluded rows here would throw that away for good.
    """
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("aaa", "Unorginized/drifters-under the boardwalk.mp3", "MD5_A"),
        _entry("zzz", "New/SF018-02 - The Drifters - Under The Boardwalk.mp3", "MD5_A"),
    ])
    stage0.exact_dup_pass(conn)
    stage1.write_parses(conn)
    rows = {
        r["remote_path"]: r["disc_id"]
        for r in conn.execute(
            "SELECT l.remote_path, p.disc_id FROM location_parses p "
            "JOIN file_locations l ON l.id = p.location_id"
        )
    }
    check(len(rows) == 2, f"an excluded location lost its parse: {rows}")
    check(rows["New/SF018-02 - The Drifters - Under The Boardwalk.mp3"] == "SF018",
          "the excluded copy's disc-ID evidence was discarded")


def test_write_parses_is_idempotent():
    tmp, conn = _fresh_db()
    _load(conn, [_entry("m1", "New/SF222-06 - Duran Duran - Sunrise.mp3", "MD5_A")])
    first = stage1.write_parses(conn)
    check(first["inserted"] == 1)
    second = stage1.write_parses(conn)
    check(second["changes"] == 0, f"re-run wrote rows: {second}")
    check(second["unchanged"] == 1)


def test_write_parses_refreshes_a_stale_parse():
    """A stored parse must never outlive the parser that made it without saying so."""
    tmp, conn = _fresh_db()
    _load(conn, [_entry("m1", "New/SF222-06 - Duran Duran - Sunrise.mp3", "MD5_A")])
    stage1.write_parses(conn)
    conn.execute("UPDATE location_parses SET artist='WRONG', payload='{}', parser_version='old'")
    conn.commit()
    res = stage1.write_parses(conn)
    check(res["reparsed"] == 1, f"a stale row was not refreshed: {res}")
    artist = next(conn.execute("SELECT artist FROM location_parses"))["artist"]
    check(artist == "Duran Duran", f"stale value survived: {artist}")


def test_non_media_is_recorded_but_not_parsed_as_a_song():
    tmp, conn = _fresh_db()
    _load(conn, [_entry("t1", "d/Thumbs.db", "MD5_T")])
    stage1.write_parses(conn)
    row = next(conn.execute("SELECT layout, artist FROM location_parses"))
    check(row["layout"] == "non_media", row["layout"])
    check(row["artist"] is None, "invented an artist from Windows shell cruft")


def test_dry_run_writes_nothing():
    tmp, conn = _fresh_db()
    _load(conn, [
        _entry("m1", "d/song.mp3", "MD5_AUDIO"),
        _entry("c1", "d/lonely.cdg", "MD5_GFX"),
    ])
    stage1.write_parses(conn, dry_run=True)
    stage1.pair_mp3g(conn, dry_run=True)
    check(next(conn.execute("SELECT COUNT(*) AS n FROM location_parses"))["n"] == 0)
    check(_pairs(conn) == set())
    check(_statuses(conn)["d/lonely.cdg"] == ("remote_only", None), "dry run mutated a status")


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
