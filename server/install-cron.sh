#!/usr/bin/env bash
# Install daily 03:20 backup cron entry. Usage: sudo bash install-cron.sh
set -euo pipefail
LINE="20 3 * * * root bash /srv/sqa/server/backup.sh >> /var/log/sqa-backup.log 2>&1"
echo "$LINE" > /etc/cron.d/sqa-backup
chmod 644 /etc/cron.d/sqa-backup
systemctl reload cron 2>/dev/null || service cron reload 2>/dev/null || true
echo "installed: /etc/cron.d/sqa-backup"
cat /etc/cron.d/sqa-backup
