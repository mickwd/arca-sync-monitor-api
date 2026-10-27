#!/usr/bin/env bash
# Backup de la base y la configuración de arca-sync-monitor-api a S3 (Linux / EC2).
#
# Uso:   ./backup.sh            (toma la configuración de /etc/arca-sync/backup.env si existe)
# Cron:  ver scripts/crontab.example
#
# Variables:
#   BACKUP_SOURCES     directorios a respaldar (separados por espacio)
#   BACKUP_DIR         directorio local donde se dejan los .tar.gz
#   S3_BUCKET          bucket destino (sin s3://)
#   S3_PREFIX          prefijo dentro del bucket
#   LOCAL_RETENTION    días que se conservan los backups locales
#   S3_RETENTION       días que se conservan en S3
#   PG_DSN             si está definida se hace pg_dump de Postgres antes de comprimir

set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/arca-sync/backup.env}"
# shellcheck disable=SC1090
[[ -f "$CONFIG_FILE" ]] && source "$CONFIG_FILE"

BACKUP_SOURCES="${BACKUP_SOURCES:-/opt/arca-sync/data /opt/arca-sync/config}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/arca-sync}"
S3_BUCKET="${S3_BUCKET:?Definir S3_BUCKET}"
S3_PREFIX="${S3_PREFIX:-backups/arca-sync}"
LOCAL_RETENTION="${LOCAL_RETENTION:-7}"
S3_RETENTION="${S3_RETENTION:-30}"
HOSTNAME_SHORT="$(hostname -s)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/${HOSTNAME_SHORT}-${TIMESTAMP}.tar.gz"
LOCK_FILE="/var/lock/arca-sync-backup.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] $*"; }

# Evita dos backups simultáneos si el anterior quedó colgado
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Ya hay un backup en curso, se omite"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

if [[ -n "${PG_DSN:-}" ]]; then
  log "Generando pg_dump"
  pg_dump --format=custom --no-owner "$PG_DSN" > "${WORK_DIR}/postgres.dump"
fi

log "Comprimiendo: ${BACKUP_SOURCES}"
TAR_ARGS=()
for src in $BACKUP_SOURCES; do
  [[ -e "$src" ]] && TAR_ARGS+=("-C" "/" "${src#/}") || log "Aviso: no existe ${src}, se omite"
done
[[ -f "${WORK_DIR}/postgres.dump" ]] && TAR_ARGS+=("-C" "$WORK_DIR" "postgres.dump")
if [[ ${#TAR_ARGS[@]} -eq 0 ]]; then
  log "Nada para respaldar"
  exit 1
fi
tar -czf "$ARCHIVE" "${TAR_ARGS[@]}"

sha256sum "$ARCHIVE" > "${ARCHIVE}.sha256"
SIZE="$(du -h "$ARCHIVE" | cut -f1)"
log "Archivo generado: ${ARCHIVE} (${SIZE})"

DEST="s3://${S3_BUCKET}/${S3_PREFIX}/${HOSTNAME_SHORT}/"
log "Subiendo a ${DEST}"
aws s3 cp "$ARCHIVE" "$DEST" --sse AES256 --storage-class STANDARD_IA --only-show-errors
aws s3 cp "${ARCHIVE}.sha256" "$DEST" --only-show-errors

log "Rotación local: se borran archivos con más de ${LOCAL_RETENTION} días"
find "$BACKUP_DIR" -name "${HOSTNAME_SHORT}-*.tar.gz*" -mtime +"$LOCAL_RETENTION" -print -delete

log "Rotación en S3: se borran objetos con más de ${S3_RETENTION} días"
CUTOFF="$(date -d "-${S3_RETENTION} days" +%Y-%m-%d)"
aws s3api list-objects-v2 \
  --bucket "$S3_BUCKET" \
  --prefix "${S3_PREFIX}/${HOSTNAME_SHORT}/" \
  --query "Contents[?LastModified<'${CUTOFF}'].Key" \
  --output text | tr '\t' '\n' | grep -v '^None$' | while read -r key; do
    [[ -z "$key" ]] && continue
    log "Eliminando s3://${S3_BUCKET}/${key}"
    aws s3 rm "s3://${S3_BUCKET}/${key}" --only-show-errors
  done

log "Backup finalizado OK"
