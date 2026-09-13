# Karaoke Library — Indexing, Dedup & Enrichment Pipeline Spec

**Status:** Draft v2 (addresses implementation review of v1; changelog in §14)
**Audience:** Claude Code instance implementing the initial library pass
**Scope:** Inventory → parse → download → dedup → verify → enrich → organize, over ~7,000 karaoke videos and ~23,300 MP3 files (mostly MP3+CDG pairs) in a disorganized shared Google Drive folder. Out of scope but designed-for: the future song-to-karaoke generation pipeline (Demucs + WhisperX/stable-ts) and its derived artifacts.

---

## 0. Prerequisites & phase gates

**Execution host (hard prerequisite, confirm before Stage 2):**
- ≥ 500GB free disk (media library measured to about 450GB plus archive headroom). If the available machine cannot hold the full set, Stage 2 must run in download→process→archive-or-release batches; the spec's stages support this but the batch plan must be written down first.
- Stable connection; multi-day transfer expected. All downloads resumable.
- The pipeline must NOT assume it runs on the machine where this spec was authored. Name the host in `pipeline_runs.notes` of the first run.

**Phase gates.** Implement and run in checkpointed phases. Stage 0's inventory report (sizes, duplicate rate, zip prevalence, filename-chaos level) is expected to reshape Stages 1–3 before their code is finalized. Do not build the whole pipeline blind and run it end-to-end.

**The shared Google Drive folder is read-only.** Other people use it. The pipeline never moves, renames, or deletes anything on Drive. All "archiving" is local and/or logical (index status). Any future Drive cleanup is a separate, human-driven task informed by the index.

---

## 1. Principles

1. **The SQLite index is the source of truth.** Filesystem layout is a convenience; a reconciliation check (`fsck`) must always be able to verify agreement.
2. **Never destroy, always archive** — and, since 2026-07-31 (§7.4, §14.19), *barely even archive*. Broken files and orphans are excluded or moved to `archive/` with the reason recorded. **Duplicate copies are not**: only exact-content duplicates are duplicates, Stage 0 collapses those logically before anything is downloaded, and the remaining copies of a song are ranked rather than archived. Deletion is manual, after the event.
3. **Every pass is idempotent, resumable, and deterministic.** Content hash is file identity; Drive file ID is remote identity. All tie-breaks (e.g., which duplicate survives) use a deterministic rule so re-runs choose identically. Running any stage twice back-to-back produces zero changes the second time (assert in tests).
4. **Every mutating stage has `--dry-run`** that prints the full planned action list (moves, renames, status changes) without executing.
5. **Metadata carries provenance and confidence.** No pass overwrites a higher-trust source. Manual fixes are never auto-overwritten.
6. **Formats coexist.** `video`, `mp3g`, `audio_only`, and (future) `audio_lrc` are peer formats of a *media item*; a cluster may mix formats.
7. **Expensive verification runs on survivors, not on everything.** Full A/V decode happens after clustering, on prospective winners and sole copies only.
8. **Human-in-the-loop where judgment is required** — via review queues with tunable thresholds, sized *before* committing to review (see §11).

---

## 2. Storage layout

```
library/
  active/                # organized playable content (§9)
  archive/
    broken/ orphans/ superseded/     # dedup_losers/ removed 2026-07-31 (§7.4, §14.19):
                                     # nothing is archived for being a duplicate any more
  staging/               # downloads land here before processing
  artifacts/             # future: stems, LRC (§12)
  db/library.sqlite3
  logs/
```

---

## 3. Database schema (SQLite)

STRICT tables, WAL mode, foreign keys ON, ISO-8601 UTC timestamps.

The v1 schema conflated content identity with physical location. v2 splits them:

### 3.1 `blobs` — content identity

| column | type | notes |
|---|---|---|
| content_hash | TEXT PK | SHA-256 of bytes |
| size_bytes | INTEGER NOT NULL | |
| integrity_status | TEXT NOT NULL DEFAULT 'unchecked' | `unchecked` / `probed_ok` / `decoded_ok` / `suspect` / `broken` |
| integrity_detail | TEXT | JSON: which checks ran, failures |
| first_hashed_at | TEXT | |

One row per distinct byte-content, no matter how many copies exist.

### 3.2 `file_locations` — physical/remote copies

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| drive_file_id | TEXT UNIQUE | Google Drive's stable file ID (see §5.1). NULL for local-only files (artifacts, extracted zip members). |
| remote_path | TEXT | rclone path at last enumeration (informational; may go stale — reconciliation keys on drive_file_id) |
| local_path | TEXT | NULL until downloaded/extracted |
| parent_location_id | INTEGER FK → file_locations | for zip members: the containing zip |
| member_path | TEXT | path within the container |
| filetype | TEXT NOT NULL | lowercased extension: `mp4` `mkv` `webm` `avi` `mp3` `cdg` `zip` `lrc` `flac` … |
| gdrive_md5 | TEXT | from rclone enumeration |
| content_hash | TEXT FK → blobs | NULL until hashed locally. **Not unique** — multiple locations may share one blob; that is the point. |
| role | TEXT | within a media item: `av` / `audio` / `graphics` / `lyrics` / `container` |
| status | TEXT NOT NULL DEFAULT 'remote_only' | `remote_only` / `excluded` (logical dedup loser, never downloaded) / `staged` / `active` / `archived` |
| archive_reason | TEXT | `exact_dup` / `dedup_loser` / `broken` / `orphan_cdg` / `superseded` / `shortcut` … |
| first_seen_at, updated_at | TEXT | |

### 3.3 `media_items` — logical playable units

Created at Stage 3, referencing blobs (not locations) via `media_item_files`:

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| format | TEXT NOT NULL | `video` / `mp3g` / `audio_only` / `audio_lrc` (future) |

| cluster_id | INTEGER FK → clusters | |
| duration_sec | REAL | ffprobe |
| is_instrumental | TEXT | `yes` / `no` / `unknown` |
| audio_codec, audio_bitrate_kbps, sample_rate, video_codec, width, height | | ffprobe; video fields NULL otherwise |
| quality_attrs | TEXT | JSON of measured attributes used for ranking (see §7.3 — no magic scalar score; verdicts are what matter) |
| quality_verdict | TEXT | `winner` / `alternate` / `sole_copy` / `pending` / `manual_review` (§7.4; `loser` renamed to `alternate` by migration 006 — nothing is archived for being a duplicate) |
| quality_rank | INTEGER | §7.4 rank within (cluster, format); 1 is the default copy to serve. A total order, so a song always has a default even while a `manual_review` question about it is open. |
| status | TEXT NOT NULL DEFAULT 'active' | `active` / `archived` / `superseded` |
| superseded_by | INTEGER FK → media_items | |
| created_at, updated_at | TEXT | |

`media_item_files(media_item_id, content_hash, role, PRIMARY KEY(media_item_id, role))` — an `mp3g` item has roles `audio` + `graphics`; `video` has `av`; future `audio_lrc` has `audio` + `lyrics`.

### 3.4 `fingerprints` — keyed by blob

| column | type | notes |
|---|---|---|
| content_hash | TEXT PK FK → blobs | fingerprint is a property of the audio content; identical blobs are fingerprinted once for free |
| chromaprint | TEXT NOT NULL | fpcalc raw fingerprint (document chosen encoding) |
| fp_duration_sec | REAL | |
| acoustid_checked_at | TEXT | |
| acoustid_recording_mbid | TEXT | **acoustic identity**: the recording this audio actually is, per AcoustID. Expected NULL for karaoke covers. Distinct from song-level `song_mbid` metadata (§3.6, §8). |
| acoustid_score | REAL | |

For `video` blobs the fingerprint is of the demuxed audio stream; still keyed by the video blob's hash.

### 3.5 `clusters` and `cluster_edges`

`clusters(id, method 'fingerprint'/'exact_hash'/'title_match'/'manual', confidence, notes)`
`cluster_edges(item_a, item_b, edge_type 'fingerprint'/'prefix_fingerprint'/'title_match'/'manual', similarity)` — persist all edges, including sub-threshold candidates, for auditability and threshold re-tuning.

**A cluster IS a song. Every active item has exactly one**, singletons included, so `cluster_id` is universally non-NULL and no downstream query needs a NULL branch (§7.3). `method` says **what made this a song**:

| method | when Stage 3 writes it |
|---|---|
| `title_match` | the names agree — the ordinary way a song acquires a second copy, and the only cluster-forming key. |
| `manual` | no name evidence; an operator recorded a §10 "these two are the same song" edge. The one non-name merge, because it is a human asserting something about the *song*, not about the audio. |
| `fingerprint` | no name evidence and no operator: the **no-name fallback** grouped items that have no key at all (opaque parses) by their audio. |
| `NULL` | **singleton** — one copy, nothing merged it, nothing to explain. A real answer, not missing data, in the same way `layout='opaque'` is. |
| `exact_hash` | reserved for clusters created outside §7.3; never written or touched here. |

Note the precedence *inverted* with the name-only reframe (§14.19). Under the arrangement model `fingerprint` was the headline whenever audio held any part of a component together, because audio identity was the stronger claim. Audio makes no identity claims now, so the name is the headline and audio only names a cluster it built alone.

Mixed evidence is not a sixth value — it is recorded in `notes`, which carries `{"items", "merge_edges", "evidence": {"audio": n, "name": m, "manual": k}, "confidence_basis"}`.

**`confidence`** is the weakest link that holds the component together, on the scale named by `notes.confidence_basis`:
- `name_evidence` — the minimum **name-evidence strength** among its name merge edges, where 1.0 is an identical normalized token set and a containment match scores `|subset| / |superset|`.
- `operator` — a §10 manual edge (1.0 unless the edge says otherwise).
- `fingerprint_similarity` — the minimum audio similarity among a no-name fallback group's merge edges.
- `none` — a singleton; `confidence` is NULL.

The scales are never mixed into one number and are not comparable: 0.85 audio similarity and 0.85 name evidence are different claims about different things. Anything reading `confidence` must read `confidence_basis` with it.

**`cluster_edges.similarity` on a `title_match` row** is that same name-evidence strength, not an audio similarity (it was NULL while such edges could not merge anything). Any query that thresholds `similarity` must filter on `edge_type` first — see §7.4's mid-band review query, which does.

### 3.6 `song_metadata` — field-level provenance

| column | type | notes |
|---|---|---|
| media_item_id | INTEGER FK | |
| field | TEXT | `artist` / `title` / `language` / `year` / `genre` / `song_mbid` / `disc_id` / `disc_series` |
| value | TEXT | genre: JSON array |
| source | TEXT | `filename` / `id3` / `musicbrainz_text` / `musicbrainz_freetext` / `musicbrainz_fp` / `musicbrainz_artist` / `wikidata` / `spotify_text` / `itunes_text` / `deezer_text` / `title_card_ocr` / `manual` (the v1 list was stale; this matches the applied CHECK constraint after migrations 001–006) |
| confidence | REAL | |
| updated_at | TEXT | |
| PRIMARY KEY (media_item_id, field, source) | | |

**Trust order — two ladders, not one.** v2 originally had a single ascending ladder, which quietly conflated two different questions. They are now separated:

**(A) IDENTITY ladder — *which song is this?*** Used by §7.3 clustering and by anything that decides whether two items are the same song.

```
filename ≈ manual  >  musicbrainz_fp (acoustic)  >  everything else
```

The filename sits at the top because in this library it is the only field that was written by someone who knew what the file was. Measured: whole source batches carry no ID3 tag at all; entire "tagged" batches carry only a Windows Media Player `PRIV` frame with no readable text; and the ID3v1 tags that do carry text truncate at a fixed 30 bytes (2,098 titles land exactly on that boundary in the live index). The name is the evidence; the tag is a damaged copy of it.

**No enrichment source may change which song an item is.** MusicBrainz, Spotify, AcoustID and OCR are *text* sources: they may correct how a title is spelled, its order, or its diacritics, and they may add fields the parse never had (`year`, `genre`, `song_mbid`). They may not, on their own, turn item X into a different song than the filename says it is — that requires a review. The one exception is acoustic: `fingerprints.acoustid_recording_mbid` is a claim about *this audio*, not about a text field, and it is treated as identity evidence in its own right (§3.4).

**(B) PRESENTATION ladder — *how is it spelled?*** Used by `v_metadata`, by Stage 6's folder names, and by the catalogue. Unchanged in order, and this is the ladder the view implements:

```
manual > musicbrainz_fp > musicbrainz_text > musicbrainz_freetext > musicbrainz_artist
       > wikidata > spotify_text > itunes_text > deezer_text > title_card_ocr > filename > id3
```

Migration `006_catalogue_sources.sql` inserted the four §9.1.2 sources without disturbing the relative order of any pre-existing one, so it is behaviour-neutral for existing rows. `musicbrainz_artist` sits below the two recording searches because it is anchored on the *artist* index — it speaks to **who** with far more authority than to **which song**. `wikidata` sits below that: its Hebrew labels are the best Hebrew name source measured anywhere in this project, but it identifies songs less reliably than MB does. `itunes_text` and `deezer_text` sit with `spotify_text` because all three are music catalogues used the same way — text corrections accepted only on high agreement — with iTunes above Deezer on the measured accuracy gap (67.5% vs 55.0%, and Deezer confuses cover acts for originals).

Note `filename` now outranks `id3` here as well (migration `003_id3_below_filename.sql`, already applied to the live index): the demotion re-attributed ~10,600 winning values from tag to filename and cost no item a value, because `id3` still wins wherever there is no filename value at all.

`musicbrainz_freetext` (migration `004_musicbrainz_freetext_source.sql`, applied) is the **unqualified** MB search of §9.1.1 — a different query shape from `musicbrainz_text`'s field-qualified one, and therefore a different `mb_cache` key rather than a replay of the same miss. It ranks below the qualified search (which matched on fields we had already segmented correctly — stronger evidence than a bag-of-words hit) and above the corrections it canonicalizes. It is a separate source rather than more `musicbrainz_text` rows for two reasons: provenance, and because `stage5.retune_enrichment()` deletes `musicbrainz_text` rows and re-derives only the qualified pass — free-text rows filed there would be silently destroyed by a retune and never come back. Each pass must own, and be able to rebuild, its own rows.

Consistent with the identity ladder above, this source cannot redirect an item: acceptance requires the candidate to agree with **both** existing fields at ≥ `MB_FIELD_AGREEMENT`, so it can only confirm and canonicalize what the filename already said.

View `v_metadata` exposes the winning value per (item, field) on ladder (B). Each enrichment pass writes only its own source's rows.

`song_mbid` is **song-level identity** (which song this is), not acoustic identity — for a karaoke cover it points at the original song's MB recording/work and its length/ISRC do NOT describe this audio. Nothing downstream may treat it as acoustic truth; acoustic identity lives only in `fingerprints.acoustid_recording_mbid`.

### 3.7 `review_queue`

`(id, kind, media_item_id/cluster_id, payload JSON, resolution JSON NULL=open, created_at, resolved_at)` — kinds: `metadata_match` / `dedup_verdict` / `quality_flag` / `pair_mismatch` / `parser_goldset` / `alignment_check` (future).

### 3.8 `pipeline_runs`

`(id, stage, started_at, finished_at, host, items_processed, items_failed, tool_versions JSON, report JSON, notes)` — `report` must include review-queue size deltas per kind (§11).

### 3.9 `artifacts` — created empty now, populated by the future pipeline (§12)

`(id, media_item_id FK source item, kind 'instrumental_stem'/'vocal_stem'/'lrc'/'enhanced_lrc'/'rendered_video', content_hash FK → blobs, tool, tool_version, params JSON, quality_check, created_at)`

### 3.10 `mb_cache`

`(query_hash PK, request TEXT, response_json TEXT, fetched_at)` — every MusicBrainz/AcoustID response cached; re-runs are free.

`spotify_cache` (§6.1) and `enrich_cache` (§9.1.2, added by migration 006) hold the same contract for the other catalogues. `enrich_cache` is keyed `(source, query_hash)` so one table serves Wikidata, the MB artist index, iTunes and Deezer instead of proliferating one table per API. **Raw responses are stored verbatim on purpose:** re-tuning an acceptance floor must never require re-querying (§11) — a re-scored §9.1.2 run reads 88 cache hits and makes 0 network requests. This is the lesson that cost ~23h of Spotify downtime.

### 3.11 `v_songs` — one row per song

A cluster **is** a song (§7.3) and every active item has one, so this is one row per cluster holding at least one active item. Columns: `song_id`, `artist`, `title`, `representative_item_id`, `name_source`, `song_method`, `song_confidence`, `copies`, `formats`, `distinct_names`, `has_playable`, `has_decoded`, `default_item_id`, `language`, `script`, `song_mbid`, `distinct_mbids`.

**It is a VIEW, not a table, and that is a decision — recorded here so it is not relitigated.** A view cannot drift from the index, needs no migration when clustering changes, and is automatically correct after every re-cluster. Same reasoning that makes `v_metadata` a view, and §1's "the index is the source of truth".

**Cost:** ~55–60s per query over the full index (22,195 songs). Every constituent piece is fast (the metadata fold 1.3s, the item/blob joins 0.1s); the time is SQLite re-deriving the pipeline, and neither `MATERIALIZED` CTE hints nor hoisting the aggregate helped by more than ~3% — both were tried, measured, and verified output-identical, and neither earned its complexity. Fine for a report, a catalogue export or an ad-hoc question; **not** for a loop or an interactive search box. A repeated caller should fold it into a temp table once per run, the pattern `stage5.materialize_metadata` already uses for `v_metadata`.

A durable `songs` **table is DEFERRED, not rejected.** It becomes necessary the moment something *outside* the index needs a song id that survives re-clustering — and, on the evidence above, a live UI would hit the latency wall before it hit the id-stability one — a public permalink, a printed songbook number, an external playlist referencing songs by id. Re-clustering renumbers clusters freely, so the day an outside system stores one of those numbers we need stable ids plus a mapping from cluster to song. Nothing needs that today, and building it today would mean maintaining a second copy of a derivable fact.

**Representative name:** the item whose artist+title come from the highest-trust sources (summed `v_metadata.source_rank`), tie-broken by §7.4 rank — the default copy — then by item id. Fully deterministic. `v_metadata` gained a `source_rank` output column (migration 006) precisely so this rule does not have to restate the trust ladder and then drift out of step with the next migration that renumbers it.

**`distinct_names` is the honesty column.** The §7.3(b) key is order-insensitive and containment-aware, so it deliberately unites "Pink" with "Pink & Nate Ruess" — a song can legitimately hold more than one raw name string. Exposing the count per row keeps that visible instead of collapsing it silently behind one representative; `> 1` means "look before trusting the displayed name". It uses the same crude lower+trim key the operator measures cluster coherence with, so the number here and the number in the `cluster` report mean the same thing. `distinct_mbids` does the same job for song identity.

---

## 4. Pipeline overview

| stage | name | needs local bytes? | mutates filesystem? |
|---|---|---|---|
| 0 | Remote inventory + logical exact-dup | no | no |
| 1 | Filename parsing + provisional pairing | no | no |
| 2 | Download | — | staging/ only |
| 3 | Hash, probe, fingerprint, cluster, dedup verdicts | yes | no |
| 4 | Full-decode verification of survivors | yes | no |
| 5 | Enrichment (MusicBrainz) | no | no |
| 6 | Organize | yes | active/ + archive/ |

Enrichment (5) now precedes Organize (6) so folders are named from the best available metadata. Organize is nonetheless **re-runnable**: if `v_metadata` winners change later (manual fixes, further enrichment), re-running Organize renames folders/files accordingly and `fsck` validates the result.

---

## 5. Stage 0 — Remote inventory (no downloads, no remote mutations)

### 5.1 Enumeration
- `rclone lsf -R --files-only --format "ipsm..."` or `rclone lsjson -R --files-only --hash` — whichever is used, the output MUST include Drive's stable **file ID**, path, size, and MD5. Verify the ID field is actually populated in a sample before the full run; if the chosen command omits it, switch commands. `drive_file_id` is the remote identity key; `remote_path` is informational only (Drive permits duplicate names in one folder — paths are not unique).
- **Shortcuts:** Drive shortcuts can appear as phantom duplicate files. Enumerate with shortcut-resolution behavior explicitly chosen (`--drive-skip-shortcuts` or resolve-and-record); if resolved, record the target's file ID and mark the shortcut location `excluded` / `shortcut`.
- Insert/update `file_locations` keyed on `drive_file_id` (upsert — re-enumeration refreshes paths, detects removals).

### 5.2 Logical exact-dup pass
- Group by (`gdrive_md5`, `size_bytes`). In each group keep one survivor; mark the rest `status='excluded'`, `archive_reason='exact_dup'`.
- **Deterministic survivor rule:** lexicographically smallest `drive_file_id`. (Any stable rule works; this one is content-independent and re-run-safe.)
- This is index-only. Nothing on Drive moves. Excluded locations are simply never downloaded.
- **Filename evidence is preserved, not discarded:** Stage 1 parses the filenames and folder paths of ALL locations in a hash group, including excluded ones, and merges the extracted hints (see §6.1).

### 5.3 Inventory report (phase gate)
Totals by filetype, size distributions, exact-dup rate, zip count and estimated member counts, duplicate-basename rate, sample of 200 random filenames. **Review this report before finalizing Stage 1–3 code.**

---

## 6. Stage 1 — Filename parsing & provisional pairing

### 6.1 Parsing
Runs over **all** `file_locations` (including `excluded`), producing parse payloads per location; hints from all copies of the same content merge onto the surviving location's eventual media item, with conflicts resolved by parse-pattern confidence (a disc-ID-structured name outranks `Track01.mp3`).

- Normalize: Unicode NFC; collapse whitespace; strip video-ID suffixes; move decorations (`(Karaoke Version)`, `HD`, `lyrics`…) into flags.
- **Hebrew diacritics:** strip by Unicode category `Mn` within the Hebrew block only (U+0591–U+05BD, U+05BF, U+05C1–U+05C2, U+05C4–U+05C5, U+05C7) into a separate normalized field. Do NOT strip by blanket range: **maqaf U+05BE** (hyphen — stripping glues words), sof pasuq U+05C3, and paseq U+05C0 are punctuation and must survive.
- Instrumental hints (`is_instrumental='yes'` at filename confidence): `karaoke`, `instrumental`, `playback`, `backing`, `minus one`, `פלייבק`, `קריוקי`.
- Disc-ID patterns: `^([A-Z]{2,4})[- ]?(\d{2,5})[- ](\d{1,2})` family. → `disc_series`, `disc_id`, source `filename`.
- Layout attempts in order: `DiscID - Artist - Title`, `Artist - Title`, `Title - Artist` (both orders scored against MB later), bare `Title`. Parent-folder names recorded as low-confidence hints in the payload, never auto-promoted.
- Keep Hebrew script as-is; never transliterate for storage. Language: script-detection suffices.

### 6.2 Parser golden-set gate (required before mass insert)
Parse 200 random filenames (stratified: videos / mp3s / zip members / Hebrew / English), human-verify via a `parser_goldset` review batch, iterate the parser until acceptable accuracy, THEN run library-wide. The parser is the highest-variance component in the pipeline; it does not get to run at 30k scale unvalidated.

### 6.3 Provisional MP3+CDG pairing
- Pair by identical basename (case-insensitive) within the same directory or zip, on the remote listing. Provisional pairs guide download batching (fetch both halves together); definitive media items are created at Stage 3 post-hash.
- CDG orphan / MP3 orphan handling as before: orphan CDG → excluded (`orphan_cdg`); orphan MP3 → future `audio_only` item, flagged for vocal-presence check (possible full originals — Demucs input, do not archive).
- Duplicate-basename collisions (Drive allows them): route to `review_queue(pair_mismatch)` rather than guessing.

---

## 7. Stage 2 — Download; Stage 3 — Hash, probe, fingerprint, cluster, dedup

### 7.1 Download (explicit stage)
- Download every non-`excluded` location to `staging/`, preserving a mapping to `drive_file_id`. Verify size+MD5 after transfer; mismatch ⇒ retry.
- **Drive quirks:** per-file download-quota errors are common on shared content — implement exponential backoff + resume; the run may span days and must be Ctrl-C-safe at any point.
- Zips: download, index members into `file_locations` (parent_location_id + member_path), extract members to staging. MP3+G libraries are often one-zip-per-song; members flow through the rest of the pipeline as first-class files.
- If disk cannot hold everything (§0): batched download→process→archive-or-release plan, written before starting.

### 7.2 Hash, probe, pair, fingerprint
1. SHA-256 every staged file → `blobs` (upsert), link locations. Locations sharing a blob beyond what Drive MD5 caught: mark extra `exact_dup`.
2. ID3 (mutagen) for MP3 blobs → `song_metadata` source `id3`. Large ID3↔filename disagreement ⇒ review.
3. `ffprobe` every blob: parse failure / zero duration / missing audio ⇒ `broken`; else record streams, `integrity_status='probed_ok'`. **No full decode here** — fingerprinting (next) decodes the audio stream anyway and surfaces most audio corruption; full A/V decode is deferred to Stage 4, survivors only.
4. CDG check: `abs(cdg_size/7200 − mp3_duration) ≤ 3s`, else suspect + `pair_mismatch` review. Confirmed pairs → `mp3g` media items; videos → `video` items; orphan MP3s → `audio_only`.
5. `fpcalc` per blob → `fingerprints`. fpcalc failure on a probed-ok file ⇒ `suspect`.

### 7.3 Clustering

**A song is (artist, title). The name is the SOLE cluster-forming key.** That is what users search, and it is the only identity that matters.

**Fingerprint and prefix_fingerprint edges take no part in the union-find at all.** They are still computed and still persisted (§3.5) — §11 must be able to retune from them without recomputation — but they no longer decide what a cluster *is*.

*Why, and why this supersedes §14.18's arrangement model.* §14.18 kept audio as a second merge key alongside names. The two are **logically incompatible with "cluster == song"**: an audio edge glues items together regardless of what they are called, so a cluster built partly from audio has no well-defined name — and a song that cannot be named cannot be searched. Measured on the live index under §14.18: **2,424 clusters held more than one distinct artist+title string** (crude lower+trim key, so this overstates true ambiguity, but the mechanism is real), against **41 names split across songs**. The audio key was buying precision this corpus did not need and costing the coherence it did. §11.1 explains why no threshold fixes it: the similarity distribution is bimodal with an empty middle.

Audio evidence keeps exactly two jobs, both secondary, **neither of them a merge**:

- **(a) Cross-song review.** Two *different* name-clusters with high audio similarity are worth a human's attention — usually a parse artifact (a typo, `AC/DC` vs `ACDC`, a Hebrew spelling variant). §7.4 queues one `possible_song_merge` row per cluster pair. Evidence to look at, never an automatic merge: if it merged, we would be back to clusters with no name. Resolving one is a §10 `manual` edge, which §7.3 then re-applies every run.
- **(b) No-name fallback.** An item whose parse yields no usable artist+title has no key at all. Those items group by audio among themselves, and a no-name audio component adopts a named song only when its strong edges point at **exactly one** named component. Ambiguity leaves it standalone and is reported, never guessed — that constraint is what stops a nameless item bridging two songs together.

**The key is the best available name, not a bootstrap-gated one.** It is whatever `v_metadata` serves: the MusicBrainz/Spotify canonical value where enrichment resolved it, the filename parse otherwise. Gating on `song_mbid` would strand exactly the population that needs grouping most — the MB bootstrap reaches only ~66% of items (18,992/28,907) and fails worst on Hebrew, where MB's Israeli coverage is thin and Spotify carries Israeli artists under Latin names ~91% of the time. Better names simply flow through and produce better keys; a re-run re-keys the affected items and reshapes their clusters with no re-architecture, because the DB sync is a full diff.

**Every active item gets a cluster**, singletons included, so `cluster_id` is non-NULL universally and every downstream query loses its NULL branch. One row per cluster is one row per song — see §3.11 `v_songs`.

Candidate generation — three sources, union of all:
- (a) Duration blocking: pairs within ±10s.
- (b) **Normalized name identity regardless of duration** — catches truncated copies, which otherwise never share a duration bucket with their full sibling, *and* re-encodes whose audio has nothing in common with their twin. The key is:
  - **order-insensitive**: the token set of artist+title *combined*, because the artist/title boundary is a parse artifact in this library, not a fact about the item (§6.1 defers the Hebrew order question to MusicBrainz for exactly this reason). Measured on the live index: the order-insensitive key finds 2,119 merge groups against 1,989 for the ordered key.
  - **containment-aware**, not strict equality: one side's tokens being a proper subset of the other's is a match (featured-artist supersets, appended transliterations, a dropped middle initial), scored `|subset| / |superset|` and accepted above a configured ratio with a minimum subset size.
  - **size-guarded, not size-truncated**: a group too large to be a plausible set of copies of one song still produces its `title_match` edges — the evidence is persisted and retunable — it simply does not merge. The earlier implementation skipped oversize groups outright and lost the evidence with the merge.
- (c) **Prefix-fingerprint comparison** for (b)-pairs with unequal durations: compare the first `min(dur_a, dur_b) − 10s` of both fingerprints; high similarity + shorter duration ⇒ truncation candidate ⇒ cluster together with the truncated copy marked suspect.

Scoring: chromaprint bit-error similarity over aligned offsets. **Scale warning:** karaoke tracks bunch at 3–4 minutes; ±10s blocking still yields tens of millions of comparisons. Naive Python loops are not acceptable — use numpy-vectorized XOR+popcount over packed uint arrays, or an inverted index over sub-fingerprint chunks (AcoustID-server style). Budget and measure this step on a 1k sample before the full run.

Thresholds: ≥0.85 is "the same recording" (used by (b) below and by §7.4's truncation scoping), ≥0.70 queues a cross-song review. **Persist all edges regardless.** Union-find ⇒ songs, in this order and no other:

1. **name-identity edges from a size-guarded group** — the sole cluster-forming key, subject to two gates:
   - **operator veto**: a pair (or, transitively, a pair of components) a reviewer has already called `different` in a `dedup_verdict` resolution is never re-merged by a name rule. A human who played both files outranks the key.
   - **`is_instrumental` agreement**: a pair is not merged on its name when one side is `yes` and the other is `no`. This gate can only ever *block*, never require: measured over active items, 15,077 are `yes`, 13,797 `unknown`, 33 NULL, and **`'no'` does not occur anywhere in the index** — not on one item, not on one location parse. So it fires zero times today. It is kept because it is the right shape for the rule and becomes real the moment a full-vocal original is ingested. A gate demanding positive *agreement* would instead refuse ~48% of merges over a question we never answered.
2. **operator `manual` edges** (§10), unconditionally. The only merge that may join two *named* songs, because it is a human asserting something about the song.
3. **the no-name fallback** ((b) above), which by construction can only attach a nameless component to an existing song or leave it alone.

A prefix_fingerprint edge ≥0.85 still marks the shorter item's `quality_attrs.truncation_suspect`, whether or not anything merged: that is a measurement about *one file* ("the same audio as its sibling, but less of it"), and §7.4.4 uses it to keep a truncated copy from being ranked the default.

**Why the audio key went** (this replaces §14.18's rationale, which kept it): the concern §14.18 solved with arrangements — that name merges would manufacture dedup losers — is dissolved rather than mitigated, because §7.4 now archives nothing. And the cost of keeping audio in the union-find was measured and is not small: 2,424 clusters with more than one name. A fingerprint cannot distinguish "different production of the same song" from "unrelated audio" (both land in the 0.60–0.65 noise band, §11.1), and it equally cannot distinguish "same recording" from "same song" — which is precisely the distinction a cluster now has to encode.

**Measured impact** (full `cluster` + `verdicts` on a scratch copy of the live index, 2026-07-31; nothing written to the live DB). Songs **21,147 → 22,195**, every one of the 28,907 active items assigned: 17,553 singletons, 4,642 multi-copy songs, largest song 10 copies (was 11). Method: 4,482 `title_match`, 160 `fingerprint` (the no-name fallback), 17,553 `NULL` singletons. Name merges 8,825 pairs, 1,941 of them from containment; **zero blocked** by the instrumental gate or by an operator veto, and zero oversize groups.

The two coherence numbers, which move in opposite directions and are the point of the change:
- **songs holding >1 distinct name: 2,424 → 1,640** (−32%). Classifying the residue by cause: 943 containment (by design — "Pink" vs "Pink & Nate Ruess", a Hebrew title with an added qualifier), 484 pure word-order/punctuation differences (by design — §6.1's unresolved artist/title order), 268 where one copy has no usable name and was attached by the audio fallback (by design), and **≤73 genuinely different token sets** — ~4%, and sampling those shows mostly the same patterns in three-way combinations. The known real failure in that residue is containment over-merging two different songs that share a phrase (measured example: two different numbers from one musical, one title a subset of the other).
- **names split across songs: 41 → 41.** Unchanged, i.e. the coherence gain cost nothing in the reverse direction.

Edge computation is unchanged by the reframe (`db_edges_added/updated/removed` all 0 against the previous run's rows): 341,349 coarse candidates, 31,181 `fingerprint` + 2,697 `prefix_fingerprint` + 8,825 `title_match` edges persisted. The no-name population is **1,216 items** — larger than the 626 `opaque` parses, because an item also has no key when only one of artist/title parsed. Of those, 309 were adopted by exactly one named song, 86 were ambiguous and left standalone (the constraint working), 959 stayed standalone.

**Verdicts:** `sole_copy` 17,553, `winner` 5,131, `alternate` 6,028, `manual_review` 274. Every song has a `default_item_id`. Both stages are idempotent — a second `cluster` reports 0 created / 0 deleted / 0 reassigned / 0 edge changes, and a second `verdicts` reports 0 verdict and 0 rank changes.

### 7.4 Ranking the copies of a song

**Only exact-content duplicates are duplicates**, and Stage 0 (§5.2) already collapsed those by `(gdrive_md5, size_bytes)` before a media_item existed. Everything §7.4 sees is a set of *distinct files*, and on this corpus we cannot show that any two of them are interchangeable:

- formats are not interchangeable (§7.4.1: `primary_format_preference` is a config flag and the non-primary format is deliberately kept active);
- two mp3+cdg copies of one song can be different karaoke *productions* — different key, different backing, guide vocal present or absent. **13,797 of 28,907 active items are `is_instrumental='unknown'`**, so for roughly half the library we genuinely cannot tell; and no item anywhere is marked `'no'`, so nothing has ever positively established that a copy carries a vocal.

So within a song we **rank** the copies and expose a default (`quality_rank` = 1), and **nothing is archived**. This is what replaces §14.18's *arrangement* axis: arrangements existed to stop name merges from manufacturing dedup losers, and once nothing is archived there are no losers to manufacture. A fingerprint-connected sub-group is no longer a crowning axis; it survives in exactly one place — scoping the duration-outlier check (§7.4.4) — because that check is a statement about one recording and nothing else.

Groups are per **(song, format)**. Ranking is a **total, deterministic order** and every member gets a rank, including `manual_review` ones, so a song always has a default copy to serve even while a human question about it is open.

Verdicts say what should *happen* to a file:

| verdict | meaning |
|---|---|
| `sole_copy` | the song has exactly one active item |
| `winner` | rank 1 of a song with more than one copy, **per format** |
| `alternate` | rank ≥ 2. **Stays active. Never archived.** (Was `loser`; migration 006 renames it — the old name described an outcome that no longer happens, and Stage 6 is not written yet, so this was the cheap moment to stop lying to it.) |
| `manual_review` | a human is needed (av split, duration outlier); the rank still stands |

0. **Duration spread is read on the same-recording group, not on the song.** Within one recording a large spread means truncation (§7.4.4). Across two productions of one song it is expected and must not page a human.
1. **Primary-format preference is a config flag** (`primary_format_preference`, set to `video` by sha-yol 2026-07-20), not a law of nature. Rationale for the original `mp3g` default: professionally timed, 10–50× smaller, seekable. Counter-consideration: CDG graphics are 300×216 — on a large projector many hosts prefer video versions. The choice is reversible and now *costlessly* so: nothing is archived either way, so the flag only decides which format is offered first.
2. Within `mp3g`: integrity status, then audio bitrate/codec (prefer ≥192 kbps). Deterministic tie-break: smallest content_hash. **Unchanged by the reframe** — the ranking keys are exactly as before; they now produce a full ordering rather than one winner plus a discard pile.
3. Within `video`: integrity, then height, then audio bitrate, same tie-break. If best-video and best-audio candidates differ ⇒ both `manual_review` (high-res reuploads with recompressed audio are common). They are still *ranked*; the verdict only says a human should choose.
4. **Duration outliers** — a `truncation_suspect`, or an item >10s from the median of its **same-recording peers** — sort last and get `manual_review` plus one grouped review row. Scoped to audio-connected peers, never to the whole song (§7.4.0). An item with no strong audio edge is its own recording: "we have never shown this audio to match anything" is not evidence that it does. *This narrows the previous rule* — before name-only clustering a cluster implied one recording, so the median could be taken over the whole group. It no longer can, and the honest consequence is that a truncated file whose fingerprint was never compared to its sibling's is no longer flagged here; `truncation_suspect` and Stage 4's decode remain the instruments that catch it.
5. **Sole copies** ⇒ `sole_copy` when the **song** has exactly one active item. That is what the rule should always have said: the old "unclustered ⇒ sole_copy" phrasing turned §7.3's recall failure into 3,572 wrong verdicts, crowning items as the only copy of their song while a filename twin sat elsewhere in the index, and then spending Stage 4 decode and Stage 5 MusicBrainz budget on both halves. Now that every item has a cluster, "unclustered" no longer exists as a state and the question answers itself. `sole_copy` regardless of quality — **except `broken` ones** (corrected by sha-yol, 2026-07-19; the original rule said "never archived: a glitchy copy beats no copy at the event"). A broken file is unplayable and replacing it beats trying to play it: a broken sole copy is archived (`archive/broken/`) and gets a `quality_flag` row so the archived songs form a replacement list rather than vanishing silently. **Unless it has a §7.3(b) name twin in another song** — then we may already hold the song under a different spelling, looking for a replacement would be wasted work, and the item is counted but kept off the list. `suspect` is not `broken`: Stage 4's decode clears it or demotes it, and the demoted case then follows this rule.
6. **Cross-song audio evidence** ⇒ one `possible_song_merge` dedup_verdict row per *cluster pair* (never one per edge), for every audio edge ≥ `verdict_review_floor` that spans two songs. The band has **no upper bound**: ≥0.85 used to auto-merge and so never needed a human, and that is exactly the band that needs one now.

   **This is the reframe's one real cost, and it exceeds the §11 budget.** Measured: **1,306** `possible_song_merge` rows, giving a `dedup_verdict` projection of 212 open + 1,475 new = **1,687 against a budget of 1,000** (over by 687). That is arithmetic, not a bug — every ≥0.85 audio edge that used to merge silently is now a question. The gate fires loudly and, under `partial`, applies all verdicts and withholds those rows; the ranking and the songs are fully materialised either way. **An operator decision is required** and the spec does not make it: drain the queue, raise `verdict_review_floor` (the 0.85+ band is 1,211 of the cluster pairs, so raising the floor barely helps — these are strong-similarity rows), accept a higher budget with a date and a name per §11, or accept that this queue drains over several review rounds. Note also that the 192 open `possible_duplicate_unclustered` rows in the live queue are superseded by this mechanism and should be retired rather than reviewed twice.

All ranking inputs recorded in `quality_attrs` JSON; there is deliberately no scalar "quality score" — verdict + rank + attributes + deterministic rules are the contract.

## 8. Stage 4 — Full-decode verification (survivors only)

`ffmpeg -v error -i <f> -f null -` on **winners and sole copies only** (≈ one file per cluster instead of all 30k+ — cuts the heaviest compute by well over half). Decode failure ⇒ mark `broken`, promote the next-ranked candidate in the cluster, decode it, repeat. Only after a cluster's final winner passes decode is its verdict frozen. Unattended, resumable, per-blob progress in the DB.

## 9. Stage 5 — Enrichment (MusicBrainz); Stage 6 — Organize

### 9.1 Enrichment
As v1, with fixes:
- AcoustID lookup per fingerprinted blob (mostly hits on full/original recordings). Hits → `song_metadata` source `musicbrainz_fp` AND `fingerprints.acoustid_recording_mbid` (acoustic identity).
- Everyone else: MB text search on `v_metadata` artist+title. 1 req/sec, descriptive User-Agent, every response into `mb_cache`.
- Acceptance: high score + both-field agreement ⇒ auto-accept `musicbrainz_text`; medium ⇒ `metadata_match` review with top-3 candidates; low ⇒ filename metadata stands.
- Hebrew: search in Hebrew script as-is; never auto-accept transliterated fuzzy matches; expect a larger manual queue for niche Israeli artists.
- `song_mbid` stored with its song-level-identity semantics (§3.6). Once present, later enrichment is lookup, not search.
- **Enrichment runs per CLUSTER, not per surviving item.** Now that clusters are song-level (§7.3), every item in a cluster is the same song by construction, so querying MusicBrainz once per surviving item re-asks the same question for each arrangement and each format of one song — pure waste against a 1 req/sec budget, and a way to get two different answers for one song. One query per cluster (keyed on the cluster's best `v_metadata` artist+title), result written to every member; unclustered items keep querying individually. Cached responses (§3.10) make a re-run free either way, but the first pass over a re-clustered index is where the saving is real.
- Enrichment runs on winners + sole copies only, and no enrichment result may change *which song* an item is — see the identity/presentation split in §3.6.
- *Implementation status (2026-07-31):* Stage 5 as built iterates per surviving item. The per-cluster form above takes effect on the next enrichment pass, after the re-cluster; until then the `mb_cache` (§3.10) absorbs the duplication for repeated queries but not the first one of each.

### 9.1.1 Free-text MB search for items the qualified search missed

`tools/mb_freetext.py`, source `musicbrainz_freetext` (§3.6, migration 004). Applied and run 2026-07-31.

**Why it exists.** 5,839 winner/sole_copy items with Latin-script metadata carried no `song_mbid` after §9.1. The qualified search builds `recording:"…" AND artist:"…"`, which fails whenever our artist/title split is wrong or our strings carry noise the qualified fields will not tolerate. MusicBrainz also accepts an **unqualified** free-text query — a different question, and a different `mb_cache` key, so it reaches the network instead of replaying the cached miss. Re-running the qualified search over these items would be an expensive no-op; this is not.

**Ranking (measured, n=200 labeled items, one query each, re-ranked offline):** recall ceiling 90.5%; **artist-substring only 87.0%** (what the tool uses); artist-gate + title tiebreak 86.5%; artist+title *additive* 78.5%; MB's own relevance score 76.0%; title-substring only 67.0%. Two consequences are load-bearing:

- **MB's `score` collapses on free-text** — candidates routinely all return 100, wrong artists included. `MB_AUTO_ACCEPT_SCORE` is meaningless here and is deliberately unused; score is a last-resort tiebreak.
- **Never combine artist and title additively.** Title tokens dilute the artist constraint and let a wrong-artist candidate win on token count. Title is only ever a subordinate tiebreak *below* the artist gate.

**Acceptance, and why it is safe.** Gate candidates on artist-token overlap, then require the pick to agree with **both** existing fields at ≥ `MB_FIELD_AGREEMENT`. We are not asking MusicBrainz what the song is; we are asking for a canonical id for something we already believe. Anything that would *redirect* us is rejected by construction — which is exactly what §3.6's identity ladder requires, and what kills the failure mode free-text otherwise invites.

**Tier 2 — order-free acceptance.** The rule above compares field to field, which is exactly why it rejects every item whose artist and title are *swapped* in our parse: such an item clears the artist gate (which tests the union of both our fields) and then dies at the field-wise comparison. Tier 2 runs only on what tier 1 refused, and drops the field alignment while keeping the containment discipline: the candidate must be **explainable by our text as a bag of words** — its artist tokens and its title tokens each covered ≥0.80 by the union of our two fields. If it is, MusicBrainz has also told us *which of our strings is the artist*. That is the segmentation oracle, and it is the same move `tools/spotify_order.py` makes for Hebrew (§6.1).

Two constraints are load-bearing rather than decorative:

- **Artist and title must explain *different* tokens.** Without this, candidates whose title is merely the artist's name pass trivially, since the same tokens satisfy both coverage checks. Measured on the 4,068 remaining items: coverage alone accepted 542, including `All That Jazz | Catherine Zeta Jones` → `Catherine Zeta-Jones | Catherine Zeta Jones` and a Hebrew item matched to an unrelated Tuvan band. Requiring the title to be covered by tokens the artist did not consume yields **415** and rejects all of those, while keeping every good recovery.
- **Tier-2 confidence occupies a band strictly below tier 1's floor** (0.60–0.84 vs ≥0.85). The two tiers share a source, so confidence is the only thing that keeps them distinguishable for a §11 retune without re-querying.

A tier-2 accept **changes our field order** — permitted explicitly by §3.6's identity ladder, which lets an external catalogue correct spelling *and* order, and safe because the candidate had to be explainable by our own text to get there.

*Run 2026-07-31:* 415 accepted from 4,068 (10.2%), **entirely from `mb_cache` — 0 network requests**, 0 errors. 321 of them corrected the artist field. Beyond order, tier 2 also recovers items whose fields carry noise the parser missed: `Hozier [<site>.com]`, `(In the style of Imagine Dragons)`, `01.Madonna`.

**Scope:** Latin script only; the Hebrew items without an MBID are deliberately untouched here and handled by §9.1.2 instead. *(The original reason given for this scope — "MB's coverage of Israeli artists is thin" — was measured wrong on 2026-07-31 and is corrected in §9.1.2: MB's Hebrew coverage is thin at the RECORDING level, not the ARTIST level. The scope boundary still stands, because the free-text **recording** search this section describes is exactly the thing Hebrew coverage is thin for.)*

**Caveat:** `title_agreement` strips version qualifiers, so an accepted MBID may name a remix or karaoke-version *recording* rather than the original. That is consistent with `song_mbid`'s song-level semantics (§3.6) and must not be read as acoustic truth; preferring earliest releases is an open refinement.

**Sampling trap worth remembering:** bad source batches cluster by `media_items.id` (the lowest ids are swapped artist/title, unstripped `[<site>.com]` site tags, and transliterated Russian misfiled as `latn`). A `LIMIT n` head slice therefore reads far worse than the population — the first bounded dry run accepted 0/7 where an evenly-strided sample of the same population accepted 31.5%. Use `--stride` for any estimate.

### 9.1.2 Catalogue enrichment — Wikidata, the MB artist index, iTunes, Deezer

`karaokemp/enrich_sources.py` (shared plumbing), `tools/heb_enrich.py` (Hebrew), `tools/catalogue_enrich.py` (Latin). Sources `wikidata` / `musicbrainz_artist` / `itunes_text` / `deezer_text`, migration `006_catalogue_sources.sql`. Responses cache in the new `enrich_cache` table (§3.10 contract, keyed by source as well). Built and run 2026-07-31.

**Why the §9.1/§9.1.1 passes could not close these gaps.** Both ask MusicBrainz the same underlying question, and both fail on the same two inputs: a Hebrew string MB has no *recording* for, and a Latin string misspelled in **our** text (`John Secada`, `Dave Mathews Band`). No query shape fixes a defect in the query's own terms.

**Measured 2026-07-31** (evenly strided samples — see §9.1.1's sampling trap):

| population | source | found it | notes |
|---|---|---|---|
| Hebrew, n=40 | Wikidata | **82.5%** | 100% Hebrew-script labels |
| | MB artist index | **62.5%** | 100% Hebrew-script names |
| | iTunes | 5.0% | not run on Hebrew |
| | Deezer | 2.5% | not run on Hebrew |
| Latin, n=40 | iTunes | **67.5%** | all carrying year *and* genre |
| | Deezer | 55.0% | no year/genre on the search endpoint |
| Hebrew title-only, n=25 | Wikidata | 20% | every hit carried a performer |

**The premise correction.** MusicBrainz files Israeli artists under a **Hebrew primary name** with a Latin sort-name (`שרית חדד` / `Hadad, Sarit`) — the opposite of Spotify and iTunes, which carry them under Latin names ~90% of the time. §9.1's "search in Hebrew, expect a large manual queue" and §9.1.1's "leave Hebrew to Spotify" both underrated MB's *artist* index. What is genuinely thin is MB's **recording** coverage: an `arid:`-scoped recording search using an artist MBID we had **just confirmed** yielded a `song_mbid` for only 4/25 (16.0%).

**So Hebrew enrichment buys segmentation, not identifiers,** and that is the accepted outcome (sha-yol, 2026-07-31: *"missing would stay missing"*). It fixes the thing §6.1 actually got wrong — `location_parses` splits Hebrew `artist_title` 1,897 vs `title_artist` 1,853, a coin flip, because §6.1 deferred order to an MB match Hebrew items never got.

**The typed order oracle.** Wikidata is a *graph*, and that is what makes it decisive: each of our two fields is searched separately and the returned entities are classified from their **claims** (artist-like: carries `P434`/is a human or band; work-like: carries a performer/composer/lyricist). If field X resolves to a person and field Y to a song, the order is settled **without comparing any text at all**. The MB artist index answers the same question independently. Agreement earns the top confidence band (0.95); a single source writes at 0.85; disagreement writes nothing.

**One branch needs a second vote.** "An artist matched and the other field matched *nothing*" is absence of evidence, not evidence. Live item 8 (`הוריקן` | `Hurricane עדן גולן (גרסת בנות) PIANO l NATI`): Wikidata has a **Serbian** band called הוריקן, our other field is decoration-laden junk matching nothing, and the branch confidently declared our *title* to be the artist. It is therefore marked `wd_resolved_artist_only` and requires MB to independently name the same field. Cost on a strided sample: 73.3% → 66.7% resolved, in exchange for removing a demonstrably wrong order decision.

**Hebrew text discipline.** A catalogue name is written **only when it is itself Hebrew script**; where our text is Hebrew and the catalogue's is Latin, ours stands and the catalogue is a signal about *order* only. Wikidata's Hebrew labels are the one external source measured good enough to store, and they correct real errors (`פאבלו רוזנברג` → `פבלו רוזנברג`, `להקות צהל` → `להקות צה״ל`).

**Version qualifiers: catalogue spelling, OUR qualifier** (`enrich_sources.merge_title`). `title_agreement` strips brackets *deliberately*, so that `Boston (SC)` and `Boston (Live from the Grove)` compare equal — right for deciding they are the same song, badly wrong for storing the result. Taking the candidate's **base** title and re-appending **our** qualifier gets both halves right, and unlike a `song_mbid` this is presentation text a human reads.

**Title-only items (541).** The safety rule is a substring test: if the performer's name is present **inside our own string** (`ילדה קטנה משה פרץ ואגם בוחבוט שרים`) we have *segmented our own text* — auto-accept. If it is not, that is Wikidata's **attribution**, and covers are the norm in a karaoke library — `metadata_match` review, never a silent write. Measured: 20% find a song entity, only ~4% clear the substring test.

**Deezer is corroboration, never a lone voice.** It matched `THE BEATLES | HELP!` to the cover act *Blues Beatles* where iTunes returned The Beatles. It ranks below `itunes_text`, so where both fire iTunes wins and Deezer is inert; it earns its place only on items iTunes missed.

**Neither iTunes nor Deezer returns a MusicBrainz id,** so this pass closes no MBID gap directly. It closes it *indirectly*, and the follow-on needs no new code: run `tools/catalogue_enrich.py` to correct the text, then re-run `tools/mb_freetext.py`. That tool only writes rows on **accept**, so every item it previously rejected is retried — and retried with different text, which is a different `mb_cache` key and therefore a real query rather than a replayed miss.

**Acceptance obeys §3.6 throughout:** every tier requires agreement with our existing fields, or (order-free) that the candidate be explainable by our own text as a bag of words. These passes correct spelling and order and add year/genre. **None of them may change which song an item is.**

### 9.2 Organize
- Move winners `staging/` → `active/<Artist> - <Title> [<item_id>]/`, names from `v_metadata` (fallback: raw filename). MP3+CDG pairs move together with matching basenames. **Zip-member winners are extracted files by this point (§7.1) — nothing playable may remain inside a zip in `active/`.**
- Losers/broken/orphans → `archive/<reason>/`.
- **Re-runnable:** on re-run, items whose `v_metadata` winner changed get renamed; `fsck` (index↔filesystem reconciliation) runs after every Organize/archive batch and must pass.
- `--dry-run` prints every planned move/rename. First real run happens only after a reviewed dry-run.
- Sanitize filesystem-unsafe characters and RTL edge cases in filenames; display strings live in the index.

## 10. Review tooling (minimal)

CLI/TUI that drains `review_queue` by kind: show payload, play file(s) via `mpv`, record resolution (writes `manual` metadata rows / final verdicts). No web app.

## 11. Review-queue realism & threshold tuning

At 30k files, medium-confidence buckets can reach thousands of items. Requirements:
- Every stage's `pipeline_runs.report` includes queue-size counts per kind, so the operator sees review burden **before** committing to it.
- Clustering and metadata-acceptance thresholds are config values; a `retune` operation re-derives verdicts/queues from persisted edges and cached MB responses without recomputation.
- If a queue exceeds an agreed budget (default: 500 items/kind), stop and retune rather than grinding through it.
- **A budget overrun must be loud.** The gate reports the per-kind projection (`open + new` vs budget) whether or not it trips, and names the offending kind and the amount it is over. The failure it replaces was a silent full-transaction rollback: the verdicts were reverted along with the review rows, so a run that changed nothing looked exactly like a run with nothing to change. A `partial` mode may apply the verdicts and withhold only the over-budget kind's review rows, reporting exactly what was withheld; the withheld rows are recomputed unchanged on the next run once the queue drains. Raising the budget is an operator decision recorded in config with a date and a name — never something a stage does to get past its own gate.

### 11.1 Fingerprint similarity is bimodal on this corpus — it is NOT a recall knob

**Read this before touching `CLUSTER_AUTO_MERGE`.** Measured over the 5,723 item pairs that share an identical normalized filename-derived artist+title:

| refined fingerprint similarity | share of name-identical pairs |
|---|---|
| ≥ 0.85 (auto-merge) | 43.2% |
| 0.70 – 0.85 | 1.3% |
| 0.65 – 0.70 | 2.7% |
| 0.60 – 0.65 (noise floor) | 43.2% |
| never compared | 9.7% |

**The middle is empty.** Chromaprint on this corpus says either "the same recording" or "nothing in common", and it says the second thing about 43% of the pairs whose filenames are identical — because they *are* different recordings: separate publisher cuts of one song, or a karaoke cover next to a full-vocal original.

Consequences, in order of importance:

1. **Lowering the auto-merge threshold cannot fix recall.** Going from 0.85 to 0.65 would recover ~2.7% of these pairs while admitting the noise band — the 1k benchmark put ~51,000 unrelated pairs in the 0.5 bin alone. There is no threshold that separates "same song, different cut" from "unrelated audio", because the fingerprint does not encode the distinction.
2. **Recall failures here are model failures, not tuning failures.** The fix was to stop asking the fingerprint a question it cannot answer: identity comes from the name (§7.3(b)), the fingerprint distinguishes recordings *within* an identity (arrangements, §7.4).
3. **Precision was never the problem.** Of 3,187 multi-item clusters, 1,392 disagreed on ordered (artist, title) but only 1,066 still disagreed order-insensitively, and sampling showed that residue is dominated by parse artifacts — order swaps, one-letter typos, appended transliterations, featured-artist supersets — not by fingerprint false merges. Genuine false merges exist but are rare.
4. Retuning `CLUSTER_AUTO_MERGE` is still legitimate for its actual job: deciding what counts as *the same recording*. It is not a dial for how much of the library gets deduplicated.

## 12. Extensibility — future generation pipeline

Unchanged in substance from v1; structures created empty now:
- `artifacts` (§3.9): derived, disposable, regenerable from (source item + tool + params); artifacts are blobs like everything else.
- Canonicalization: new `audio_lrc` media item (files: `instrumental_stem` + `lrc` artifact) joins the source item's cluster; old item → `status='superseded'`, `superseded_by=<new>`; files → `archive/superseded/`. One row update + one move.
- `is_instrumental` + orphan-MP3 vocal-presence flags identify full originals (Demucs input) already in-library; `title_match` edges link full↔karaoke siblings so generation prefers in-library sources. Since §7.3 merges on name identity, such a sibling pair usually sits in ONE song as two ranked copies — and where `is_instrumental` marks the pair `yes`/`no` it is deliberately left unmerged, which is itself the signal that a full original is present. (That gate has never fired: no item in the index is marked `no`. Until a full-vocal original is ingested, "a full original is present" is a hypothesis this index cannot yet confirm for any song.)
- Line-level LRC is the floor; enhanced/word-level when the aligner is confident (English yes, Hebrew not required). `params.granularity` records which.
- `alignment_check` review kind exists for timing QA.

## 13. Operational requirements

- Python 3.11+; `mutagen`, `numpy` (fingerprint comparison), `fpcalc`/`chromaprint`, `rclone`, `ffmpeg`/`ffprobe` as external binaries. Plain CLIs, no daemons.
- Idempotency: any stage run twice back-to-back ⇒ zero changes second time (tested). All tie-breaks deterministic (§5.2, §7.4).
- Resumability: per-item progress in DB statuses; Ctrl-C-safe everywhere.
- `--dry-run` on every mutating stage.
- **DB backups: `sqlite3 ... ".backup <dest>"` or `VACUUM INTO`** before every stage run — never a raw file copy of a live WAL database (corrupt-snapshot risk). Keep 20 rotating.
- Batch reports per stage (counts by status, space, queue sizes) → `logs/` + `pipeline_runs.report`.
- No destructive ffmpeg: never rewrite source media in place.
- No writes to the shared Drive folder, ever (§0).

## 14. Changelog v1 → v2

1. **Schema:** `files` split into content-addressed `blobs` + `file_locations`; `content_hash` no longer UNIQUE/NOT NULL on locations (multiple copies of one blob are the normal case; NULL until hashed). Fingerprints keyed by blob.
2. **Remote identity:** Drive stable file ID stored and used as the remote key; paths demoted to informational (non-unique on Drive). Shortcut and download-quota handling specified.
3. **Drive is read-only:** Stage 0 "archiving" defined as index-only exclusion; no server-side mutations of the shared folder.
4. **Truncated-duplicate fix:** clustering candidates also generated from normalized (artist,title) regardless of duration + prefix-fingerprint comparison; duration outliers can't be crowned sole_copy.
5. **Stage reorder:** Enrichment before Organize; Organize made explicitly re-runnable with rename-on-metadata-change semantics.
6. **Filename evidence preserved:** all copies in a hash group are parsed and hints merged onto the survivor.
7. **Download stage made explicit** with host/disk prerequisites, size budget, batching fallback, MD5 verification, quota backoff.
8. **Full decode moved after clustering,** survivors only, with loser-promotion on failure.
9. **Fingerprint-comparison scale** addressed: numpy popcount / inverted-index requirement + mandatory 1k-sample benchmark.
10. **Nikud stripping** narrowed to Mn-category marks; maqaf/sof-pasuq/paseq preserved.
11. **mp3g-vs-video primary** demoted from fact to config flag requiring operator confirmation; non-primary format kept active by default.
12. **Deterministic tie-breaks** specified (survivor = smallest drive_file_id; quality ties = smallest content_hash).
13. **Backups** switched to `.backup`/`VACUUM INTO`; `--dry-run` mandated on all mutating stages.
14. **Review-queue budgets,** threshold `retune` from persisted edges/cache, and the 200-file parser golden-set gate added.
15. **MBID semantics split:** `song_mbid` (song-level identity, metadata) vs `acoustid_recording_mbid` (acoustic identity, fingerprints); labeled so nothing downstream trusts song_mbid as acoustic truth.
16. Fixed v1 internal inconsistencies: `cdg_discid` source removed from trust ladder (disc IDs come from filenames); scalar `quality_score` replaced by `quality_attrs` JSON + deterministic verdict rules; zip-member winners explicitly extracted before Organize.
17. **Phase gates:** Stage 0 inventory report reviewed before Stages 1–3 code is finalized; execution host named as prerequisite.
18. **Filename-first identity (2026-07-31).** §7.3's "title-only equality never auto-merges" is withdrawn. A cluster is now a SONG (name identity, order-insensitive and containment-aware), and the recordings inside it are ARRANGEMENTS (audio-connected components) that §7.4 crowns separately — so name evidence groups, audio evidence dedupes, and no file is archived because two filenames agreed. The old rule's real concern (a full-vocal original merged behind its karaoke cover) is now enforced with `is_instrumental` agreement, which identifies that case directly, plus a veto on any pair an operator has already called `different`. Driven by measurement: 56.8% of the 5,723 name-identical item pairs had been split, and 3,572 items were crowned `sole_copy` with an exact filename twin in the index. Consequential edits: §3.5 (cluster `method`/`confidence` given real semantics; `title_match.similarity` now carries name-evidence strength instead of NULL), §3.6 (trust order split into an identity ladder and a presentation ladder; no enrichment source may change *which song* an item is; `filename` above `id3` per migration 003), §7.4.0/§7.4.5 (verdicts per (cluster, arrangement, format); `sole_copy` requires genuinely having no twin), §9.1 (enrichment per cluster, not per surviving item), §11 (loud budget gate) and §11.1 (**fingerprint similarity is bimodal on this corpus and is not a recall knob** — the most reusable finding of the whole exercise).
19. **Name-only clustering, the `songs` object, and ranked copies (2026-07-31, later the same day).** **This SUPERSEDES the arrangement model of §14.18**, which is only hours old — the note above stands as the reasoning that got us here, not as current behaviour.

    Three decisions, taken together because they are one decision:

    a. **The name is the SOLE cluster-forming key.** `fingerprint`/`prefix_fingerprint` edges leave the union-find entirely. They are still computed and still persisted (§3.5) and §11 still retunes from them, but they no longer decide what a cluster is. §14.18 kept audio as a *second* merge key, and that is **logically incompatible with "cluster == song"**: an audio edge glues items together regardless of what they are called, so a cluster built partly from audio has no well-defined name — and a song that cannot be named cannot be searched, which is the only thing anyone ever does with this library. Measured under §14.18: **2,424 clusters held more than one distinct artist+title string**, against **41 names split across songs**. The audio key bought precision this corpus did not need and cost the coherence it did. Audio keeps two secondary jobs, neither a merge: a `possible_song_merge` review when two *different* songs sound alike (§7.4.6), and a **no-name fallback** for the items whose parse yields no key at all, constrained so a nameless item can never bridge two songs together.

    b. **Best-available name, not bootstrap-gated.** The key is whatever `v_metadata` serves — canonical where MusicBrainz/Spotify resolved it, the filename parse otherwise. Gating on `song_mbid` would strand exactly the population that needs grouping most: the MB bootstrap reaches ~66% of items (18,992/28,907) and fails worst on Hebrew. Better names flow through and produce better keys with no re-architecture; a re-run re-keys the affected items and reshapes their songs, because the DB sync is a full diff.

    c. **Rank duplicates, do not archive them.** **Only exact-content duplicates are duplicates**, and Stage 0 (§5.2) already collapsed those before a media_item existed. Formats are not interchangeable, and two mp3+cdg copies of one song can be different karaoke *productions* — 13,797 of 28,907 active items are `is_instrumental='unknown'`, so for roughly half the library we cannot tell. §7.4 therefore **ranks** the copies and exposes a default; the non-default ones are `alternate` and stay active. `loser` is renamed by migration 006 rather than merely redefined, because Stage 6 is not written yet and a value called `loser` sitting next to a directory called `dedup_losers/` is an instruction to a stage nobody has built. `archive/dedup_losers/` is removed from the storage layout.

    Plus: **every active item gets a cluster**, singletons included, so `cluster_id` is universally non-NULL and every downstream query loses its NULL branch; and **`v_songs`** (§3.11) exposes one row per song — a VIEW, deliberately, with the durable `songs` table explicitly deferred until something outside the index needs a song id that survives re-clustering.

    Consequential edits: §1.2 and §2 (nothing is archived for being a duplicate), §3.3 (`quality_rank`; `alternate`), §3.5 (`method` precedence inverted — name is the headline now, audio only names a cluster it built alone; `NULL` means singleton), §3.11 (new), §7.3 (rewritten), §7.4 (rewritten, retitled "Ranking the copies of a song"), §12. Migration `006_songs_and_ranked_copies.sql`; `v_metadata` gains a `source_rank` column so `v_songs` need not restate the trust ladder.

    **Two corrections to the record made while doing this.** (i) §14.18 and its config notes claimed `is_instrumental` was 15,077 `yes` / 13,797 `unknown` / 0 `no`. Re-verified: that is right, and there are additionally **33 NULLs**. The interesting part is the zero — `'no'` occurs nowhere in the index, not on one item and not on one location parse — so the instrumental gate can only ever *block*, never *require*, and it fires zero times today. It is kept because it is the correct shape for the rule. (ii) The measurement "42 names split across clusters" reproduces as **41**, and only under a song = *cluster-else-own-id* reading; counting over clustered items alone it is **3**. Both readings are recorded because the first is the one comparable to the post-change number.
