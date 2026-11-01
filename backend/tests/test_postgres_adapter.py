import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from video_intelligence.domain import FrameObservation, ObjectObservation  # noqa: E402
from video_intelligence.infrastructure.postgres import PostgresStore  # noqa: E402
from video_intelligence.observables import ObservableAppearance  # noqa: E402


def test_sqlalchemy_repository_contract_on_in_memory_sqlite():
    store = PostgresStore("sqlite+pysqlite:///:memory:")
    store.add_video("video-1", "fixture.mp4", "videos/video-1.mp4", "2026-01-01T00:00:00Z")
    store.add_job("job-1", "video-1", "processing", "2026-01-01T00:00:00Z")
    store.add_observations(
        "video-1",
        (
            FrameObservation(
                0.0,
                (
                    ObjectObservation(
                        "track-1",
                        4,
                        5,
                        "person",
                        bbox=(1, 2, 7, 12),
                        appearance=ObservableAppearance(upper_color="red", lower_color="blue"),
                        track_scope="camera-a/video-1",
                    ),
                ),
            ),
        ),
    )
    store.add_events("video-1", [{"event_id": "event-1", "event_type": "enter", "zone_id": "gate", "object_id": "track-1", "object_label": "person", "timestamp_seconds": 0.0}])

    assert store.get_video("video-1")["filename"] == "fixture.mp4"
    assert store.search_events(object_label="person")[0]["event_id"] == "event-1"
    track = store.search_tracks(video_id="video-1")[0]
    assert track["track_id"] == "track-1"
    assert track["track_scope"] == "camera-a/video-1"
    assert track["appearance"]["upper_color"] == "red"
