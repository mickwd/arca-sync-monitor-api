from __future__ import annotations

import tarfile
from datetime import UTC, datetime, timedelta

from scripts.s3_backup import rotate, run_backup


class FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, **kwargs):
        yield {"Contents": self.objects}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or []
        self.uploaded: list[tuple[str, str, str, dict]] = []
        self.deleted: list[str] = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        with tarfile.open(filename) as tar:
            self.members = tar.getnames()
        self.uploaded.append((filename, bucket, key, ExtraArgs))

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.objects)

    def delete_objects(self, Bucket, Delete):
        self.deleted.extend(o["Key"] for o in Delete["Objects"])


def test_run_backup_uploads_encrypted_archive(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "arca_sync.db").write_text("contenido")

    s3 = FakeS3()
    key = run_backup(source, "bucket-test", "backups/arca", retention_days=7, s3=s3)

    assert key.startswith("backups/arca/data-") and key.endswith(".tar.gz")
    _, bucket, _, extra = s3.uploaded[0]
    assert bucket == "bucket-test"
    assert extra["ServerSideEncryption"] == "AES256"
    assert "data/arca_sync.db" in s3.members


def test_rotate_deletes_only_expired_objects():
    now = datetime(2026, 6, 30, tzinfo=UTC)
    s3 = FakeS3(
        [
            {"Key": "b/old.tar.gz", "LastModified": now - timedelta(days=20)},
            {"Key": "b/new.tar.gz", "LastModified": now - timedelta(days=2)},
        ]
    )
    assert rotate(s3, "bucket", "b", retention_days=14, now=now) == ["b/old.tar.gz"]
    assert s3.deleted == ["b/old.tar.gz"]
