from __future__ import annotations

from video_intelligence.embedding_index import IndexingSummary
from video_intelligence.semantic import SemanticSearchService
from video_intelligence.storage import Store


class _FakeSemanticIndex:
    def __init__(self):
        self.events = None

    def index_text(self, records):
        self.events = list(records)
        return IndexingSummary(1, "events__bge-m3-v1", 1024, "bge-m3", "1")

    def index_video_path(self, video_id, media_path, *, every=1):
        return IndexingSummary(2, "frames__siglip2-v1", 768, "siglip2", "1")

    def search_text(self, query, *, limit=10, filters=None):
        return [
            {"score": 0.9, "payload": {"event_id": "event-1", "video_id": "video-1", "timestamp_seconds": 2.0}},
            {"score": 0.8, "payload": {"event_id": "missing", "video_id": "video-1", "timestamp_seconds": 2.0}},
            {"score": 0.7, "payload": {"video_id": "video-1", "timestamp_seconds": 3.0, "frame_index": 4}},
        ][:limit]


def test_semantic_service_indexes_and_verifies_authoritative_candidates(tmp_path):
    store = Store(":memory:")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    store.add_video("video-1", "video.mp4", str(video), "2026-01-01T00:00:00+00:00")
    store.add_events(
        "video-1",
        [{
            "event_id": "event-1",
            "event_type": "enter",
            "zone_id": "gate",
            "object_id": "track-1",
            "object_label": "person",
            "timestamp_seconds": 2.0,
        }],
    )
    index = _FakeSemanticIndex()
    service = SemanticSearchService(store, index)

    summary = service.index_events(store.search_events(video_id="video-1"))
    assert summary.indexed == 1
    assert index.events[0].payload["event_id"] == "event-1"
    assert service.index_video_frames("video-1", str(video), every=2).indexed == 2

    result = service.search_text("person entered", limit=3)
    assert len(result.matches) == 2
    assert result.matches[0]["event"]["event_id"] == "event-1"
    assert result.matches[1]["source"] == "video_frame"
    assert result.discarded == 1
