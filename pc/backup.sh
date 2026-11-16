#!/usr/bin/env bash
# Linux/macOS: fetches a fresh backup over SSH and keeps the newest N.
#   ./backup.sh myserver [keep=10] [dir=.]
# Schedule it with cron, e.g.:  0 20 * * *  /path/to/backup.sh myserver 10 ~/mc-backups
set -euo pipefail
SERVER=${1:?usage: backup.sh <ssh-host> [keep] [dir]}
KEEP=${2:-10}
DIR=${3:-.}
mkdir -p "$DIR"
NAME="mc-$(date +%Y-%m-%d_%H%M).tar.zst"
log() { echo "$(date '+%F %R')  $*" | tee -a "$DIR/backup.log"; }

log "Fetching backup from $SERVER ..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=20 "$SERVER" sudo -n mc-backup pc > "$DIR/$NAME.part"; then
  rm -f "$DIR/$NAME.part"; log "ERROR: backup failed"; exit 1
fi
expected=$(ssh -o BatchMode=yes "$SERVER" cat /run/mc-backup.sha256)
actual=$( (sha256sum 2>/dev/null || shasum -a 256) < "$DIR/$NAME.part" | cut -c1-64)
if [ "$expected" != "$actual" ]; then
  rm -f "$DIR/$NAME.part"; log "ERROR: checksum mismatch"; exit 1
fi
mv "$DIR/$NAME.part" "$DIR/$NAME"
log "OK: $NAME ($(du -h "$DIR/$NAME" | cut -f1), checksum verified)"
ls -1 "$DIR"/mc-*.tar.zst | sort -r | tail -n +$((KEEP+1)) | while read -r f; do
  log "Deleting old backup: $(basename "$f")"; rm -f "$f"
done
