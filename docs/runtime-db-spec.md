# Karaokemp Runtime DB — Specification

**Status:** in effect since cutover.
**Audience:** this repo and anyone building against the runtime DB.

This document specifies the database that the karaoke *program* (search UI, DJ
console, ingest tooling) runs against at and around the event. It is produced by
**exporting** from the library-cleanup pipeline DB (`schema.sql`), not by
transforming it in place. The pipeline DB is archived at cutover and remains the
provenance record; the runtime DB is deliberately small, flat, and free of
pipeline vocabulary.

## 1. Design principles

1. **Winners, not evidence.** The pipeline DB stores every parse, fingerprint,
   API response and trust ladder needed to *derive* answers. The runtime DB
   stores only the answers: one row per song, one row per playable copy, one row
   per file. Anything needed to *re-argue* a pipeline decision lives in the
   archived pipeline DB, joined by frozen id (see §4).
2. **Stable ids.** `songs.id` and `versions.id` are frozen at export and never
   renumbered. Re-running any pipeline stage after cutover must not touch the
   runtime DB; new material enters only through ingest (§5.5).
3. **`songs.id` is the canonical identity.** Not artist+title (display text — a
   fuzzy match key, editable without the song changing identity) and not
   `song_mbid` (a sparse outward annotation, §2.1).
4. **No derivation machinery at runtime.** No views, no trust ladder, no
   re-appliable resolutions. Post-cutover the only writers are humans and the
   ingest tool; last write wins.
5. **Simplicity over completeness.** Four tables. Every fix an operator makes is
   an ordinary UPDATE on them (§6).

## 2. Schema

STRICT tables, WAL mode, foreign keys ON, ISO-8601 UTC timestamps — same
conventions as the pipeline DB.

```sql
-- One row per song, as a user or DJ sees it. id is FROZEN at export (copied
-- from the pipeline cluster_id, for traceability back to the archived DB) and
-- never renumbered afterwards.
CREATE TABLE songs (
    id          INTEGER PRIMARY KEY,
    artist      TEXT,                -- display string, native script (no transliteration)
    title       TEXT,
    artist_norm TEXT,                -- lower/trim/punctuation-stripped; search & ingest match key
    title_norm  TEXT,
    language    TEXT,
    year        INTEGER,             -- future search facets; already enriched, free to carry
    genre       TEXT,                -- JSON array
    song_mbid   TEXT,                -- nullable, NON-unique; see §2.1
    created_at  TEXT
) STRICT;
CREATE INDEX ix_songs_norm ON songs(artist_norm, title_norm);

-- One row per playable copy of a song. id frozen from pipeline media_items.id.
CREATE TABLE versions (
    id              INTEGER PRIMARY KEY,
    song_id         INTEGER NOT NULL REFERENCES songs(id),
    format          TEXT NOT NULL CHECK (format IN ('video','mp3g','audio_only','audio_lrc')),
    rank            INTEGER NOT NULL,      -- 1 = best copy WITHIN a format; no cross-format
                                           -- meaning (§3, §5.3) — the app picks the format
    duration_sec    REAL,
    is_instrumental TEXT CHECK (is_instrumental IS NULL OR is_instrumental IN ('yes','no','unknown')),
    unplayable_at   TEXT,                  -- NULL = playable; set from the DJ console (§5.4)
    unplayable_note TEXT,
    ingested_at     TEXT                   -- NULL for original library; set by ingest (§5.5)
) STRICT;
CREATE INDEX ix_versions_song ON versions(song_id, rank);

-- Version → physical file(s). An mp3g version has two rows (audio + graphics);
-- a video version has one (av).
CREATE TABLE files (
    version_id   INTEGER NOT NULL REFERENCES versions(id),
    role         TEXT NOT NULL CHECK (role IN ('av','audio','graphics','lyrics')),
    relpath      TEXT NOT NULL,     -- relative to the library root
    size_bytes   INTEGER,           -- relink prefilter (§7)
    content_hash TEXT,              -- SHA-256; ingest exact-dup rejection, relink, fsck
    PRIMARY KEY (version_id, role)
) STRICT;
CREATE INDEX ix_files_hash ON files(content_hash);

-- Operator worklist + logbook. See §6 for semantics and Appendix A for the
-- issue taxonomy. All reference columns nullable on purpose: song-level,
-- version-level and library-level (both NULL, e.g. song requests) issues.
CREATE TABLE issues (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    song_id     INTEGER REFERENCES songs(id),
    version_id  INTEGER REFERENCES versions(id),
    payload     TEXT,               -- JSON; e.g. {"other_song_id": …} for duplicates
    resolution  TEXT,               -- NULL = open; a record of what was done, nothing re-applies it
    created_at  TEXT,
    resolved_at TEXT
) STRICT;
CREATE INDEX ix_issues_open ON issues(kind, resolved_at);
```

### 2.1 `song_mbid` — annotation, never identity

Nullable because coverage is structurally partial (78% at the 2026-08-09
export — the earlier ~30% figure predated the §9.1.1/§9.1.2 enrichment passes;
the Hebrew songs are the bulk of the gap and will mostly stay NULL forever —
MusicBrainz has the Hebrew artists but not their recordings). Non-unique because two library songs can
legitimately share an MBID: two distinct karaoke productions of one song that
never clustered. A UNIQUE constraint would turn an annotation into a false
merge instruction. Its value is future metadata refresh (year/genre/anything by
lookup instead of re-running text matching).

### 2.2 The `_norm` maintenance rule

`artist_norm`/`title_norm` are derived (lower, trim, strip punctuation; Hebrew
kept as-is — it has no case). Every write path that touches `artist`/`title`
(name fixes, merges, ingest) must recompute them. Centralize this in one admin
function; do not trust each caller.

## 3. Export from the pipeline DB

A new tool (`tools/export_runtime.py`) reads the pipeline DB and writes a fresh
runtime DB file. Mapping:

| runtime | pipeline source |
|---|---|
| `songs` | `v_songs` materialized (id ← `cluster_id`; artist/title/language) + `v_metadata` winners for year/genre/song_mbid |
| `versions` | `media_items` where `status='active'`; id, format, `quality_rank` → `rank`, duration_sec, is_instrumental |
| `files` | `media_item_files` joined through `blobs` to a concrete path; size from `blobs.size_bytes` |
| `issues` | seeded only with export-time flags worth carrying (e.g. `distinct_names > 1` → `kind='duplicate_suspect'`); the pipeline `review_queue` itself does NOT migrate |

Export filters:

- Only versions whose audio/av blob has `integrity_status IN ('probed_ok','decoded_ok')`.
  Known-broken files do not ship; there are no integrity columns at runtime —
  everything present is presumed playable until marked otherwise (§5.4).
- Only songs left with at least one exported version.

**Cross-format rank ties — decided 2026-08-22 (sha-yol): the app chooses, the
export makes no judgement.** Pipeline `quality_rank` is per (cluster, *format*),
so a song holding both a video and an mp3g copy exports two rank-1 versions
(529 songs, 2.5%, at the 2026-08-09 export). The export deliberately does NOT
fold `PRIMARY_FORMAT_PREFERENCE` into `rank`: format preference is a playback
policy that depends on what the room has (a projector, a screen, neither), and
it can change on the night. Baking it into a frozen integer would make a venue
decision permanent and unreviewable.

The consequence is a real contract, not a footnote: **`rank` orders copies
WITHIN a format and carries no cross-format meaning.** Any consumer that wants
one row per song must supply its own format preference — see §5.3.

Path source: the spec'd final state is post-Organize `active/` relative paths.
**Until Organize runs, the export uses existing `file_locations.local_path`**
(interim mode); `relpath` is then relative to the current staging root. A
re-export after Organize rewrites paths; this is fine because export is
repeatable until cutover (§4).

Everything else in the pipeline DB is deliberately dropped: `blobs`,
`file_locations`, `location_parses`, `provisional_pairs`, `clusters`,
`cluster_edges`, `fingerprints`, `song_metadata`, `title_cards`,
`pipeline_runs`, `artifacts`, `review_exports`, all caches, all views.

## 4. Cutover

- **Export is cheap and repeatable; cutover is the one-time event.** The runtime
  DB can be generated today for building and testing the player, and thrown
  away and re-exported freely.
- The in-flight manual review batches resolve into the *pipeline* DB (manual
  metadata rows, merge edges) where the pipeline machinery re-applies them.
  Cutover — the moment ids freeze and the runtime DB becomes authoritative —
  happens only after the last review batch is folded back and a final export
  runs, after Organize.
- After cutover the pipeline DB is archived read-only. Review-type problems
  change vocabulary: "same song" becomes a merge on the runtime DB, "wrong
  name" becomes an UPDATE on `songs` (§6).

## 5. Flows

### 5.1 Search
`LIKE '%…%'` over `artist_norm`/`title_norm`. At ~22k songs a full scan is
milliseconds; FTS5 is an upgrade path, not a need. Hebrew stays in Hebrew
script. Year/genre columns are present for future facets. Songs with no
playable version (every version has `unplayable_at` set) should be greyed out
or filtered — a derived state, never stored.

### 5.2 Runtimes for the DJ
`versions.duration_sec`, copied from the pipeline at export. No live file
probing at the event.

### 5.3 Playback
`rank` is per (song, format) — a bare `ORDER BY rank LIMIT 1` picks between a
video and an mp3g copy arbitrarily (§3). The app must name its own format
preference:

```sql
SELECT * FROM versions
 WHERE song_id = ? AND unplayable_at IS NULL
 ORDER BY CASE format WHEN 'video' THEN 0 ELSE 1 END,   -- app's policy, not the DB's
          rank
 LIMIT 1;
```

Its file paths come from `files`. An mp3g version yields two paths (audio +
graphics) that the player opens together. Making the preference a setting the
DJ can flip is cheap and worth doing — it is one `CASE` arm.

### 5.4 Marking unplayable at the event
The DJ learns something narrower than "this song is broken": *the version just
played* is bad. One UPDATE sets `unplayable_at`/`unplayable_note` on that
version, and the song's default copy silently becomes the next rank — the same
query as §5.3. Alternatives for damage control are the remaining ranked
versions. Song-level unplayability is only ever the derived state "no playable
version".

### 5.5 Ingest (pre/post event)
1. Hash the new file; reject if `content_hash` already in `files` (exact dup).
2. Parse artist/title from the filename (parser is code, not schema).
3. Norm-match against `songs`.
4. If a matched song has a version with `unplayable_at IS NULL` → reject,
   unless the uploader overrides.
5. Accepted: append a `versions` row (`rank` = max+1 for the song,
   `ingested_at` set) or create a new `songs` row first. An override against a
   playable song also files `issues(kind='ingest_override')`.

Ingest can create near-duplicate songs (name mismatch); this is expected and
handled by the merge operation (§6), which is needed post-cutover regardless.

### 5.6 Manual review (bonus)
The `issues` table: a worklist plus logbook drained by an operator with direct
UPDATEs. See §6 and Appendix A.

## 6. Issue semantics — what `issues` is and is not

Every problem splits into an **immediate serving-state change** (takes effect
tonight: an unplayable mark, a rank swap, a name UPDATE) and/or a **deferred
judgment** (an `issues` row for the offline operator). The schema needs no
structure beyond nullable `song_id`/`version_id` + JSON payload, because every
fix is expressible as ordinary UPDATEs:

- merge two songs: repoint `versions.song_id`, re-rank, delete the loser row;
- split a song: new `songs` row, repoint the offending versions;
- fix a name: UPDATE `songs` (+ recompute norms, §2.2);
- demote a flawed default: swap `rank` values;
- ban a copy: set `unplayable_at`.

Deliberate losses relative to the pipeline `review_queue`:

- **Re-applyability.** A pipeline resolution is a durable fact that re-runs
  re-apply mechanically. A runtime issue is a to-do item; `resolution` merely
  records what was done.
- **Provenance.** No trust ladder; last write wins. Acceptable because the only
  writers are humans.
- **Evidence.** Fingerprints, cluster edges, cached API responses stay in the
  archived pipeline DB — archaeology by frozen id when genuinely needed.

Merging destroys a stable id (the loser's). Today nothing external stores song
ids, so deletion is clean. **Trigger to revisit:** the moment anything stores
song ids (event request log, saved playlists), merge needs a tombstone
(`merged_into` column) instead of a delete.

## 7. Path changes, relink, fsck

A rename/move/reorganize on disk is fully recoverable: a relink tool walks the
library root, stats everything (fast), hashes **only** files whose size matches
a lost row (`files.size_bytes` is the prefilter), and rewrites `relpath`.
Hash matching is exact-content only — a re-encoded or edited file is new
material and goes through ingest. `content_hash` is not unique in `files`
(identical CDG bytes were reused across productions), so relink assigns matches
greedily per row; harmless, identical bytes are interchangeable.

The same tool doubles as fsck: every row's relpath exists with matching size;
every media file on disk is claimed by a row.

---

## Appendix A — issue taxonomy

| kind | refs | immediate action | offline fix |
|---|---|---|---|
| (none needed) | version | "song unplayable" → mark the played version; next rank serves | investigate if worth it |
| `wrong_name` | song | none (DJ doesn't edit live) | UPDATE `songs` + norms |
| `defect` | version | none — keeps serving; that's the point | usually a rank swap |
| `duplicate` | song + payload `other_song_id` | none | merge (§6) |
| `split` | song | possibly mark the odd version unplayable | new song row, repoint versions |
| `wrong_song` | version | as above | repoint one version |
| `mispaired` | version | mark unplayable | re-pair via `files` edits; evidence is pipeline archaeology |
| `missing_file` | version | mark unplayable | relink/fsck (§7), clear the mark |
| `remove` | song | mark all versions unplayable | delete rows, archive files |
| `request` | none (payload artist/title) | none | pre-event worklist → ingest |
| `ingest_override` | version | none | operator sanity check |
| `duplicate_suspect` | song + payload | seeded at export from `distinct_names > 1` | confirm/merge or resolve |

The list is open — `kind` is uncontrolled text on purpose.
