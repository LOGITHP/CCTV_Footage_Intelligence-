"""Repository contracts shared by local and PostgreSQL implementations."""

from __future__ import annotations

from typing import Any, Protocol


class VideoRepository(Protocol):
    def add_video(self, video_id: str, filename: str, media_path: str, created_at: str) -> dict[str, Any]: ...

    def get_video(self, video_id: str) -> dict[str, Any] | None: ...


class JobRepository(Protocol):
    def add_job(self, job_id: str, video_id: str, status: str, created_at: str) -> dict[str, Any]: ...

    def update_job(self, job_id: str, status: str, error: str | None = None) -> dict[str, Any] | None: ...

    def get_job(self, job_id: str) -> dict[str, Any] | None: ...


class EventRepository(Protocol):
    def add_events(self, video_id: str, events: list[dict[str, Any]]) -> None: ...

    def update_evidence(self, event_id: str, path: str) -> None: ...

    def search_events(
        self,
        *,
        video_id: str | None = None,
        object_id: str | None = None,
        zone_id: str | None = None,
        object_label: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_event(self, event_id: str) -> dict[str, Any] | None: ...


class VideoStore(VideoRepository, JobRepository, EventRepository, Protocol):
    """Complete store contract used by the existing API implementation."""

    def close(self) -> None: ...
