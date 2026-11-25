#!/usr/bin/env bash
# Daily sqlite backup: keep last 7 copies. Usage: sudo bash backup.sh
set -euo pipefail
DB="${SQA_DB:-/srv/sqa/server/data/sqa.db}"
DEST="${SQA_BACKUP_DIR:-/srv/backups}"
KEEP="${SQA_KEEP:-7}"
[ -f "$DB" ] || { echo "no db at $DB"; exit 1; }
mkdir -p "$DEST"
STAMP="$(date +%F_%H%M)"
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$DEST/sqa-$STAMP.db'"
else
  cp -f "$DB" "$DEST/sqa-$STAMP.db"
fi
gzip -f "$DEST/sqa-$STAMP.db"
ls -1t "$DEST"/sqa-*.db.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup ok: $DEST/sqa-$STAMP.db.gz"
ls -lh "$DEST" | tail -n +2
