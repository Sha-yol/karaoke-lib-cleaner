# lib-cleaner

The pipeline that cleaned up and organized the **Karaokemp** karaoke library. Karaokemp is an
Israeli Burning Man karaoke camp. The library it inherited was about 438 GB and 53,674 files:
karaoke videos, MP3+CDG pairs, and a lot of
Hebrew material, all with inconsistent or missing tags.

The pipeline turned that into a catalogue of **21,526 songs, 28,786 playable versions and
51,600 files**. 83% of the songs carry a MusicBrainz id; the rest are mostly Hebrew, where
MusicBrainz has the artists but not the recordings. It exported the catalogue as a small
four-table runtime DB that the karaoke player runs against.

> **This repo is an archive.** Cutover has happened: the runtime DB is authoritative, and new
> material enters through runtime ingest ([`docs/runtime-db-spec.md`](docs/runtime-db-spec.md)
> §5.5), not by re-running this pipeline. The code is published to show how the catalogue
> was built. No media, no databases and no API caches are included.

## How it works

Two databases, deliberately separate:

- **Pipeline index** (`schema.sql`, SQLite, about 580 MB when the build finished). This holds
  every piece of evidence: file locations, parses, fingerprints, cached MusicBrainz, catalogue
  and LLM responses, a per-source trust ladder (`v_metadata`) and the review queue. Answers
  are *derived* from evidence, so re-tuning a threshold never means re-fetching.
- **Runtime DB** ([`docs/runtime-db-spec.md`](docs/runtime-db-spec.md)). Four flat tables
  (`songs`, `versions`, `files`, `issues`) with winners only and no pipeline vocabulary.
  Produced by `tools/export_runtime.py` in about 80 seconds.

The build ran in the stages below, all driven by `python -m karaokemp.cli <command>` unless a
tool is named.

| # | step | where | commands |
|---|---|---|---|
| 0 | Inventory the Drive folder; exact-dup pass on Drive md5 | `stage0.py` | `enumerate`, `dedup`, `report` |
| 1 | Deterministic filename/path parser; MP3+CDG pairing at content level | `stage1.py` | `parse-stats`, `parse`, `pair`, `resolve-pair` |
| 1b | LLM segmentation of the paths the regex parser could not resolve | `tools/llm_parse.py`, `tools/llm_batch.py` | `extract --sweep` → batch → `ingest` → `promote` |
| 2 | Download by Drive file id, verify-then-commit | `stage2.py` | `download` |
| 3 | SHA-256, ffprobe, media items, ID3, chromaprint; cluster versions into songs; rank copies | `stage3.py`, `cluster.py`, `verdicts.py` | `hash`, `probe`, `items`, `id3`, `fingerprint`, `cluster`, `verdicts` |
| 4 | Full ffmpeg decode of every copy that would be served | `stage4.py` | `decode --until-stable` |
| 5 | Enrichment: MusicBrainz (qualified + free text), AcoustID, Wikidata, MB artist index, iTunes, Deezer, Spotify order fixes, title-card OCR | `stage5.py`, `enrich_sources.py`, `titlecard.py`, `tools/mb_freetext.py`, `tools/catalogue_enrich.py`, `tools/heb_enrich.py` | `enrich`, `acoustid`, `titlecard` |
| 6 | Organize into `active/<shard>/<Artist> - <Title> [id]/`; fsck | `stage6.py` | `organize`, `fsck` |
| → | Export the runtime DB | `tools/export_runtime.py` | `--out runtime.db` |

The design choices that matter most, each measured against the real library (details in
[`docs/history/HISTORY.md`](docs/history/HISTORY.md)):

- **Content identity is separate from location.** `blobs` key on SHA-256; `file_locations`
  are copies. An MP3+CDG pair is a content-level fact, so a directory split can't break it.
- **The filename is the source of truth.** ID3 tags in this library are truncated or absent,
  so `id3` ranks below `filename`. Fingerprint-derived metadata also ranks below `filename`:
  a karaoke backing track fingerprints onto *other* karaoke recordings, and MusicBrainz then
  names the cover act.
- **A cluster is a song.** Copies of a song are ranked *within a format* (video vs mp3g), and
  nothing is archived for being second best. The player picks the format.
- **Every stage is idempotent and gated.** A sampled benchmark or dry run was reviewed before
  each full run, and every paid answer is cached in the index.
- **Hebrew stays Hebrew.** Native script, never transliterated. Artist/title order is
  resolved from catalogues, because the filenames carry no reliable cue.

## Layout

```
karaokemp/          the pipeline package (config, db, stage0-6, cluster, verdicts, enrichment, CLI)
tools/              standalone passes run against the index, plus export_runtime and backup_db.sh
tests/              invariant tests; many pin a specific trap found in the real data
schema.sql          the pipeline index schema (migrations 001-008 folded in)
docs/runtime-db-spec.md       the runtime DB contract
docs/history/       the build log, the original spec, the investigation, the simplification plan
```

## Running it

Python 3.11+, plus `numpy` and `mutagen` (the build host used the distro packages). There is
deliberately no `requirements.txt`; a test pins that. The external tools are `rclone` (with a
Drive remote), `ffmpeg`/`ffprobe` and `fpcalc` (chromaprint).

```sh
python -m venv .venv && . .venv/bin/activate
pip install numpy mutagen pytest
PYTHONPATH=. pytest -q tests
```

The tests need no network, media or external tools. They were developed on Linux. On Windows,
set `PYTHONUTF8=1`; many tests still fail there during temp-directory cleanup (SQLite files
are still open when the directory is deleted).

Configuration comes from the environment:

| variable | used for |
|---|---|
| `KARAOKEMP_LIBRARY` | library root (default `~/karaokemp-library`): `staging/`, `active/`, `archive/`, `db/`, `logs/` |
| `KARAOKEMP_DB` | override the index path |
| `KARAOKEMP_DRIVE_FOLDER_ID` | **required for Stages 0 and 2**: the shared Drive folder to enumerate |
| `KARAOKEMP_RCLONE_REMOTE` | rclone remote name (default `gdrive`) |
| `KARAOKEMP_MB_CONTACT` | **required for any network enrichment**: contact info for the MusicBrainz User-Agent |
| `KARAOKEMP_ACOUSTID_KEY` | AcoustID lookups |
| `ANTHROPIC_API_KEY` | `tools/llm_batch.py` only (`pip install anthropic`) |

## History

- [`docs/history/HISTORY.md`](docs/history/HISTORY.md): the day-by-day build log, frozen at
  cutover. It records every deviation from the spec, every retune and why, and the traps the
  tests pin down.
- [`docs/history/original-spec-v2.md`](docs/history/original-spec-v2.md): the spec the build
  started from. Parts were superseded, and the log says where.
- [`docs/history/investigation-summary.md`](docs/history/investigation-summary.md): the first
  survey of the library.
- [`docs/history/simplification-plan.md`](docs/history/simplification-plan.md): the
  post-build reduction plan and its outcome.

## License

MIT. See [`LICENSE`](LICENSE).
