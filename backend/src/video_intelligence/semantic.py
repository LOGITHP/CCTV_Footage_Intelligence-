"""Verified semantic indexing and retrieval over uploaded-video evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .embedding_index import EmbeddingIndexOrchestrator, EmbeddingIndexingError, EmbeddingRecord, IndexingSummary


class SemanticSearchError(RuntimeError):
    """Raised when semantic indexing/search cannot satisfy evidence rules."""


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    matches: tuple[dict[str, Any], ...]
    discarded: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"matches": [dict(item) for item in self.matches], "discarded": self.discarded}


class SemanticSearchService:
    """Use real embeddings while validating every candidate against the store."""

    def __init__(self, store: Any, orchestrator: EmbeddingIndexOrchestrator) -> None:
        self.store = store
        self.orchestrator = orchestrator

    @staticmethod
    def _event_text(event: Mapping[str, Any]) -> str:
        return (
            f"{event.get('event_type', 'event')} object {event.get('object_label', 'object')} "
            f"{event.get('object_id', '')} zone {event.get('zone_id', '')} "
            f"at {float(event.get('timestamp_seconds', 0.0)):.6f} seconds"
        )

    def index_events(self, events: Iterable[Mapping[str, Any]]) -> IndexingSummary:
        records = []
        for event in events:
            event_id = str(event.get("event_id") or "")
            video_id = str(event.get("video_id") or "")
            if not event_id or not video_id:
                raise SemanticSearchError("event indexing requires event_id and video_id")
            payload = {
                "event_id": event_id,
                "video_id": video_id,
                "timestamp_seconds": float(event["timestamp_seconds"]),
                "event_type": event.get("event_type"),
                "zone_id": event.get("zone_id"),
                "object_id": event.get("object_id"),
                "object_label": event.get("object_label", "object"),
            }
            records.append(EmbeddingRecord(event_id, self._event_text(event), payload))
        try:
            return self.orchestrator.index_text(records)
        except (EmbeddingIndexingError, ValueError, TypeError) as exc:
            raise SemanticSearchError(str(exc)) from exc

    def index_video_frames(
        self,
        video_id: str,
        media_path: str,
        *,
        every: int = 1,
    ) -> IndexingSummary:
        try:
            return self.orchestrator.index_video_path(video_id, media_path, every=every)
        except (EmbeddingIndexingError, ValueError, TypeError, OSError) as exc:
            raise SemanticSearchError(str(exc)) from exc

    def search_text(
        self,
        query: str,
        *,
        limit: int = 10,
        video_id: str | None = None,
    ) -> SemanticSearchResult:
        try:
            hits = self.orchestrator.search_text(
                query,
                limit=limit,
                filters={"video_id": video_id} if video_id else None,
            )
        except (EmbeddingIndexingError, ValueError, TypeError) as exc:
            raise SemanticSearchError(str(exc)) from exc

        verified: list[dict[str, Any]] = []
        discarded = 0
        for hit in hits:
            payload = hit.get("payload") if isinstance(hit, Mapping) else None
            if not isinstance(payload, Mapping):
                discarded += 1
                continue
            event_id = payload.get("event_id")
            if event_id:
                event = self.store.get_event(str(event_id))
                if event is None:
                    discarded += 1
                    continue
                if video_id and str(event.get("video_id")) != video_id:
                    discarded += 1
                    continue
                indexed_timestamp = payload.get("timestamp_seconds")
                if indexed_timestamp is not None and abs(float(indexed_timestamp) - float(event["timestamp_seconds"])) > 1e-6:
                    discarded += 1
                    continue
                verified.append({"score": hit.get("score"), "event": dict(event), "source": "event"})
                continue

            indexed_video_id = payload.get("video_id")
            if not indexed_video_id or self.store.get_video(str(indexed_video_id)) is None:
                discarded += 1
                continue
            if video_id and str(indexed_video_id) != video_id:
                discarded += 1
                continue
            verified.append(
                {
                    "score": hit.get("score"),
                    "source": "video_frame",
                    "video_id": str(indexed_video_id),
                    "timestamp_seconds": payload.get("timestamp_seconds"),
                    "frame_index": payload.get("frame_index"),
                }
            )
        return SemanticSearchResult(tuple(verified), discarded)


__all__ = ["SemanticSearchError", "SemanticSearchResult", "SemanticSearchService"]
