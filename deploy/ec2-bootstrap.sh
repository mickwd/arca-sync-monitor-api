#!/usr/bin/env bash
# Preparación inicial de una instancia EC2 Ubuntu 24.04 (correr una sola vez como root).
# También se puede pasar como user-data al lanzar la instancia.
set -euo pipefail

apt-get update
apt-get install -y ca-certificates curl unzip jq
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
ARCH="$(dpkg --print-architecture)"
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# AWS CLI v2 para los backups (las credenciales vienen del Instance Profile, no de claves fijas)
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
rm -rf /tmp/aws /tmp/awscliv2.zip

mkdir -p /opt/arca-sync/releases /opt/arca-sync/shared /opt/arca-sync/data \
  /var/log/arca-sync /var/backups/arca-sync /etc/arca-sync
usermod -aG docker ubuntu || true

echo "Bootstrap OK"
echo "Siguiente paso: configurar /etc/arca-sync/backup.env e instalar scripts/crontab.example"
