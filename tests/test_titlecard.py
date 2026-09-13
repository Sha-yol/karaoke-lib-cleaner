"""Stage 5 title-card OCR invariant tests. NO network and NO third-party package: this
stage is export → external process → apply (§10's shape), so the only external tool touched
is ffmpeg, and the tests that need it skip themselves loudly when it is unavailable. Pinned:

  * the core defect this design exists to prevent: `artist` is written ONLY from
    `styled_artist`; a card whose only name is a `writer_credit` (KaraFun parentheses,
    Hebrew מילים/לחן) writes NO artist row and no song_metadata row anywhere containing
    that name;
  * `year` is promoted for producer='karaoke_channel' and NOT for 'karafun' (whose year is
    a "© RECISIO" karaoke-producer copyright, not the song's);
  * card_found=false writes the evidence row and zero song_metadata rows;
  * v_metadata precedence: a title_card_ocr title loses to musicbrainz_text and
    spotify_text, and beats id3 and filename;
  * the worklist excludes items that already have a title_cards row (re-run resume) and
    items that already have a title (the whole point is the title-less ones);
  * classify_layout on synthetic solid-colour frames; extract_frames skipping a timestamp
    past the end of the file instead of failing the item;
  * `extract` writes a well-formed manifest with the MEASURED model routing (dark → haiku,
    purple → haiku, unknown → sonnet) and --dry-run writes nothing at all;
  * `ingest` applies a good line through the same `promote()`, rejects each malformed-line
    class WHOLE (never half-applying), and is idempotent on a second run;
  * §11 retune re-derives promotion from stored evidence with no second read.

Run: python3 tests/test_titlecard.py    (or: python3 -m pytest tests/ -q)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from karaokemp import config, db, titlecard


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


def _add_video_item(conn, *, title=None, title_source="filename", local_path="/nonexistent.mp4",
                    filetype="mp4", verdict="sole_copy", status="active"):
    """A winner/sole video item with one staged location — the shape the worklist wants."""
    _seq[0] += 1
    n = _seq[0]
    sha = f"tcsha{n:04d}"
    conn.execute("INSERT INTO blobs (content_hash, size_bytes, integrity_status) "
                 "VALUES (?, 1000, 'decoded_ok')", (sha,))
    cur = conn.execute(
        "INSERT INTO media_items (format, duration_sec, quality_verdict, status) "
        "VALUES ('video', 200.0, ?, ?)", (verdict, status))
    item_id = cur.lastrowid
    conn.execute("INSERT INTO media_item_files (media_item_id, content_hash, role) "
                 "VALUES (?,?,'av')", (item_id, sha))
    cur = conn.execute(
        "INSERT INTO file_locations (drive_file_id, remote_path, local_path, filetype, "
        "content_hash, role, status) VALUES (?,?,?,?,?,'av','staged')",
        (f"drive{n}", f"/remote/SONG-{n}.{filetype}", local_path, filetype, sha))
    loc_id = cur.lastrowid
    if title is not None:
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
            "updated_at) VALUES (?,'title',?,?,0.5,?)",
            (item_id, title, title_source, db.utcnow()))
    conn.commit()
    return item_id, loc_id


def _card(**kw):
    """A TitleCard with every field explicit."""
    fields = {"card_found": True, "producer": "other", "title": None, "styled_artist": None,
              "writer_credit": None, "year": None, "musical_key": None, "script": None}
    fields.update(kw)
    return titlecard.TitleCard(**fields)


def _result(item_id, card, **kw):
    return titlecard.CardResult(media_item_id=item_id, card=card, **kw)


def _meta(conn, item_id, source="title_card_ocr"):
    return {r["field"]: r["value"] for r in conn.execute(
        "SELECT field, value FROM song_metadata WHERE media_item_id=? AND source=?",
        (item_id, source))}


# --- work-packet / results fixtures ----------------------------------------------------------

def _packet_line(item_id, loc_id, *, layout="karaoke_channel_like", model="haiku"):
    return {"media_item_id": item_id, "location_id": loc_id, "layout": layout,
            "model": model, "frames": [f"/artifacts/titlecards/{item_id}/t2.5.jpg"],
            "frame_ts": [2.5, 5.0, 9.0], "remote_path": f"/remote/SONG-{item_id}.mp4"}


def _write_packet(records) -> Path:
    """Hand-write a manifest, so the ingest tests need neither ffmpeg nor real video."""
    path = titlecard.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                    encoding="utf-8")
    return path


def _write_results(tmp: Path, lines, name="results.jsonl") -> Path:
    path = tmp / name
    path.write_text("".join(
        (line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n"
        for line in lines), encoding="utf-8")
    return path


def _result_line(item_id, **kw):
    rec = {"media_item_id": item_id, "card_found": True, "producer": "karaoke_channel",
           "title": None, "styled_artist": None, "writer_credit": None, "year": None,
           "musical_key": None, "script": "latin", "model": "haiku"}
    rec.update(kw)
    return rec


# --- the core defect: writer credits are not artists ----------------------------------------

def test_artist_promoted_only_from_styled_artist():
    """`IN THE STYLE OF <name>` is a PERFORMING artist and is promotable."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn)
        promoted = titlecard.promote(conn, _result(
            item, _card(producer="karaoke_channel", title="Waterloo Sunset",
                        styled_artist="The Kinks", script="latin"), location_id=loc))
        conn.commit()
        check(sorted(promoted) == ["artist", "title"], promoted)
        rows = _meta(conn, item)
        check(rows.get("artist") == "The Kinks", rows)
        check(rows.get("title") == "Waterloo Sunset", rows)
        conn.close()


def test_writer_credit_is_never_promoted_to_artist():
    """THE defect this design exists to prevent. A KaraFun card's parenthesised name is the
    SONGWRITER (measured: 'The Shoop Shoop Song (It's In His Kiss)' credits Rudy Clark, who
    wrote it — the performers are Betty Everett and Cher). It must reach `title_cards` and
    NOTHING else: no artist row, and no song_metadata row of any field or source carrying
    that string."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn)
        promoted = titlecard.promote(conn, _result(
            item, _card(producer="karafun", title="The Shoop Shoop Song (It's In His Kiss)",
                        writer_credit="Rudy Clark", script="latin"), location_id=loc))
        conn.commit()
        check(promoted == ["title"], promoted)
        rows = _meta(conn, item)
        check("artist" not in rows, f"a writer credit must never become an artist: {rows}")
        leaked = conn.execute(
            "SELECT COUNT(*) FROM song_metadata WHERE value LIKE '%Rudy Clark%'"
        ).fetchone()[0]
        check(leaked == 0, "writer_credit leaked into song_metadata")
        card_row = conn.execute(
            "SELECT * FROM title_cards WHERE media_item_id=?", (item,)).fetchone()
        check(card_row["writer_credit"] == "Rudy Clark", "evidence must still be recorded")
        check(card_row["styled_artist"] is None)
        conn.close()


def test_hebrew_writer_credit_is_never_promoted():
    """מילים / לחן are words-by / music-by credits, same rule, Hebrew script preserved."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn, filetype="avi")
        promoted = titlecard.promote(conn, _result(
            item, _card(producer="other", title="עד סוף הקיץ", writer_credit="רפאל מירילה",
                        script="hebrew"), location_id=loc))
        conn.commit()
        check(promoted == ["title"], promoted)
        rows = _meta(conn, item)
        check("artist" not in rows, rows)
        check(rows["title"] == "עד סוף הקיץ", "Hebrew must be stored in Hebrew script")
        conn.close()


# --- year: producer-dependent ---------------------------------------------------------------

def test_year_promoted_for_karaoke_channel_only():
    """The Karaoke Channel prints a YEAR field (the song's). KaraFun prints '© 2011 RECISIO'
    — the karaoke producer's copyright year, which is not the song's and must not be
    promoted."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        kc, kc_loc = _add_video_item(conn)
        kf, kf_loc = _add_video_item(conn)
        titlecard.promote(conn, _result(kc, _card(
            producer="karaoke_channel", title="Waterloo Sunset", styled_artist="The Kinks",
            year=1967, musical_key="C"), location_id=kc_loc))
        titlecard.promote(conn, _result(kf, _card(
            producer="karafun", title="Something", writer_credit="Someone", year=2011),
            location_id=kf_loc))
        conn.commit()
        check(_meta(conn, kc).get("year") == "1967", _meta(conn, kc))
        check("year" not in _meta(conn, kf), "a RECISIO copyright year is not the song's")
        # the karafun year is still recorded as evidence, just not promoted
        check(conn.execute("SELECT year FROM title_cards WHERE media_item_id=?",
                           (kf,)).fetchone()["year"] == 2011)
        conn.close()


# --- no card ---------------------------------------------------------------------------------

def test_no_card_writes_evidence_and_no_metadata():
    """A lyrics/branding/blank frame must produce a title_cards row (so the item leaves the
    worklist and is never re-read) and zero song_metadata rows."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn)
        promoted = titlecard.promote(conn, _result(
            item, _card(card_found=False, producer="none"), location_id=loc))
        conn.commit()
        check(promoted == [], promoted)
        row = conn.execute("SELECT * FROM title_cards WHERE media_item_id=?",
                           (item,)).fetchone()
        check(row is not None and row["card_found"] == 0)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == 0)
        conn.close()


def test_no_card_ignores_any_stray_fields():
    """Defence in depth: card_found=false wins even if the reader also filled a title —
    nothing lyric-shaped may reach the DB through a self-declared miss."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn)
        promoted = titlecard.promote(conn, _result(
            item, _card(card_found=False, producer="none", title="some scrolling line"),
            location_id=loc))
        conn.commit()
        check(promoted == [], promoted)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == 0)
        conn.close()


# --- v_metadata precedence -------------------------------------------------------------------

def _title_winner(conn, item_id):
    row = conn.execute(
        "SELECT value, source FROM v_metadata WHERE media_item_id=? AND field='title'",
        (item_id,)).fetchone()
    return (row["value"], row["source"]) if row else (None, None)


def test_v_metadata_precedence_of_title_card_ocr():
    """OCR of the producer's own card outranks a filename parse and an id3 tag, and loses to
    the catalogue-matched corrections (spotify_text, musicbrainz_text)."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn)
        now = db.utcnow()
        for source, value in (("filename", "SONG-abcdef"), ("id3", "Track 03")):
            conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                         "confidence, updated_at) VALUES (?,'title',?,?,0.9,?)",
                         (item, value, source, now))
        titlecard.promote(conn, _result(
            item, _card(producer="karaoke_channel", title="From The Card"), location_id=loc))
        conn.commit()
        check(_title_winner(conn, item) == ("From The Card", "title_card_ocr"),
              _title_winner(conn, item))

        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                     "confidence, updated_at) VALUES (?,'title','From Spotify',"
                     "'spotify_text',0.1,?)", (item, now))
        conn.commit()
        check(_title_winner(conn, item) == ("From Spotify", "spotify_text"),
              "spotify_text must outrank title_card_ocr even at lower confidence")

        conn.execute("INSERT INTO song_metadata (media_item_id, field, value, source, "
                     "confidence, updated_at) VALUES (?,'title','From MB',"
                     "'musicbrainz_text',0.1,?)", (item, now))
        conn.commit()
        check(_title_winner(conn, item) == ("From MB", "musicbrainz_text"),
              "musicbrainz_text must outrank title_card_ocr")
        conn.close()


# --- worklist ---------------------------------------------------------------------------------

def test_worklist_excludes_done_and_titled_items():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        wanted, _ = _add_video_item(conn)
        titled, _ = _add_video_item(conn, title="Already Known")
        done, done_loc = _add_video_item(conn)
        titlecard.promote(conn, _result(done, _card(card_found=False, producer="none"),
                                        location_id=done_loc))
        conn.commit()
        ids = [r["item_id"] for r in titlecard.titlecard_worklist(conn)]
        check(ids == [wanted], f"expected only the title-less, un-OCR'd item: {ids}")
        # include_done drops both "done" filters — this is what keeps ingest idempotent
        ids_all = [r["item_id"] for r in titlecard.titlecard_worklist(conn, include_done=True)]
        check(sorted(ids_all) == sorted([wanted, titled, done]), ids_all)
        conn.close()


def test_worklist_requires_video_local_path_and_survivor_verdict():
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        ok, _ = _add_video_item(conn)
        _add_video_item(conn, filetype="mp3")            # not a video: no frames to read
        _add_video_item(conn, local_path=None)           # not downloaded yet
        _add_video_item(conn, verdict="alternate")           # §9.1: winners + sole copies only
        _add_video_item(conn, status="archived")
        ids = [r["item_id"] for r in titlecard.titlecard_worklist(conn)]
        check(ids == [ok], ids)
        conn.close()


# --- frames & layout classifier ---------------------------------------------------------------

def _solid_jpeg(colour: str) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-f", "lavfi", "-i", f"color=c={colour}:s=64x64", "-frames:v", "1",
         "-f", "image2", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, check=True)
    return proc.stdout


def test_classify_layout_on_synthetic_frames():
    """Reporting-and-routing classifier: black ⇒ Karaoke-Channel-like, purple ⇒ KaraFun-like,
    anything else ⇒ unknown (which is the signal that a new producer layout showed up)."""
    if shutil.which("ffmpeg") is None:
        print("  (skipped: ffmpeg not installed)")
        return
    check(titlecard.classify_layout(_solid_jpeg("black")) == "karaoke_channel_like")
    check(titlecard.classify_layout(_solid_jpeg("0x4B0082")) == "karafun_like", "indigo")
    check(titlecard.classify_layout(_solid_jpeg("0x7B2FA8")) == "karafun_like", "purple")
    check(titlecard.classify_layout(_solid_jpeg("white")) == "unknown")
    check(titlecard.classify_layout(_solid_jpeg("green")) == "unknown")
    check(titlecard.classify_layout(b"not a jpeg at all") == "unknown")


def test_model_routing_is_the_measured_one():
    """The 6-case eval: Haiku 4.5 handled both Latin layouts (5/6, including both
    writer-credit cases and both no-card cases) and failed ONLY the Hebrew card, which
    Sonnet then read exactly. Hebrew cards are photographic and classify as 'unknown', so
    'unknown' — which is also where an unseen producer lands — routes to Sonnet."""
    check(titlecard.model_for_layout("karaoke_channel_like") == "haiku")
    check(titlecard.model_for_layout("karafun_like") == "haiku")
    check(titlecard.model_for_layout("unknown") == "sonnet")
    check(titlecard.model_for_layout("karaoke_channel_like", "sonnet") == "sonnet",
          "--model-override must force one model for the whole packet")


def _make_video(path: Path, seconds: int = 3, colour: str = "black") -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-f", "lavfi", "-i", f"color=c={colour}:s=64x64:d={seconds}",
         "-c:v", "mpeg4", "-y", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, check=True)


def test_extract_frames_skips_timestamps_past_the_end():
    """A short file has no frame at 9s. That must cost the timestamp, not the item."""
    if shutil.which("ffmpeg") is None:
        print("  (skipped: ffmpeg not installed)")
        return
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "clip.avi"
        _make_video(vid, seconds=3)
        frames = titlecard.extract_frames(vid, (1.0, 2.0, 300.0))
        check(len(frames) == 2, f"expected 2 usable frames, got {len(frames)}")
        check(all(f.startswith(b"\xff\xd8") for f in frames), "frames must be JPEG")
        check(titlecard.extract_frames(Path(td) / "missing.avi") == [],
              "a missing file yields no frames rather than raising")


# --- extract: the work packet -------------------------------------------------------------------

def test_extract_writes_manifest_with_measured_model_routing():
    """One line per item under ARTIFACTS_DIR/titlecards, frames on disk, and the routing the
    eval measured: dark → haiku, purple → haiku, anything unrecognised → sonnet."""
    if shutil.which("ffmpeg") is None:
        print("  (skipped: ffmpeg not installed)")
        return
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        expected = {}
        for colour, layout, model in (("black", "karaoke_channel_like", "haiku"),
                                      ("0x4B0082", "karafun_like", "haiku"),
                                      # green: neither dark nor purple. Stands in for the
                                      # Hebrew cards' photographic backgrounds, which is what
                                      # 'unknown' means in practice and why it routes to Sonnet.
                                      ("green", "unknown", "sonnet")):
            vid = tmp / f"SONG-{colour}.mp4"
            _make_video(vid, seconds=10, colour=colour)
            item, loc = _add_video_item(conn, local_path=str(vid))
            expected[item] = (loc, layout, model)

        rep = titlecard.extract_work_packet(conn)
        check(rep.worklist == 3 and rep.processed == 3 and rep.failed == 0, rep.as_dict())
        check(rep.layouts == {"karaoke_channel_like": 1, "karafun_like": 1, "unknown": 1},
              rep.layouts)
        check(rep.models == {"haiku": 2, "sonnet": 1}, rep.models)
        check(rep.frames_written == 9, rep.frames_written)   # 3 items × 3 timestamps

        packet = titlecard.load_manifest()
        check(set(packet) == set(expected), packet.keys())
        for item, (loc, layout, model) in expected.items():
            rec = packet[item]
            check(set(rec) == {"media_item_id", "location_id", "layout", "model", "frames",
                               "frame_ts", "remote_path"}, rec.keys())
            check(rec["location_id"] == loc and rec["layout"] == layout, rec)
            check(rec["model"] == model, f"routing for {layout}: {rec['model']}")
            check(rec["frame_ts"] == [2.5, 5.0, 9.0], rec["frame_ts"])
            check(rec["remote_path"].startswith("/remote/"), rec["remote_path"])
            check(len(rec["frames"]) == 3, rec["frames"])
            for ts, p in zip(rec["frame_ts"], rec["frames"]):
                fp = Path(p)
                check(fp.parent == titlecard.frames_dir(item), p)
                check(fp.name == f"t{ts:g}.jpg", p)
                check(fp.read_bytes().startswith(b"\xff\xd8"), f"{p} must be a JPEG")
        # the operator runbook ships with the packet, prompt verbatim (§10 precedent)
        instr = (titlecard.titlecards_dir() / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        check(titlecard.PROMPT in instr, "the prompt must be reproducible from the packet")
        check("results.jsonl" in instr and "never let it influence" in instr.lower(), instr[:200])
        conn.close()


def test_extract_model_override_forces_one_model():
    if shutil.which("ffmpeg") is None:
        print("  (skipped: ffmpeg not installed)")
        return
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        vid = tmp / "SONG-uuid.mp4"
        _make_video(vid, seconds=10)                       # black ⇒ would route to haiku
        _add_video_item(conn, local_path=str(vid))
        rep = titlecard.extract_work_packet(conn, model_override="sonnet")
        check(rep.models == {"sonnet": 1}, rep.models)
        check(all(r["model"] == "sonnet" for r in titlecard.load_manifest().values()))
        conn.close()


def test_extract_dry_run_writes_nothing_but_reports_the_split():
    if shutil.which("ffmpeg") is None:
        print("  (skipped: ffmpeg not installed)")
        return
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        vid = tmp / "SONG-uuid.mp4"
        _make_video(vid, seconds=10, colour="0x4B0082")
        _add_video_item(conn, local_path=str(vid))
        rep = titlecard.extract_work_packet(conn, dry_run=True)
        check(rep.processed == 1 and rep.layouts.get("karafun_like") == 1, rep.as_dict())
        check(rep.models == {"haiku": 1}, rep.models)
        check(rep.frames_written == 0 and rep.manifest is None, rep.as_dict())
        check(not titlecard.manifest_path().exists(), "a dry run must write no manifest")
        check(not titlecard.titlecards_dir().exists() or
              not any(titlecard.titlecards_dir().iterdir()), "a dry run must write no frames")
        check(conn.execute("SELECT COUNT(*) FROM title_cards").fetchone()[0] == 0)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == 0)
        conn.close()


def test_extract_counts_a_missing_file_as_failed_and_omits_it_from_the_packet():
    """An item that yields no frames must not appear in the manifest: never ask the operator
    to read a file that does not exist."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        _add_video_item(conn, local_path=str(tmp / "gone.mp4"))
        rep = titlecard.extract_work_packet(conn)
        check(rep.failed == 1 and rep.processed == 1, rep.as_dict())
        check(titlecard.load_manifest() == {}, "no frames ⇒ no manifest line")
        conn.close()


# --- ingest: applying the operator's results ------------------------------------------------------

def test_ingest_applies_a_good_line():
    """The end state must be exactly what the old in-process API path produced for the same
    card: title + artist + year in song_metadata, everything else in title_cards."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item, loc = _add_video_item(conn)
        _write_packet([_packet_line(item, loc)])
        results = _write_results(tmp, [_result_line(
            item, title="Waterloo Sunset", styled_artist="The Kinks", year=1967,
            musical_key="C")])

        rep = titlecard.ingest_results(conn, results)
        check(rep.lines_read == 1 and rep.applied == 1 and rep.rejected == 0, rep.as_dict())
        check(rep.promoted_title == 1 and rep.promoted_artist == 1, rep.as_dict())
        check(_meta(conn, item) == {"title": "Waterloo Sunset", "artist": "The Kinks",
                                    "year": "1967"}, _meta(conn, item))
        row = conn.execute("SELECT * FROM title_cards WHERE media_item_id=?",
                           (item,)).fetchone()
        check(row["location_id"] == loc and row["musical_key"] == "C", dict(row))
        check(row["model"] == "haiku", row["model"])
        check(json.loads(row["frame_ts"]) == [2.5, 5.0, 9.0], row["frame_ts"])
        # raw_response is the operator's line VERBATIM (§11: evidence is what was said)
        check(json.loads(row["raw_response"])["title"] == "Waterloo Sunset", row["raw_response"])
        conn.close()


def test_ingest_routes_a_writer_credit_and_a_no_card_line():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        kf, kf_loc = _add_video_item(conn)
        none_item, none_loc = _add_video_item(conn)
        _write_packet([_packet_line(kf, kf_loc, layout="karafun_like"),
                       _packet_line(none_item, none_loc)])
        results = _write_results(tmp, [
            _result_line(kf, producer="karafun", title="The Shoop Shoop Song",
                         writer_credit="Rudy Clark", year=2011),
            _result_line(none_item, card_found=False, producer="none", script=None,
                         model="sonnet"),
        ])
        rep = titlecard.ingest_results(conn, results)
        check(rep.applied == 2 and rep.rejected == 0, rep.as_dict())
        check(rep.writer_credit_only == 1 and rep.no_card == 1, rep.as_dict())
        check(rep.promoted_artist == 0, "a writer credit is never an artist")
        check(_meta(conn, kf) == {"title": "The Shoop Shoop Song"}, _meta(conn, kf))
        check(_meta(conn, none_item) == {}, _meta(conn, none_item))
        check(conn.execute("SELECT COUNT(*) FROM song_metadata WHERE value LIKE '%Rudy%'"
                           ).fetchone()[0] == 0)
        conn.close()


def _reject_case(conn, tmp, packet, line):
    """Ingest one line against `packet`; return the report. Nothing may be applied."""
    _write_packet(packet)
    results = _write_results(tmp, [line])
    rep = titlecard.ingest_results(conn, results)
    return rep


def test_ingest_rejects_each_malformed_line_class_and_applies_nothing():
    """Reject the line rather than guess. Every case here is one where the alternative is
    inventing data — which is exactly what §6.1 refused to do for filenames."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item, loc = _add_video_item(conn)
        titled, titled_loc = _add_video_item(conn, title="Already Known")
        good = _packet_line(item, loc)
        ghost_id = 999_999

        cases = [
            ("unknown media_item_id",
             [good, _packet_line(ghost_id, loc)], _result_line(ghost_id, title="X")),
            ("not in the current worklist",
             [good, _packet_line(titled, titled_loc)], _result_line(titled, title="X")),
            ("not in the work packet manifest",
             [good], _result_line(item + 12345, title="X")),
            ("bad producer",
             [good], _result_line(item, producer="the_karaoke_channel", title="X")),
            ("bad script",
             [good], _result_line(item, title="X", script="ivrit")),
            ("bad year",
             [good], _result_line(item, title="X", year=197)),
            ("bad year",
             [good], _result_line(item, title="X", year=20250)),
            ("card_found is true but there is no title",
             [good], _result_line(item, title=None)),
            ("card_found is true but there is no title",
             [good], _result_line(item, title="   ")),
            ("bad card_found",
             [good], _result_line(item, card_found="yes", title="X")),
            ("bad media_item_id",
             [good], {"card_found": False, "producer": "none"}),
            ("not JSON",
             [good], "{this is not json"),
            ("bad title",
             [good], _result_line(item, title=1967)),
        ]
        for expect, packet, line in cases:
            rep = _reject_case(conn, tmp, packet, line)
            check(rep.applied == 0 and rep.rejected == 1,
                  f"{expect!r}: expected exactly one rejection, got {rep.as_dict()}")
            reason = rep.rejections[0]["reason"]
            check(reason.startswith(expect), f"expected {expect!r}, got {reason!r}")
            check(conn.execute("SELECT COUNT(*) FROM title_cards").fetchone()[0] == 0,
                  f"{expect!r}: a rejected line must write no evidence row")
            check(conn.execute("SELECT COUNT(*) FROM song_metadata WHERE "
                               "source='title_card_ocr'").fetchone()[0] == 0,
                  f"{expect!r}: a rejected line must promote nothing")
        conn.close()


def test_ingest_rejects_a_duplicate_media_item_id_in_one_file():
    """Two answers for one item is ambiguous, not a merge — take the first, reject the
    second, and say so."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item, loc = _add_video_item(conn)
        _write_packet([_packet_line(item, loc)])
        results = _write_results(tmp, [_result_line(item, title="First"),
                                       _result_line(item, title="Second")])
        rep = titlecard.ingest_results(conn, results)
        check(rep.applied == 1 and rep.rejected == 1, rep.as_dict())
        check(rep.rejections[0]["reason"].startswith("duplicate media_item_id"),
              rep.rejections)
        check(_meta(conn, item)["title"] == "First", _meta(conn, item))
        conn.close()


def test_ingest_reports_rejections_grouped_by_reason():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        a, a_loc = _add_video_item(conn)
        b, b_loc = _add_video_item(conn)
        _write_packet([_packet_line(a, a_loc), _packet_line(b, b_loc)])
        results = _write_results(tmp, [_result_line(a, title="X", year=12),
                                       _result_line(b, title="Y", year=99999)])
        rep = titlecard.ingest_results(conn, results)
        check(rep.rejected == 2 and rep.rejected_by_reason == {"bad year": 2},
              rep.as_dict())
        conn.close()


def test_ingest_is_idempotent():
    """Re-ingesting the same file must be a no-op beyond updated_at — that is what makes a
    half-finished operator session safe to resume by just re-running the whole file."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item, loc = _add_video_item(conn)
        _write_packet([_packet_line(item, loc)])
        results = _write_results(tmp, [_result_line(
            item, title="Waterloo Sunset", styled_artist="The Kinks", year=1967)])

        first = titlecard.ingest_results(conn, results)
        before = [dict(r) for r in conn.execute(
            "SELECT media_item_id, field, value, source, confidence FROM song_metadata "
            "ORDER BY field")]
        cards_before = conn.execute("SELECT COUNT(*) FROM title_cards").fetchone()[0]

        second = titlecard.ingest_results(conn, results)
        check(second.applied == first.applied == 1 and second.rejected == 0,
              second.as_dict())
        after = [dict(r) for r in conn.execute(
            "SELECT media_item_id, field, value, source, confidence FROM song_metadata "
            "ORDER BY field")]
        check(after == before, f"{before} != {after}")
        check(conn.execute("SELECT COUNT(*) FROM title_cards").fetchone()[0] == cards_before)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == 3)
        conn.close()


def test_ingest_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        item, loc = _add_video_item(conn)
        _write_packet([_packet_line(item, loc)])
        results = _write_results(tmp, [_result_line(
            item, title="Waterloo Sunset", styled_artist="The Kinks", year=1967)])
        rep = titlecard.ingest_results(conn, results, dry_run=True)
        check(rep.applied == 1 and rep.promoted_title == 1 and rep.promoted_artist == 1,
              rep.as_dict())
        check(conn.execute("SELECT COUNT(*) FROM title_cards").fetchone()[0] == 0)
        check(conn.execute("SELECT COUNT(*) FROM song_metadata").fetchone()[0] == 0)
        conn.close()


def test_ingest_without_a_work_packet_is_an_actionable_error():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = _fresh_env(tmp)
        results = _write_results(tmp, [_result_line(1, title="X")])
        try:
            titlecard.ingest_results(conn, results)
        except titlecard.TitleCardError as exc:
            check("titlecard extract" in str(exc), str(exc))
            conn.close()
            return
        raise AssertionError("an ingest with no manifest must raise, not invent frame_ts")


# --- prompt invariants ---------------------------------------------------------------------------

def test_prompt_carries_the_load_bearing_rules():
    """The prompt is handed to the operator verbatim, so a reword must not silently drop the
    rules the whole design rests on."""
    check("IN THE STYLE OF" in titlecard.PROMPT and "writer_credit" in titlecard.PROMPT)
    check("card_found=false" in titlecard.PROMPT and "NEVER report lyric text" in titlecard.PROMPT)
    check("מילים" in titlecard.PROMPT and "transliterate" in titlecard.PROMPT)
    check("RECISIO" in titlecard.PROMPT, "the © year caveat must stay in the prompt")
    check('"media_item_id"' in titlecard.PROMPT, "the output contract must be stated")


def test_no_third_party_imports():
    """The pipeline is stdlib-only again. `anthropic` / `pydantic` are not installed on this
    host and must never be needed."""
    src = (Path(__file__).resolve().parent.parent / "karaokemp" / "titlecard.py").read_text()
    for banned in ("import anthropic", "from anthropic", "import pydantic", "from pydantic"):
        check(banned not in src, f"{banned!r} must not reappear")
    check(not (Path(__file__).resolve().parent.parent / "requirements.txt").exists(),
          "requirements.txt must stay deleted — the project has no external dependencies")


# --- §11 retune -----------------------------------------------------------------------------------

def test_retune_re_derives_promotion_from_stored_evidence():
    """§11: the evidence row is kept so promotion rules can be retuned with no re-read."""
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_env(Path(td))
        item, loc = _add_video_item(conn)
        titlecard.promote(conn, _result(item, _card(
            producer="karaoke_channel", title="Waterloo Sunset", styled_artist="The Kinks",
            year=1967), location_id=loc))
        conn.commit()
        conn.execute("DELETE FROM song_metadata WHERE source='title_card_ocr'")
        conn.commit()
        rep = titlecard.retune_titlecards(conn)
        check(rep["cards_re_derived"] == 1 and rep["fields_written"] == 3, rep)
        rows = _meta(conn, item)
        check(rows == {"title": "Waterloo Sunset", "artist": "The Kinks", "year": "1967"}, rows)
        check(json.loads(conn.execute(
            "SELECT frame_ts FROM title_cards WHERE media_item_id=?",
            (item,)).fetchone()["frame_ts"]) == list(titlecard.FRAME_TIMESTAMPS))
        conn.close()


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
