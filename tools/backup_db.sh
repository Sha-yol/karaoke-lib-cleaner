#!/usr/bin/env bash
# Snapshot the library DB, verify it, keep the last KEEP locally and REMOTE_KEEP on Drive.
#
# Safe to run unattended. Everything it deletes is matched against the backup naming pattern
# inside the backup directory only; the live DB lives one level up and is never a candidate.
#
#   tools/backup_db.sh              # snapshot, verify, upload, prune
#   tools/backup_db.sh --local-only # skip Drive (no network)
#   tools/backup_db.sh --dry-run    # show what would happen, touch nothing
set -euo pipefail

# Local retention MUST match config.BACKUPS_TO_KEEP (§13): both rotate the same directory, so
# the tighter number silently wins. It used to be 3 here, which quietly pruned pre-stage restore
# points that §13 intended to keep. tests/test_backup_retention.py pins the two together.
KEEP="${KEEP:-20}"
# Drive keeps fewer — it is off-site disaster recovery, not the restore-point history, and 20
# compressed snapshots would be ~4.6 GB of quota.
REMOTE_KEEP="${REMOTE_KEEP:-3}"
LIB="${KARAOKEMP_LIBRARY:-$HOME/karaokemp-library}"
DB="$LIB/db/library.sqlite3"
BACKUP_DIR="$LIB/db/backups"
REMOTE="${KARAOKEMP_BACKUP_REMOTE:-gdrive-rw:karaokemp-backups}"

LOCAL_ONLY=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --local-only) LOCAL_ONLY=1 ;;
        --dry-run)    DRY_RUN=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
run()  { if (( DRY_RUN )); then echo "  would run: $*"; else "$@"; fi; }

[[ -f "$DB" ]] || { echo "no database at $DB" >&2; exit 1; }

# A snapshot taken while stage5 is writing makes sqlite restart the backup loop indefinitely
# (observed: 3min+ on a 417MB DB before it was killed). Bail out rather than thrash.
if pgrep -f 'karaokemp.*(enrich|stage[0-9])' >/dev/null 2>&1; then
    echo "a pipeline stage is running — refusing to snapshot (it would restart forever)" >&2
    echo "re-run when it finishes, or use the newest existing backup" >&2
    exit 1
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
SNAPSHOT="$BACKUP_DIR/library-$TS.sqlite3"
mkdir -p "$BACKUP_DIR"

log "snapshotting -> $(basename "$SNAPSHOT")"
run sqlite3 "$DB" ".backup '$SNAPSHOT'"

if (( ! DRY_RUN )); then
    log "verifying"
    check="$(sqlite3 "$SNAPSHOT" 'PRAGMA quick_check;')"
    if [[ "$check" != "ok" ]]; then
        echo "integrity check FAILED: $check — keeping the bad file for inspection" >&2
        exit 1
    fi
    # A structurally valid but empty database would still pass quick_check.
    items="$(sqlite3 "$SNAPSHOT" 'SELECT COUNT(*) FROM media_items;')"
    (( items > 0 )) || { echo "snapshot has 0 media_items — refusing to trust it" >&2; exit 1; }
    log "ok — $items media items, $(du -h "$SNAPSHOT" | cut -f1)"
fi

if (( ! LOCAL_ONLY )); then
    GZ="$SNAPSHOT.gz"
    log "compressing"
    if (( DRY_RUN )); then
        echo "  would run: gzip -6 -c $SNAPSHOT > $GZ"
    else
        gzip -6 -c "$SNAPSHOT" > "$GZ"
    fi

    log "uploading to $REMOTE/"
    run rclone copyto "$GZ" "$REMOTE/$(basename "$GZ")" --stats=30s --stats-one-line

    if (( ! DRY_RUN )); then
        # Trust it only if Drive's own md5 matches ours, then drop the local .gz — the
        # uncompressed snapshot is the local copy of record.
        local_md5="$(md5sum "$GZ" | cut -d' ' -f1)"
        remote_md5="$(rclone md5sum "$REMOTE/$(basename "$GZ")" | cut -d' ' -f1)"
        if [[ "$local_md5" != "$remote_md5" ]]; then
            echo "md5 mismatch after upload (local $local_md5, remote $remote_md5)" >&2
            exit 1
        fi
        log "upload verified ($local_md5)"
        rm -f "$GZ"
    fi

    log "pruning Drive to newest $REMOTE_KEEP"
    # lsf sorts lexically, which for this ISO-8601 naming is also chronological.
    # `|| true` because grep exits 1 when there is nothing to prune, and pipefail would
    # treat a clean no-op as a failed backup.
    { rclone lsf "$REMOTE/" 2>/dev/null | grep -E '^library-.*\.sqlite3\.gz$' || true; } \
      | sort -r | tail -n +$((REMOTE_KEEP + 1)) \
      | while read -r old; do
            log "  deleting remote $old"
            run rclone deletefile "$REMOTE/$old"
        done
fi

log "pruning local to newest $KEEP"
find "$BACKUP_DIR" -maxdepth 1 -name 'library-*.sqlite3' -printf '%f\n' | sort -r | tail -n +$((KEEP + 1)) \
  | while read -r old; do
        log "  deleting local $old"
        run rm -f -- "$BACKUP_DIR/$old"
    done

log "done"
(( DRY_RUN )) || { echo; echo "local:"; ls -1t "$BACKUP_DIR"/library-*.sqlite3 | xargs -n1 basename; }
