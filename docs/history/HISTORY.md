# Karaokemp Library Pipeline — Progress Tracker

> **Frozen at cutover.** This is the working log from the build, kept as it was apart from
> redacting personal data and third-party identifiers. Instructions like "read this first
> every session" describe how it was used then. Commands and files it names may no longer
> exist; see [`simplification-plan.md`](simplification-plan.md) for what was removed.

Cross-session state for implementing [`original-spec-v2.md`](original-spec-v2.md).
Background on the library itself: [`investigation-summary.md`](investigation-summary.md).

**How to use this file:** read it first at the start of every session. Update it at the end of
every session, and whenever a phase gate is passed or a decision is made. Anything that will be
needed after a `/clear` belongs here — not in the transcript.

Last updated: 2026-08-27 — **STAGES 0–6 COMPLETE. THE LIBRARY IS ORGANIZED ON DISK.**
Stage 6 ran 2026-08-27: 28,786 items placed into `active/<shard>/<Artist> - <Title> [id]/`
(50 shards, 51,600 files = 50,762 renames + 838 hardlinks, 401 GB), 246 orphans archived,
staging emptied. `fsck` green after a same-day repair (see the Stage 6 section).
Two exports exist: the DustMic **demo** DB keyed on Drive file ids, and the production
runtime DB keyed on `active/`-relative paths.
Next: drain the 789 open `metadata_match` rows (each one is a directory rename on re-run),
hand the owner the Drive bundle, then cutover.

---

## Execution host (spec §0 — record in `pipeline_runs.notes` of the first run)

| fact | value |
|---|---|
| hostname | `penguin` (ChromeOS Crostini / Debian 12 VM) |
| Python | 3.11.2 |
| free disk on `/` | **597 GB** (602 GB total, 5.2 GB used) |
| Drive access | ChromeOS FUSE mount, 9p transport, `msize=4120` (small — I/O is slow) |
| mount path | `~/karaokemp-song-lib` → `/mnt/chromeos/GoogleDrive/SharedWithMe/<shared-folder>/` |
| mount verified | listing, recursion, and byte-reads all work (2026-07-14) |
| rclone remote | `gdrive:`, `type = drive`, **`scope = drive.readonly`** |
| Drive folder ID | `<folder-id>` — pin with `--drive-root-folder-id` |
| sudo | passwordless |

**The `drive.readonly` scope is a real win:** spec §0's "never write to the shared Drive folder"
is now enforced by the OAuth token itself, not merely by pipeline discipline. A write is
impossible, not just forbidden. Do not re-authorize with a broader scope — nothing in this
pipeline needs one.

**Pin enumeration with `--drive-root-folder-id <folder-id>`** rather than
navigating `--drive-shared-with-me` paths: it targets the karaoke folder exactly and sidesteps
shared-with-me path ambiguity. Verified 2026-07-14 — it resolves to 19 top-level dirs, matching
the FUSE mount's listing exactly (mount showed the same 19 plus one loose `.mp4`), which
independently confirms both views point at the same folder.

Library size from the prior investigation: **53,674 files, ~438.4 GB**.

---

## ⚠ STAGE 0 FINDING THAT RESHAPED STAGE 1/3 — ✅ RESOLVED 2026-07-16, keep for context

**Fixed as specified below.** §6.3 pairs over the full pre-exclusion listing and stores pairs at
content level in `provisional_pairs` (keyed on `gdrive_md5`), so no directory can split one.
Verified on the live DB: **0 stored pairs have a half that is not downloadable**, and 699 stored
content pairs have their two §5.2 survivors sitting in different directories — held together
anyway, exactly as intended. (699 ≠ the 1,120 below because the units differ: 1,120 counts
dir+stem *groups* with one half excluded; 699 counts distinct *content pairs* whose survivors
split. Same phenomenon, different denominator.) The trap is pinned by
`test_pair_survives_the_5_2_survivor_split` — **do not "simplify" §6.3 to pair over survivors.**

Original analysis retained below, because it is the reason the design looks the way it does.

**The §5.2 survivor rule splits 1,120 mp3+cdg pairs across directories.**

Mechanism: "lexicographically smallest `drive_file_id`" is applied to each file independently,
and Drive IDs are effectively random. For a song present in both `Unorginized/` and
`New/SF001…/`, the mp3's smallest ID and the cdg's smallest ID land in *different* directories
roughly half the time. The rule is deterministic and re-run-safe exactly as §5.2 specifies — it
is simply **pair-blind**, and pairs are 87% of this library's files.

Verified, not theorized: 1,120 pairs that are intact on Drive have exactly one half excluded.

**No bytes are lost.** Every distinct md5 keeps exactly one survivor, so both halves survive
*somewhere*. What is destroyed is the directory-based pairing signal.

**Where the damage would land:** §6.3 pairs "by identical basename within the same directory."
Run over post-exclusion survivors, the orphaned mp3 becomes an `audio_only` item and the orphaned
cdg gets excluded as `orphan_cdg` — shredding a working mp3g track into a stray audio file plus a
discarded graphics file, 1,120 times. Measured effect of naive post-exclusion pairing:

| pairing computed over | paired | orphan_mp3 | orphan_cdg |
|---|---|---|---|
| all locations (correct) | 23,151 | 205 | 124 |
| non-excluded only (naive) | 21,855 | **1,180** | **261** |

**Required fix (no change to §5.2's rule):** Stage 1 must discover pairs over the **full
pre-exclusion listing** (all 53,674 rows), exactly as §5.2 already mandates for filename evidence
("parses the filenames and folder paths of ALL locations in a hash group, including excluded
ones"). Express the result at blob level — `media_item_files` already keys on `content_hash`, not
`file_locations` (§3.3), so a pair is a content-level fact and directories are irrelevant to it.
§9.2's Organize then co-locates both halves into `active/<Artist> - <Title> [id]/` at the end.

Do **not** "fix" this by making the dedup pass pair-aware; the blob-level architecture already
handles it, and changing the survivor rule would be a real deviation from §5.2 for no gain.

## Phase status

| phase | spec | status | gate |
|---|---|---|---|
| 0 — prerequisites & toolchain | §0, §13 | **done** (2026-07-14) | host named, toolchain installed, `gdrive:` authorized, §5.1 sample check passed |
| 0 — Stage 0 remote inventory | §5 | **done** — report reviewed; zip cut + single-pass disk decision confirmed by sha-yol (2026-07-15) | inventory report reviewed by sha-yol (§5.3) |
| 1 — filename parsing & pairing | §6 | **DONE** (2026-07-16) — parser gate-passed, 53,674 parses stored, 22,835 content pairs | ✅ §6.2 golden set: 200/200 resolved, no round 2 |
| 2 — download | §7.1 | **done** (2026-07-18) — 51,008/51,008 staged, 0 verify failures | ~~disk/batch plan~~ single-pass fit (43 GiB now free — see disk warning) |
| 3 — hash/probe/fingerprint/cluster/dedup | §7.2–7.4 | **DONE** (2026-07-19) — §7.2 + §7.3 (3,196 clusters) + §7.4 (verdicts on all 28,907 items) | ✅ 1k-sample fingerprint benchmark PASSED (§7.3) |
| 4 — full-decode verification | §8 | **DONE** (2026-07-20) — 24,569 decoded, stable in one round, 19 unplayable | ✅ 100-sample decode benchmark (caught deviation #15) |
| 5 — enrichment | §9.1, §9.1.1, §9.1.2 | **DONE** (2026-08-02) — AcoustID (24,208 checked), qualified MB text search, free-text MB (§9.1.1), catalogue pass (§9.1.2: Wikidata / MB artist index / iTunes / Deezer), LLM filename parsing + MBID re-runs. **song_mbid: 17,072 / 21,576 songs (79.1%)** — the residual gap is Hebrew and is structural (see `mb-hebrew-artists-not-recordings`). | review-queue budget ≤1000/kind (§11, raised 500→700→1000) |
| 6 — organize | §9.2 | **DONE** (2026-08-27) — 28,786 items placed, 246 orphans archived, staging empty, `fsck` green | ✅ reviewed dry run (2026-08-27); per-copy dirs + artist-initial shards confirmed by sha-yol |
| review drain | §10 | **IN PROGRESS** — 1,158 resolved / 1,158 open (metadata_match 601, dedup_verdict 233, pair_mismatch 221, quality_flag 103) | only `metadata_match` gates Organize; the other three change ranks/pairings, not paths |

---

## Code layout

```
karaokemp/config.py   paths, Drive pinning, thresholds, tool_versions()
karaokemp/db.py       connect/init/backup, pipeline_run() context manager
karaokemp/stage0.py   §5.1 enumerate, §5.2 exact-dup, §5.3 report
karaokemp/stage1.py   §6.1 parser (pure fns) + mass insert, §6.2 goldset/ingest, §6.3 pairing
                      + resolve_pair_mismatch / verdict re-application (see below)
karaokemp/stage2.py   §7.1 by-ID download: verify-then-commit, backoff, resume
karaokemp/cli.py      init | enumerate | dedup | report | parse-stats | parse | pair
                      | goldset | ingest-goldset | resolve-pair | download
schema.sql            §3 schema, STRICT/WAL/FK
tests/test_stage0.py          invariant tests (12/12 passing)
tests/test_stage1.py          parser invariants (58/58 passing)
tests/test_stage1_pairing.py  §6.1 mass insert + §6.3 pairing + verdicts (22/22 passing)
tests/test_stage2.py          download verify/resume/defer invariants (11/11 passing)
```

Run with `PYTHONPATH=. python3 -m karaokemp.cli <cmd>`; tests via `PYTHONPATH=. python3
tests/test_stage0.py`. **Library lives at `~/karaokemp-library`** (override: `KARAOKEMP_LIBRARY`),
deliberately outside the code repo — it will hold hundreds of GB.

Raw `lsjson` dumps are written to `library/enumerations/` *before* parsing, so a parse bug never
costs a re-fetch of 53k files. `cli enumerate --from-dump <path>` re-parses without touching Drive.

## Spec deviations (deliberate, documented)

1. **`file_locations.size_bytes` added** (not in §3.2). §5.2 requires the Stage 0 exact-dup pass
   to group by `(gdrive_md5, size_bytes)`, but §3.2 gives locations no size and §3.1 puts
   `size_bytes` on `blobs` — which cannot exist until Stage 3 hashes bytes. As written, §5.2 asks
   Stage 0 to group on a field that appears three stages later. The new column is Drive's
   *reported* size (remote claim); `blobs.size_bytes` remains the *verified* local count. Keeping
   both is what lets Stage 3 catch a claim/reality mismatch.
2. **Removals are reported, not auto-mutated.** §5.1 says re-enumeration "detects removals" but
   §3.2's status enum has no `removed` value. Inventing one to silently retire rows would be a
   destructive guess (§1.2). `detect_removals()` surfaces them in the run report for a human.
3. **Disc field order is keyed on `(top-folder, series)`, not series** — and only where verified.
   §6.1 implies the series determines layout; this library disproves it (see the census above).
4. **Weak decorations are stripped only inside brackets.** §6.1 says move decorations to flags
   without qualification; done literally, that corrupts hundreds of real artist and title names.
5. **3+-space runs are treated as a separator before whitespace collapse.** §6.1 says "collapse
   whitespace"; done first, it destroys the only separator 48 rows have.
6. **§6.2's "zip members" stratum replaced with `low_confidence`.** There are no zips (§5.3), and
   the rows the parser handles worst are where review effort actually pays.
7. **Artist promoted from verified artist-folder roots**, against §6.1's "never auto-promoted".
   Scoped to `ARTIST_FOLDER_ROOTS` and to rows whose filename yielded no artist, so it can only
   add information — never override the name. Driven by sha-yol's §6.2 review (see that section).
8. **Non-media files and opaque names get no parse at all** (`non_media` / `opaque` layouts).
   §6.1 assumes every location is a song; 763 here are not, and parsing them fabricated artists.
9. **`location_parses` table added** (not in §3). §6.1 says Stage 1 produces "parse payloads per
   location", but §3 has nowhere to put them: `song_metadata` keys on `media_item_id`, and media
   items do not exist until Stage 3. Same class of gap as deviation #1, resolved the same way.
   Stage 3 promotes these into `song_metadata` (source `filename`) once items exist.
10. **`provisional_pairs` keyed on `gdrive_md5`, not `content_hash`.** §6.3's pair must survive to
   Stage 3, but `blobs` do not exist until Stage 3 hashes bytes. md5 is the content key that
   exists now, is 100% populated (§5.3), and survives §5.2's exclusion. See the ⚠ section.
11. **An orphan CDG with a same-folder candidate goes to review, not straight to `excluded`.**
   §6.3 says orphan CDG → excluded, and for a cdg with no mp3 anywhere that is right. But its
   pairing rule is exact-basename, and 16 cdgs here have their mp3 in the same folder under a
   differently-typed name. Auto-excluding those loses a working track to a typo (§1.2). They go
   to `review_queue(pair_mismatch)` — §6.3's own escape hatch for the ambiguous case — and stay
   downloadable pending a verdict. The pairing rule itself is **not** widened; nothing is guessed.
12. **CDG check tolerance is asymmetric `[−30s, +3s]`, not the spec's `±3s`** — a §11 retune
   from the measured delta distribution of all 22,854 pairs; see the Stage 3 retune section.
   Both bounds are config values (`CDG_TOLERANCE_GRAPHICS_*`).

## Bug caught by tests before it ever ran (2026-07-14)

The first exact-dup implementation filtered groups with `HAVING COUNT(*) > 1`. If a survivor is
later deleted from Drive, its group collapses to a single member — which then vanishes from the
query, leaving the **last remaining copy stranded as `excluded` forever**, never downloaded, song
silently gone. A §1.2 violation. Fixed by ranking with a window function over *all* groups
including singletons, so a lone member is rank 1 and gets restored to `remote_only`.
Regression test: `test_survivor_restored_when_previous_survivor_disappears`.

## Phase 0 findings (2026-07-14)

### Toolchain — nothing was installed on this host; now resolved

Every external dependency in spec §13 was missing at session start. Installed via apt (177
packages, mostly the ffmpeg dependency tree). All now resolve:

| tool | version |
|---|---|
| rclone | 1.60.1 (Debian 12 build, dated late 2022) |
| ffmpeg / ffprobe | 5.1.9 |
| fpcalc (chromaprint) | 1.5.1 |
| sqlite3 | 3.40.1 |
| numpy | 1.24.2 |
| mutagen | 1.46.0 |

Python 3.11.2 satisfies §13 and ships the `sqlite3` module, so the DB layer needs no CLI.

**rclone version caveat:** 1.60.1 predates current upstream by years. Drive support was mature
well before it, so it should be fine — but if OAuth or `--drive-shared-with-me` misbehaves, try a
current upstream rclone before assuming a deeper cause.

### Stage 0 enumeration command: `lsjson`, not `lsf` — §5.1 gate PASSED (2026-07-14)

`rclone lsjson` gives structured output rather than a format string to parse, and supports
`--hash`, `-R`, `--files-only`.

The §5.1 requirement is to confirm the ID field is actually **populated** on a real sample, not
merely that it exists. Verified against `Unorginized/`:

```
{"Path":"BS5417 - 10 Urge Overkill  - Girl You'll Be A Woman Soon.cdg","Size":1391136,
 "Hashes":{"md5":"98793923c5019ccddc4afb9d4982ede7"},
 "ID":"<file-id>"}
```

Both load-bearing fields are real: `ID` (→ `drive_file_id`, remote identity key) and
`Hashes.md5` (→ `gdrive_md5`, the §5.2 exact-dup grouping key). `Size` is populated too, so the
§5.3 inventory report gets sizes from the same call at no extra cost.

Working command shape (note: the path goes *inside* the remote arg, not as a second positional):

```
rclone lsjson "gdrive:<subpath>" --drive-root-folder-id <folder-id> \
  --files-only --hash -R
```

Also observed in the sample: CDG/MP3 pairs differ only by extension on a shared basename, so the
§6.3 provisional pairing signal is visible in the remote listing with no downloads — as the spec
anticipated.

`lsjson --original` shows the underlying object's ID rather than the shortcut's — that's the
mechanism if we choose resolve-and-record for the §5.1 shortcut question (still open).

### BLOCKER 1 — disk is 597 GB, spec §0 requires ≥2 TB

The 2 TB figure in the spec was an estimate made before the library was measured. The real number
is ~438 GB, which does fit in 597 GB — but with only ~159 GB (26%) of headroom, and that assumes
`staging/` → `active/`+`archive/` are *moves within one filesystem* (renames, not copies), which
they are under the layout in §2. So a single-pass run is arguably feasible.

Not proceeding on that reasoning alone, because it's tight and §0 makes this an explicit gate.
Needs a decision from sha-yol — see "Decisions needed".

### ~~BLOCKER 2~~ RESOLVED 2026-07-14 — the FUSE mount cannot satisfy Stage 0; rclone + OAuth was required

**Resolved:** sha-yol authorized the `gdrive:` remote in a real terminal (the in-session `! rclone
config` attempt died at EOF — the interactive TUI needs a TTY the harness doesn't provide; run it
outside Claude Code). The §5.1 sample check then passed. Original analysis retained below.

This is the important one. The prior investigation used the FUSE mount and found it far faster
than the Drive API. But Stage 0 (§5.1) is specified around two fields the mount does not expose:

- **`drive_file_id`** — the remote identity key. Everything in `file_locations` is keyed on it,
  and §5.1 demotes paths to informational precisely because Drive allows duplicate names in one
  folder. The mount surfaces only paths.
- **`gdrive_md5`** — the grouping key for the §5.2 logical exact-dup pass, which is what lets us
  skip downloading duplicates. Without it, exact-dup detection can't happen until after Stage 2
  has already downloaded every copy — which inverts the pipeline's whole point and makes the disk
  situation materially worse.

Checked for the IDs on the mount: no xattrs (`getfattr` absent, 9p exposes none), `stat` shows
plain POSIX metadata only. They are not there to be had.

So Stage 0 needs `rclone` configured with a Google Drive remote, which requires an **interactive
OAuth flow that only sha-yol can complete**. The mount stays useful as a fast local read path for
Stage 2 (and as a cross-check on rclone's enumeration), but it cannot be the enumeration source.

Note also: `--drive-shared-with-me` will be needed, since this folder is shared *to* the account
rather than owned by it (owned by a third party).

---

## STAGE 1 PARSER CENSUS (2026-07-14) — §6.1's specific patterns are half-wrong for this library

The §6.2 gate exists because "the parser is the highest-variance component". It earned its keep
immediately. Every item below was measured against the real 53,674-row listing, **not** inferred
from the spec's illustrative examples. Regression tests pin each one (`tests/test_stage1.py`).

### Spec patterns that do not exist here (do not build; do not re-add)

- **Video-ID suffixes are never bracketed.** §6.1's `[XXXXXXXXXXX]` form: **0 rows.** The real
  form is a bare trailing `-VZwiiKF3F7Y` (52 rows). Length alone cannot detect it — `tobewithyou`
  and `wet_wet_wet` are real title words of exactly 11 legal chars. Requiring a digit **and** an
  uppercase letter separates a random ID from lowercase prose.
- **Hebrew diacritics: 0 rows.** Not one file carries a mark in U+0591–U+05C7, and maqaf U+05BE
  appears 0 times. §6.1's most emphatic, most precisely-worded paragraph is a **no-op on this
  library.** Implemented anyway (it is ~10 lines, spec-mandated, and correct) and tested — but do
  not spend further effort here. Same category of finding as the zip subsystem, smaller blast radius.

### Spec patterns that are wrong here (fixed, with tests)

| §6.1 says | reality | fix |
|---|---|---|
| disc ID is `^[A-Z]{2,4}…` | `sf012-08…`, `dk089-01…` are real rows | case-insensitive, **but only for known series** — "Slow 12-8 Blues…" parsed as series SLOW/disc 12/track 8 |
| "collapse whitespace" | a run of **3+** spaces is the separator in 48 rows (`AMS1060 08   Pras & Mya   Ghetto Superstar`) | canonicalise 3+ runs to ` - ` first, then collapse. A **2**-space run is NOT a separator (296 rows; mostly typos or a Hebrew title beside its transliteration) |
| decorations → flags | `clean`/`live`/`cc`/`christmas`/`video` are also real names | two strengths (below) |
| `DiscID - Artist - Title` | order is a property of the **source batch**, not the series | `DISC_BATCH_ORDER` + per-row comma rule (below) |

### ⚠ THE BIG ONE — artist/title order is a property of the BATCH, not the series

A series-keyed lookup table is **wrong in principle** and this library disproves it directly:

- `New/SF…` → **Artist - Title** (`SF222-06 - Duran Duran - Sunrise`) — 10,715 rows
- `Unorginized/SF…` → **Title - Artist** (`SF314-09 - R U Mine - Arctic Monkeys`) — 300 rows
- `MRH` flips the same way between `New/` and `English karaoke/`.
- Worse: **`Unorginized/SF` is internally mixed** — `sf019-03 - crow, sheryl - all i wanna do`
  (Artist-Title) sits beside `SF314-09 - R U Mine - Arctic Monkeys` (Title-Artist). **No
  folder-level rule can resolve that batch.**

Caught by self-reviewing the goldset: `Unorginized/SF014-02 - Crazy - Patsy Cline` was emitting
`artist='Crazy', title='Patsy Cline'` **at 0.95 confidence**. High confidence + wrong is the worst
failure mode available — it tells Stage 5 not to bother checking.

**Resolution — three tiers, strongest first** (`_resolve_order`):
1. `comma` (0.95) — exactly one field is an inverted name (`Murs, Olly`). **Per-row evidence, so
   it outranks any batch default** — and it is the only thing that correctly splits the mixed
   sub-batches inside `Unorginized/SF`. Fires on 477 rows.
2. `batch` (0.85) — `(top-folder, series)` has a verified house style. 12,441 rows.
3. `unresolved` (0.50) — records the commoner Artist-Title reading **and says so**. 187 rows.
   §6.1 already hands this case to MB scoring at Stage 5; the parser's job is to flag it, not win it.

`DISC_BATCH_ORDER` contains **only** combinations verified against real rows. Mixed batches are
deliberately absent, with a comment saying so. Mean confidence fell 0.665 → 0.641 — that drop **is
the fix**, not a regression.

### Decorations come in two strengths (measured; each was live corruption)

- **STRONG** (removed anywhere): karaoke-domain terms that never occur in real names — `karaoke`,
  `instrumental`, `playback`, `karafun`, `multiplex`, `minus one`, `no lead vocal`, `karaoke video`,
  `hd`, plus `קריוקי` / `פלייבק` / `караоке`.
- **WEAK** (removed **only inside brackets/parens**): ordinary words that are also real names —
  `clean` → **Clean Bandit** (a band, 24 bare rows) · `cc` → **10cc** (29) · `christmas` → *Do They
  Know It's Christmas* (234) · `live` → *How Do I Live* (172) · `video` → *Video Killed The Radio
  Star* (4).

The trade is **deliberately asymmetric**: a decoration left in a title is visible and fixable
downstream; a word eaten out of an artist name is gone silently. `hd` is STRONG because it is a
decoration in all 23 bare rows and is never a title word; `video` is WEAK but `karaoke video` is a
STRONG compound (ordered before bare `karaoke`, or `karaoke` matches first and strands `video`).

Also fixed: stripping `()` from name ends was **truncating 1,093 titles** mid-parenthesis
(`Guerrilla Radio (Pixel`, `צבעים (גרסת בנות`). Bracket handling is now balance-preserving →
**1,093 → 22**, and those 22 are genuinely malformed sources (paren opened in the artist, closed in
the title). `Christmas - Christmas [Karaoke]` used to parse to **nothing at all**.

### ⚠ STRUCTURAL FINDING — this is two disjoint libraries, and it reshapes the §7.4.1 decision

|  | Latin-named | Hebrew-named | Hebrew folder only |
|---|---|---|---|
| cdg | 23,275 | 0 | 0 |
| mp3 | 23,305 | **5** | 46 |
| video | 524 | **4,542** | 1,858 |

**The mp3+cdg half is ~100% Latin-named disc batches; the Hebrew half is ~100% video.** These
barely overlap. Consequences:

1. `primary_format_preference` (§7.4.1) **is not a global toggle.** Preferring `mp3g` is in effect
   preferring the English mp3g library — **the Hebrew material has no mp3g form to prefer.**
   It is video or it is nothing. Whatever is chosen, the Hebrew content's fate is unchanged, so the
   knob is narrower than §7.4.1 implies. This should be on the table when that decision is made.
2. §6.2's "Hebrew mp3s" stratum has a **population of 5** — not under-sampled, near-empty. The
   goldset sampler redistributes shortfall across strata (else it silently returns 164, not 200).
3. Hebrew order is genuinely mixed (`Title - Artist` dominates but both occur), so Hebrew rows are
   capped at 0.45 with an `hebrew_order_ambiguous` note — 3,557 rows for MB to settle.

### Parser output at 53,674 rows (`cli parse-stats`, writes nothing)

mean confidence **0.641** · **2** unparsed · missing artist **3.5%** · missing title **0.0%**

| layout | rows |  | notes | rows |
|---|---|---|---|---|
| artist_title | 38,663 | | order_from_batch | 12,441 |
| disc_artist_title | 12,047 | | tight_dash_split | 4,157 |
| title_only | 1,802 | | hebrew_order_ambiguous | 3,557 |
| disc_title_artist | 1,058 | | lastname_first_unswapped | 761 |
| disc_title_only | 61 | | order_from_comma | 477 |
| style_of | 41 | | order_unresolved | 187 |

`tight_dash_split` (4,157) is the one to watch: splitting `lauren hill-nothing even matters` on a
tight dash is right far more often than wrong, but it is a weaker signal than a spaced dash and
only fires as a fallback. Sampling showed it mostly correct; `Chapter 08 / 118` is the failure mode.

---

## §6.2 GOLDEN-SET REVIEW — ROUND 1 (sha-yol, 2026-07-14/15) — the Stage 1 gate

## ✅ GATE PASSED (sha-yol, 2026-07-16) — "No round 2." Stage 1 may proceed to §6.3 + mass insert.

sha-yol reviewed all 200 rows: **57 bad, 27 explicit ok, 116 unmarked**. They confirmed on 2026-07-16
that **unmarked means ok** — they worked by marking only defects. All 200 are now resolved in
`review_queue.resolution`; **0 open**.

The distinction is preserved in the data rather than flattened: `resolution.verdict_source` is
`marked` (84 rows) or `unmarked_means_ok` (116), so a later reader can tell an explicit verdict
from an inferred one. `ingest-goldset` still leaves blanks OPEN by default and only infers with
`--blank-means-ok` — a reviewer's convention must be *stated*, never assumed by the code (§1.2).

**Effective accuracy after round 1 fixes: 187/200 (93.5%) acceptable; 13 (6.5%) need MB by design.**

**Result: 44 of 57 defects fixed. The remaining 13 need MusicBrainz — as sha-yol predicted.**

Their framing was exactly right and shaped the work: *"Some can be inferred from folder hints, for
specific folders. We can fine tune the parser to catch them. But some will require searching
against MB, I don't see how a consistent parser could catch everything."*

### Fixed — structural causes (~2,800 rows library-wide, all with regression tests)

| defect | cause | rows |
|---|---|---|
| `Thumbs.db` / `desktop.ini` parsed as songs | no non-media filter | 137 |
| `._Green Day - …` → artist `._Green Day` | macOS AppleDouble sidecars (all exactly 4,096 B — resource forks, not media) | 29 |
| `SONG-<uuid>` → artist `SONG`; `Chapter_03-113` → artist `Chapter 03` | opaque names split on a dash, manufacturing an artist from nothing | 626 |
| `רפאל מירילה 6904` | trailing catalogue number glued to the artist | 1,057 |
| Hebrew batch order backwards | `קריוקי בעברית 1` is `Title - Artist - NNNN`, artist **last** (2- and 3-part) | 1,959 |
| `יזהר אשדות -יש לך אותי` unsplit | a dash is a separator with space on **either** side, not only both | 302 |
| `[ Lyrics ]` left in title | bare `lyrics` can be a title word; bracketed cannot | — |
| artist-less Hebrew rows | artist promoted from **verified** artist-folder roots | 429 |

### The folder-hint deviation (§6.1) — deliberate, scoped, and sha-yol-driven

§6.1: *"Parent-folder names recorded as low-confidence hints… never auto-promoted."* That rule is
right for a folder named `M` or `English karaoke`. It is **wrong** for `קריוקי בעברית 1/karaoke/<ARTIST>/`,
where the folder is the only artist signal and sha-yol marked every such row bad.

Verified before encoding: **31 artist folders / 560 files** (אייל גולן 133, שלמה ארצי 93, שלומי שבת 54,
משינה 39 …) and **8 folders / 75 files** under `…/ים תיכוני/`. Promotion is scoped two ways:
1. only under `ARTIST_FOLDER_ROOTS` (paths actually checked), and
2. only when the **filename yielded no artist** — it can add information, never override the name.

Everywhere else §6.1's rule stands: the folder stays a hint. Tests pin both halves.

### ⚠ A fix that the census KILLED — do not re-add

Stripping a leading track number (`14 להשתטות` → `להשתטות`) looks obviously right and matches 217
rows. **Do not do it.** Those 217 include **`4 Non Blondes`** (a band) and **`7 Nation Army`** (a
title) — stripping would silently corrupt both. Same asymmetry as the weak decorations: a stray
`14` on a title is visible and fixable; an eaten band name is gone. `test_leading_number_is_never_stripped`
pins this so a future session cannot "improve" it back.

### Not fixed — genuine MusicBrainz territory (13 rows), deliberately NOT chased

Nine have **no separator at all**: `Disturbed The Light`, `Celine Dion My Heart Will Go On`,
`nickelback how you remind me`, `Elvis Presley Cant Help Falling In Love`, `אריק סיני העיירה שלי`.
Splitting these requires already knowing "Disturbed" is a band — that is a **lookup, not a parse**.
§6.1 already routes this to MB scoring at Stage 5, and §9.1 is where it belongs. The rest are a
remix/DJ chain (`נסיכה - רמיקס - Dj Yaniv O - עומר אדם`), a `=` separator (1 row), `FROZEN` (a film,
not an artist), and one casualty of the deliberate 2-space rule (`Roxette  It Must Have Been Love`).

**Do not try to fix these in the parser.** Chasing them means hardcoding artist names, which is
what MB is for and what §6.1 explicitly defers.

### Parser output after round 1 (`cli parse-stats`, writes nothing)

mean confidence **0.651** · **1** unparsed · missing artist **3.4%** · missing title **1.4%**

| layout | rows |  | notes | rows |
|---|---|---|---|---|
| artist_title | 36,307 | | order_from_batch | 12,437 |
| disc_artist_title | 12,041 | | tight_dash_split | 3,558 |
| title_artist | 1,959 | | order_from_folder_batch | 1,959 |
| disc_title_artist | 1,060 | | hebrew_order_ambiguous | 1,897 |
| title_only | 1,011 | | catalogue_code_stripped | 1,057 |
| opaque | 626 | | lastname_first_unswapped | 761 |
| title_artist_from_folder | 429 | | opaque_name | 626 |
| non_media | 137 | | order_from_comma | 477 |
| disc_title_only / style_of / unparsed | 65 / 38 / 1 | | artist_from_folder | 429 |
| | | | order_unresolved | 187 |

---

## STAGE 1 COMPLETE (2026-07-16) — §6.1 mass insert + §6.3 pairing

Both ran at full scale against the live DB. Idempotence verified at full scale: a second `parse`
reports **0 changes** over all 53,674 rows; a second `pair` reports 0 exclusions, 0 restorations,
and adds 0 review rows. Tests: **89 passing** (12 Stage 0 + 58 parser + 19 pairing).

### §6.1 mass insert — `location_parses`, 53,674 rows

Parses **all** locations including the 2,562 `excluded` ones: §5.2 discarded bytes, never
evidence, and the duplicate is often the better-labelled copy (`SF018-02 - The Drifters - Under
The Boardwalk` vs `drifters-under the boardwalk`). Stage 3 merges a hash group's hints onto the
survivor's item, so dropping those rows here would lose the good label permanently.

Rows carry `parser_version` — a hash of `stage1.py`'s source, not a hand-bumped constant, because
the failure mode of forgetting to bump is silent (stale verdicts served by a parser that no longer
exists). Re-parsing 53k names is seconds, so staleness is cheap to fix once detectable.

### §6.3 pairing — 22,835 content pairs

| | count |
|---|---|
| content pairs (`audio_md5`, `graphics_md5`) | **22,835** |
| paired mp3 blobs / cdg blobs | 22,834 / 21,996 |
| orphan mp3 blobs (kept — `audio_only`, Demucs input) | 201 |
| orphan cdg → `excluded`/`orphan_cdg` | 104 locations |
| orphan cdg → `review_queue(pair_mismatch)` | 16 |
| basename collisions | **0** |

**Pairs are NOT one-to-one, and that is correct.** 759 cdg blobs pair with more than one mp3 blob:
the graphics track was reused byte-identically across several re-encodes of the same audio
(one cdg for `Under The Boardwalk` serves 4 distinct mp3 files). Stage 3's clustering collapses the
audio variants. Do not "fix" this into a 1:1 mapping — `test_one_cdg_may_pair_with_several_mp3s`.

**Basename collisions: 0.** §5.3's "770 colliding names / 1,828 files" is real but is *not*
mp3/cdg — it is `thumbs.db`, `desktop.ini`, and `chapter_NN-NNN.avi`. The §11 budget worry in the
old handoff was unfounded; `pair_mismatch` sits at 16 of 500.

### The 16 queued orphan CDGs — two matchers, both scoped to the cdg's own folder

§6.3's exact-basename rule under-pairs. Recovery uses two keys, strongest first, and **queues for
review rather than auto-pairing** (auto-pairing would be the guess §6.3 refuses):

- **`disc` (2 rows)** — catalogue identity `(series, disc, track)`. Strongest available:
  it survives damage that destroys the name outright. `SF275-16 - Pink - Sober.cdg` pairs with
  `SF275-16 - Sober - Pink.MP3` — artist/title typed in *opposite orders on the two halves*, so no
  name rule can ever match them. SF217-05's two halves have had their middles mangled by a
  find-and-replace and still agree on the catalogue number.
- **`name` (14 rows)** — parsed `(artist, title)` via the §6.2-gated parser. Catches punctuation
  drift: `alesha -lipstick.cdg` / `alesha-lipstick.mp3` — one space apart.

A fuzzy (difflib) matcher was tried first and **rejected**: at cutoff 0.75 it claimed 76 of 120
orphans, but most were same-artist-different-song (`Bridge Over Troubled Water` matched
`Bright Eyes`). The parser is the better matcher precisely because it already knows the difference
between an artist field and a title field.

### Traps pinned by tests (do not "simplify" these away)

- **Never pair over post-exclusion rows.** `test_pair_survives_the_5_2_survivor_split` builds a
  fixture that is *deliberately split*, and `test_naive_post_exclusion_pairing_would_have_failed`
  guards the guard — it asserts the negative, so the fixture cannot rot into a tautology.
- **Orphan-cdg exclusion must stay reversible.** If the mp3 later appears on Drive the cdg is
  restored to `remote_only`. A one-way write strands it as `excluded` forever → never downloaded →
  song silently gone. Same shape as the §5.2 survivor bug Stage 0 already hit.
- **Never clobber `exact_dup`.** That reason is Stage 0's bookkeeping; Stage 1 only touches the
  survivor. An already-excluded duplicate is already never downloaded, so overwriting gains nothing.
- **A cdg under collision review is not also queued as an orphan**, nor excluded — that would ask
  the same question twice and pre-empt the verdict being asked for.

## Decisions needed from sha-yol

_(none open — see `primary_format_preference` below, resolved 2026-07-20)_

## Decisions deferred (deliberately — do not resolve early)

_(none open)_

## §5.3 INVENTORY REPORT — 2026-07-14 (the §0 phase gate)

Full JSON incl. the 200-filename sample: `~/karaokemp-library/logs/inventory-report-*.json`

**Cross-check passed.** rclone enumerated 53,674 files / 470,715,610,427 bytes — matching
`investigation-summary.md`'s independent FUSE-mount measurement *to the byte*. Two unrelated
methods agreeing exactly means both views are complete and correct.

| | files | GiB |
|---|---|---|
| audio (mp3) | 23,356 | 89.4 |
| graphics (cdg) | 23,275 | 36.3 |
| video (mp4/avi/mpg/vob/dat/mpeg/wmv/mkv) | 6,924 | **312.6** |
| other | 119 | 0.1 |
| **total** | **53,674** | **438.4** |

- **Exact-dup rate: 4.77%** — 2,562 locations excluded, 35.3 GiB saved. Lower than hoped; this
  library is not mostly redundant.
- **md5 coverage: 100%.** No blind spots in the dedup pass.
- **Shortcuts: 0** — by three independent signals (no shortcut mimetype, no `OrigID`, no
  duplicate file IDs). The §5.1 shortcut decision is moot.
- **Zips: 0.** See scope note below.
- **Video is 71% of bytes but 13% of files.** AVI alone is 129.6 GiB. Any download-time or
  disk-pressure decision is really a decision about video.
- **Duplicate basenames: 770 names / 1,828 files.** Mostly `thumbs.db` (43), `desktop.ini` (18),
  and generic VCD-style files `chapter_NN-NNN.avi` (13–15 copies each, *different* bytes — dedup already
  removed the identical ones). Those chapter files carry near-zero filename metadata; §6.1's
  parent-folder hints will be the only signal for them.
- **Non-media detritus: 115 files** (thumbs.db, desktop.ini, .bat, .jar, .sfk peak file, .part,
  images, a few docs) — trivial in size, but Stage 1 should not try to parse them as songs.
- **Size distribution:** p50 3.0 MB, p90 12.8 MB, p99 90.4 MB, max 479 MB. Two 0-byte files
  (1 jpg, 1 mpg — the mpg is real breakage), 22 files under 1 KB.

### DISK DECISION — RESOLVED: single-pass fits, no batching needed

**403.1 GiB to download after dedup, vs 597 GiB free on `penguin` → ~194 GiB (32%) headroom.**
`staging/` → `active/`+`archive/` are moves within one filesystem (renames, not copies), so peak
usage is not doubled. The spec's ≥2 TB figure in §0 was written before the library was measured;
the real number does not require it. **No §0 batch plan needs writing.** Revisit only if
artifacts (§12, Demucs stems) are generated on this host — those are large and would change this.

### SCOPE CUT — the zip subsystem is dead code; do not build it

**There are zero zips (and zero rar/7z) in the library.** The spec invests heavily in them:
§7.1 zip download/member-indexing/extraction, §6.3 pairing "within the same directory *or zip*",
§9.2 "zip-member winners are extracted files by this point — nothing playable may remain inside a
zip in `active/`", plus `file_locations.parent_location_id`/`member_path` and the `container`
role. **None of it is needed.** Leave the schema columns (harmless, and a future drop of zipped
MP3+G would use them), but write no zip-handling code. This is a meaningful reduction in Stage 2
scope and exactly what the §0 phase gate is for.

### Stage 1 parser notes (from the report's evidence)

- `.mp333` — a real 3.8 MB mp3 with a typo'd extension. Extension-based classification alone puts
  it in "other" and Stage 1 would skip a genuine song. Content-sniff or normalize known typos.
- `ברנרות 2018/.mp4` — a file literally named `.mp4`: Python reads that as a *stem* with no
  suffix, so it classified as `none` despite being a 180 KB video. Edge case for `filetype_of()`.
- `מידברנרות 2016/…/watch[1]` — browser-saved file, no extension, 168 KB.
- `.part` × 2 — incomplete downloads, will fail decode at Stage 3/4. Expect `broken`.
- Hebrew filename with dots parsed an extension of `הב - סאבלימינל ולירן אביב`. Harmless here
  (it is in the "other" bucket) but confirms extension-splitting is not a safe classifier.

## Decisions made

- **2026-07-20 — `primary_format_preference` set to `video`** (was the `mp3g` default;
  §7.4.1, config.py). sha-yol's reasoning, stated as weak/speculative: video files are likely
  later additions to the library meant to cover gaps or replace bad copies, so they were more
  likely to have actually been played and vetted — but they flagged this as a guess, not
  evidence. Chosen deliberately non-destructively: **checked that nothing in Stages 3–5
  consumes this constant yet** (verdicts.py keeps a winner in EACH format per mixed-format
  cluster and archives neither automatically — "the operator's call via review, never
  automatic"), so setting it required no re-run of verdicts/decode/enrichment and touched no
  already-computed state. It only takes effect once Stage 6 (or the review tooling) is built
  to actually read it. **Fully reversible**: flip the config value back; nothing has been
  archived or deleted under the old or new value yet.

- **2026-07-19 — SPEC CORRECTION from sha-yol (§7.4.5): broken sole copies ARE archived.**
  The spec said "bad sole copies get `quality_flag` review, never archived (a glitchy copy
  beats no copy at the event)". sha-yol overrides: *"if there's one broken version of a song,
  it's better to archive it than to play it. broken or bad is unplayable, and replacing it
  is better than trying to play a broken file."* Consequences for the §7.4 dedup pass
  (not yet built — build it this way):
  1. A `broken` blob's item is never crowned `sole_copy`-and-kept; it goes `loser`/archived
     (`archive/broken/`) even when it is the only copy of the song.
  2. Still queue/report it (`quality_flag`) — not to decide keeping, but so the archived songs
     form a **replacement list** (to be replaced) instead of vanishing silently.
  3. `suspect` is not auto-archived: it goes through Stage 4 full decode first; decode-ok
     clears it, decode-fail makes it `broken` → archived per (1).
  4. Items are still CREATED for broken blobs (unchanged) — an archived song needs an item row
     to be tracked, clustered, and listed for replacement.

- **2026-07-14 — Stage 0 enumerates via rclone, not the FUSE mount.** The mount exposes neither
  `drive_file_id` nor `gdrive_md5` (see BLOCKER 2). The mount is kept as a fast local read path
  for Stage 2 and as a cross-check on rclone's enumeration.
- **2026-07-14 — `lsjson` over `lsf`** for §5.1 enumeration; structured output, no format-string
  parsing. Gate passed on a real sample.
- **2026-07-14 — pin to the folder ID** via `--drive-root-folder-id`, not `--drive-shared-with-me`
  path navigation.
- **2026-07-14 — keep the `drive.readonly` scope.** It makes §0's read-only rule
  token-enforced rather than discipline-enforced.

---

## Standing constraints (from the spec — do not violate)

- **The shared Drive folder is read-only.** No moves, renames, deletes, or writes to it, ever
  (§0, §13). All archiving is local and/or index-only.
- The SQLite index is the source of truth; `fsck` must always be able to reconcile it against the
  filesystem (§1.1).
- Never destroy, always archive (§1.2). Deletion is manual and after the fact.
- Every mutating stage needs `--dry-run`; every stage must be idempotent, resumable, deterministic.
- DB backup via `.backup` / `VACUUM INTO` before every stage run — never a raw copy of a live WAL
  database. Keep 20 rotating (§13).
- Don't build the pipeline blind and run it end-to-end — the phase gates above exist because
  Stage 0's report is expected to reshape Stages 1–3 (§0).

---

## Open threads / next session starts here

- [x] Confirm the apt toolchain install finished and every §13 binary resolves. — done 2026-07-14
- [x] sha-yol authorized the `gdrive:` remote (must be run in a real terminal, not via harness `!`).
- [x] §5.1 sample check: `ID` and `md5` confirmed populated on real data. — done 2026-07-14
- [x] Build the storage layout (§2) + DB schema (§3). — 12 STRICT tables + `v_metadata`.
- [x] Build + run Stage 0 enumeration. 53,674 rows, 0 missing IDs, 0 removals, 100% md5.
- [x] Shortcuts counted: **zero**. §5.1 shortcut decision moot.
- [x] Run the §5.2 exact-dup pass. 2,222 groups, 2,562 excluded. Idempotence verified at full
      scale (second run: 0 changes planned).
- [x] Produce the §5.3 inventory report. — see above.
- [x] Disk decision resolved from evidence: single-pass fits (403.1 GiB vs 597 GiB free).
- [x] Stage 1 §6.1 parser built (`karaokemp/stage1.py`), 44 invariant tests, all patterns derived
      from a census of real rows. Dry parse over all 53,674: 2 unparsed, mean confidence 0.641.
- [x] Stage 1 §6.2 golden set generated: 200 stratified rows → `review_queue` + a TSV review sheet.
      Idempotent (re-run inserts 0; never overwrites a verdict already given).
- [x] **§0 gate CLOSED** (2026-07-16): sha-yol confirmed the §5.3 report, the **zip scope cut**, and
      the **single-pass disk decision**.
- [x] **§6.2 gate CLOSED — PASSED** (2026-07-16): 200/200 resolved, 0 open, **no round 2**.
      44/57 defects fixed; 13 are MB territory by design.
- [x] **§6.1 mass insert done** (2026-07-16): `location_parses`, 53,674 rows, incl. excluded ones.
      Re-run = 0 changes.
- [x] **§6.3 pairing done** (2026-07-16): 22,835 content pairs in `provisional_pairs`, keyed on
      `gdrive_md5`, computed over the full pre-exclusion listing. 0 pairs have a non-downloadable
      half. 104 orphan cdgs excluded, 16 queued for review, 201 orphan mp3 blobs kept.
- [x] Pair-splitting trap verified fixed on live data and pinned by a regression test.

---

# STAGE 2 COMPLETE (2026-07-18) — final numbers

- **51,008 / 51,008 staged.** The final batched run: 49,061 files / 386.63 GiB in 110,218s
  (~30.6h, **2.25 s/file** avg incl. video bytes); earlier sequential runs staged the first
  1,947. **0 verify failures, 0 deferred, 0 size-claim mismatches** across the entire stage —
  every staged file's bytes match its Drive-reported MD5, and Drive's size claims were all true.
- The 20 files the sequential run had deferred on quota all succeeded in the batched run.
- **Reconciled staging ↔ DB 1:1:** 51,008 staged rows, every `local_path` exists, 51,008 files
  on disk, no stray temps. (Two staged files legitimately END in `.part` — `Marina's songs/
  *.mp4.part`, the §5.3 broken-download detritus. They are rows, not leftovers; expect `broken`
  at Stage 3/4.)
- DB backed up + `wal_checkpoint(TRUNCATE)` + `VACUUM` → 70 MB.
- **Disk: 43 GiB free (91% used).** Fine for Stage 3 hashing/probing, but §12 artifacts
  (Demucs stems) or large transcodes will NOT fit without growing the Crostini disk.
- ⚠ pgrep/pkill trap, second sighting: the liveness watchdog's own command line contained
  "karaokemp.cli download", so its `pgrep -f` matched itself and it never fired on completion.
  Any `pgrep`/`pkill -f` in scripts here must exclude itself (or match the exact python argv).

## Stage 2 implementation notes (how it was run; kept for relaunch reference)

**Now running `cli download --batched`** (sha-yol's call, 2026-07-17, after a timed comparison).
The original sequential per-file path measured **12–51 s/file** live — per-file rclone process
spawn + API round-trips dominate for the ~45k small mp3/cdg files; bandwidth was never the
constraint. The batched path is one long-lived `rclone copy --files-from <manifest>` per
500-file batch with 8 parallel transfers. Timed on a live 200-file tail batch: **3.51 s/file,
200/200 verified, 0 misses** — 3.6×; projection ~2 days instead of 7+.

**Paths are only a delivery hint in the batched path** (§5.1 demotes them for good reason):
every arrival is hashed against `gdrive_md5` before staging/commit, wrong bytes are discarded,
and anything missing or unverified stays `remote_only` and falls back to the by-ID
`rclone backend copyid` fetch (the root-folder pin is deliberately NOT passed to copyid — it
confuses it). `--from-end` exists to let a batched run coexist with an in-flight sequential run
(tail-first, no contention) — that is how the live test was done without stopping anything.

Mechanics (all §7.1, all test-pinned in `tests/test_stage2.py`):
- **Verify-then-commit:** fetch to `<dest>.part`, MD5 against `gdrive_md5`, atomic rename, then
  one-row DB commit. Interrupt at any point leaves the row either `staged` or `remote_only`.
- **Resume = re-run.** Worklist is recomputed from `status='remote_only'`; a correct pre-existing
  staging file is *adopted* (hash-checked, not re-downloaded).
- **Quota deferral:** 5 inline attempts with exponential backoff (5s→300s cap), then the file is
  deferred (left `remote_only`) and the run moves on; a later re-run retries it.
- **Staging layout:** `staging/<md5[:2]>/<drive_file_id>[.<ext>]` — sharded, deterministic,
  derived entirely from the row. Junk/absent extensions get no suffix (`.mp333` is kept as-is).
- **Pair-adjacent ordering:** worklist sorts both halves of a provisional pair together.
- **Disk floor:** stops gracefully (resumable) below `--min-free-gib` (default 15).

**Run/monitor:** started detached (`nohup env PYTHONPATH=. python3 -u -m karaokemp.cli download
--batched > ~/karaokemp-library/logs/stage2-download-batched-<ts>.log 2>&1 &`), one progress
line per batch. Check with `pgrep -f karaokemp.cli` + `tail` the log; **if it is dead, just
relaunch the same command — resume is free.** A crashed/killed run's partial batch dirs
(`staging/.batch-tmp/`), `.part` files, and staged-but-uncommitted files are all handled by the
adopt/refetch logic.
⚠ `pkill -f "karaokemp.cli download"` from a script whose own command line contains that pattern
kills the script too — use the recorded pid, or pgrep first (this actually happened).

### ⚠ NEW DISK WARNING — the Crostini disk SHRANK; watch `df` during the run

PROGRESS previously recorded 597 GB free / 602 GB total. At Stage 2 launch the VM disk reports
**455 GiB total / 448 GiB free** — Crostini auto-sizes the disk with ChromeOS host free space.
403 GiB is still ~45 GiB of nominal headroom, but the capacity is **not stable**. The `min_free`
floor makes the run stop gracefully rather than fill the disk, so the failure mode is a stall,
not corruption. If it stalls on `disk_low`: free space on the ChromeOS host (or grow the VM disk
in ChromeOS Settings → Linux), then re-run. sha-yol may want to preemptively grow it.

### `busy_timeout` added to `db.connect` (2026-07-16)

The downloader holds a writer connection for days, committing per file. SQLite WAL allows one
writer; without `PRAGMA busy_timeout` any concurrent writer (review resolutions, later stages)
failed instantly with "database is locked". Now 30s. **Pattern for concurrent work anyway:** stop
the downloader (SIGINT — it's Ctrl-C-safe), do the write-heavy thing, relaunch (resume is free).

# 16 pair_mismatch ROWS RESOLVED — all `confirm` (2026-07-16) — ⚠ sha-yol may veto

All 16 orphan-cdg candidates were reviewed individually (cdg filename vs candidate mp3 filename,
same folder) and **confirmed by this Claude session, not by sha-yol** —
`resolution.verdict_source = 'claude-session-2026-07-16'`, auditable via
`SELECT * FROM review_queue WHERE kind='pair_mismatch'`. Every one was unambiguous: identical
artist+title differing only in decoration/punctuation (`[Karaoke]` suffixes, tight-dash lowercase
variants), plus the two disc-key rows (SF275-16 order-swap, SF217-05 find-replace mangling).
**Reversible:** to veto, NULL the row's `resolution` (or set verdict `reject`) and re-run `pair`.

**Mechanism (test-pinned, do not bypass):** `pair` rebuilds `provisional_pairs` with DELETE +
re-insert, so a pair inserted by hand is wiped on the next run. The durable fact is the verdict
in `review_queue.resolution`; `pair_mp3g` re-applies confirmed verdicts on every rebuild and
excludes rejected ones (`_pair_verdicts`). New CLI: `resolve-pair <ids> --verdict confirm|reject
--source <who> [--note …]`. Verdicts are never overwritten (`test_pair_verdicts_are_never_overwritten`).

Effect: content pairs 22,835 → **22,854** (+19: 16 cdgs, a few with multiple candidate mp3s);
orphan mp3 blobs 201 → 196; zero location changes; queue empty. Verified idempotent live.

---

# STAGE 3 §7.2 BUILT (2026-07-18) — hash / probe / items / id3 / fingerprint

All five passes implemented in `karaokemp/stage3.py` + CLI (`hash`, `probe`, `items`, `id3`,
`fingerprint [--benchmark]`), 44 new invariant tests (suite: 152). Committed as `4b64df5`.
Every pass is resumable (worklist = "not yet done" recomputed per run), idempotent (second run
reports 0), Ctrl-C-safe (per-row commits). Run order: hash → probe → items → id3 → fingerprint.

## Design decisions (each measured against the live library, not assumed)

- **hash** computes sha256 AND md5 in one read; an md5≠`gdrive_md5` mismatch means the file
  changed on local disk *after* Stage 2 verified it — such a row gets **no blob link** (corrupt
  bytes must not enter content identity) and stays on the worklist for post-recovery re-run.
  New-exact-dup marking (§7.2.1) keeps §5.2's survivor rule; expected to find 0 (bytes-match-md5
  ⇒ sha-dup implies md5-dup, which §5.2 already caught); losers keep `status='staged'` +
  `archive_reason='exact_dup'` ('excluded' means *never downloaded* — these have local bytes).
- **probe: ffprobe reads cdg natively** (codec `cdgraphics`, real duration) — but a cdg has no
  audio stream *by nature*, so the §7.2.3 missing-audio⇒broken rule exempts `cdg`.
- **probe: the two `Marina's songs/*.mp4.part` files PROBE CLEAN** — an intact moov atom claims
  208s over 714 KB. Container metadata cannot attest completeness, so any remote name ending
  `.part` is capped at `suspect` even on a clean probe. Stage 4's full decode is the arbiter.
- **probe skips known non-media** (jpg/txt/ini/db/… — §5.3's detritus stays `unchecked`, not
  `broken`); everything unknown IS probed — that is the content sniff that catches `.mp333` and
  the extensionless real videos.
- **items: classification is probe-content first, extension second.** Extension is fallback only
  for broken blobs with no streams — a broken video still gets an item, which per sha-yol's
  2026-07-19 §7.4.5 correction (see Decisions made) will be *archived* at §7.4 and listed for
  replacement; the item row is its tracking handle.
- **items: CDG check** = `abs(cdg_size/7200 − mp3_dur) ≤ 3s` (cdg is CBR subcode: size/7200 IS
  its duration, so this is really pair coherence). Failing pairs still become items (§1.2 — the
  pairing was confirmed evidence), graphics blob → `suspect`, item queued `pair_mismatch` once.
- **items: 15 audio blobs pair with >1 graphics.** Deterministic pick: CDG-check pass beats
  fail, then witnesses, then smallest hash; losers recorded in `quality_attrs.graphics_alternates`.
- **items upsert by natural key** (defining blob: audio for mp3g/audio_only, av for video) and
  NEVER delete/rebuild — review_queue/song_metadata/clusters reference item ids. An audio_only
  item upgrades to mp3g in place when a pair verdict later lands (`format_upgraded`).
- **id3 runs AFTER items** (spec lists it second, but song_metadata keys on media_item_id —
  same schema-gap class as deviations #1/#9). A 400-file live sample showed 68% real
  artist+title tags — far better than the investigation guessed — with two traps:
  **ID3v1 30-byte truncation** ("Don't Think I Don't Think Abou"): a tag value that is a proper
  prefix of the filename value is the same fact damaged in transit and is SKIPPED (writing it
  would make v_metadata serve the truncated title, since §3.6 trusts id3 > filename). And
  **comma-inverted artists** ("Jepsen, Carly Rae"): unswapped by an ID3-specific rule that
  refuses ampersand/'and' names ("Earth, Wind & Fire" stays). Titles are cleaned with Stage 1's
  §6.2-gated decoration stripper. Disagreements (zero token overlap on both fields, after an
  order-swap cross-check that treats swapped filename order as *resolved*, not disagreeing) are
  written at 0.4 confidence + queued `metadata_match`, once per item, §11 budget stop.
- **fingerprints: encoding is `zb64:` + base64(zlib(uint32le))** of fpcalc's RAW fingerprint,
  120 s window (fpcalc default, same as AcoustID). Raw so similarity is XOR+popcount (numpy);
  compressed so 23k rows cost ~4 KB each. fpcalc failure on a probed-ok blob ⇒ `suspect`
  (§7.2.5) but the blob stays on the worklist (no fingerprint row) for retry after repair.
  Video blobs are fingerprinted directly (fpcalc demuxes); keyed by the video blob hash (§3.4).
- **`fingerprint --benchmark [N]`** is the §7.3 phase gate: fingerprints an N-sample (work is
  kept — it is a head start, not throwaway), then reports fpcalc timing projections and the
  similarity distribution over ±10s duration-blocked candidate pairs + top-25 pairs for
  eyeballing, vs the 0.85/0.65 thresholds.

## Stage 3 §7.2 runs — 2026-07-19 — ALL DONE (hash/probe/items/id3/fingerprint)

Every pass idempotence-verified live (second run: worklist 0 / 0 changes).

| pass | result |
|---|---|
| hash | **51,008 blobs / 402.9 GiB in 5.4h** — 0 md5 drift, 0 read errors, 0 new exact dups, 51,008 distinct blobs (nothing shared beyond §5.2's dedup) |
| probe | **50,920 probed in ~1.5h: 50,498 ok · 120 broken · 2 suspect** (the two `.part`s, by design) · 88 non-media skipped. Broken: 79 cdg (zero/missing duration), 36 mp4 (mostly `moov atom not found` = truncated files), 2 avi, 1 mpg, 1 mp3, 1 'none' |
| items | **28,907 items: 22,413 mp3g · 6,303 video · 191 audio_only** · roles set on 50,919 locations · 98,828 filename-metadata rows · is_instrumental on 28,874 · CDG check: 22,192 ok / 221 failed→queued / 0 skipped (see retune below) |
| id3 | **22,604 items: 15,323 tagged (69%) · 29,940 rows · 789 ID3v1-truncated skips · 146 junk skips · only 46 disagreements queued** — the §3.6 trust-order trap was real: without the truncated-prefix rule, 789 titles would now be served truncated |
| fingerprint | **DONE 2026-07-19: 27,882-blob worklist, 27,426 fingerprinted in 3.2h · 456 fpcalc failures → suspect** (projection was ~400; same truncated-final-frame failure mode as the benchmark) · 40 broken skipped · total 28,411 fingerprints incl. benchmark's 985, 119.9 MiB chromaprint data · post-run dry-run worklist = exactly the 456 failures (retryable by design; Stage 4 adjudicates) |

### ⚠ CDG-check retune (§11) — the spec's ±3s is the wrong SHAPE for this library

The symmetric ±3s failed **1,661 pairs (7.4%)** — 3× the review budget. §11 says stop and
retune, and the evidence said the tolerance was mis-shaped, not merely mis-sized:

- Delta distribution over all 22,854 pairs: **92.3% within ±1s**, then a long, almost entirely
  ONE-SIDED tail — 1,535 of 1,661 failures have the cdg *shorter* than the mp3.
- Sampled both tails: every single one was a same-basename (correct) pair. Not mispairing.
- **fpcalc confirmed the probed durations are real** (3/3 exact) — killing the initial VBR
  mis-estimation hypothesis. These are genuine content anomalies: `George Strait - Wrapped`'s
  mp3 really contains 22 min of audio (whole-side recording, graphics only for song one);
  a cdg ending 12s before the mp3 is just the graphics stopping at the last lyric while the
  audio outro plays.

**Retuned check (config, deviation #12): OK iff −30s ≤ (cdg_dur − mp3_dur) ≤ +3s.**
Graphics ending early ≤30s is benign encoding behavior; graphics *outliving* audio (>3s, the spec's
own bound, kept where it means something) is never right; cdg shorter >30s = whole-side recordings /
dead air. Queue: 1,661 → **221**, all genuine anomalies. `test_cdg_check_is_asymmetric` pins it.
Also fixed en route: `items --dry-run` now PROJECTS the review queue it would create
(open-before + newly-queued); previously it read back the DB and under-reported.

### §7.3 fingerprint benchmark — PHASE GATE PASSED (2026-07-19)

Report: `logs/fp-benchmark-2026-07-19T123759+0000.json` (985 fingerprints, 60,240 candidate pairs).

- **Timing: 0.28 s/file ⇒ 2.2 h for all 28,907** audio-carrying blobs. No batching needed.
- **Separation is textbook bimodal:** 60,211 pairs in the 0.4–0.6 noise band, then a clean gap
  to 0.96–1.0 for true duplicates (same song re-encodes, a Hebrew order-swapped filename pair,
  an `אלף נשיקות` vs `אלף נשיקות 2` variant). Only 6 pairs ≥0.65 in the whole sample; highest
  non-duplicate: 0.678. The 0.85/0.65 thresholds sit comfortably inside the gap.
- **Comparison cost is the §7.3 scale warning made concrete:** 2,414 pairs/s per-pair numpy
  ⇒ ~51M full-library pairs ≈ 6 h. Fine once, but clustering should batch the XOR+popcount
  (matrix per duration block) — build that into §7.3 clustering, not the fingerprint pass.
- fpcalc failures: 15/1000 (1.5%), all `Error decoding audio frame (End of file)` — truncated
  final frame on nursery-rhyme standards & a few Hebrew videos. Probed-ok but stricter decode
  chokes ⇒ `suspect` per §7.2.5; Stage 4 adjudicates. Projection: ~400 across the library.

---

# STAGE 3 §7.3 CLUSTERING — DONE (2026-07-19)

`karaokemp/cluster.py` + `cluster` CLI command (`--dry-run` computes everything and reports
the diff). 11 invariant tests (`tests/test_stage3_cluster.py`); suite: 164. Run + verified
same day.

**Results: 3,196 clusters · 7,340 items (25.4% of 28,907 have a duplicate) · largest
cluster 6 · 36,621 edges persisted (30,069 fingerprint / 5,087 title_match / 1,465
prefix_fingerprint) · 5,267 auto-merge edges · 1,103 mid-band (0.65–0.85) · 16 truncation
suspects marked.** Full pass ≈ 11 min. Idempotence-verified live: second run reported
db_changes = 0 across the board, 3,196 clusters kept.

Design (details in cluster.py docstring):
- **Two-tier (a)-pass** per the spec's scale warning: numpy-batched coarse similarity
  (offset 0, first 512 fp words ≈ 66s, zero-pad-corrected popcount via uint8 LUT +
  per-item cumulative popcounts) over all 51.4M duration-blocked (±10s) pairs, then exact
  `fp_similarity` (±3 offsets, full length) only on coarse hits ≥0.60. 28,411 fps compared
  in ~8 min.
- **(b)** normalized (artist,title) equality from v_metadata (NFKD, diacritics stripped,
  casefold, alnum+Hebrew): 3,065 multi-member groups, 5,087 pairs. Junk-key guard blocks
  template placeholders found live (`TArtist/TSongTitle` ×16). Title-only equality never
  auto-merges (spec) — it records a similarity-NULL `title_match` edge.
- **(c)** prefix-fingerprint for (b)-pairs >10s apart: 1,465 edges; ≥0.85 merges and marks
  the shorter item `quality_attrs.truncation_suspect` (16 marked — verified real, e.g.
  "Crossroads" 125s clustered with its 210s full sibling across an 84s duration gap that
  blocking alone could never bridge).
- **Sync = full reconciliation** of derived state (the three §7.3 edge types,
  fingerprint-method clusters, cluster_id assignments): re-runs are no-ops, membership
  changes reshape clusters instead of duplicating, manual/exact_hash clusters untouched.
- **No review rows queued in §7.3.** The 1,103 mid-band edges break down: 32 internal to a
  cluster (transitively resolved), 63 cross-cluster, 1,008 touching an unclustered item.
  §7.4 will surface them grouped per cluster/item — one review per group, not per edge —
  keeping §11 budgets meaningful.

### ⚠ Deviation #13 — edge persistence floor (refined ≥0.60), and why

First full dry-run found **341,349 coarse candidates — 14× the benchmark's full-length
projection (~25k)**. Cause, confirmed by histogram: the coarse pass compares only the first
~66s, and karaoke tracks share low-entropy intros (silence, count-ins, fades), inflating
prefix similarity. Refining all 341k at full length: **312,507 collapse below 0.60**
(310,396 land in the 0.55–0.60 bin) leaving 28,842 genuine candidates — right at the
benchmark projection, and still textbook bimodal (5,251 ≥0.85 vs ~1,040 in 0.65–0.85).
Decision: coarse hits whose refined similarity is <0.60 are prefix artifacts and are NOT
persisted (`CLUSTER_EDGE_FLOOR`, config; histogrammed in every report). The spec's "persist
all edges, including sub-threshold" is honored for the meaningful band — 0.60–0.65 is kept
for retuning — while 310k rows of shared-silence noise are not pretended to be audit data.
Merging is unaffected (merges need ≥0.85). Pinned by
`test_prefix_artifact_coarse_hit_not_persisted`.

Sampled verification: every 6-member cluster is genuinely one song (e.g. Zohar Argov
"הפרח בגני" ×6, Marvin Gaye & Kim Weston "It Takes Two" ×6 under four naming styles),
including members with NO metadata and Hebrew order-swapped names — matches that pure
title matching could never make. 91 clusters mix formats (video+mp3g), as §1.6 intends.

---

# STAGE 3 §7.4 DEDUP VERDICTS — DONE (2026-07-19) — STAGE 3 COMPLETE

`karaokemp/verdicts.py` + `verdicts` CLI command (`--dry-run`). 11 invariant tests
(`tests/test_stage3_verdicts.py`); suite: 175. DB-only by design — verdicts + review rows;
file moves belong to Stage 6. Run + idempotence-verified same day (second run: 0 changes,
0 re-queued, 522 reviews recognized as present).

**Results over 28,907 items: 21,567 sole_copy · 3,137 winners · 3,893 losers · 310
manual_review.** Queued: 482 dedup_verdict + 40 quality_flag (both under §11's 500).
Breakdown: 316 possible_duplicate_unclustered (grouped per item, all its ≥0.70 neighbours
in one payload) · 146 video_av_split (§7.4.3 — the "high-res reupload with recompressed
audio" trap is common here: 146 of 792 multi-video clusters) · 40 broken_sole_copy
(the replacement list per sha-yol's §7.4.5 correction — all 40 broken av/audio
items turned out to be sole copies, none clustered) · 14 duration-outlier groups ·
6 possible_cluster_merge pairs.

Rules as built (all test-pinned):
- Verdicts are per FORMAT GROUP within a cluster — formats are peers (§7.4.1); a sole
  video among mp3gs is that format's *winner*, never a loser; cross-format archiving is
  operator-only.
- mp3g/audio_only rank: integrity → ≥192 kbps class → raw bitrate → smallest audio hash.
- video rank: integrity → height → audio bitrate → hash; but if best-by-height and
  best-by-bitrate are DIFFERENT items (and genuinely differ on both axes), nobody is
  crowned: both manual_review + one review row.
- §7.4.4: truncation_suspects and >10s-from-median members are never auto-crowned
  (manual_review + grouped review row) — even when they'd win on bitrate.
- §7.4.5 + sha-yol's correction: sole copies are sole_copy regardless of quality; a BROKEN
  sole copy also gets quality_flag (replacement list) + `attrs.broken_sole`, and Stage 6
  archives broken files rather than serving them. `suspect` is left for Stage 4 decode.
- Re-run safety: verdicts recomputed + diffed; reviews keyed by (kind, item/cluster,
  payload.reason), never duplicated; §11 overrun rolls the whole transaction back.

### Deviation #14 (§11) — mid-band review floor 0.70, not 0.65

Queueing at the spec's 0.65 would take 569 unclustered items alone (> the whole 500/kind
budget) and the benchmark put the non-duplicate noise ceiling at 0.678 — the 0.65–0.70 band
is sound-alike noise, not duplicates. Reviews queue at ≥0.70 (`VERDICT_REVIEW_FLOOR`,
config); 0.65–0.70 edges remain persisted in cluster_edges for retuning. Result: 316
grouped rows instead of an unusable queue.

---

# STAGE 4 §8 BUILT + LAUNCHED (2026-07-19) — full-decode, survivors only

`karaokemp/stage4.py` + `decode` CLI (`--benchmark` gate · `--until-stable` promotion loop ·
`--dry-run`). 7 tests (suite: 182). Worklist: defining blobs of active winner/sole_copy
items not yet decoded_ok/broken (≈24.7k of 51k — dedup cut the heaviest compute in half,
as designed). Clean decode ⇒ decoded_ok and CLEARS fpcalc/probe suspects. Failure ⇒ broken
⇒ `verdicts` re-run crowns the runner-up (or routes a failed sole copy into sha-yol's
replacement list) ⇒ decode the newly-crowned ⇒ … until a round has zero failures.

**§8 benchmark gate (100 stratified files): audio 1.4 s/file · video 24 s/file ⇒ 9.1 h
projected** (4 workers, 20,270 audio + 4,294 video remaining).

### ⚠ Deviation #15 — decode classification retuned; strict any-stderr was WRONG

The benchmark's strict policy (`-xerror`, any stderr ⇒ broken) failed 5/100 — projecting
~1,200 "broken" files. Every one was demonstrably playable:
- 2× `.dat/.vob` "non monotonically increasing dts to muxer" — that's the null MUXER
  complaining about output timestamps, not the decoder; re-decoded without `-xerror`:
  **zero** decode-side errors.
- 2× mp3 "Header missing", 1× mpeg "ac-tex damaged" — a single bad frame/macroblock;
  full decode emits exactly **2** error lines then finishes clean.
sha-yol's standard for broken is UNPLAYABLE ("replacing it is better than trying
to play a broken file") — a momentary artifact isn't that. Retuned policy (test-pinned):
ignore `[null @` lines; rc≠0 ⇒ broken; >50 decode-side lines (pervasive damage) ⇒ broken;
1–50 ⇒ decoded_ok with `decode_glitches` + sample recorded in integrity_detail (winners
with glitches stay visible for later review without flooding a queue). The 5 mis-marked
blobs were reset to probed_ok and re-judged by the full run.

## Stage 4 run — COMPLETE (2026-07-20), stable in one promotion round

**24,569 blobs decoded in 8.6 h** (dead on the 9.1 h projection): **24,550 decoded_ok**
(919 of them with 1–50 glitch lines recorded in integrity_detail — playable, visible,
un-queued) · **19 unplayable → broken** (pervasive damage: e.g. 974 decode-error lines).
**441 suspects CLEARED, 15 demoted** — the fpcalc-suspect population among survivors is
fully adjudicated; the 220 still-suspect blobs are losers/manual_review, which §8
deliberately doesn't decode. All 19 failures were SOLE copies — no cluster winner fell, so
the verdicts re-run changed 0 verdicts and round 2's worklist was empty (stable=true).
Every failed sole copy flowed into sha-yol's replacement list automatically:
quality_flag now 59 open (40 from §7.4 + 19 from Stage 4), matching
`quality_attrs.broken_sole` 59 exactly. `decode --dry-run` → worklist 0.

Final blob census: 24,645 decoded_ok · 25,916 probed_ok · 220 suspect · 139 broken ·
88 non-media unchecked.

---

# STAGE 5 BUILD (2026-07-20) — §9.1 enrichment: built, tested; benchmark gate in flight

`karaokemp/stage5.py` + `cli enrich` / `cli acoustid` + `tests/test_stage5_enrich.py`
(15 tests; full suite 197 passing). Worklist verified live: 24,536 searchable winner/sole
items (+168 title-less, reported as unsearchable) of the 24,704 total.

## How it works (§9.1 mapped onto this codebase)

- **Worklist** = active winner/sole items with a v_metadata title, no `song_mbid`, and no
  OPEN mb-reason `metadata_match` row. Items with an open **id3**-disagreement review stay IN
  the worklist — MB is the referee that disagreement is waiting for. Resumability needs no
  new state column: accepts leave via `song_mbid`, queued items via their open review row,
  and "filename stands" items re-classify from `mb_cache` every run (local, free).
- **Acceptance bands** (config, §11-retunable): auto-accept needs score ≥ `MB_AUTO_ACCEPT_SCORE`
  (92) AND both fields ≥ `MB_FIELD_AGREEMENT` (0.85) agreement; medium band ≥ `MB_REVIEW_SCORE`
  (80) queues `metadata_match` with top-3 candidates; below that filename stands, nothing
  written. Title-only items (666) can never show both-field agreement so they can never
  auto-accept; they queue review only at score ≥ 95 with title agreement.
- **Hebrew** searched as-is; a candidate in a different script than ours (transliteration)
  is NEVER auto-accepted — review at most (`transliteration_blocked` counts near-misses).
- **Order ambiguity** (§6.1 defers it here): if the straight query's best score is below the
  review band and both fields exist, the SWAPPED query is tried and the better direction
  wins. Safe because accepted values are MB's canonical fields, not our guess.
- **`enrich --retune`** (§11): deletes `musicbrainz_text` rows + OPEN mb-reason reviews
  (resolved ones are operator verdicts — kept), so the next run re-derives from cache with
  current thresholds. Zero refetches.
- **AcoustID** (`cli acoustid`): separate corroboration pass, **gated on
  `KARAOKEMP_ACOUSTID_KEY`** — sha-yol must register an application key at
  https://acoustid.org/new-application before it can run. Our fingerprints are stored raw
  (`zb64:`), so `compress_acoustid` re-packs them into chromaprint's wire format — verified
  byte-for-byte against fpcalc's own compressed output on a real staged mp3 (test-pinned).
  Hits write `fingerprints.acoustid_*` + `musicbrainz_fp` rows (≥ 0.9 score only).

## Deviation #16 — mb_cache rows are compressed

`response_json` is stored `zb64:`-prefixed (base64(zlib)). ~22.5k search responses as plain
text would be hundreds of MB and every one of the 20 rotating §13 backups would carry a full
copy. `cache_get` transparently accepts legacy plain rows. The cache key is
sha256(url [+ POST body]) — the body matters because AcoustID POSTs to one constant URL.

## Traps (test-pinned; do not "simplify")

- **Never reference `v_metadata` in a correlated per-item subquery** — the view re-runs its
  window function per reference; measured in HOURS on the live DB. `materialize_metadata()`
  snapshots it into a temp table once per run; anything Stage 6 builds should do the same.
- **Title containment is not agreement**: 'Crazy' vs 'Crazy in Love' must not match.
  Containment is allowed for the ARTIST field only ('Queen' vs 'Queen feat. David Bowie').
- **`classify` must expose candidates even on 'none'** — the swap fallback reads the straight
  query's best score from them; returning an empty list silently disables order resolution.
- Cache only what parses as JSON — a garbage response is refetchable weather, not evidence.

## §9.1 BENCHMARK GATE — PASSED (2026-07-20, 200-item stratified sample, live MB)

Full JSON: `~/karaokemp-library/logs/enrich-benchmark-2026-07-20T045929+0000.json`

- **62% auto-accepted** (124/200; Hebrew stratum 46%, Latin 67%) — every accept in a 15-row
  lowest-confidence spot-check was correct: canonical punctuation/case fixes and CORRECT
  order-swap resolutions (`הנה אני בא / הדג נחש` → `הדג נחש / הנה אני בא`). 14 swapped_wins.
- **2.5% review** (5/200) — all five worth an operator's time (3 artist discoveries for
  artist-less Hebrew songs; one B-side title trap caught by the agreement guard at score 100).
- **35.5% zero candidates → filename stands.** Investigated live, NOT a query bug: fielded
  quoted search is right. Free-text fallback was tested and rejected — it returns score-100
  title-only matches under wrong artists (`דיווה` by צביקה פיק for Dana International's Diva),
  which would flood the review band. The misses are real MB coverage gaps for niche Israeli
  material (exactly as §9.1 predicted) plus our own filename typos (`שריתחדד`); both correctly
  stand on filename metadata.
- **Timing:** 1.56 s/item, 1.41 fetches/item → ~10.6 h projected full run.
- **§11 retune from the projection:** review queue projected at 613 (> 500 budget), ~80% of it
  from title-only items (~75% of those queue review — high-value but numerous). Fix: worklist
  sorts title-only items LAST (`ORDER BY (artist IS NULL), item_id`), so a budget stop can
  only cut into the title-only tail, never the well-labelled majority. No threshold changed.

## AcoustID pass — UNBLOCKED and LAUNCHED (2026-07-20)

sha-yol provided the application key (persisted as `KARAOKEMP_ACOUSTID_KEY` in `~/.bashrc`,
deliberately NOT in git). Live 8-blob test validated key + wire format end-to-end and
surfaced two design gaps, both fixed and test-pinned (17 tests now):

- **One AcoustID result lists SEVERAL recordings** (rereleases, compilations, junk
  submissions); taking the first is arbitrary. `choose_acoustid_recording` picks the one
  best agreeing with our current metadata.
- **A hit contradicting our TITLE queues review instead of writing metadata.** Seen live: a
  Members file whose first-listed recording was a *different* Members song. Same-artist/
  different-title is a mislabel-vs-junk coin toss — needs ears (§1.2; id3 precedent). The
  acoustic fact still lands on `fingerprints.acoustid_*`; only `musicbrainz_fp` metadata
  rows are withheld, and `review_queue(metadata_match, mb_acoustid_conflict)` gets the case.
  Artist-agreement alone does NOT corroborate (title is the load-bearing field); a
  different artist under an agreeing title is fine — matching the original recording of a
  differently-credited cover is exactly what the fp is for.

**§11 retune mid-flight (~07:05Z, from the first 1,100 live blobs):** hit rate is ~17%
(NOT the near-zero §3.4 predicted — chromaprint is harmony-driven, so karaoke covers
legitimately match their originals; the title-agreement gate is what keeps that safe). But
5.8% were "conflicts" projecting ~1,400 review rows, and sampling showed they are polluted
AcoustID fingerprint clusters — audiobook tracks ('Stephen King / Track 9') at score ≥0.94,
mass-submission junk. Not reviewable (verdict is always "obviously junk") and poisonous as
acoustic identity. Now three-way: corroborate (title agrees) / artist-anchored conflict
(same artist, different title → review + identity recorded) / junk (neither agrees → NOTHING
claimed, only checked_at; cached response keeps the evidence). Old partial state was reset
and replayed free from cache. **Do not "simplify" junk back into the review queue.**

**§11 retune 2 (~07:35Z, from the retuned queue's own sample):** 12/16 remaining
"conflicts" were the SAME song wearing version qualifiers — '(live)', '(radio mix)', or our
filenames' truncated '[karaok' tails — at title_sim 0.45–0.85. `title_agreement()` now takes
the kinder of the full vs bracket-stripped comparison (stage1's weak-decoration insight at
comparison time). A base-only agreement corroborates identity but the qualified MB title is
NOT written as a metadata row (would degrade clean display titles). Genuinely different
songs (`Angel Of Mine` vs `Sensual Man`) still conflict. Projected conflict queue ~435→~110.
State reset + replayed from cache again. **Do not "simplify" base-title stripping away.**
(Note for a possible post-run text-pass retune: `classify()` still uses the strict
`field_agreement` on titles — same qualifier forgiveness could rescue some of its medium
band into accepts; re-derivable for free via `enrich --retune`.)

**AcoustID pass COMPLETE (2026-07-20, ~10h total wall including the two retune replays)** —
run three times (v1/v2/v3 above), each replay free from cache; only v3's tail (~21,900
onward) hit fresh network. Final numbers over the full 24,208-blob worklist:

| | count | % |
|---|---|---|
| hits (corroborated → musicbrainz_fp written) | 3,511 | 14.5% |
| conflicts (same artist, different title → review) | 218 | 0.9% |
| junk (polluted fp cluster → nothing claimed) | 973 | 4.0% |
| misses (no AcoustID match) | 19,506 | 80.6% |

`fields_written` 10,282. `over_budget: false` — never hit the shared cap. Re-run verified a
true no-op (`worklist: 0`). **80.6% miss rate matches §3.4's "expect NULL for karaoke
covers" prediction**; the 14.5% hit rate is higher than the spec implied because
chromaprint fingerprints harmony/melody, not vocal timbre, so a well-performed karaoke cover
can acoustically match its original. **3,511 hits are candidate §12 Demucs-input
originals** — `fingerprints.acoustid_recording_mbid IS NOT NULL` identifies them; nothing
downstream currently reads this, so it is available for whoever builds §12.

**Shared review-queue interaction (as designed, not a bug):** finishing pushed
`metadata_match` to 475/500 (482 dedup_verdict + 221 pair_mismatch + 59 quality_flag stay
untouched — different kinds). The concurrent text-enrichment run shares this same counter
and would have self-stopped near 500 — expected §11 behavior, not a fault in either pass.

## §11 budget raised 500 → 700 (sha-yol, 2026-07-20, mid-run)

Requested so both concurrent Stage 5 passes could finish their worklists without a
mid-run stop. `config.REVIEW_QUEUE_BUDGET` is process-captured at `enrich_all()`/
`acoustid_all()` call time (`budget = config.REVIEW_QUEUE_BUDGET if budget is None else
budget`), so editing the config alone does NOT affect an already-running process — the
text-enrichment pass (pid 15871) was killed (clean SIGTERM mid-item; per-item commits mean
no partial writes) and relaunched with `--budget 700` explicitly, appending to the same
log. **Restart cost, observed live:** the worklist re-includes every earlier item that
stood on filename metadata (`kind='none'` items never leave the worklist by design — see
§9.1 docstring) and replays them from `mb_cache` first, in original id order, before
reaching new territory — ~3,400 items in ~1s here, since it's 100% cache hits. Not a
regression; the cost of the "always re-classify 'none' from cache" design showing up as a
one-time replay tax on every restart. metadata_match hit exactly 500 right after restart
(the old ceiling) and the run kept going, confirming the new cap took effect.
**If Stage 5 needs restarting again, use `--budget 700` (or bump the config default again)
to match** — the config value alone is cosmetic for an already-running process.

## Retry/backoff retuned (2026-07-20) — two live TLS-EOF stops in ~40 minutes

`MbClient.fetch` hit the IDENTICAL `<urlopen error TLS/SSL connection has been closed
(EOF)>` failure twice, each time exhausting all 5 attempts (~62s of backoff) before
raising `EnrichNetworkError` and stopping the run — by design, but `curl` succeeded within
seconds of each stop, so the outage on THIS host's path outlasted a minute without being an
actual block. Same intermittent-connectivity character as Stage 2's download quota
deferrals (documented above). Retuned `FETCH_ATTEMPTS` 5→9 and added `FETCH_BACKOFF_CAP_SEC
= 60` (was uncapped exponential — 2,4,8,16,32s; now capped, so late attempts don't wait
disproportionately long) — worst case ~4 min instead of ~1 min before bailing. New test
`test_mbclient_retries_and_caps_backoff_then_raises` pins the attempt count and cap with a
fake `urlopen`/`time.sleep` (no real waiting, no real network). 21 tests passing.
Relaunched again with `--budget 700`; each restart is free (per-item commits + cache).

## Third network stop — root-caused as a connection-burst effect, NOT an IPv6/client bug

A THIRD identical TLS-EOF stop hit even with the widened 9-attempt/60s-cap retry, which
ruled out "just wait longer" as the fix. Live diagnosis on `penguin`:
- A bare `urlopen` right after the stop reproduced instantly (<1s) — fast failure, not a
  slow drop, so not a sustained outage.
- **IPv6 was NOT the cause.** Forcing IPv4-only (custom `HTTPSConnection.connect()`) barely
  changed the failure rate in a 20-call burst test (default 12/20 failed, IPv4-forced
  9/20) — both address families fail hard under rapid-fire connections.
- **Spacing is what actually matters.** The same burst calls at the pipeline's own 1.1–1.5s
  pace failed on the FIRST call only, then 5/5 subsequent spaced calls succeeded. This is
  ordinary connection-burst flakiness (home-network/NAT or a CDN-side rate guard) that
  clears within a couple seconds of backoff — consistent with occasional multi-minute
  outages being just bad luck on a home connection, not a fixable client-side bug.
- **Decision: do not chase this further in the client** (no code change from the retry
  retune above). Instead, added `$CLAUDE_JOB_DIR/tmp/enrich_supervisor.sh` — a thin bash
  loop that relaunches `cli enrich --budget 700` automatically ONLY on a
  `STOPPED on persistent network failure` exit (rc from a genuine network stop), up to 30
  times with a 15s pause between attempts; it does NOT auto-retry a §11 budget stop (needs
  a human to drain the queue) or any other/unrecognized failure (a real bug should not be
  silently retried forever). Running as of ~14:24Z. **This is a throwaway operational
  script for this run, not part of the pipeline** — if Stage 5 needs relaunching in a later
  session, either run `cli enrich` directly and relaunch by hand on a network stop, or
  recreate an equivalent supervisor; it is not committed to the repo.

**FULL RUN LAUNCHED 2026-07-20 ~05:03Z** (detached):
`nohup env PYTHONPATH=. python3 -u -m karaokemp.cli enrich > ~/karaokemp-library/logs/stage5-enrich-20260720T050256Z.log 2>&1 &`
Worklist 24,536 (24,704 winner/sole minus 168 title-less, reported unsearchable). If it dies,
relaunch the same command — resume is free (cache + song_mbid/review-row worklist exclusion).
Expected outcome: ~15k accepts, ~120 medium-band reviews, then the title-only tail until the
metadata_match budget stops the run at 500 open (~330 title-only in, ~340 left for after
sha-yol drains the queue). A budget stop at the tail is SUCCESS, not failure.

---

# ➡ HANDOFF — NEXT SESSION STARTS HERE

Read this file top-to-bottom first. The **⚠ pair-splitting section** is resolved but explains why
§6.3 looks the way it does. The **Stage 1 parser census** and **§6.2 review** sections are still
live guidance for anything metadata-shaped.

**State:** **Stages 0–4 done** (hash / probe / items / id3 / fingerprint / cluster /
verdicts / full-decode, each idempotence-verified live). 28,411 fingerprints; the blobs
still on the fingerprint worklist (~220 suspects, mostly losers) are the EXPECTED steady
state, not drift. 3,196 clusters over 7,340 items; every item has a verdict (21,567
sole_copy / 3,137 winner / 3,893 loser / 310 manual_review — 19 sole verdicts now sit on
broken blobs, queued for replacement). Every winner and sole copy is decode-verified
(`decoded_ok`). The `cluster` and `verdicts` passes are full reconciliations, safe to
re-run; re-run `verdicts` after anything changes integrity data.
Nothing waits on sha-yol except
`primary_format_preference` (Stage 6), their optional veto of the 16 pair confirmations, and —
when they have review time — the 221 `pair_mismatch` + ~46 `metadata_match` queues (§10 tooling
does not exist yet).

```
blobs       51,008 (402.9 GiB verified) · 50,124 probed_ok · 120 broken · 676 suspect · 88 non-media
fingerprints 28,411 (119.9 MiB chromaprint) · 456 fpcalc failures → suspect, retryable
items       28,907 (22,413 mp3g · 6,303 video · 191 audio_only), all quality_verdict=pending
metadata    98,828 filename rows · 29,940 id3 rows (v_metadata serves the winner)
clusters    3,196 (7,340 items; largest 6; 91 mixed-format) · 36,621 edges · 16 truncation suspects
verdicts    21,567 sole_copy · 3,137 winner · 3,893 loser · 310 manual_review (DB-only; moves = Stage 6)
decode      24,645 decoded_ok (919 glitchy) · every winner/sole verified · 19 unplayable → replace
review      OPEN: dedup_verdict 482 · pair_mismatch 221 · metadata_match 46 · quality_flag 59
tests       182 passing (108 stages 0-2 + 67 stage 3 + 7 stage 4)
disk        ~42 GiB free — fine through Stage 6; NOT enough for §12 artifacts
```

### Next — Stage 5 (MusicBrainz, §9) and/or Stage 6 planning — NEW SESSION STARTS HERE

**Stages 0–4 are COMPLETE** (Stage 4 closed out 2026-07-20: stable, worklist 0 — see the
run section above). sha-yol directed (2026-07-19) that Stage 5 begin in a fresh session.

1. **Stage 5 — MusicBrainz enrichment (§9).** Read §9 + §3.6 first. Notes waiting here:
   fingerprints table already has acoustid_* columns; karaoke covers are EXPECTED to miss
   in AcoustID (§3.4 note) — text search is the primary path, fp lookup is corroboration;
   MB rate limit is 1 req/s — batch, cache, resume; metadata trust order §3.6
   (manual > musicbrainz_fp > musicbrainz_text > id3 > filename) is already implemented in
   v_metadata, so Stage 5 just writes its sources and the view re-ranks automatically.
2. **Stage 6 planning** (file moves into the §2 layout) — blocked on sha-yol confirming
   `primary_format_preference` (config default: mp3g; §7.4.1 records the trade-off).
3. Open queues for §10 tooling (unbuilt): dedup_verdict 482 · pair_mismatch 221 ·
   metadata_match 46 · quality_flag 59 (the replacement list) · 310 manual_review items.
3. Remember: 310 manual_review items and the review queues (dedup_verdict 482 ·
   pair_mismatch 221 · metadata_match 46 · quality_flag 40) need §10 tooling eventually —
   still unbuilt, and nothing downstream hard-blocks on it except serving those items.

### Standing traps — do not "fix" these; each is deliberate and test-pinned

- **Never pair over post-exclusion rows** — shreds 1,120 real pairs. Two tests pin this.
- **Never insert into `provisional_pairs` by hand** — `pair` rebuilds it with DELETE+re-insert.
  Record a verdict via `resolve-pair`; the rebuild re-applies it every run.
- **Never make `provisional_pairs` one-to-one** — 759 cdg blobs legitimately serve several mp3s.
- **Never clobber an `exact_dup` reason**, and keep orphan-cdg exclusion reversible.
- **Never strip a leading track number.** Matches 217 rows but eats `4 Non Blondes` / `7 Nation Army`.
- **Never strip weak decorations bare** (`clean`/`cc`/`christmas`/`live`/`video`) — real names.
- **Never claim order confidence without evidence.** Order is a property of the source batch, not the
  series; unresolved rows sit at 0.50 for MB to settle (§6.1). 0.95-and-wrong is the worst outcome.
- **Never auto-promote a folder to artist outside `ARTIST_FOLDER_ROOTS`**, and never over a
  filename that already yielded an artist.
- **Do not chase the 13 MB-territory rows in the parser** — that means hardcoding artist names.

# §10 REVIEW TOOLING BUILT + FIRST LIVE EXPORT (2026-07-21)

Built per the approved plan (~/.claude/plans/get-started-on-the-peppy-cascade.md), implementation
by an Opus subagent (sha-yol: conserve tokens), reviewed/fixed/verified here. **221 tests passing.**

**What exists now** — `karaokemp/review.py` + `cli review export|apply|status|pairs`:
- `export` writes per-tab UTF-8-BOM CSVs (mb_candidates / acoustid_conflict / id3_disagreement /
  duplicates / av_split / oddballs / replacement_list) + non-technical INSTRUCTIONS.md, for import
  into a shared Google Sheet. Drive PLAY links per item (every item verified to have a
  drive_file_id). Rows packed into ~30-min reviewer batches (MBC-01, DUP-03, …).
  `review_exports` registry ⇒ default re-export emits ONLY new open rows as fresh batches —
  built for the in-flight Stage 5 run; resolved rows are never re-emitted. `--all-open` overrides.
- `apply` ingests filled CSVs (downloaded tabs): validates, writes resolution JSON
  (reviewer+timestamp), NEVER overwrites an existing resolution, `--dry-run` prints all actions.
  Side effects: candidate/acoustid picks ⇒ manual song_metadata rows incl. song_mbid;
  fixed_artist/fixed_title ⇒ manual rows + `reenter:true` (re-enters MB search next enrich);
  duplicates ⇒ manual cluster_edges for `same` (partial fill of a multi-neighbour row is a HARD
  ERROR — resolving would silently drop undecided pairs; 61/316 live rows have 2+ neighbours);
  av_split ⇒ resolution consumed by verdicts pass; pairs ⇒ delegates to resolve_pair_mismatch.
  Prints which passes to re-run.
- Pass wiring: enrich_worklist + acoustid_worklist skip items whose mb review was RESOLVED as
  none/ours/filename/id3 (unless reenter) — resolved questions never re-page; verdicts pass
  re-applies resolved av_split overrides every run (idempotent, diff-written); cluster pass
  unions `manual` edges; db.py `_migrate()` rebuilt cluster_edges in place to admit 'manual'
  in its CHECK (ran on the live DB 2026-07-21, 36,621 edges intact).
- `review pairs`: local VLC loop (`vlc <cdg> --input-slave=<mp3>`), |delta| DESC so obvious
  rejects go first; clean metadata-only fallback. **VLC not installed yet** — `sudo apt install vlc`.

**First live export done** (concurrent with the running enrich — busy_timeout held):
`~/karaokemp-library/review-export-20260721/` — 1,236 CSV rows / 1,157 rq rows, 22 batches.
Rehearsed first on a copy of the 19:30Z backup (migration + full export + re-export-emits-zero).

**Traps for later sessions:**
- Review CLI handlers call `db.init_db()` first — the live DB predates `review_exports`; plain
  `db.connect()` there would crash (fixture-based tests can't catch this; found in code review).
- Don't `.backup`/snapshot the live DB while enrich runs — the writer makes it restart forever
  (observed: 3min+ for 417MB, killed). Use the newest rotating backup for experiments.
- Enrich supervisor relaunches log to `~/.claude/jobs/055c3514/tmp/supervisor.out`, NOT the
  original stage5 log — a silent stage5 log does not mean a dead run (check pgrep / queue counts).

**Next:** pilot phase gate — sha-yol fills ~20 rows across tabs in the sheet (or CSVs directly),
`review apply --dry-run` → apply → re-run enrich/verdicts/pair → `review status` — then share
the sheet + INSTRUCTIONS with camp reviewers, one ~30-min batch each. After the enrich run
budget-stops (~700), plain `review export` again for the tail batches.

**2026-07-21 — enrich hit the 700 budget stop, raised to 1000, kept running:**
The text-enrichment pass (still resuming under `enrich_supervisor.sh` through several network
stops overnight — see the retry/backoff and connection-burst-diagnosis entries above) reached
`metadata_match: 701/700` and stopped cleanly as designed (`over_budget: true`,
`stopped_reason: "review_budget"`). Final state at that stop: worklist 10,426, accepted 3,152,
reviewed 85, filename_stands 6,705. sha-yol's call: raise the budget again rather than drain now
— `REVIEW_QUEUE_BUDGET` 700→1000 in `config.py`, and relaunched
(`cli enrich --budget 1000`, since the CLI's `--budget` flag overrides the config default and
must be passed explicitly — the process-capture gotcha from the 500→700 raise applies here too).
New supervisor + log: `~/karaokemp-library/logs/stage5-enrich-20260721T084800Z.log`. sha-yol plans
to actually review/drain the queue later today via the §10 tooling above; this is a deliberate
deferral, not a decision to stop reviewing.

## 2026-07-21 (evening) — mirror-dup fix, pilot ingress, AcoustID retired from conflicts, sheet build

**Duplicates mirrored (a,b)+(b,a) — root-caused and fixed.** sha-yol spotted the same pair
appearing twice in the duplicates tab. Cause: `verdicts._midband_reviews` attached a both-loose
edge to BOTH endpoints, queuing two `possible_duplicate_unclustered` rows that each listed the
other item as neighbour. Fix: attach each edge to its first loose endpoint only
(`verdicts.py`, `break` in the loose-attach loop; pinned by
`test_both_loose_edge_queues_one_review_not_mirrored_pair`). The queue sync is insert-only, so
the live mirrors could not fix themselves: one-time cleanup deleted all 316 open rows (0
resolved — verified before touching) + their `review_exports` entries; the fixed verdicts pass
regenerated 192 canonical rows (263 unique pairs, verified 0 mirrors by unordered-pair SQL).

**av_split learns `neither`** (both copies unusable → both forced loser on every verdicts
re-run; cluster may end up winnerless — operator's call). Decision matching is now
case-insensitive everywhere ('a' == 'A').

**Drive-preview silent-audio trap (major reviewer-facing caveat).** sha-yol flagged item 4122
(`שלמה ארצי/Yareah.avi`, option B in av_split rq526) as "no audio". ffmpeg volumedetect on the
staged copy: mean −20.7 dB, max −0.5 dB — the file's mp3 track is perfectly healthy. Google
Drive's preview transcoder silently drops audio for some avi/mpg (DX50+mp3-in-avi here).
195/292 open av_split items are avi/mpg/mpeg → av_split CSVs now carry `a_type`/`b_type`
columns and INSTRUCTIONS warns: silent avi/mpg preview ⇒ answer `unsure` + note, NOT `neither`.

**AcoustID retired as a conflict tiebreaker (sha-yol's call).** Random sample of 15
`mb_acoustid_conflict` rows corroborated: artist usually right, title frequently a different
song by the same artist at score 1.0 (karaoke backing tracks fingerprint onto wrong
recordings); a minority of conflicts had AcoustID more correct, accepted loss. All 218 open
conflicts bulk-resolved `verdict:"ours"` (resolution note marks the bulk decision; stage5
worklists already exclude verdict='ours' permanently). The 3,511 non-conflict AcoustID hits
from the completed pass are untouched — only conflicts were condemned by the evidence.

**Pilot ingress PASSED.** sha-yol edited the exported CSVs in place (8 decisions incl. Hebrew
reviewer name, fixed_* correction, lowercase a/b, a `3` candidate pick). `review apply
--dry-run` → apply: 8 applied, 0 errors. Verdicts re-run applied both av_split overrides
(4 verdict changes). Their rq526 pick of A (720p) noted: B (4122) is NOT broken, just the Drive
preview — no conflict with the choice, it keeps the sharper copy.

**Re-export (--all-open) after all of the above:** 1,235 rows, 6 tabs (acoustid tab gone),
MBC-01..08 (708 rows — enrichment grew it overnight), DUP-01..06 renumbered fresh (263 pair
rows), AVS-01..04 with type columns, ID3/ODD/RAQ one batch each.

**Sheet delivery — blocked on a one-time auth.** MCP `create_file` requires the payload inline
in the tool call; 224k chars of base64 is transcription-unreliable for a model (Opus subagent
corrupted it twice and correctly refused — a silently corrupt sheet is worse than failure).
Found `rclone` configured on the machine but with `scope = drive.readonly`. Added a separate
`[gdrive-rw]` remote (scope `drive.file` — can only touch files it creates; conf backed up to
rclone.conf.bak-20260721). Waiting on sha-yol to run `rclone config reconnect gdrive-rw:`; then
`rclone copyto <xlsx> "gdrive:Karaokemp Review.xlsx" --drive-import-formats xlsx` converts to a
native Google Sheet server-side. Fallback delivered: `karaokemp-review.xlsx` (6 tabs +
Instructions tab + decision dropdowns) sent to sha-yol directly — drag into Drive → File → Save
as Google Sheets. Workbook builder: scratchpad `build_review_xlsx.py` (openpyxl, installed
--user; drops raw *_url columns — HYPERLINK formulas carry the links).

**New traps:**
- review_queue sync is insert-only: fixing a queue-generation bug does NOT clean existing rows.
- Drive preview can play video without its (healthy) audio — never judge "no audio" from the
  browser preview for avi/mpg; check `ffmpeg -af volumedetect` on the staged copy.
- The `[gdrive]` rclone remote is READ-ONLY by scope; use `[gdrive-rw]` for uploads (once
  authorized). Never re-scope the original remote.

**Next:** sha-yol authorizes gdrive-rw → upload sheet → add dropdown validation is already in the
workbook → share with reviewers. Enrichment continues toward budget 1000; when it stops, plain
`review export` for tail batches, rebuild the workbook, and rclone-update the same sheet file.

## 2026-07-25 — durability: work pushed off-machine, rotating DB backup, catalogue sheet

**Stage 5 text enrichment is DONE.** Run 59 (2026-07-21) finished with `over_budget: false`,
`stopped_reason: null` — the worklist drained on its own, it did not hit the raised 1000 budget.
Final: worklist 7,189 / reviewed 272 / filename_stands 6,917 / unsearchable 168. Metadata coverage
is 28,874 of 28,907 items (99.9%). Nothing has been running since.

**The whole project was living on one 93%-full disk.** `main` was **33 commits ahead of origin** —
every bit of §10/§11 work plus HISTORY.md itself existed only on this machine. sha-yol pushed
(`ecec162..70c9397`). That was the single largest exposure and it cost one command.

**DB backup to Drive, verified.** `.backup` snapshot (never a file copy — see the writer trap
above) → `PRAGMA quick_check` → gzip (449MB → 243MB, ~33s) → `rclone` to
`gdrive-rw:karaokemp-backups/`. Confirmed by md5 identical on both ends, not by exit code.
Automated as `tools/backup_db.sh`: refuses to run while a pipeline stage is live (the
snapshot-restart-forever trap is now enforced, not just documented), rejects a snapshot with
0 media_items (quick_check alone would pass an empty DB), verifies md5 before deleting the local
.gz, keeps newest 3 locally and 3 on Drive. `--dry-run` / `--local-only`.

**Local backups pruned 20 → 3** (6.8G → 1.3G, disk 36G → 41G free). The 17 deleted were all
2026-07-19/20 snapshots, superseded and older than the verified Drive copy.

**Catalogue sheet — the readable view of finished work** (`tools/build_catalogue_xlsx.py`,
uploaded as native Google Sheet `karaokemp-catalogue.xlsx`). Tabs: Summary / Catalogue (24,706
keepers) / Duplicates (3,895 superseded) / Pending (306). Row counts sum to exactly 28,907 active
items, so nothing is lost in the joins; 0 keepers lack a Drive play link. This is the counterpart
to the review workbook: that one collects decisions, this one shows what was decided.

**Two things the catalogue spot-check surfaced (data, not export bugs):**
- The `song_metadata.language` field does **not** hold a language. stage1 fills it from
  `detect_script()` (spec §6.1 "script-detection suffices") — 24,753 rows say `latn`, meaning the
  *filename* used Latin characters. A transliterated Hebrew song reads `latn`. The sheet column is
  therefore labelled **Script**, not Language, with values mapped to words. Don't reintroduce the
  mislabel; and don't use this field to answer "how many Hebrew songs do we have".
- **426 items have neither artist nor title** (plus 1,209 no-artist, 432 no-title). Causes seen:
  bare video-id filenames (`_-t4oEtf3NJi4.mp4`), numeric names (`3 .mp4`), an empty stem
  (`.mp4`), and `Marina's songs/UA53WrfjMnM.mp4.part` — **a truncated download**, worth its own
  look. They sort to the bottom of each tab rather than the top, and are counted under KNOWN GAPS
  on the Summary. The 1,209 no-artist rows are mostly "Artist Title" unsplit in the title field.

**New traps:**
- `gdrive-rw` is `scope = drive.file`: it can only see files it created, and it **cannot grant
  access to anyone**. Sharing the catalogue/backup with camp members is a manual action in the
  Drive UI — rclone cannot do it under this scope, and re-scoping the remote is not worth it.
- A gzipped DB on Drive is disaster recovery, not sharing — nobody can read it without the repo
  and a Python environment. The catalogue Sheet is the artifact humans actually consume.

**Next:** manual review is the critical path — 442/1,827 resolved (24%), and the bulk is untouched:
MBC-01..08 (709 rows, 1 done) and DUP-01..06 (192 rows, 0 done). 237 `pair_mismatch` + 200
`parser_goldset` rows have never been exported to a batch at all. Share both sheets with reviewers.
Re-run `tools/build_catalogue_xlsx.py` + `rclone copyto` to refresh the catalogue as decisions land.

## 2026-07-26 — title-card OCR reshaped as export → apply; the SDK dependency is gone

**The blocker, restated.** `stage5.enrich_worklist` filters `WHERE m.title IS NOT NULL`, so an
item with no title never reaches MusicBrainz, never queues a `metadata_match` review, and never
moves. `count_unsearchable()` has been reporting these every run since the pass first ran and
nothing has ever happened to any of them — by construction, not by accident. Measured read-only
against the live DB today: **171 title-less active winners/sole copies, 168 of them videos with a
local file** — i.e. essentially the whole stranded set is reachable by reading pixels. They are
title-less because §6.1 refused to invent metadata from `SONG-<uuid>.mp4` / `Chapter_NN.avi`
(splitting on the dash manufactures artist='SONG'), which was the right call about FILENAMES and
the wrong conclusion about the FILES: every one of these videos carries an on-screen title card in
the first ~10 seconds, and ffprobe finds nothing in the containers because Google's transcoder
("ISO Media file produced by Google Inc.") stripped the tags.

**The dependency is deleted, and with it `requirements.txt`.** The first cut of this stage called
Claude through the `anthropic` SDK — the only non-stdlib import in ~12,500 lines. sha-yol rejected
it: stage5 does its MusicBrainz/AcoustID HTTP with `urllib`, stage3 shells out to ffmpeg/fpcalc,
the index is sqlite3, and one PyPI package for one pass is not worth breaking that. (It was also
never actually runnable here — pip is PEP-668 managed and `python3.11-venv` is not installed.)
So the OCR moved OUT of the Python process entirely.

**The shape is §10's, not a client's.** `review.py` already established export → external process
→ apply for the volunteer review sheet; this is the same pattern with Claude Code subagents in the
operator's own session as the external process:

* `titlecard extract` writes the WORK PACKET — frames at 2.5/5/9 s to
  `ARTIFACTS_DIR/titlecards/<media_item_id>/t<ts>.jpg` plus one `manifest.jsonl` line per item
  (`media_item_id`, `location_id`, `layout`, `model`, `frames`, `frame_ts`, `remote_path`) and an
  `INSTRUCTIONS.md` carrying the prompt verbatim, exactly as the review export ships its own.
  Frames are derived, disposable data, so they live in §3.9 `artifacts`, never in the repo.
  `--dry-run` classifies and reports the layout histogram and the model split without writing a
  single byte — which is the only thing worth knowing before committing an operator's session.
* `titlecard ingest <results.jsonl>` validates each line and applies it through the **existing**
  `promote()`. Per-item commit, resumable, idempotent: `promote` upserts both the evidence row and
  the `song_metadata` rows, so re-ingesting a file changes nothing but `updated_at`.

`remote_path` is in the manifest as a HUMAN LABEL only — so the operator can eyeball which file a
card belongs to. It must never be shown to the reader. These names are opaque by construction;
letting a reader "help" from the path would reintroduce precisely the invented metadata §6.1
refused to write.

**Model routing is measured, not guessed.** A 6-case eval covering every failure mode this stage
has: **Haiku 4.5 got 5/6.** It read the Karaoke Channel card correctly (title, `IN THE STYLE OF`
performer, year, key); it routed BOTH KaraFun parenthesised names to `writer_credit` rather than
`artist`, including the hard case where the songwriter is also a famous performer of the song; and
it returned `card_found=false` for both a branding bumper and a lyrics frame without transcribing
any lyric text. It failed **only** on the Hebrew card — one letter wrong in the title, and both
credit lines dropped. Sonnet then read two Hebrew cards perfectly, exact titles and both credits.
So the routing rides the colour classifier that was already there:

| layout | what it is | model |
|---|---|---|
| `karaoke_channel_like` (dark) | 62 of 95 measured SONG frames | `haiku` |
| `karafun_like` (purple) | 30 of 95 | `haiku` |
| `unknown` | Hebrew cards (photographic backgrounds) + any unseen producer | `sonnet` |

That is the cheap model on the two Latin layouts it is already perfect on, and the better model
exactly where Haiku measurably failed — which is also, conveniently, where a producer layout we
have never seen would land. `--model-override` forces one model for a whole packet.

**Why `styled_artist` and `writer_credit` are separate columns** (unchanged, and the reason the
strict validation matters): only a Karaoke Channel card's `IN THE STYLE OF <name>` line names a
PERFORMER. The name in parentheses under a KaraFun title is the SONGWRITER — measured: a card
titled "The Shoop Shoop Song (It's In His Kiss)" credits Rudy Clark, who wrote it; the famous
performers are Betty Everett and Cher. Hebrew `מילים:` / `לחן:` are words-by / music-by. Writing an
authorship credit into `artist` would silently mislabel ~60 rows with a name that never performed
the song. The two never share a field, `promote()` only ever reads `styled_artist`, and a test
pins that no `song_metadata` row of any field or source ever carries the writer's name.

**Ingest rejects rather than guesses.** Unknown `media_item_id`, an id not in the packet manifest,
an id not in the current worklist, a bad `producer`/`script` enum, a year that is not a plausible
4-digit year, a `card_found=true` row with no title, a duplicate id in one file — each rejects the
WHOLE line (no half-application) and is reported with a reason and the item it meant. Same
principle as §6.1: this stage exists because we refused to invent metadata, and ingesting a guess
would undo that at the last step.

**Trap worth knowing:** the eligibility check is "in the worklist **or** already has a
`title_cards` row", not just "in the worklist". The first successful ingest of a line is what
gives the item a `title_cards` row and a title, which is exactly what removes it from the
worklist — a plain worklist check would reject every line of a file the moment it succeeded, and
idempotent re-ingest would be impossible. `titlecard_worklist(include_done=True)` exists for the
same reason. Do not "tighten" this.

**Runbook (end to end):**

```
python3 -m karaokemp.cli titlecard extract --dry-run          # layouts + model split, writes nothing
python3 -m karaokemp.cli titlecard extract                    # frames + manifest.jsonl + INSTRUCTIONS.md
#   ... operator session: for each manifest line, hand `frames` + the prompt to a subagent
#   running that line's `model`; append one JSON object per item to results.jsonl ...
python3 -m karaokemp.cli titlecard ingest results.jsonl --dry-run
python3 -m karaokemp.cli titlecard ingest results.jsonl
python3 -m karaokemp.cli enrich                               # the titled items are now searchable
```

`titlecard --retune` (and `titlecard retune`) is unchanged: §11 re-derivation of the promoted rows
from the retained `title_cards` evidence, no re-read, no operator time.

**Suite: 254 passed.** No test touches the network or needs a third-party package; the only
external tool is ffmpeg, and the tests that need it skip themselves loudly when it is absent.

## 2026-07-30 — title-card OCR applied to the live DB: 128 stranded items are searchable

The stage above went from built to **applied**. Migration `002_title_card_ocr_source.sql` is on the
live DB and the OCR results are ingested. Everything below is measured, not projected.

**The OCR run.** 139 items extracted read-only (414 frames; one `.avi`, item 5704, yielded no frames
at all). Read by 8 subagents off a shared prompt — 6 Haiku × ~18 items, 2 Sonnet × 16 — routed by
the colour classifier exactly as the table above specifies. 138 results came back: 138 parsed, no
duplicates, none missing.

The worklist dropped 168 → 139 first, because `non_media` items are now excluded: 32 of the 168
title-less items with a "video" file are not playable media at all — 28 macOS AppleDouble sidecars
(every one exactly 4,096 B of resource fork), two `.part` incomplete downloads, a sidecar `.mpg`,
and a file whose name is the bare extension. ffmpeg extracts nothing from any of them, so a video
filetype alone was never sufficient; stage 1 had already ruled them out and the worklist now says so.

**Migration, verified.** All 207,177 pre-existing rows preserved with every per-source count
byte-identical, `v_metadata` unchanged at 129,237, `quick_check: ok`, no FK violations. The
precedence CASE was renumbered wholesale but every pre-existing relative order is intact, which is
what makes it behaviour-neutral. `spotify_text` stays at 4, above `title_card_ocr` at 3 — the
concurrent Spotify §6.1 order pass was mid-flight throughout and its 1,868 rows were untouched.

**Ingest, measured.**

| | before | after |
|---|---|---|
| `song_metadata` | 207,177 | **207,425** (+248) |
| `v_metadata` winners | 129,237 | **129,480** (+243) |
| `title_cards` | — | 138 rows (128 with a card) |
| unsearchable (no title) | 168 | **43** |

138 applied, 0 rejected. The +248 is 128 titles + 60 artists + 60 years. `v_metadata` gained five
fewer because five OCR **artist** values lost to `musicbrainz_fp` — a fingerprint identifies the
recording and outranks reading a card, which is the precedence order doing its job. All 128 titles
won; those items had no title to compete with, which was the whole point. **All 128 landed inside
the §9.1 worklist and none outside it.**

**The writer-credit trap held in production, not just in tests.** 52 songwriter credits sit in
`title_cards.writer_credit`; zero reached `song_metadata` in any field. Zero non-Karaoke-Channel
rows carry a `styled_artist` or a `year`. Zero `card_found=true` rows lack a title. The ten
`card_found=false` items are exactly the frames showing scrolling lyrics or a branding bumper —
recorded as "no card" with every field null, so no lyric text entered the dataset.

**What is still stranded: 43.** Ten are the no-card items above and one is the no-frames `.avi`.
The remainder were not in the 139: the 168 baseline was measured days earlier and the DB moved
under it (the Spotify pass alone added 1,868 rows and shifted winner/sole status). Treat 43 as
measured-now, not as reconciled against the old baseline.

**Operational note.** `tools/backup_db.sh` guards with `pgrep -f 'karaokemp.*(enrich|stage[0-9])'`,
which does **not** match `tools/spotify_order.py` — a snapshot taken mid-Spotify-pass would not be
refused. It was safe here only because the pass had exited. Worth widening.

Also worth knowing: the script's upload step is `rclone copyto` of a ~242 MB gzip to Drive and can
run 15+ minutes after the local snapshot is already written and verified. Because the whole script
is usually piped to `tail`, its log stays invisible for that entire window and it looks hung. The
local restore point exists and has passed `quick_check` + a `media_items > 0` test long before the
upload finishes; the upload is off-site DR, not the restore point.

**Suite: 254 passed.**

## 2026-08-01/02 — §9.1.2 catalogue enrichment: Wikidata, MB artist index, iTunes, Deezer

sha-yol asked for more enrichment/bootstrapping sources keyed off the filename, for Hebrew items
and for everything else still missing an MBID: MusicBrainz with aliases (Hebrew only), the
iTunes Search API, the Deezer Search API, and Wikidata. **No API keys were needed** — all four
are open endpoints; MB and Wikidata only want a descriptive User-Agent, which config already had.

### The premise correction that reshaped the work

§9.1/§9.1.1 both assumed "MB's coverage of Israeli artists is thin". Measured on evenly-strided
samples (n=40 Hebrew, n=40 Latin, n=25 Hebrew title-only), that is wrong in a specific way:

| population | Wikidata | MB artist | iTunes | Deezer |
|---|---|---|---|---|
| Hebrew | **82.5%** (100% Hebrew script) | **62.5%** (100% Hebrew script) | 5.0% | 2.5% |
| Latin  | — | — | **67.5%** (+year+genre) | 55.0% |

MusicBrainz files Israeli artists under a **Hebrew primary name** with a Latin sort-name
(`שרית חדד` / `Hadad, Sarit`) — the opposite of Spotify/iTunes. Its Hebrew thinness is at the
**recording** level: an `arid:`-scoped recording search using an artist MBID we had *just
confirmed* returned a song_mbid for only 4/25 (16.0%). So Hebrew enrichment buys artist/title
**segmentation** and canonical Hebrew spelling, not identifiers — the accepted outcome
("missing would stay missing" — sha-yol).

### Results, merged into the live index 2026-08-02

| metric | before | after | delta |
|---|---|---|---|
| items with `song_mbid` | 18,992 | **19,651** | +659 |
| items with `year` | 15,520 | **17,850** | +2,330 |
| items with `genre` | 55 | **2,146** | +2,091 |

+659 MBIDs = 218 Hebrew (arid-scoped) + 441 from **re-running `mb_freetext` on iTunes-corrected
text**. Neither iTunes nor Deezer returns an MBID; they close the gap indirectly, and that
follow-on needed no new code — `mb_freetext` only writes rows on accept, so every item it
previously rejected is retried, with different text and therefore a different `mb_cache` key.

Per source: `itunes_text` 2,093 items / 8,372 rows · `deezer_text` 2,154 / 4,308 ·
`musicbrainz_artist` 826 / 2,080 · `wikidata` 576 / 1,312. Hebrew paired: 830/1,427 resolved
(58.2%). Title-only: 94 queued for review, 1 auto-segmented. Review queue 750 → 844 open,
inside the 1,000 budget.

### Three defects the probes caught before they reached the DB

1. **Wrong order from absence of evidence.** Wikidata's "artist matched, other field matched
   nothing" branch declared item 8's *title* to be the artist — Wikidata has a **Serbian** band
   called `הוריקן` and our other field was junk matching nothing. That branch now requires MB to
   independently name the same field. Cost 73.3% → 66.7% resolved; removed a wrong write.
2. **Imported version qualifiers.** `Boston (SC)` matched `Boston (Live from the Grove)` —
   correct for deciding same-song, wrong to store. `es.merge_title` now takes the catalogue's
   *base* title and re-appends *our* qualifier.
3. **Attribution vs segmentation on title-only items.** A Wikidata performer is auto-accepted
   only when its name is a **substring of our own string** (that is segmentation); otherwise it
   is Wikidata's attribution and covers are the norm here, so it goes to review. 20% find a song
   entity, only ~4% clear the substring test.

### Things I got wrong, recorded so they are not re-learned

- **Decorations, twice.** First measured ~1.4% (too narrow a list — missed `ישראלי מזרחי`), then
  over-corrected to "75%" off three unrepresentative examples. Truth: **60 of 628** unresolved
  items (9.6%), matching the earlier ~10% note. The fix recovered **31** items and ran on 2,969
  cache hits vs 88 fetches — §11 retuning from cache works exactly as intended.
- **Deezer vs iTunes.** The n=40 probe said 55% vs 67.5%; the full run said **59.0% vs 57.3%**.
  Deezer still ranks below iTunes (it matched `THE BEATLES | HELP!` to the cover act *Blues
  Beatles*), but "the weaker source" was an overread of a small sample.
- **The merge landed 441 MBIDs short of its own projection** because the tool moved only the
  four NEW sources and the `mb_freetext` re-run writes under the pre-existing
  `musicbrainz_freetext`. Now merged separately with DO NOTHING — it may ADD what the earlier
  run missed, never revise what it decided. Verified additive: 1,717 new rows, 0 contradicting.
- **`INSERT … SELECT … ON CONFLICT` needs a `WHERE` clause** in SQLite or it fails with
  `near "DO": syntax error`. The `enrich_cache` insert was the only one lacking one. The failed
  attempt rolled back atomically — 0 rows written — which is why `with conn:` matters.

### Migration numbering

Written as 005, renumbered to **006** by the concurrent session, which landed its own 005
(songs + ranked copies) first. 005 added the view `v_songs`, which SELECTs FROM `v_metadata`;
since `ALTER TABLE … RENAME` validates every view, 006 must drop **both** views before its
table swap and recreate both — and 005's `v_metadata` exposes a `source_rank` column `v_songs`
depends on. 006 now recreates both verbatim from 005. Verified by replaying
`git show HEAD:schema.sql` → 005 → 006 on a scratch DB.

Ran against a `VACUUM INTO` working copy (`KARAOKEMP_DB` override, new) while the other session
held the live index, then merged with `tools/merge_enrich_work.py`, which refuses unless every
referenced `media_item_id` still exists in live. Pre-migration restore point:
`db/backups/library-20260802T113404Z.sqlite3` (verified ok, 28,907 items).

**Reversible:** `DELETE FROM song_metadata WHERE source IN ('wikidata','musicbrainz_artist','itunes_text','deezer_text');`
`enrich_cache` survives that deletion on purpose (§11).

---

## 2026-08-27 — Stage 6 §9.2 Organize: built, run, and the bug it shipped with

**The library is now organized on disk.** `karaokemp organize` placed 28,786 active items into

    active/<shard>/<Artist> - <Title> [<item_id>]/<Artist> - <Title> [<item_id>].<ext>

50 shard directories, 28,786 item directories, 51,600 files (50,762 renames + 838 hardlinks),
401 GB, staging emptied (256 shard dirs pruned). Zero item failures.

### The two shape decisions (sha-yol, 2026-08-22)

**One directory per COPY, not per song.** 28,786 items across 21,526 songs, so ~7.3k
directories are alternate copies sitting next to the one they duplicate. `[item_id]` is what
tells them apart, and it is the join key to `files.relpath` in the runtime DB — not decoration.

**Shard by the artist's first character, stripping a leading article for the KEY only.**
Measured before deciding: 51 raw buckets, largest T at 3,076 — but 1,627 of those were a
leading "The", i.e. half the English catalogue filed under a word nobody searches by. Stripping
it for the shard key (the displayed name keeps it) moved T to 1,579 and left B largest at 2,415.
Rare buckets are deliberately NOT merged: ו holds 6 items, which costs nothing, while
"first letter, always" is a rule a human can apply without being taught one.

### Hardlinks, and why file_locations grew

742 blobs — every one a `.cdg` — are paired with more than one mp3 (one lyric file, several
backing tracks). A rename can only put a file in one place, so the lowest-numbered item owning
a blob gets the rename and the other 838 placements are `os.link`, each with its own
`file_locations` row (`drive_file_id` NULL). That table is the physical-copy table and its
`content_hash` is documented as deliberately non-unique, so this is the shape it was built for.
Both directories end up independently playable, which is the point: an mp3 without its cdg is
not a karaoke track.

### ⚠ THE BUG — index rows pointing at files that were never created

`fsck` failed straight after the run: **125 missing files**, all under `archive/orphans/`.

Cause, in the orphan loop: the file was moved only `if src.exists()`, but `local_path` was
updated to the destination **unconditionally**. 246 orphan rows were updated; only 121 files
actually existed to move. The other 125 — interrupted-download `.part` files and stray `.mp4`s
whose staged copy was already gone — left the index naming a path nothing had created, and
fsck would have failed on them forever.

**Blast radius, checked before touching anything:** zero of the affected rows are referenced by
any active `media_item`, and all 246 still carry a `drive_file_id`. No song lost a file; nothing
became unrecoverable.

**Fixed two ways.** The code now writes `local_path=NULL, archive_reason='orphan_missing'` in
that branch — the honest statement "there is no local copy", with the Drive provenance left
intact as the way back. The 125 live rows were repaired to match, verified by stat rather than
assumed. `test_orphan_whose_staged_file_vanished_is_not_recorded_at_a_path_it_never_reached`
pins it.

**Lesson worth keeping:** the guard and the write have to sit in the same branch. Writing an
intended destination outside the `if` that creates it is how an index starts describing a
filesystem that does not exist — and `--dry-run` cannot catch it, because in a dry run neither
half executes.

### fsck is the gate, and it works

It caught this within seconds of the run finishing, which is exactly why §9.2 mandates it after
every organize/archive batch. Green afterwards: 51,846 locations checked, 0 missing, 0 size
mismatches, 0 strays, 0 incomplete items.
