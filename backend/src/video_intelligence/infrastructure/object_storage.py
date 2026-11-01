"""Local and S3-compatible object storage adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .config import ObjectStorageSettings


class ObjectStorageUnavailable(RuntimeError):
    pass


class ObjectStorage(Protocol):
    def put_file(self, source: str | Path, key: str, *, content_type: str = "application/octet-stream") -> str: ...

    def signed_url(self, key: str, *, expires_seconds: int | None = None) -> str: ...

    def delete(self, key: str) -> None: ...


class LocalObjectStorage:
    def __init__(self, settings: ObjectStorageSettings) -> None:
        self.root = Path(settings.local_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(self, source: str | Path, key: str, *, content_type: str = "application/octet-stream") -> str:
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(source).read_bytes())
        return key

    def signed_url(self, key: str, *, expires_seconds: int | None = None) -> str:
        return ""

    def delete(self, key: str) -> None:
        target = self.root / key
        try:
            target.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("object key resolves outside local object root") from exc
        target.unlink(missing_ok=True)


class S3ObjectStorage:
    def __init__(self, settings: ObjectStorageSettings) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise ObjectStorageUnavailable("install boto3 to enable MinIO/S3 object storage") from exc
        self.settings = settings
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
        )
        try:
            self.client.head_bucket(Bucket=settings.bucket)
        except Exception:
            self.client.create_bucket(Bucket=settings.bucket)

    def put_file(self, source: str | Path, key: str, *, content_type: str = "application/octet-stream") -> str:
        self.client.upload_file(str(source), self.settings.bucket, key, ExtraArgs={"ContentType": content_type})
        return key

    def signed_url(self, key: str, *, expires_seconds: int | None = None) -> str:
        return str(
            self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.settings.bucket, "Key": key},
                ExpiresIn=expires_seconds or self.settings.presign_ttl_seconds,
            )
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.settings.bucket, Key=key)


def build_object_storage(settings: ObjectStorageSettings) -> ObjectStorage:
    if settings.backend == "local":
        return LocalObjectStorage(settings)
    if settings.backend == "s3":
        return S3ObjectStorage(settings)
    raise ObjectStorageUnavailable(f"unsupported object storage backend: {settings.backend}")
