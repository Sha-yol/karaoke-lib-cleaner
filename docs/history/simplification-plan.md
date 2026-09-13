# Simplification plan — reduce to a rederivable pipeline, then merge the HDD library

> **Outcome (2026-09-13).** Cutover happened before most of this plan ran, and cutover changed
> its premise. The runtime DB is now authoritative ([`../runtime-db-spec.md`](../runtime-db-spec.md) §4),
> and new material, the HDD library included, enters through runtime **ingest** (§5.5), not
> by re-running this pipeline. So the repo was reduced to a reference archive instead:
>
> * **Done before cutover:** the Spotify cache folded into the index, fp-derived MBIDs
>   dropped, migration 008 (fp below filename), and the llm_parse recovery pass.
> * **Done at publication:** the Phase 4 deletions (one-shot tools, `review.py` + `cli review`,
>   `goldset`), and migrations 001–008 folded into `schema.sql`. Folding 008 exposed a stale
>   `LLM_PARSE_RANK` (4, live rank 5), which is now fixed.
> * **Not done, overtaken by cutover:** Phase 0's corrections file, cache-key freeze, and
>   offline mode; Phase 2's local-filesystem source; and Phase 3's golden diff. The archived
>   pipeline DB keeps every resolution and cache row these were meant to protect.
> * **Kept deliberately:** the fingerprint code. It is how the evidence was produced.

Status: **drafted 2026-08-22, not started.** Supersedes nothing; `HISTORY.md` remains the
historical record (and stops at 2026-08-02 — it does not cover llm_parse v2, §14.19 name-only
clustering, or `export_runtime`, i.e. steps 3, 5 and 6 below).

## The goal, stated precisely

Reduce the repo to the six steps that actually produce the runtime DB, so that (a) the runtime
DB is cheap to re-derive and (b) the same pipeline can be pointed at the external-drive library
and the two can be merged.

The six steps:

1. inventory the files, checksum them, find unplayable files and exact duplicates
2. parse artist + title from filename + path with the deterministic parser
3. parse with an LLM where the deterministic parser fails
4. enrich metadata via MusicBrainz / Spotify / other catalogues; get canonical spelling
5. cluster versions into songs by exact artist+title match
6. produce the runtime DB from the interim tables

## Two corrections to the framing, both load-bearing

**The main work is additive, not subtractive.** Step 1 as built is Drive-shaped: `stage0`
enumerates via `rclone lsjson` against a pinned folder ID, the exact-dup pass groups on
`gdrive_md5`, and `stage2` downloads by `drive_file_id`. None of that exists for a local HDD.
`HISTORY.md` BLOCKER 2 records the FUSE mount being *rejected* as an enumeration source
precisely because it lacks those two fields. No amount of deleting produces the replacement.

**The database is the asset, not a byproduct.** ~207 MB of the 584 MB index is purchased API
and LLM answers. "Re-derive the runtime DB" must never mean "re-ask everyone."

| cache | banked | cost to re-buy |
|---|---|---|
| `mb_cache` | 61,150 responses / 166 MB | ~17 h at MB's 1 req/s |
| `enrich_cache` | 13,294 / 40 MB | Wikidata + MB-artist + iTunes + Deezer |
| `llm_parses` | 8,791 stems (1,594 v1 + 7,197 v2) | Batch API spend, the full 43-batch sweep |
| `fingerprints` | 28,411 | 3.2 h of fpcalc |
| `spotify_cache` | 2,333 queries | ~700 req/day dev quota, ~24 h Retry-After |
| `title_cards` | 138 | LLM pixel reads |

The runtime DB, by contrast, exports in **77 seconds**. The relationship to make explicit:
the pipeline index is durable and backed up; the runtime DB is a cheap derivative.

---

## Phase 0 — protect what cannot be rebuilt

Nothing is deleted until this phase is complete.

- [x] **Fold the Spotify cache into the index.** Done 2026-08-22: `spotify_cache` 0 → 2,333
      rows via `tools/import_spotify_cache.py`, all queries round-trip byte-identical,
      re-run inserts 0. It had been living in a loose `spotify/search_cache.jsonl`, outside
      the DB, outside git, and outside `tools/backup_db.sh`.

- [ ] **Export the human decisions to a versioned file in the repo.** 795 `manual`
      `song_metadata` rows (rank 14 — beats every other source), 1,158 resolved
      `review_queue` rows, 533 manual `cluster_edges` (454 active items in manual clusters).
      **Key on `content_hash`, not `media_item_id`**, so the file survives re-clustering *and*
      applies to HDD files that hash the same.

- [ ] **Add an `apply_corrections` step** that replays that file into a fresh index. This is
      what converts ~2,200 rows of human judgment from a *process* into an *input*, and it is
      the difference between "rederivable" and "rederivable minus the best data we have."

- [ ] **Freeze the cache-key interfaces.** `stage5.cache_key`, `stage5.mb_search_url`, the
      `enrich_sources` URL builders, and the `llm_parse` stem normalizer are now public
      contracts, not implementation details. `mb_cache` keys on `sha256(url [+ body])`; change
      how a URL is built — quoting, field order, escaping — and 61,150 rows go unreachable in
      one commit, silently, with the whole suite still green.
      Add a cache-hit-rate check against the live index that fails loudly on drift.

      Good news worth preserving: `llm_parses` keys on **stem**, so an identically-named file
      on the HDD hits the cached parse for free. That is a real head start on the merge, and
      it holds only as long as the normalizer does not drift.

- [ ] **Add offline mode.** Every enrichment step runnable with the network off, where a cache
      miss is a loud error rather than a silent refetch. This makes "do not re-spend" a
      property of the code instead of a discipline that one careless run breaks.

- [x] **`title_card_ocr` stays a live sibling of step 3** (sha-yol, 2026-08-22). It is an LLM
      read of pixels, the same category as `llm_parse`, and not manual review — so it keeps
      its stage rather than being frozen into the corrections file. 248 rows / 128 songs that
      had no title at all.

---

## Phase 1 — fix the metadata trust ladder before anything re-derives

Measured 2026-08-22 against the live index. `musicbrainz_fp` sits at rank 13, above
`musicbrainz_text` (12) and everything else but `manual`. It changes the served value on
**1,503 artists and 639 titles**, and the two populations behave oppositely:

* **Overriding `musicbrainz_text` (801 rows) — never an improvement.** `musicbrainz_text` rows
  were written only at score ≥ 92 with BOTH fields agreeing, so this is fp overriding the most
  corroborated evidence in the ladder. Sampled 18: roughly half outright wrong (`Alexa Goddard`
  over `Adele`, `Studio 99` over `Blondie`, `Tony Evans Dancebeat Studio Band` over `Adele`,
  `Sweet Little Band` over `P!nk`, and one literal `[unknown]` over `Kaiser Chiefs`), the rest
  cosmetic credit-format churn (`Nelly, Kelly Rowland` over `Nelly feat. Kelly Rowland`).
  Zero improvements.

  Mechanism: a karaoke track fingerprints onto *another* karaoke/covers recording, and
  MusicBrainz then names the cover act rather than the original performer. This is the same
  failure that got AcoustID retired as a conflict tiebreaker on 2026-07-21; the finding simply
  extends to the non-conflict hits.

* **Overriding `filename` (1,266 rows) — genuine rescues.** The `ZPBX1-*` batch carries a bare
  catalogue code in the artist field and artist+title unsplit in the title. fp correctly
  supplied `The Script`, `Keane`, `Beyoncé`, `LMFAO`.

### DECIDED (sha-yol, 2026-08-22): the fingerprint source is noise and comes out

- [ ] **Delete the fp-derived `song_mbid`s.** 3,510 rows. Where fp matched a cover, the MBID
      identifies the *cover's* recording and then feeds clustering identity and the runtime
      export — worse than no MBID.

      Cost, measured: **1,633 items lose their only `song_mbid`** and go to none. Accepted;
      those are exactly the population the recovery pass below targets.

      Reversible: the acoustic evidence is NOT in `song_metadata`. 3,729 rows of
      `fingerprints.acoustid_recording_mbid` survive the deletion untouched, so the claim
      layer can be rebuilt from the evidence layer if this is ever revisited (§11).

- [ ] **Migration 008: demote `musicbrainz_fp` below `filename`.** Not merely below the
      corroborated catalogues — filename wins over fp outright. fp rows are kept as evidence
      rather than deleted (10,280 rows), inert at the new rank.

### The recovery pass — why removing fp is what unblocks these files

Measured 2026-08-22, and it confirms the suspicion that fp was actively suppressing better
answers rather than merely adding bad ones:

* 865 items have fp overriding a **differing** `filename` artist (the 1,266 figure quoted
  earlier spans artist and title together; artist alone is 865).
* They are covered by **885 distinct stems**, of which **882 have never been llm-parsed**.
* **882 of 882 sit in the sweep's SKIP set** — and they are there *because of fp*. The
  predicate is `has song_mbid AND artist+title both winning above rank 4`; fp supplied both
  halves. The fingerprint answer is the reason the LLM never looked at these filenames.

So the recovery needs **no new tooling**. Removing fp drops both halves of the skip predicate
and the stems fall into the existing workset on their own:

- [ ] 1. Delete fp `song_mbid`s + apply migration 008 (above)
- [ ] 2. `tools/llm_parse.py extract --sweep` → the ~882 stems are now eligible (≈5 batches at
         the sweep's measured ~200 stems/batch), then `promote`
- [ ] 3. Run the corrected text through `tools/mb_freetext.py`. Precedent: the iTunes-corrected
         re-run yielded +441 MBIDs with no new code, because `mb_freetext` only writes on
         accept, so every previously-rejected item retries under a different `mb_cache` key.
         Budget expectations from the last run: marginal yield on residue is ~6–8%, not the
         30.3% headline of run 1 — pre-sample a strided n≈125 to estimate rather than
         extrapolating from run 1.
- [ ] 4. Whatever still misses **stays on `filename`.** That is the accepted outcome, not a
         failure state.

---

## Phase 2 — make step 1 run against the HDD

- [ ] **Local filesystem enumeration source**, replacing `stage0`'s rclone path: walk the
      drive, compute md5 + sha256 in one pass, populate `file_locations` with NULL
      `drive_file_id` (the column is already nullable). The exact-dup pass then groups on our
      own md5 rather than Drive's.

- [ ] **Retire `stage2` for local libraries.** Nothing to download; `local_path` is known at
      enumeration time. This removes the single largest module from the critical path.

- [ ] **Add provenance.** Nothing currently records which library a file came from. Required
      before any merge decision can even be expressed.

### DECIDED (sha-yol, 2026-08-22): the merge rule

HDD items join the **same song** as additional **versions**, ranked **below** the existing
library's copies. They are fallbacks: when live play demotes a top version of an existing
song, the next-ranked copy — possibly from the new library — takes over.

This needs no new model. §14.19 already made a cluster a song, made the non-default copies
`alternate`, kept them active, and archives nothing; ranked-fallback is exactly that shape.
And the runtime export already computes the chain: `rt_versions.fallback_rank` is a
`ROW_NUMBER() OVER (PARTITION BY cluster_id, format ORDER BY quality_rank, id)`, so once new
items sort last they are picked up automatically.

- [ ] Make provenance a **major key in the ranking order**, ahead of the existing
      integrity → format-class → bitrate/height → hash chain, so no incoming item can ever
      displace an existing rank-1 regardless of how good its bitrate looks.
- [ ] Byte-identical copies across the two libraries still collapse on `content_hash` before
      any of this — they are one blob, not two versions.
- [ ] **Open, and out of scope here:** "live play demotes a top version" implies the player can
      record a demotion somewhere and have it persist. `rt_versions` can *serve* the fallback
      chain but nothing today can *write* a demotion back. That is a runtime feature; note it
      so the merge is not mistaken for having delivered it.

---

## Phase 3 — establish the acceptance test

- [ ] Pin the current runtime DB (21,533 songs / 28,848 versions / 51,246 files) as **golden
      output**.
- [ ] Re-run the reduced pipeline and diff against it. Every difference must be a source
      dropped on purpose. Without this, there is no way to show the cleanup was lossless —
      which is the whole reason deletion comes last.

---

## Phase 4 — delete

Only once Phases 0–3 hold.

**Outright, no argument** — one-shot tools that already did their job and wrote their rows:

- `tools/spotify_order.py` + `tools/spotify_order_daily.sh` (finished; cache now in the DB)
- `tools/apply_order_fix.py`, `tools/fix_titles.py`, `tools/rescore_medium_band.py`,
  `tools/cluster_b_diff.py`
- `tools/merge_enrich_work.py`, `tools/merge_llm_parse_work.py` — artifacts of one-writer
  SQLite during long concurrent runs; a clean re-derivation has no concurrent writer
- `tools/build_catalogue_xlsx.py` — the runtime DB replaces it
- `karaokemp/review.py` + `cli review *` + the `review_exports` table (after Phase 0 has
  frozen the resolutions to a file)
- `goldset` / `ingest-goldset` — the §6.2 gate served its purpose and passed
- collapse `tools/migrations/001..007` into `schema.sql`, which is already reconciled and was
  re-verified 2026-08-22 as converging with the live index at 36/36 objects, zero drift

**Keep:**

- `tools/backup_db.sh` — it is protecting ~207 MB of purchased answers, not just an index
- every trap-pinning test, regardless of what else goes: the leading-track-number rule
  (`4 Non Blondes`, `7 Nation Army`), the weak-decoration list, the pair-splitting guards,
  the ID3v1 truncated-prefix rule

**Fingerprinting — partial, not total.** The instinct to drop it is right and §14.19 already
removed fp edges from the union-find, but it still does two jobs:

- 348 active items cluster *only* via the fp no-name fallback (items with no parseable
  artist+title have no other key)
- `verdicts.py` uses fp grouping to scope the duration-outlier check

Recommendation: delete the coarse-similarity clustering machinery and the mid-band review
plumbing; keep the already-purchased 3,511 AcoustID hits as *data* (demoted per Phase 1) and
delete the code that produced them. Same "convert process to data" move as Phase 0 — the
subsystem goes, the answers stay.

**Docs:**

- `PROGRESS.md` → `HISTORY.md`, **unedited**. Its "do not re-add" traps and the Stage 1 parser
  census are the most valuable prose in the repo.
- Write a new short spec (~200 lines) describing the six steps as they actually are.
  `original-spec-v2.md` is 72 KB and still specifies zips (there are zero), Demucs, a
  Drive-only Stage 0, and the archive/loser model §14.19 retired.

---

## Order rationale, in one line

Freeze the irreplaceable data → fix the ladder → build the local source → prove equivalence
against the golden output → *then* delete. Deleting first is faster right up until the moment
something is missing and there is no longer any way to tell what.
