import json
import numpy as np
import pytest

from video_intelligence.domain import ObjectObservation
from video_intelligence.model_adapters import (
    MetaMuseGroundedResponder,
    MuseSparkGroundedResponder,
    ModelSettings,
    SAMDetector,
    SAMTracker,
    build_detector,
    build_tracker,
    model_capabilities,
)
from video_intelligence.pipeline import DetectedObject


def test_meta_muse_grounded_responder_offline_fallback():
    responder = MetaMuseGroundedResponder(api_key="test-key", api_url="http://127.0.0.1:9", timeout=0.1)
    citations = [
        {
            "event_id": "evt-123",
            "timestamp_seconds": 12.345,
            "zone_id": "gate-1",
            "object_id": "person-42",
            "object_label": "person",
            "event_type": "enter",
        }
    ]
    answer = responder.respond("Who entered gate-1?", citations)
    assert "person-42" in answer
    assert "gate-1" in answer
    assert "12.345" in answer


def test_meta_muse_grounded_responder_empty_citations():
    responder = MetaMuseGroundedResponder(api_key="test-key", api_url="http://127.0.0.1:9", timeout=0.1)
    answer = responder.respond("Who is there?", [])
    assert "No verified citations" in answer


def test_muse_spark_alias():
    assert MuseSparkGroundedResponder is MetaMuseGroundedResponder


def test_sam_detector_synthetic_frame():
    detector = SAMDetector(min_area=10.0)
    # Create synthetic frame with high-contrast geometric objects
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[50:150, 50:100] = 255  # Vertical rectangle (person-like aspect)
    frame[160:200, 150:240] = 255  # Horizontal rectangle (vehicle-like aspect)

    detections = detector.detect(frame)
    assert len(detections) >= 2
    for det in detections:
        assert isinstance(det, DetectedObject)
        assert det.area >= 10.0
        assert det.bbox is not None
        assert len(det.bbox) == 4
        assert det.segmentation_polygon is not None
        assert len(det.segmentation_polygon) >= 3


def test_sam_tracker_updates_and_preserves_polygon():
    tracker = SAMTracker(max_distance=50.0, track_scope="test-run")
    polygon1 = ((10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0))
    polygon2 = ((12.0, 12.0), (32.0, 12.0), (32.0, 32.0), (12.0, 32.0))

    det1 = DetectedObject(
        label="person",
        x=20.0,
        y=20.0,
        area=400.0,
        bbox=(10.0, 10.0, 30.0, 30.0),
        category="person",
        segmentation_polygon=polygon1,
    )
    obs1 = tracker.update((det1,))
    assert len(obs1) == 1
    assert isinstance(obs1[0], ObjectObservation)
    track_id = obs1[0].object_id
    assert track_id.startswith("sam-track-")
    assert obs1[0].segmentation_polygon == polygon1

    # Update next frame nearby
    det2 = DetectedObject(
        label="person",
        x=22.0,
        y=22.0,
        area=400.0,
        bbox=(12.0, 12.0, 32.0, 32.0),
        category="person",
        segmentation_polygon=polygon2,
    )
    obs2 = tracker.update((det2,))
    assert len(obs2) == 1
    # Track ID should be preserved
    assert obs2[0].object_id == track_id
    assert obs2[0].segmentation_polygon == polygon2


def test_build_detector_and_tracker_for_sam():
    settings = ModelSettings(profile="sam")
    detector = build_detector(settings)
    assert isinstance(detector, SAMDetector)

    tracker = build_tracker(settings, profile="sam")
    assert isinstance(tracker, SAMTracker)


def test_model_capabilities_surfaces_sam_and_meta_muse():
    settings = ModelSettings(profile="sam", meta_api_key="LLM_test_key", meta_model="muse-spark-1.3")
    caps = model_capabilities(settings)
    assert caps["detectors"]["sam"]["available"] is True
    assert "Meta SAM 3.1" in caps["detectors"]["sam"]["label"]
    assert caps["trackers"]["sam"]["available"] is True
    assert caps["chat"]["meta_api_configured"] is True
    assert caps["chat"]["model"] == "muse-spark-1.3"


def test_chat_query_with_meta_mode(monkeypatch):
    import video_intelligence.api as api_module
    from video_intelligence.storage import Store
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CHAT_MODEL_MODE", "meta")
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    client = TestClient(api_module.app)
    video_id = client.post("/api/v1/videos", files={"file": ("test.mp4", b"data", "video/mp4")}).json()["video_id"]
    client.post(
        f"/api/v1/videos/{video_id}/process",
        json={
            "zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
            "observations": [
                {"timestamp": 0, "objects": [{"object_id": "track-1", "x": 20, "y": 5, "label": "person"}]},
                {"timestamp": 1, "objects": [{"object_id": "track-1", "x": 5, "y": 5, "label": "person"}]},
            ],
        },
    )
    res = client.post("/api/v1/chat", json={"message": "Who entered gate?", "zone_id": "gate"})
    assert res.status_code == 200
    assert res.json()["answer_mode"] == "meta-muse-grounded-citations"
    assert len(res.json()["citations"]) > 0
