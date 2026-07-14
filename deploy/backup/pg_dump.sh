#!/usr/bin/env bash
# Nightly pg_dump stopgap (CR-008) — run by mes-backup.timer / mes-backup.service.
# Full offsite + WAL archiving is deferred (P4-07); this is local-only, second
# directory, keep last 14 days. DATABASE_URL comes from /etc/mes/.env.
set -euo pipefail

BACKUP_DIR="${MES_BACKUP_DIR:-/var/backups/mes}"
KEEP_DAYS="${MES_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="${BACKUP_DIR}/mes-${STAMP}.sql.gz"

: "${DATABASE_URL:?DATABASE_URL must be set (via /etc/mes/.env)}"

mkdir -p "${BACKUP_DIR}"

pg_dump "${DATABASE_URL}" | gzip > "${OUT_FILE}.tmp"
mv "${OUT_FILE}.tmp" "${OUT_FILE}"

# prune anything older than KEEP_DAYS
find "${BACKUP_DIR}" -maxdepth 1 -name 'mes-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete

echo "mes backup: wrote ${OUT_FILE}"
