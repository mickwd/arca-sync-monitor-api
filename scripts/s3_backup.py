"""Backup de directorios a S3 con boto3 y rotación por cantidad de días.

Uso:
    python scripts/s3_backup.py --source /var/lib/arca-sync --bucket mi-bucket \
        --prefix backups/arca-sync --retention-days 14

Comprime el directorio en un .tar.gz con timestamp, lo sube con cifrado del lado del
servidor (SSE-S3) y borra del bucket los backups más viejos que la retención.
Las credenciales se toman de la cadena estándar de AWS (rol de la instancia EC2,
variables de entorno o ~/.aws/credentials).
"""

from __future__ import annotations

import argparse
import logging
import sys
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("s3_backup")


def create_archive(source: Path, dest_dir: Path, now: datetime) -> Path:
    name = f"{source.name}-{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    archive = dest_dir / name
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=source.name)
    return archive


def upload(s3: Any, archive: Path, bucket: str, prefix: str) -> str:
    key = f"{prefix.rstrip('/')}/{archive.name}"
    s3.upload_file(
        str(archive),
        bucket,
        key,
        ExtraArgs={"ServerSideEncryption": "AES256", "StorageClass": "STANDARD_IA"},
    )
    return key


def rotate(s3: Any, bucket: str, prefix: str, retention_days: int, now: datetime) -> list[str]:
    """Borra los objetos del prefijo más viejos que `retention_days`. Devuelve las keys borradas."""
    cutoff = now - timedelta(days=retention_days)
    expired: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                expired.append(obj["Key"])
    # delete_objects acepta hasta 1000 keys por llamada
    for i in range(0, len(expired), 1000):
        chunk = expired[i : i + 1000]
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]})
    return expired


def run_backup(
    source: Path, bucket: str, prefix: str, retention_days: int, s3: Any | None = None
) -> str:
    if not source.exists():
        raise FileNotFoundError(f"No existe el origen: {source}")
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as tmp:
        archive = create_archive(source, Path(tmp), now)
        size_mb = archive.stat().st_size / 1024 / 1024
        key = upload(s3, archive, bucket, prefix)
        logger.info("Backup subido a s3://%s/%s (%.2f MB)", bucket, key, size_mb)
    deleted = rotate(s3, bucket, prefix, retention_days, now)
    if deleted:
        logger.info("Rotación: %d backups eliminados", len(deleted))
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup de un directorio a S3")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="backups")
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run_backup(args.source, args.bucket, args.prefix, args.retention_days)
    except Exception:
        logger.exception("Falló el backup")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
