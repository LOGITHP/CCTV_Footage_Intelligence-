"""Retention planning and explicit deletion propagation.

Planning is harmless and is the default.  A deletion requires both a policy
flag and a per-call confirmation, and local files are deleted only when they
are below the configured media root.  This makes accidental cleanup during an
offline test impossible by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from ..infrastructure.config import RetentionSettings
from .metrics import MetricsHooks


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class DeletableObjectStorage(Protocol):
    def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    video_id: str
    files: tuple[str, ...] = ()
    object_keys: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "files": list(self.files),
            "object_keys": list(self.object_keys),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RetentionResult:
    planned: int
    deleted: int
    skipped: int
    failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failures": list(self.failures),
        }


class RetentionService:
    def __init__(
        self,
        store: Any,
        *,
        settings: RetentionSettings | None = None,
        object_storage: DeletableObjectStorage | None = None,
        outbox: Any | None = None,
        vector_deleters: Iterable[Callable[[str], None]] = (),
        metrics: MetricsHooks | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or RetentionSettings()
        self.object_storage = object_storage
        self.outbox = outbox
        self.vector_deleters = tuple(vector_deleters)
        self.metrics = metrics or MetricsHooks()

    def expired_videos(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        cutoff = (now or _now()) - timedelta(days=self.settings.days)
        if not hasattr(self.store, "list_videos"):
            return []
        return [
            video
            for video in self.store.list_videos()
            if self._created_before(video.get("created_at"), cutoff)
        ]

    @staticmethod
    def _created_before(value: str | None, cutoff: datetime) -> bool:
        if not value:
            return False
        try:
            return _parse(value) <= cutoff
        except (TypeError, ValueError):
            return False

    def plan(self, video_id: str) -> DeletionPlan:
        video = self.store.get_video(video_id)
        if video is None:
            return DeletionPlan(video_id, warnings=("video not found",))
        files: list[str] = []
        object_keys: list[str] = []
        warnings: list[str] = []
        media_path = video.get("media_path")
        if media_path:
            files.append(str(media_path))
        if video.get("media_object_key"):
            object_keys.append(str(video["media_object_key"]))
        if hasattr(self.store, "list_video_artifacts"):
            for artifact in self.store.list_video_artifacts(video_id):
                path = artifact.get("evidence_path") or artifact.get("path")
                if path:
                    files.append(str(path))
                key = artifact.get("object_key")
                if key:
                    object_keys.append(str(key))
        safe_files: list[str] = []
        media_root = Path(self.settings.media_root).resolve()
        for raw_path in dict.fromkeys(files):
            path = Path(raw_path)
            try:
                path.resolve().relative_to(media_root)
            except ValueError:
                warnings.append(f"refusing file outside RETENTION_MEDIA_ROOT: {raw_path}")
            else:
                safe_files.append(raw_path)
        return DeletionPlan(video_id, tuple(safe_files), tuple(dict.fromkeys(object_keys)), tuple(warnings))

    def execute(
        self,
        plan: DeletionPlan,
        *,
        confirm: bool = False,
        dry_run: bool | None = None,
        allow_delete: bool | None = None,
    ) -> dict[str, Any]:
        """Apply a plan only when explicit safety gates are satisfied."""

        effective_dry_run = self.settings.dry_run if dry_run is None else dry_run
        effective_allow_delete = self.settings.delete_enabled if allow_delete is None else allow_delete
        if plan.object_keys and self.object_storage is None:
            self.metrics.retention_planned(1)
            return {
                "status": "skipped",
                "reason": "object_storage_unconfigured",
                "plan": plan.as_dict(),
            }
        if plan.warnings or effective_dry_run or not effective_allow_delete or not confirm:
            reason = "planned"
            if plan.warnings:
                reason = "unsafe_paths"
            elif not confirm:
                reason = "confirmation_required"
            elif effective_dry_run:
                reason = "dry_run"
            elif not effective_allow_delete:
                reason = "deletion_disabled"
            self.metrics.retention_planned(1)
            return {"status": "skipped", "reason": reason, "plan": plan.as_dict()}

        failures: list[str] = []
        deleted_files = 0
        deleted_objects = 0
        for raw_path in plan.files:
            path = Path(raw_path)
            try:
                if path.exists():
                    path.unlink()
                    deleted_files += 1
            except OSError as exc:
                failures.append(f"{raw_path}: {exc}")
        if self.object_storage is not None:
            for key in plan.object_keys:
                try:
                    self.object_storage.delete(key)
                    deleted_objects += 1
                except Exception as exc:
                    failures.append(f"{key}: {exc}")
        for deleter in self.vector_deleters:
            try:
                deleter(plan.video_id)
            except Exception as exc:
                failures.append(f"vector index: {exc}")
        if not failures:
            try:
                if hasattr(self.store, "delete_video"):
                    self.store.delete_video(plan.video_id)
                if self.outbox is not None and hasattr(self.outbox, "delete_for_aggregate"):
                    self.outbox.delete_for_aggregate(plan.video_id)
            except Exception as exc:
                failures.append(f"authoritative store: {exc}")
        self.metrics.retention_planned(0)
        return {
            "status": "deleted" if not failures else "partial",
            "video_id": plan.video_id,
            "files_deleted": deleted_files,
            "objects_deleted": deleted_objects,
            "failures": failures,
        }

    def run(
        self,
        *,
        now: datetime | None = None,
        execute: bool = False,
        confirm: bool = False,
    ) -> RetentionResult:
        candidates = self.expired_videos(now=now)
        self.metrics.retention_planned(len(candidates))
        deleted = skipped = 0
        failures: list[str] = []
        for video in candidates:
            plan = self.plan(str(video["video_id"]))
            result = self.execute(plan, confirm=confirm if execute else False)
            if result.get("status") == "deleted":
                deleted += 1
            elif result.get("status") in {"skipped", "partial"}:
                skipped += 1
            failures.extend(result.get("failures", []))
        return RetentionResult(len(candidates), deleted, skipped, tuple(failures))
