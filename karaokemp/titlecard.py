"""Stage 5 §9.1 — title-card OCR: unblocking the items §9.1 cannot even search.

`stage5.enrich_worklist` filters `WHERE m.title IS NOT NULL` (stage5.py:412), so an item
with no title never reaches MusicBrainz, never queues a `metadata_match` review, and never
moves. `count_unsearchable` reported 168 such items on the last run and then nothing
happened to any of them — by construction, not by accident.

They are title-less because §6.1 refused to invent metadata: 308 `SONG-<uuid>.mp4` and ~300
`Chapter_NN.avi` names match `stage1.OPAQUE_NAME` and parse to layout='opaque' with
artist/title NULL, since splitting `SONG-<uuid>` on the dash manufactures artist='SONG'.
That was the right call about FILENAMES. It is the wrong conclusion about the FILES: these
are videos, and every one carries an on-screen title card in the first ~10 seconds.
ffprobe finds nothing usable in the containers (the mp4s were transcoded by Google —
"ISO Media file produced by Google Inc." — which strips the original tags), so the pixels
are the only remaining signal.

This stage extracts a few frames, gets the card read, and writes the recovered title into
`song_metadata` (source 'title_card_ocr'). That is the whole job: **it does not identify
songs, it unblocks the MB search that already exists.** An item that gains a title here
enters the next `enrich` run's worklist through the front door and is accepted / reviewed /
left alone by the same §9.1 bands as everything else.

SHAPE — export → external process → apply, the §10 precedent (`review.py`), not an API
client. The reading is done by Claude Code subagents driven by an operator's session, so
this module never makes a network call and the pipeline keeps its zero-dependency property
(everything here is stdlib on purpose: stage5 does its MusicBrainz/AcoustID HTTP with
urllib, stage3 shells out to ffmpeg/fpcalc, the index is sqlite3). Two commands:

  * `extract` writes the WORK PACKET — frames under `ARTIFACTS_DIR/titlecards/<item>/` plus
    a `manifest.jsonl` naming, per item, the frames, the classified layout and the model the
    operator should route it to. Frames are derived, disposable data; §3.9 `artifacts` is
    exactly where that belongs, not the repo.
  * `ingest` applies the operator's `results.jsonl` through the SAME `promote()` the stage
    has always used. It validates strictly and rejects a line rather than guessing, per-item
    commit, idempotent — re-ingesting a file changes nothing but `updated_at`.

Two rules are structural, not advisory:

* **A writer credit is never an artist.** Only a Karaoke Channel card's
  `IN THE STYLE OF <name>` line names a PERFORMER. The name in parentheses under a KaraFun
  title is the SONGWRITER (measured: a card titled "The Shoop Shoop Song (It's In His
  Kiss)" credits Rudy Clark, who wrote it — the famous performers are Betty Everett and
  Cher), and Hebrew `מילים:` / `לחן:` lines are words-by / music-by credits. Those land in
  `title_cards.writer_credit`, a column nothing promotes and nothing reads but a human.
  Promotion cannot mix them up because they never share a field with `styled_artist`.
* **Nothing lyric-shaped may enter the DB.** Frames after the intro show scrolling lyrics.
  The prompt targets the title card only and requires `card_found=false` on a lyrics or
  branding frame, rather than reporting a lyric line as a title.

Same year caveat: KaraFun cards print `© <year> RECISIO`, the karaoke producer's copyright
year, NOT the song's release year — so `year` is promoted only for `karaoke_channel`.

Hebrew titles are stored in Hebrew script, never transliterated (standing project rule; end
users search in Hebrew, and a correct-but-Latin "Eyal Golan" is unusable). Same rule §6.1
already applies to the parser.

Resumability needs no new state column: an item with a `title_cards` row is out of the
worklist, card or no card. Per-item commits, Ctrl-C-safe, like every other pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .db import utcnow

# Model routing, per layout. MEASURED, not guessed — a 6-case eval covering every failure
# mode this stage has (2026-07-26):
#   Haiku 4.5 got 5/6. It read the Karaoke Channel card correctly (title, `IN THE STYLE OF`
#   performer, year, key); it routed BOTH KaraFun parenthesised names to `writer_credit`
#   rather than `artist`, including the hard case where the songwriter is also a famous
#   performer of the song; and it returned card_found=false for both a branding bumper and a
#   lyrics frame, without transcribing any lyric text. It failed ONLY on the Hebrew card:
#   one letter wrong in the title, and both credit lines dropped.
#   Sonnet then read two Hebrew cards perfectly — exact titles, both credits.
# So Hebrew (and anything else we have not seen) goes to Sonnet and the two known Latin
# layouts go to Haiku. The colour classifier already separates them: Hebrew cards in this
# library have photographic backgrounds and land in 'unknown', which is also where an unseen
# producer lands — and an unseen producer is exactly the case worth spending the better
# model on.
MODEL_HAIKU = "haiku"
MODEL_SONNET = "sonnet"
MODELS = (MODEL_HAIKU, MODEL_SONNET)
MODEL_FOR_LAYOUT = {
    "karaoke_channel_like": MODEL_HAIKU,   # dark card, Latin script — 5/6 eval, cheap
    "karafun_like":         MODEL_HAIKU,   # purple card, Latin script — incl. both writer-credit cases
    "unknown":              MODEL_SONNET,  # Hebrew + any unseen producer — Haiku's only failure
}
DEFAULT_MODEL = MODEL_HAIKU

# t=2.5/5/9s: measured across all 95 SONG frames, the card is up by ~3s and still up at 9s
# on the long intros, while 9s is early enough that lyrics have not started on the short
# ones. Three frames also cover the outliers whose 3s frame is a branding bumper.
FRAME_TIMESTAMPS = (2.5, 5.0, 9.0)
FFMPEG_TIMEOUT_SEC = 60

# Video extensions present in this library (§5.3 census). mp3/cdg items have no frames.
VIDEO_FILETYPES = ("mp4", "avi", "mpg", "mpeg", "vob", "wmv", "dat", "mkv")

# Ordinal, not a probability (§3.6). It only ever tie-breaks between two title_card_ocr
# rows for the same field, since precedence against other sources is decided by v_metadata's
# source CASE — so the absolute value carries no claim about OCR accuracy.
OCR_CONFIDENCE = 0.8

# The result contract's enums. `ingest` rejects anything outside them rather than coercing:
# an unrecognised producer means the reader saw a layout we have no promotion rules for, and
# guessing one would be the exact failure mode the styled_artist/writer_credit split exists
# to prevent.
PRODUCERS = ("karaoke_channel", "karafun", "other", "none")
SCRIPTS = ("latin", "hebrew", "other")
# Plausible-year window. The library's oldest content is 1950s country and its newest is
# contemporary pop; anything outside this is an OCR slip (a duration read as a year, a
# catalogue number, a truncated digit), not a year.
YEAR_MIN, YEAR_MAX = 1900, 2100


class TitleCardError(Exception):
    """Actionable failure — no work packet, unreadable results file. Not weather."""


CARD_FIELDS = ("card_found", "producer", "title", "styled_artist", "writer_credit",
               "year", "musical_key", "script")


@dataclass
class TitleCard:
    """The result contract for one item. A plain dataclass: this used to be a pydantic model
    backing an SDK structured-output call, and it is not one any more — the pipeline has no
    third-party dependencies and `ingest` validates the JSON itself (see `_parse_result`).

    `styled_artist` and `writer_credit` are separate fields on purpose — see the module
    docstring; the split is the design."""

    card_found: bool = False
    producer: str = "none"
    title: str | None = None
    styled_artist: str | None = None
    writer_credit: str | None = None
    year: int | None = None
    musical_key: str | None = None
    script: str | None = None


def card_payload(card: TitleCard) -> dict:
    """Plain dict of a TitleCard, in the contract's field order."""
    return {f: getattr(card, f) for f in CARD_FIELDS}


@dataclass
class CardResult:
    """One item's OCR outcome: the TitleCard plus the provenance `title_cards` records
    (which location the frames came from, which timestamps were sent, which model answered,
    and the raw response). Bundled so `promote` takes exactly what it stores."""

    media_item_id: int
    card: TitleCard
    location_id: int | None = None
    frame_ts: tuple = FRAME_TIMESTAMPS
    model: str = DEFAULT_MODEL
    raw_response: str | None = None


# --- artifact paths (§3.9) ------------------------------------------------------------------
# Read `config.ARTIFACTS_DIR` at call time, never at import: the tests (and KARAOKEMP_LIBRARY)
# repoint the whole §2 layout at a temp dir after this module is imported.

def titlecards_dir() -> Path:
    return Path(config.ARTIFACTS_DIR) / "titlecards"


def manifest_path() -> Path:
    return titlecards_dir() / "manifest.jsonl"


def frames_dir(media_item_id: int) -> Path:
    return titlecards_dir() / str(media_item_id)


def _ts_label(ts: float) -> str:
    """'2.5' / '5' / '9' — %g so whole timestamps do not become 't5.0.jpg'."""
    return f"{float(ts):g}"


# --- frame extraction ----------------------------------------------------------------------

def extract_frames(local_path, timestamps=FRAME_TIMESTAMPS) -> list[bytes]:
    """JPEG bytes for each timestamp, in order. Timestamps that yield no frame are SKIPPED,
    not fatal: a 40-second Chapter file has no frame at 9s, and that is not a reason to give
    up on the two frames it does have.

    `-nostdin` + `stdin=DEVNULL` are both mandatory and were learned the hard way: ffmpeg
    otherwise consumes the CALLER's stdin, so a loop reading from a pipe or a terminal has
    its input eaten by the first extraction. (ffprobe is the odd one out — it rejects
    `-nostdin` with "Option not found" — so anything shelling out to ffprobe passes
    stdin=DEVNULL alone.)
    """
    frames: list[bytes] = []
    for ts in timestamps:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{float(ts)}", "-i", str(local_path),
            "-frames:v", "1", "-vf", "scale=640:-1", "-f", "image2", "-",
        ]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=FFMPEG_TIMEOUT_SEC, check=False)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode == 0 and proc.stdout:
            frames.append(proc.stdout)
    return frames


def _timed_frames(local_path, timestamps=FRAME_TIMESTAMPS) -> list[tuple[float, bytes]]:
    """(timestamp, jpeg) pairs, skipping timestamps that yielded nothing.

    One `extract_frames` call per timestamp, which costs exactly the same ffmpeg invocations
    as one call for all three — but keeps the mapping. `extract_frames` deliberately returns
    a bare list (a short file silently drops its 9s frame), and the manifest has to name each
    file after the timestamp it actually came from, so the association cannot be inferred
    from position."""
    out: list[tuple[float, bytes]] = []
    for ts in timestamps:
        got = extract_frames(local_path, (ts,))
        if got:
            out.append((float(ts), got[0]))
    return out


def _mean_rgb(jpeg_bytes: bytes) -> tuple[int, int, int] | None:
    """Mean colour of a JPEG, via ffmpeg scaling it to a single pixel. Through a temp file
    rather than a pipe: `-nostdin` and `-i pipe:0` are mutually exclusive, and losing
    `-nostdin` is exactly the stdin-eating bug documented in `extract_frames`."""
    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        tmp.write(jpeg_bytes)
        tmp.flush()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", tmp.name, "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=FFMPEG_TIMEOUT_SEC, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return None
    if proc.returncode != 0 or len(proc.stdout) < 3:
        return None
    return proc.stdout[0], proc.stdout[1], proc.stdout[2]


def classify_layout(jpeg_bytes: bytes) -> str:
    """'karaoke_channel_like' | 'karafun_like' | 'unknown' from the frame's mean colour.

    A deterministic, free pre-classifier over the two layouts that account for 92 of the 95
    measured SONG frames: The Karaoke Channel's card is black (62 frames), KaraFun's is
    purple (30). Thresholds below separated all 95 correctly.

    This is REPORTING and ROUTING, not semantics. It decides which model reads the frames
    (see MODEL_FOR_LAYOUT) and it makes a producer layout we have never seen show up as a
    spike in `unknown` in the run report instead of being silently OCR'd under assumptions
    that do not hold. Which field means what is decided by the reader's output — never by
    this function.
    """
    rgb = _mean_rgb(jpeg_bytes)
    if rgb is None:
        return "unknown"
    r, g, b = rgb
    if r < 60 and g < 60 and b < 60:
        return "karaoke_channel_like"
    if b > g and r > g:          # purple = red and blue both above green
        return "karafun_like"
    return "unknown"


def model_for_layout(layout: str, override: str | None = None) -> str:
    """Routed model for a layout. `override` forces one model for a whole run (`--model-override`),
    for re-reading a batch with the better model without re-classifying anything."""
    if override:
        return override
    return MODEL_FOR_LAYOUT.get(layout, MODEL_SONNET)


# --- the reading instructions ---------------------------------------------------------------

# The prompt does three jobs, in decreasing order of how badly a mistake would hurt:
# keep authorship credits out of the artist field, keep lyrics out of the DB entirely, and
# keep Hebrew in Hebrew. It is handed to the operator verbatim (INSTRUCTIONS.md) rather than
# sent by this process — the reading happens in a Claude Code session, not here.
PROMPT = """These images are still frames captured from the first ten seconds of a karaoke \
video, in chronological order. Your only job is to read the on-screen TITLE CARD if one is \
present.

Producers seen in this library:
- "The Karaoke Channel": a black card with a large title, the line "IN THE STYLE OF" \
followed by a name, and YEAR / DURATION / KEY fields.
- "KaraFun": a purple card with a "karafun" logo, the title, a name in parentheses \
underneath, a publisher line, and a "(c) <year> RECISIO" line.
- Other producers, including Hebrew cards with the title in a banner and credits following \
"מילים:" (words by) and/or "לחן:" (music by).
Set producer to whichever of these produced the card, or "none" when no card is found.

Field rules. These matter more than anything else in this prompt:
- "IN THE STYLE OF <name>" names the PERFORMING artist. That name, and only that kind of \
name, goes in styled_artist.
- A name in parentheses under a KaraFun title, and any name following "מילים:" or "לחן:", \
is the SONGWRITER or COMPOSER — not the performer. It goes in writer_credit and must never \
be reported as the performer. If you are not certain a name is a performing artist, it \
belongs in writer_credit.
- year is the song's year only when the card states it as such (the Karaoke Channel "YEAR" \
field). A "(c) <year> RECISIO" line is the karaoke producer's own copyright year, not the \
song's — leave year null in that case.
- musical_key is the card's KEY field, if it has one.
- script is the script the title itself is written in.

If the frames show only scrolling song lyrics, a generic branding or promotional screen, a \
blank screen, or no readable card, set card_found=false and leave every other field null. \
NEVER report lyric text as a title — a line of a song is not a title card. Do not \
transcribe song lyrics anywhere in your answer.

Hebrew text must be returned in Hebrew script, exactly as written on the card. Do not \
transliterate it into Latin letters and do not translate it.

Answer with ONE JSON object on a single line, and nothing else:
{"media_item_id": <the id you were given>, "card_found": true|false, \
"producer": "karaoke_channel"|"karafun"|"other"|"none", "title": string|null, \
"styled_artist": string|null, "writer_credit": string|null, "year": <4-digit int>|null, \
"musical_key": string|null, "script": "latin"|"hebrew"|"other"|null, \
"model": "<the model that read the frames>"}"""


# --- promotion -----------------------------------------------------------------------------

def promote(conn, result: CardResult) -> list[str]:
    """Write the evidence row, then the promotable song_metadata rows. Returns the field
    names promoted (possibly empty — a found card with no readable title promotes nothing).

    What is promotable, and why each rule exists:
      * title         — always, when present. This is the entire point of the stage: it is
                        what puts the item back in §9.1's worklist.
      * artist        — ONLY from styled_artist. `writer_credit` is an authorship credit and
                        is written nowhere but `title_cards`; see the module docstring.
      * year          — ONLY for producer='karaoke_channel'. KaraFun's year is a "© <year>
                        RECISIO" line: the karaoke producer's copyright, not the song's.
    musical_key and script are evidence for a human, not catalogue fields (§3.6 has no
    column for either), so they stay in `title_cards` too.
    """
    card = result.card
    now = utcnow()
    conn.execute(
        "INSERT INTO title_cards (media_item_id, location_id, producer, card_found, title, "
        "styled_artist, writer_credit, year, musical_key, script, frame_ts, model, "
        "raw_response, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(media_item_id) DO UPDATE SET "
        "location_id=excluded.location_id, producer=excluded.producer, "
        "card_found=excluded.card_found, title=excluded.title, "
        "styled_artist=excluded.styled_artist, writer_credit=excluded.writer_credit, "
        "year=excluded.year, musical_key=excluded.musical_key, script=excluded.script, "
        "frame_ts=excluded.frame_ts, model=excluded.model, "
        "raw_response=excluded.raw_response, extracted_at=excluded.extracted_at",
        (result.media_item_id, result.location_id, card.producer, int(bool(card.card_found)),
         card.title, card.styled_artist, card.writer_credit, card.year, card.musical_key,
         card.script, json.dumps([float(t) for t in result.frame_ts]), result.model,
         result.raw_response, now),
    )
    if not card.card_found:
        return []

    values: list[tuple[str, str]] = []
    if card.title:
        values.append(("title", card.title))
    if card.styled_artist:
        values.append(("artist", card.styled_artist))
    if card.year and card.producer == "karaoke_channel":
        values.append(("year", str(card.year)))
    for fld, val in values:
        conn.execute(
            "INSERT INTO song_metadata (media_item_id, field, value, source, confidence, "
            "updated_at) VALUES (?,?,?,'title_card_ocr',?,?) "
            "ON CONFLICT(media_item_id, field, source) DO UPDATE "
            "SET value=excluded.value, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (result.media_item_id, fld, val, OCR_CONFIDENCE, now),
        )
    return [fld for fld, _ in values]


# --- worklist ------------------------------------------------------------------------------

def titlecard_worklist(conn, limit: int | None = None, *, include_done: bool = False) -> list:
    """Winner/sole video items with a local file, no title yet, and no title_cards row.

    "No title yet" is asked of `song_metadata` rather than `v_metadata`. The two are
    equivalent for existence (v_metadata emits a title row iff some non-NULL title row
    exists), so this is a free choice, not a performance workaround.
      NOTE: stage5.materialize_metadata's docstring claims correlated per-item subqueries
      against v_metadata "measured in HOURS on the live DB". That does NOT reproduce.
      Measured 2026-07-27, read-only against the live DB (24,850 winner/sole items,
      206,113 song_metadata rows): the worklist-shaped query — TWO correlated v_metadata
      subqueries per item, in the WHERE clause so neither can be optimised away, over
      every item with no LIMIT — completes in **0.74s**. Not hours; not minutes. Whatever
      produced the original observation, it was not this query shape. stage5's
      materialize-once approach is harmless (1.5s) and is left alone, but do not carry
      that claim forward as a reason to avoid the view.

    A `title_cards` row — card or no card — is what takes an item out of this list, so a
    re-run resumes rather than re-paying for frames we already read (§11: evidence is kept
    so promotion can be retuned without a re-OCR).

    `include_done=True` drops BOTH "done" filters (the title_cards row and the existing
    title) and answers a different question: "was this item ever a legitimate member of this
    worklist?" — which is what `ingest` needs, because the first ingest of a line is what
    makes the item fail both filters, and re-ingesting the same file must still be a no-op
    rather than a wall of rejections. See `_eligible_ids`.

    MIN(l.id) picks one location deterministically when a blob has several; SQLite fills the
    bare columns from that same row (documented single-MIN/MAX behaviour).
    """
    placeholders = ",".join("?" for _ in VIDEO_FILETYPES)
    done_filter = "" if include_done else """
          AND NOT EXISTS (SELECT 1 FROM title_cards t WHERE t.media_item_id = i.id)
          AND NOT EXISTS (
              SELECT 1 FROM song_metadata s
              WHERE s.media_item_id = i.id AND s.field='title' AND s.value IS NOT NULL)"""
    rows = conn.execute(
        f"""
        SELECT i.id AS item_id, MIN(l.id) AS location_id, l.local_path, l.filetype,
               l.remote_path
        FROM media_items i
        JOIN media_item_files f ON f.media_item_id = i.id
        JOIN file_locations l ON l.content_hash = f.content_hash
        WHERE i.status='active' AND i.quality_verdict IN ('winner','sole_copy')
          AND l.local_path IS NOT NULL
          AND lower(l.filetype) IN ({placeholders})
          -- Stage 1 already ruled these out as carrying no media; a video filetype alone
          -- does not make a file playable. Measured on the live DB 2026-07-27: 32 of the
          -- 168 title-less items with a local video file parse as 'non_media' — 28 macOS
          -- AppleDouble sidecars ('._AC-DC - Highway to Hell (Karaoke).mp4', all exactly
          -- 4,096 B resource forks), 2 '.part' incomplete downloads, a sidecar .mpg, and
          -- a file whose name is the bare extension. ffmpeg extracts nothing from any of
          -- them, so without this filter ~19% of every run's frame extraction and OCR
          -- spend goes to files that cannot contain a title card.
          AND NOT EXISTS (
              SELECT 1 FROM location_parses p
              WHERE p.location_id = l.id AND p.layout = 'non_media')
          {done_filter}
        GROUP BY i.id
        ORDER BY i.id
        """,
        VIDEO_FILETYPES,
    ).fetchall()
    return rows[:limit] if limit is not None else rows


# --- extract: write the work packet ---------------------------------------------------------

@dataclass
class ExtractReport:
    worklist: int = 0
    processed: int = 0
    frames_written: int = 0
    failed: int = 0
    layouts: dict = field(default_factory=dict)   # cheap classifier histogram; a spike in
                                                  # 'unknown' means a new producer layout
    models: dict = field(default_factory=dict)    # routing split, the operator's workload
    manifest: str | None = None
    dry_run: bool = False
    stopped_reason: str | None = None
    last_error: str | None = None
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_sec"] = round(self.elapsed_sec, 1)
        return d


def extract_work_packet(conn, *, limit: int | None = None, timestamps=FRAME_TIMESTAMPS,
                        model_override: str | None = None, dry_run: bool = False,
                        progress_every: int = 10, on_progress=None) -> ExtractReport:
    """One pass over the worklist, producing the operator's work packet.

    Per item: frames to `ARTIFACTS_DIR/titlecards/<media_item_id>/t<ts>.jpg`, one manifest
    line naming them. An item that yields no frames at all is counted as failed and skipped —
    never allowed to stop the run — and gets no manifest line, so the operator is never asked
    to read a file that does not exist.

    `dry_run=True` walks the same worklist and extracts and classifies the same frames but
    writes nothing at all: no JPEGs, no manifest. It answers the only questions worth asking
    before committing an operator's session — how many items, which layouts, and how the
    model routing splits.
    """
    worklist = titlecard_worklist(conn, limit)
    report = ExtractReport(worklist=len(worklist), dry_run=dry_run)
    started = time.monotonic()
    records: list[dict] = []
    try:
        for i, row in enumerate(worklist, 1):
            timed = _timed_frames(row["local_path"], timestamps)
            if not timed:
                report.failed += 1
                report.last_error = f"item {row['item_id']}: no frames from {row['local_path']}"
            else:
                # The 2.5s frame is the classifier's input: it is the one measured to be on
                # the card for every producer. If a file is so short that even 2.5s missed,
                # fall back to the earliest frame we did get rather than skipping the item.
                layout = classify_layout(timed[0][1])
                model = model_for_layout(layout, model_override)
                report.layouts[layout] = report.layouts.get(layout, 0) + 1
                report.models[model] = report.models.get(model, 0) + 1
                if not dry_run:
                    out_dir = frames_dir(row["item_id"])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    paths = []
                    for ts, blob in timed:
                        p = out_dir / f"t{_ts_label(ts)}.jpg"
                        p.write_bytes(blob)
                        paths.append(str(p))
                        report.frames_written += 1
                    records.append({
                        "media_item_id": row["item_id"],
                        "location_id": row["location_id"],
                        "layout": layout,
                        "model": model,
                        "frames": paths,
                        "frame_ts": [ts for ts, _ in timed],
                        # HUMAN LABEL ONLY. It is in the packet so an operator can sanity-check
                        # which file a card belongs to; it must never be used as an extraction
                        # hint. These names are opaque by construction (SONG-<uuid>.mp4,
                        # Chapter_NN.avi) — that is the whole reason this stage exists — and
                        # letting a reader "help" from the path would reintroduce exactly the
                        # invented metadata §6.1 refused to write.
                        "remote_path": row["remote_path"],
                    })
            report.processed += 1
            if on_progress and (i % progress_every == 0 or i == len(worklist)):
                report.elapsed_sec = time.monotonic() - started
                on_progress(report)
    except KeyboardInterrupt:
        report.stopped_reason = "interrupted"

    if not dry_run:
        report.manifest = str(_write_manifest(records))
        _write_instructions(titlecards_dir(), report, len(records))
    report.elapsed_sec = time.monotonic() - started
    return report


def _write_manifest(records: list[dict]) -> Path:
    """Rewrite the manifest wholesale, atomically. It describes THIS packet: the worklist
    already excludes items that have been ingested, so a manifest that accumulated old items
    would keep asking the operator to re-read finished work. os.replace so an interrupted
    write cannot leave a half-line the ingest would reject."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def load_manifest(path=None) -> dict[int, dict]:
    """media_item_id -> manifest record. Raises if there is no packet: an ingest without one
    cannot know a line's frame_ts or location_id, and inventing them would put fiction in the
    provenance columns."""
    p = Path(path) if path is not None else manifest_path()
    if not p.exists():
        raise TitleCardError(
            f"no work packet at {p} — run `titlecard extract` first (it writes the frames "
            f"and the manifest the results are validated against)")
    out: dict[int, dict] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        out[int(rec["media_item_id"])] = rec
    return out


def _write_instructions(out: Path, report: ExtractReport, n_items: int) -> None:
    """The operator's runbook, next to the packet — same idea as §10's INSTRUCTIONS.md for
    sheet reviewers. It carries the prompt VERBATIM so the reading is reproducible without
    reading this source file."""
    out.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Title-card OCR — work packet",
        "",
        f"Items in this packet: **{n_items}** "
        f"(worklist {report.worklist}, {report.failed} yielded no frames and were skipped).",
        "",
        "Model routing (see `MODEL_FOR_LAYOUT` in `karaokemp/titlecard.py` for the 6-case "
        "eval behind it): " +
        ", ".join(f"`{m}` × {n}" for m, n in sorted(report.models.items())) + ".",
        "",
        "## What to do",
        "",
        "1. Read `manifest.jsonl`. Each line is one item: `media_item_id`, `frames` (JPEG "
        "paths, chronological), `layout`, and `model` — the model that item must be read "
        "with.",
        "2. For each item, hand the frames and the PROMPT below to a subagent running the "
        "line's `model`. Batch items of the same model together; do not mix models within a "
        "batch.",
        "3. Append each answer as ONE line to `results.jsonl`. One JSON object per line, no "
        "wrapping array, no prose.",
        "4. `remote_path` in the manifest is a human label so you can eyeball which file a "
        "card belongs to. **Never show it to the reader and never let it influence an "
        "answer** — these filenames are opaque by construction and that is exactly why this "
        "stage exists.",
        "5. Apply: `python3 -m karaokemp.cli titlecard ingest <results.jsonl> --dry-run`, "
        "then without `--dry-run`. Rejected lines are reported with reasons; fix and "
        "re-ingest — ingesting the same file twice is a no-op.",
        "",
        "## The prompt (verbatim)",
        "",
        "```",
        PROMPT,
        "```",
        "",
    ]
    (out / "INSTRUCTIONS.md").write_text("\n".join(lines), encoding="utf-8")


# --- ingest: apply the operator's results ---------------------------------------------------

@dataclass
class IngestReport:
    lines_read: int = 0
    applied: int = 0
    rejected: int = 0
    promoted_title: int = 0
    promoted_artist: int = 0
    writer_credit_only: int = 0   # a card whose only name is an authorship credit
    no_card: int = 0
    rejections: list = field(default_factory=list)   # [{line, media_item_id, reason}]
    dry_run: bool = False

    @property
    def rejected_by_reason(self) -> dict:
        out: dict[str, int] = {}
        for r in self.rejections:
            key = r["reason"].split(":")[0]
            out[key] = out.get(key, 0) + 1
        return out

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "rejections"}
        d["rejected_by_reason"] = self.rejected_by_reason
        d["rejections"] = self.rejections
        return d


class _RejectLine(Exception):
    """One bad line. Reject it whole — never half-apply."""


def _clean(value, what: str) -> str | None:
    """Trimmed string or None. A non-string where a string belongs is a reject, not a cast:
    `"title": 1967` means the reader answered in the wrong shape and the rest of its answer
    cannot be trusted either."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _RejectLine(f"bad {what}: expected a string or null, got {type(value).__name__}")
    value = value.strip()
    return value or None


def _clean_year(value):
    """int or None. A 4-digit numeric STRING is accepted (unambiguous, and a JSON writer that
    quotes numbers is a formatting slip, not an ambiguity); anything else — a float, a
    2-digit year, a duration read as a year — is rejected rather than guessed."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _RejectLine("bad year: got a boolean")
    if isinstance(value, str):
        if not (value.strip().isdigit() and len(value.strip()) == 4):
            raise _RejectLine(f"bad year: {value!r} is not a 4-digit year")
        value = int(value.strip())
    if not isinstance(value, int):
        raise _RejectLine(f"bad year: expected an integer, got {type(value).__name__}")
    if not (YEAR_MIN <= value <= YEAR_MAX):
        raise _RejectLine(f"bad year: {value} outside {YEAR_MIN}–{YEAR_MAX}")
    return value


def _parse_result(raw: str) -> tuple[int, TitleCard, str | None]:
    """(media_item_id, TitleCard, model) from one results.jsonl line, or `_RejectLine`.

    Strict on purpose. Every check here is a case where the alternative is inventing data:
    a missing title on a found card, an enum value we have no promotion rule for, a year that
    is not a year. The stage exists because §6.1 refused to invent metadata; ingesting a
    guess would undo that decision at the last step.
    """
    try:
        rec = json.loads(raw)
    except ValueError as exc:
        raise _RejectLine(f"not JSON: {exc}") from None
    if not isinstance(rec, dict):
        raise _RejectLine(f"not a JSON object: got {type(rec).__name__}")

    mid = rec.get("media_item_id")
    if isinstance(mid, bool) or not isinstance(mid, int):
        if isinstance(mid, str) and mid.strip().isdigit():
            mid = int(mid.strip())
        else:
            raise _RejectLine(f"bad media_item_id: {mid!r}")

    found = rec.get("card_found")
    if not isinstance(found, bool):
        raise _RejectLine(f"bad card_found: expected true or false, got {found!r}")

    producer = rec.get("producer")
    if producer not in PRODUCERS:
        raise _RejectLine(f"bad producer: {producer!r} not in {list(PRODUCERS)}")

    script = rec.get("script")
    if script is not None and script not in SCRIPTS:
        raise _RejectLine(f"bad script: {script!r} not in {list(SCRIPTS)}")

    title = _clean(rec.get("title"), "title")
    if found and not title:
        raise _RejectLine("card_found is true but there is no title — the title is the only "
                          "field this stage exists to recover")

    card = TitleCard(
        card_found=found, producer=producer, title=title,
        styled_artist=_clean(rec.get("styled_artist"), "styled_artist"),
        writer_credit=_clean(rec.get("writer_credit"), "writer_credit"),
        year=_clean_year(rec.get("year")),
        musical_key=_clean(rec.get("musical_key"), "musical_key"),
        script=script,
    )
    return mid, card, _clean(rec.get("model"), "model")


def _eligible_ids(conn) -> set[int]:
    """Items an ingest line may legitimately name: the current worklist, PLUS items that
    already carry a title_cards row.

    The second half is not laxity, it is what makes re-ingest idempotent. The first ingest of
    a line writes the title_cards row and (usually) a title, which is precisely what removes
    the item from the worklist — so a plain worklist check would reject every line of a file
    the moment it succeeded. An item in neither set (an alternate copy, an mp3, one that already had
    a title before this stage ever ran) has no business in a results file and is rejected."""
    ids = {r["item_id"] for r in titlecard_worklist(conn)}
    ids |= {r[0] for r in conn.execute("SELECT media_item_id FROM title_cards")}
    return ids


def ingest_results(conn, results_path, *, manifest=None, dry_run: bool = False) -> IngestReport:
    """Apply an operator's results.jsonl through the existing `promote()`.

    Per-item commit (the established crash-resume discipline), and idempotent: `promote`
    upserts both the evidence row and the song_metadata rows, so re-ingesting the same file
    changes nothing but `updated_at` / `extracted_at`.
    """
    packet = load_manifest(manifest)
    eligible = _eligible_ids(conn)
    report = IngestReport(dry_run=dry_run)
    seen: set[int] = set()

    with open(results_path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            report.lines_read += 1
            mid = None
            try:
                mid, card, model = _parse_result(raw)
                if mid in seen:
                    raise _RejectLine("duplicate media_item_id in this file — two answers for "
                                      "one item is ambiguous, not a merge")
                if mid not in packet:
                    raise _RejectLine("not in the work packet manifest")
                if not _row_exists(conn, mid):
                    raise _RejectLine("unknown media_item_id — no such media_items row")
                if mid not in eligible:
                    raise _RejectLine("not in the current worklist")
            except _RejectLine as exc:
                report.rejected += 1
                report.rejections.append(
                    # `mid` is unset when the reject fired before the id was parsed; peek at
                    # it anyway, because "line 5: bad year" is a lot less useful to the
                    # operator than "line 5 (item 4122): bad year".
                    {"line": lineno, "media_item_id": mid if mid is not None else _peek_id(raw),
                     "reason": str(exc)})
                continue

            seen.add(mid)
            rec = packet[mid]
            result = CardResult(
                media_item_id=mid, card=card,
                location_id=rec.get("location_id"),
                frame_ts=tuple(rec.get("frame_ts") or FRAME_TIMESTAMPS),
                model=model or rec.get("model") or DEFAULT_MODEL,
                # The verbatim line, not a re-serialisation: §11 retunability means the
                # evidence must be what the reader actually said, byte for byte.
                raw_response=raw,
            )
            if not dry_run:
                promoted = promote(conn, result)
                conn.commit()
            else:
                promoted = _would_promote(card)
            report.applied += 1
            if card.card_found:
                if "title" in promoted:
                    report.promoted_title += 1
                if "artist" in promoted:
                    report.promoted_artist += 1
                elif card.writer_credit:
                    report.writer_credit_only += 1
            else:
                report.no_card += 1
    return report


def _peek_id(raw: str):
    """Best-effort media_item_id for a REJECTION MESSAGE only — never for applying anything.
    A line rejected for a bad year still knows which item it meant, and the operator needs
    that to fix it."""
    try:
        rec = json.loads(raw)
        mid = rec.get("media_item_id")
        return mid if isinstance(mid, int) and not isinstance(mid, bool) else None
    except (ValueError, AttributeError):
        return None


def _row_exists(conn, media_item_id: int) -> bool:
    return conn.execute("SELECT 1 FROM media_items WHERE id=?",
                        (media_item_id,)).fetchone() is not None


def _would_promote(card: TitleCard) -> list[str]:
    """Dry-run mirror of `promote`'s promotion rules — reporting only, writes nothing. Kept
    tiny and adjacent so the two cannot drift far; `promote` remains the only writer."""
    if not card.card_found:
        return []
    out = []
    if card.title:
        out.append("title")
    if card.styled_artist:
        out.append("artist")
    if card.year and card.producer == "karaoke_channel":
        out.append("year")
    return out


def retune_titlecards(conn) -> dict:
    """§11 retune: drop this pass's own song_metadata rows and re-derive them from the
    RETAINED `title_cards` evidence with the current promotion rules — no re-OCR, no operator
    session. Same contract as `stage5.retune_enrichment` re-deriving from mb_cache."""
    cur = conn.execute("DELETE FROM song_metadata WHERE source='title_card_ocr'")
    deleted = cur.rowcount
    rows = conn.execute("SELECT * FROM title_cards").fetchall()
    written = 0
    for row in rows:
        card = TitleCard(**{
            "card_found": bool(row["card_found"]), "producer": row["producer"] or "none",
            "title": row["title"], "styled_artist": row["styled_artist"],
            "writer_credit": row["writer_credit"], "year": row["year"],
            "musical_key": row["musical_key"], "script": row["script"],
        })
        written += len(promote(conn, CardResult(
            media_item_id=row["media_item_id"], card=card, location_id=row["location_id"],
            frame_ts=tuple(json.loads(row["frame_ts"] or "[]")),
            model=row["model"], raw_response=row["raw_response"])))
    conn.commit()
    return {"title_card_ocr_rows_deleted": deleted, "cards_re_derived": len(rows),
            "fields_written": written}
