#!/usr/bin/env bash
# Se ejecuta en la instancia EC2 (lo invoca deploy.yml por SSH).
# Estructura:
#   /opt/arca-sync/releases/<tag>/        código de cada versión
#   /opt/arca-sync/current -> releases/<tag>
#   /opt/arca-sync/shared/.env            configuración persistente entre releases
set -euo pipefail

RELEASE="${1:?Falta el nombre del release}"
BASE=/opt/arca-sync
DIR="${BASE}/releases/${RELEASE}"
KEEP=5
COMPOSE=(sudo docker compose -p arca-sync -f docker-compose.yml -f deploy/docker-compose.prod.yml)

echo "==> Desplegando ${RELEASE}"
sudo mkdir -p "$DIR"
sudo tar -xzf "/tmp/arca-sync-${RELEASE}.tar.gz" -C "$DIR"
sudo ln -sfn "${BASE}/shared/.env" "${DIR}/.env"
rm -f "/tmp/arca-sync-${RELEASE}.tar.gz"

cd "$DIR"
"${COMPOSE[@]}" build api
"${COMPOSE[@]}" up -d --remove-orphans api postgres
sudo ln -sfn "$DIR" "${BASE}/current"

echo "==> Limpiando releases viejos (se conservan ${KEEP})"
ls -1dt "${BASE}"/releases/*/ | tail -n +$((KEEP + 1)) | xargs -r sudo rm -rf
sudo docker image prune -f > /dev/null
echo "==> Deploy de ${RELEASE} OK"
