import pytest

from video_intelligence.embedding_index import EmbeddingIndexOrchestrator, EmbeddingIndexingError
from video_intelligence.infrastructure.vectors import versioned_collection_name


class _TextEncoder:
    dimension = 3

    def text(self, value):
        return [1.0, 2.0, 3.0]


class _Index:
    collection = "events__bge-m3-v1"
    dimension = 3

    def __init__(self):
        self.points = []

    def upsert(self, point_id, vector, payload):
        self.points.append((point_id, vector, payload))

    def search(self, vector, *, limit, filters=None):
        assert vector == [1.0, 2.0, 3.0]
        return [{"id": "event-1", "score": 0.9, "payload": {"event_id": "event-1"}}][:limit]


def test_indexing_uses_declared_dimension_and_preserves_evidence_payload():
    index = _Index()
    summary = EmbeddingIndexOrchestrator(text_encoder=_TextEncoder(), text_index=index).index_text(
        [{"id": "event-1", "text": "person entered", "payload": {"timestamp_seconds": 2.5}}]
    )
    assert summary.indexed == 1
    assert summary.dimension == 3
    assert index.points[0][2]["timestamp_seconds"] == 2.5


def test_indexing_rejects_encoder_dimension_drift():
    class WrongEncoder(_TextEncoder):
        dimension = 2

        def text(self, value):
            return [1.0, 2.0]

    with pytest.raises(EmbeddingIndexingError, match="expected 3"):
        EmbeddingIndexOrchestrator(
            text_encoder=WrongEncoder(),
            text_index=_Index(),
            text_dimension=3,
        ).index_text([("event-1", "person")])


def test_versioned_collection_name_separates_model_revisions():
    assert versioned_collection_name("events", "bge-m3", "1") != versioned_collection_name(
        "events", "bge-m3", "2"
    )


def test_text_search_uses_real_encoder_and_index_dimension():
    orchestrator = EmbeddingIndexOrchestrator(
        text_encoder=_TextEncoder(),
        text_index=_Index(),
        text_dimension=3,
    )
    assert orchestrator.search_text("person entered")[0]["score"] == 0.9
