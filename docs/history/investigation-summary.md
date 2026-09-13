# Karaoke Library Investigation Summary

Investigation of the shared Google Drive folder mounted locally at:
`~/karaokemp-song-lib` → `/mnt/chromeos/GoogleDrive/SharedWithMe/<shared-folder>/`
(originally shared via a Drive folder link owned by a third party)

Date of investigation: 2026-07-13/14.

## Method note

Initial approach used the Google Drive API (via MCP connector), recursing folder-by-folder
with one API call per folder. This proved extremely wasteful — each folder listing had to
round-trip through the agent's context — and was abandoned partway through (after crawling
~19 top-level folders, 704 files, ~12.8 GB) once the tree's true scale became apparent.

Switched to using the user's local ChromeOS FUSE mount of the same Drive folder, which
allows direct filesystem traversal (`find`, `stat`, `python3` reading raw bytes) without any
API round-trips. All numbers below (except where explicitly marked as coming from the web)
were produced this way, run directly against the live mount.

## Size and file counts — measured directly from the filesystem

- **Total: 53,674 files, ~438.4 GB** (470,715,610,427 bytes), across 732 directories.
- Directory listing (readdir) is fast (~1.5 min for the full tree) and does not trigger
  per-file network fetches. Summing file sizes via `find -printf '%s'` took ~2 min total,
  suggesting size metadata is available cheaply alongside directory entries on this mount.

### File type breakdown (by extension, case-insensitive)

| Type | Extensions | Count |
|---|---|---|
| Audio | `.mp3` | 23,356 |
| Karaoke graphics (paired with mp3) | `.cdg` | 23,275 |
| Video | `.mp4` (3,701), `.avi` (2,060), `.mpg` (750), `.vob` (238), `.mpeg` (43), `.wmv` (28), `.dat` (104, VCD-style streams), `.mkv` (1) | 6,925 |
| Other | images, docs, db/ini files, misc | ~500 |

`.cdg` files are raw CD+Graphics subcode data (24-byte packs: command byte, instruction byte,
4 bytes Q-parity, 16 bytes graphics data, 4 bytes P-parity) — not human-readable text, and not
audio or video content themselves; they're the lyrics/graphics overlay track that a karaoke
player renders in sync with the paired `.mp3`.

## ID3 metadata — measured directly (raw byte inspection, no library dependencies available on host)

- Tagging is **inconsistent** across the library and appears to depend on which source batch
  a file came from.
- Random sample of 15 mp3s across the tree: 6 had ID3v2 headers, 9 had legacy ID3v1 trailers,
  0 had neither.
- However, an entire 18-track batch (`DK26-01` through `DK26-18`, in `Unorginized/`)
  had **no ID3 tag at all** (no `ID3` header, no `TAG` trailer) — metadata for that batch lives
  only in the filename.
- Files from the `SF001`–`SF339` batch do carry ID3v2 headers, but
  the one inspected in detail only contained a `PRIV` frame (a Windows Media Player
  `MediaClassPrimaryID` GUID) — no readable `TIT2`/`TPE1`/`TALB` text frames. So even
  "tagged" files in this library aren't reliably carrying human-readable metadata; the
  filename remains the actual source of truth throughout.

