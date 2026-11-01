"""Offline-safe SQLite backup and restore helpers.

The helpers use SQLite's online backup API for a consistent snapshot.  They do
not overwrite an existing destination unless the caller explicitly opts in.
PostgreSQL deployments can use their normal database-native backup tooling;
this module intentionally does not execute external commands.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import sqlite3
import tempfile
from typing import Any

from ..infrastructure.config import BackupSettings
from .metrics import MetricsHooks


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_path(value: str | Path) -> Path:
    path = Path(value)
    if str(path) == ":memory:":
        raise ValueError("file backup is not available for an in-memory database")
    return path


def _validate_sqlite(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"SQLite quick_check failed for {path}")
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class BackupManifest:
    source: str
    destination: str
    created_at: str
    size_bytes: int
    sha256: str
    operation: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _manifest_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".manifest.json")


def _write_snapshot(source: Path, destination: Path, *, overwrite: bool) -> None:
    if destination.exists() and not overwrite:
        raise FileExistsError(f"backup destination exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
        )
        temporary = Path(handle.name)
        handle.close()
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def backup_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
    metrics: MetricsHooks | None = None,
) -> BackupManifest:
    source_path = _sqlite_path(source)
    destination_path = _sqlite_path(destination)
    _validate_sqlite(source_path)
    manifest_path = _manifest_path(destination_path)
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"backup manifest exists: {manifest_path}")
    _write_snapshot(source_path, destination_path, overwrite=overwrite)
    manifest = BackupManifest(
        source=str(source_path),
        destination=str(destination_path),
        created_at=_timestamp(),
        size_bytes=destination_path.stat().st_size,
        sha256=_checksum(destination_path),
        operation="backup",
    )
    manifest_path.write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (metrics or MetricsHooks()).backup_created()
    return manifest


def restore_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
    expected_sha256: str | None = None,
    metrics: MetricsHooks | None = None,
) -> BackupManifest:
    source_path = _sqlite_path(source)
    destination_path = _sqlite_path(destination)
    _validate_sqlite(source_path)
    actual_checksum = _checksum(source_path)
    if expected_sha256 and actual_checksum != expected_sha256:
        raise ValueError("backup checksum does not match expected_sha256")
    _write_snapshot(source_path, destination_path, overwrite=overwrite)
    manifest = BackupManifest(
        source=str(source_path),
        destination=str(destination_path),
        created_at=_timestamp(),
        size_bytes=destination_path.stat().st_size,
        sha256=_checksum(destination_path),
        operation="restore",
    )
    (metrics or MetricsHooks()).backup_created()
    return manifest


def backup_database(
    database: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
    metrics: MetricsHooks | None = None,
) -> BackupManifest:
    """Back up a SQLite path or SQLite URL without running external commands."""

    value = str(database)
    if value.startswith("sqlite+pysqlite:///"):
        value = value.removeprefix("sqlite+pysqlite:///")
    elif value.startswith("sqlite:///"):
        value = value.removeprefix("sqlite:///")
    elif "://" in value:
        raise ValueError("backup_database only handles SQLite; use native PostgreSQL tooling")
    return backup_sqlite(value, destination, overwrite=overwrite, metrics=metrics)


class BackupRestoreService:
    """Environment-configured convenience wrapper."""

    def __init__(self, settings: BackupSettings | None = None, *, metrics: MetricsHooks | None = None) -> None:
        self.settings = settings or BackupSettings()
        self.metrics = metrics or MetricsHooks()

    def backup(self, *, destination: str | Path | None = None, source: str | Path | None = None) -> BackupManifest:
        source_path = source or self.settings.database_path
        destination_path = destination or (Path(self.settings.root) / f"video-intelligence-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite3")
        return backup_database(
            source_path,
            destination_path,
            overwrite=self.settings.allow_overwrite,
            metrics=self.metrics,
        )

    def restore(
        self,
        source: str | Path,
        *,
        destination: str | Path | None = None,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> BackupManifest:
        destination_path = destination or self.settings.database_path
        if overwrite and not self.settings.allow_overwrite:
            raise PermissionError(
                "overwrite is disabled by BACKUP_ALLOW_OVERWRITE; enable it explicitly in settings"
            )
        return restore_sqlite(
            source,
            destination_path,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
            metrics=self.metrics,
        )


BackupManager = BackupRestoreService
