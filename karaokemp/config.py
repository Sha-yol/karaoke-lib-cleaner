"""Configuration for the karaokemp library pipeline.

Paths default to ~/karaokemp-library (on /, which has ~597 GB free on `penguin`) and can be
overridden with KARAOKEMP_LIBRARY. The code repo and the media library are deliberately
separate: the library is hundreds of GB and has no business inside a source tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# --- Drive -------------------------------------------------------------------------------

RCLONE_REMOTE = os.environ.get("KARAOKEMP_RCLONE_REMOTE", "gdrive")

# The shared karaoke folder, pinned by stable folder ID rather than reached by navigating
# --drive-shared-with-me paths: it targets the folder exactly and sidesteps path ambiguity.
# The folder belongs to a third party, so no id ships with this repo: set
# KARAOKEMP_DRIVE_FOLDER_ID. Anything that talks to Drive calls require_drive_folder_id() first.
DRIVE_ROOT_FOLDER_ID = os.environ.get("KARAOKEMP_DRIVE_FOLDER_ID", "")


def require_drive_folder_id() -> str:
    if not DRIVE_ROOT_FOLDER_ID:
        raise SystemExit("KARAOKEMP_DRIVE_FOLDER_ID is not set: it pins rclone to the shared "
                         "karaoke folder (the id in its Drive URL).")
    return DRIVE_ROOT_FOLDER_ID


# The shared Drive folder is READ-ONLY (spec §0). The authorized token carries
# scope=drive.readonly, so writes are impossible rather than merely forbidden. Nothing in this
# pipeline needs a broader scope; do not re-authorize with one.
RCLONE_BASE_FLAGS = ["--drive-root-folder-id", DRIVE_ROOT_FOLDER_ID]

# --- Storage layout (§2) -----------------------------------------------------------------

LIBRARY_ROOT = Path(os.environ.get("KARAOKEMP_LIBRARY", Path.home() / "karaokemp-library"))

ACTIVE_DIR = LIBRARY_ROOT / "active"
ARCHIVE_DIR = LIBRARY_ROOT / "archive"
# 'dedup_losers' was removed 2026-07-31 (§7.4 reframe, changelog §14.19). NOTHING is archived
# for being a duplicate any more: only exact-content duplicates are duplicates, and Stage 0
# (§5.2) collapses those before a media_item exists — they never reach an archive directory.
# Lower-ranked copies of a song are `alternate`, stay active, and are ranked rather than moved.
# The directory was empty on the live host when this changed, so nothing was orphaned; Stage 6
# is not written yet, which is exactly why the name had to stop existing before it was.
ARCHIVE_SUBDIRS = ("broken", "orphans", "superseded")
STAGING_DIR = LIBRARY_ROOT / "staging"
ARTIFACTS_DIR = LIBRARY_ROOT / "artifacts"
DB_DIR = LIBRARY_ROOT / "db"
# KARAOKEMP_DB overrides the index path ALONE, leaving every media path pointing at the real
# library. That combination is what makes it safe to run an enrichment pass against a working
# COPY of the index while another session holds the live one: SQLite's WAL permits exactly one
# writer, so two passes committing per item to the same file serialize into lock contention at
# best and a stalled run at worst. Snapshot with `VACUUM INTO` (atomic, consistent, does not
# disturb the source's WAL), point this at the snapshot, and merge the new rows back after —
# every pass writes only its own `song_metadata` source rows, so a merge is an INSERT, not a
# reconciliation.
DB_PATH = Path(os.environ["KARAOKEMP_DB"]) if os.environ.get("KARAOKEMP_DB") else (
    DB_DIR / "library.sqlite3"
)
LOGS_DIR = LIBRARY_ROOT / "logs"
BACKUP_DIR = DB_DIR / "backups"
# Raw rclone enumeration dumps land here: re-parsing must never require re-fetching.
ENUM_DIR = LIBRARY_ROOT / "enumerations"

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

BACKUPS_TO_KEEP = 20  # §13

# --- Thresholds (§11: thresholds are config, so `retune` can re-derive without recompute) ---

REVIEW_QUEUE_BUDGET = 1000  # per kind; exceed => stop and retune rather than grind (§11)
                           # raised 500->700 by sha-yol 2026-07-20 mid-Stage-5; raised again
                           # 700->1000 2026-07-21 after metadata_match hit 701/700 -- sha-yol is
                           # deferring the actual review/drain to later today via the new §10
                           # review tooling, wants the enrich pass to keep running meanwhile.
PRIMARY_FORMAT_PREFERENCE = "video"  # §7.4.1 — confirmed by sha-yol 2026-07-20, see docs/history/HISTORY.md
CLUSTER_AUTO_MERGE = 0.85  # §7.3
CLUSTER_CANDIDATE = 0.65  # §7.3
# §7.3 batched-comparison knobs (see cluster.py). The coarse floor sits between the noise
# band's edge and the review threshold: the benchmark put only 29/60,240 pairs (0.048%) at
# ≥0.6 vs 50,951 in the 0.5 bin — 0.60 projects to ~25k refine candidates over 51M pairs,
# while 0.55 would admit the noise band by the hundreds of thousands.
CLUSTER_COARSE_FLOOR = 0.60
# Persistence floor on the REFINED (full-length, best-offset) similarity of (a)-candidates.
# The coarse prefix reads systematically high — karaoke tracks share low-entropy intros
# (silence, count-ins), so the first-run dry run found 341k coarse candidates vs the
# benchmark's full-length projection of ~25k. A pair whose full-length sim is under 0.60
# failed even its coarse promise and is noise under any plausible retune; (b) title-pair
# edges are exempt (bounded, and low sims there are the "different arrangements" evidence).
CLUSTER_EDGE_FLOOR = 0.60
# §7.4 review floor for QUEUEING audio-evidence review rows (deviation #14, §11-retuned):
# at the spec's 0.65 the queue takes 569 unclustered items alone (budget 500/kind), and the
# benchmark's non-duplicate noise ceiling was 0.678 — 0.65–0.70 is sound-alike noise. Edges
# there stay persisted (retunable); they just don't page a human.
#
# 2026-07-31: this floor now has NO UPPER BOUND. It used to select a mid-band (0.70–0.85),
# because ≥0.85 auto-merged and needed no human. Audio no longer merges anything, so a ≥0.85
# edge between two different songs is the STRONGEST reason to ask — see §7.4's
# `possible_song_merge`.
#
# 2026-08-05 (sha-yol, explicit): raised 0.70 -> 0.985 because at 0.70 this kind projected 1,494
# new rows against a 1,000 budget and `verdicts` had been unable to complete a FULL run since
# 2026-08-02 (run #84 was already over by 336). Every run rolled back, so `possible_song_merge`
# rows have never once reached the queue.
#
# The retune is far more aggressive than it looks, and it has to be. Cross-cluster audio
# similarity here is NOT concentrated at the bottom — it is concentrated at the TOP:
#     [0.70,0.75) 61   [0.75,0.80) 30   [0.80,0.85) 27
#     [0.85,0.90) 35   [0.90,0.95) 300  [0.95,1.00) 722
# 442 cluster pairs sit at similarity exactly 1.0 (identical audio that filename clustering
# split into two songs). So every "normal" retune is useless: 0.90 still projects 1,400 and
# 0.95 still projects 1,186. Only >=0.98 fits at all. Raising this floor therefore discards
# WEAK evidence and keeps strong — the opposite of the usual direction, and the reason the
# number is 0.985 rather than the 0.85 the paragraph above would suggest.
#
# What this gives up, deliberately: ~756 cluster pairs in the 0.70-0.985 band are now never
# queued. They are accepted losses ("we can find them in test runs or at the event"), NOT
# evidence that was disproved — the edges stay persisted and this is retunable per §11, so
# lowering the floor once the queue is drained re-surfaces every one of them.
VERDICT_REVIEW_FLOOR = 0.985
# §7.4.3 av-split policy (sha-yol, 2026-08-06, executive decision).
#
# When a video song's best PICTURE and best AUDIO are different files, §7.4.3 used to ask a
# human which to serve, and parked BOTH copies in `manual_review` until one answered. The
# standing answer is now "always take the higher-quality video", so asking is pure latency:
# every one of the 161 questions ever answered by a reviewer came back 'A' (best picture), and
# the 2026-08-05 merges regenerated 167 more of them.
#
# 'prefer_video'  — no review row, no manual_review; the deterministic rank order already puts
#                   the better picture first (rank_key_video sorts on height before bitrate),
#                   so the policy is implemented by NOT overriding it.
# 'ask'           — the pre-2026-08-06 behaviour: queue a `video_av_split` row per split and
#                   hold both items in manual_review.
#
# An explicit human override still wins either way: `resolved_av_overrides` re-applies any
# RESOLVED video_av_split row every run, so a reviewer who says 'B' (or both/neither) on a
# specific song keeps that answer regardless of this policy.
AV_SPLIT_POLICY = "prefer_video"
# §8 decode classification (deviation #15): decode-side error lines a file may emit and
# still count as playable (decoded_ok + glitches recorded). Measured: one bad mp3 frame or
# one damaged macroblock = 2 lines; pervasive corruption = hundreds. Null-muxer lines are
# excluded before counting — they say nothing about the input.
DECODE_GLITCH_TOLERANCE_LINES = 50
CLUSTER_COARSE_WORDS = 512      # uint32 words of fp prefix compared in the batched pass (~66s)
CLUSTER_DURATION_BLOCK_SEC = 10.0  # §7.3(a) duration blocking half-window

# --- §7.3(b) name identity: filename-first clustering (2026-07-31 reframe) -----------------
# Measured on the live DB before the reframe: 5,723 item pairs share an identical normalized
# filename-derived artist+title. Their fingerprint similarity is BIMODAL with an empty middle:
# 43.2% ≥0.85, 1.3% in 0.70–0.85, 2.7% in 0.65–0.70, 43.2% pinned at the 0.60–0.65 noise
# floor, 9.7% never compared at all. 56.8% of them were therefore split, and 3,572 items were
# crowned `sole_copy` while an exact filename twin sat elsewhere in the library.
#
# The failure is RECALL and it is not reachable by lowering CLUSTER_AUTO_MERGE: dropping to
# 0.65 would recover ~2.7% of these pairs while admitting the noise band (the 1k benchmark put
# ~51k unrelated pairs in the 0.5 bin alone). The knob is the wrong instrument — see §11.
#
# The reframe: the NAME says which SONG this is. (This block records the FIRST reframe of
# 2026-07-31, which kept audio as a second merge key and called the recordings inside a
# cluster "arrangements". The SECOND reframe below supersedes that: audio no longer merges
# anything, and §7.4 ranks the copies instead of crowning one per arrangement. The numbers
# above are what motivated both and are still the reason the name key exists at all.)
CLUSTER_NAME_MERGE = True          # §7.3(b) name identity may auto-merge (was: never)
CLUSTER_NAME_MIN_TOKENS = 2        # a one-word artist+title carries too little to key on
# Group-size guards. The old MAX_TITLE_GROUP=30 SKIPPED oversize groups entirely — no edges,
# no evidence, and part of why 553 twin pairs were never compared. Now the two effects are
# separated: a large group still produces `title_match` edges (auditable, retunable), it just
# does not auto-merge, because at that size the shared key is far more likely to be a junk
# template value we have not yet blacklisted than 13 copies of one song.
CLUSTER_NAME_GROUP_MAX_MERGE = 12  # largest real group measured is 7
CLUSTER_NAME_GROUP_MAX_EDGES = 60  # beyond this the key is junk; n² pairs buy nothing
# Containment: one side's tokens being a proper subset of the other's ("Pink" vs
# "Pink & Nate Ruess", an appended transliteration, a dropped middle initial). Measured on the
# live DB: the order-insensitive key alone finds 2,119 merge groups vs 1,989 for the ordered
# key; containment is what carries the featured-artist and transliteration supersets.
CLUSTER_NAME_CONTAINMENT = True
CLUSTER_NAME_CONTAINMENT_MIN_TOKENS = 3   # 2-token subsets are mostly bare titles: too weak
CLUSTER_NAME_CONTAINMENT_RATIO = 0.6      # |subset| / |superset|: the shared part must dominate
CLUSTER_NAME_CONTAINMENT_MAX_POSTINGS = 400  # rarest-token posting list cap (candidate gen)

# --- §7.3 name-ONLY clustering (2026-07-31, second reframe — supersedes the arrangement
# --- model of §14.18 the same day) ---------------------------------------------------------
# Fingerprint and prefix_fingerprint edges no longer take part in the union-find at all. They
# are still computed and still persisted (§3.5), and they still do two jobs — cross-song
# review evidence, and the no-name fallback below — but they do not decide what a cluster IS.
#
# The reason is structural, not a threshold: an audio edge merges items regardless of what
# they are called, so a cluster built partly from audio has NO WELL-DEFINED NAME, and a song
# that cannot be named cannot be searched. Measured under the arrangement model: 2,424
# clusters held more than one distinct artist+title string, against 41 names split across
# clusters. The audio key bought precision this corpus did not need and cost the coherence it
# did. See also §11.1 — the similarity distribution is bimodal, so no threshold recovers this.
#
# Items with NO usable name have no key at all, so audio regroups them: nameless<->nameless
# strong edges union freely, and a nameless component adopts a named song only when its strong
# edges point at exactly ONE. Ambiguity leaves it standalone (reported, never guessed) — that
# constraint is what stops a nameless item bridging two songs together.
CLUSTER_NONAME_FALLBACK = True
# §7.2.4 CDG pair-coherence check, retuned per §11 from the measured delta distribution
# (2026-07-19, all 22,854 pairs; see PROGRESS). The spec's symmetric ±3s failed 1,661 pairs —
# but sampling both tails showed every one was a same-basename (correct) pair, and fpcalc
# confirmed the probed durations are real. The asymmetry is mechanistic, not cosmetic:
#   * graphics ending EARLY is normal encoding behavior — the cdg stream stops at the last lyric
#     while the audio outro plays (92% of pairs are within ±1s; the benign tail reaches ~30s);
#   * graphics OUTLIVING the audio is never right — wrong pair or truncated mp3.
CDG_TOLERANCE_GRAPHICS_LONGER_SEC = 3.0    # spec's bound, kept where it is meaningful
CDG_TOLERANCE_GRAPHICS_SHORTER_SEC = 30.0  # beyond this: whole-side recordings, dead air — review

# --- Stage 5 §9.1 enrichment (MusicBrainz / AcoustID) --------------------------------------

MB_API_ROOT = "https://musicbrainz.org/ws/2"
# MB requires a descriptive User-Agent with contact info; anonymous UAs get throttled hard.
# Set KARAOKEMP_MB_CONTACT (an email or URL); a network fetch refuses to run without it.
MB_CONTACT = os.environ.get("KARAOKEMP_MB_CONTACT", "")


def mb_user_agent() -> str:
    if not MB_CONTACT:
        raise SystemExit("KARAOKEMP_MB_CONTACT is not set: MusicBrainz requires contact info "
                         "in the User-Agent.")
    return f"karaokemp-lib-cleanup/0.1 ( {MB_CONTACT} )"
MB_RATE_LIMIT_SEC = 1.1     # MB asks for 1 req/s; a margin keeps 503s rare
MB_SEARCH_LIMIT = 8         # candidates fetched per query (top-3 go to review payloads)
# §9.1 acceptance bands. All retunable from mb_cache without refetching (§11).
MB_AUTO_ACCEPT_SCORE = 92        # high band: auto-accept needs score ≥ this AND both fields agreeing
MB_REVIEW_SCORE = 80             # medium band: queue metadata_match with top-3 candidates
MB_TITLE_ONLY_REVIEW_SCORE = 95  # title-only items can never show both-field agreement; only a
                                 # near-perfect single-field match is worth an operator's time
MB_FIELD_AGREEMENT = 0.85        # per-field normalized-similarity floor for auto-accept

# §6.1/§6.2 LLM filename parsing (tools/llm_parse.py, migration 007).
# The agent's own confidence a parse must carry before `promote` writes it into song_metadata.
#
# MEASURED on the auto-scored swap arm (n=200, claude-sonnet-5, prompt v1, 2026-08-11 —
# `extract --goldset-swaps`, scored against a key held out of the batch file):
#
#     overall            195/200 (97.5%) both fields correct
#     still-swapped        0/200          <- what this arm exists to test
#     floor 0.75         188 promoted, 98.4% correct, 12 withheld
#     floor 0.65         198 promoted, 98.0% correct,  2 withheld
#
# 0.65 (sha-yol's call, 2026-08-11): the 0.75 cut was withholding 10 items that were CORRECT
# against 2 that were not. They cluster at exactly 0.70 and share a shape — Hebrew performers
# the model did not recognise by name but placed correctly from the folder's naming
# convention. That is precisely the population this source exists to serve, so 0.75 spent
# recall where the model was right. Dropping to 0.65 buys those 10 back for 0.4 points of
# precision and still withholds the two lowest calls, one of which is the arm's single
# genuine error (a parent folder that named someone other than the performer, self-flagged
# at 0.50).
#
# CAVEAT, and why this is not yet the final number: the swap arm only covers stems some
# catalogue had already answered. The ~4,800-stem main workset — stems NOTHING answered — is
# measured by the stratified `--goldset` arm, not this one. Re-check this floor against that
# arm before treating it as settled.
#
# Retuning NEVER costs a re-run: every parse is kept verbatim in `llm_parses`, floor and all,
# so acceptance is re-decided offline (§11 — the lesson that cost ~23h of Spotify downtime).
LLM_PARSE_FLOOR = 0.65

ACOUSTID_API_ROOT = "https://api.acoustid.org/v2/lookup"
# AcoustID needs a registered application key (https://acoustid.org/new-application).
# Empty ⇒ the acoustid pass reports itself blocked instead of running.
ACOUSTID_API_KEY = os.environ.get("KARAOKEMP_ACOUSTID_KEY", "")
ACOUSTID_RATE_LIMIT_SEC = 0.4    # AcoustID allows 3 req/s per application
ACOUSTID_MIN_SCORE = 0.9         # acoustic-identity claims below this are noise, not identity


def ensure_layout() -> None:
    """Create the §2 storage layout. Idempotent."""
    for d in (ACTIVE_DIR, STAGING_DIR, ARTIFACTS_DIR, DB_DIR, LOGS_DIR, BACKUP_DIR, ENUM_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for sub in ARCHIVE_SUBDIRS:
        (ARCHIVE_DIR / sub).mkdir(parents=True, exist_ok=True)


def _version_of(binary: str, *args: str) -> str:
    """First line of `binary args`, or 'MISSING'. Never raises — this is for provenance."""
    if shutil.which(binary) is None:
        return "MISSING"
    try:
        out = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=30, check=False
        )
        first = (out.stdout or out.stderr).strip().splitlines()
        return first[0] if first else "unknown"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"error: {exc}"


def tool_versions() -> dict[str, str]:
    """Tool provenance for pipeline_runs.tool_versions (§3.8)."""
    import sqlite3
    import sys

    return {
        "python": sys.version.split()[0],
        "sqlite3": sqlite3.sqlite_version,
        "rclone": _version_of("rclone", "version"),
        "ffmpeg": _version_of("ffmpeg", "-version"),
        "fpcalc": _version_of("fpcalc", "-version"),
    }
