from fastapi.testclient import TestClient

import video_intelligence.api as api_module
from video_intelligence.chat import camera_run_video_id
from video_intelligence.cameras import CameraMonitoringReport, MonitoringStatus
from video_intelligence.storage import Store


def test_grounded_chat_tracks_and_alert_rule_use_persisted_events(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)
    video_id = client.post("/api/v1/videos", files={"file": ("fixture.mp4", b"data", "video/mp4")}).json()["video_id"]

    rule = client.post("/api/v1/alerts/rules", json={"name": "Gate entry", "event_type": "enter", "zone_id": "gate"})
    assert rule.status_code == 201

    response = client.post(
        f"/api/v1/videos/{video_id}/process",
        json={
            "zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
            "observations": [
                {"timestamp": 0, "objects": [{"object_id": "track-1", "x": 20, "y": 5, "label": "person"}]},
                {"timestamp": 1, "objects": [{"object_id": "track-1", "x": 5, "y": 5, "label": "person"}]},
            ],
        },
    )
    assert response.status_code == 202
    assert response.json()["alerts"][0]["status"] == "open"

    chat = client.post("/api/v1/chat", json={"message": "Which person entered zone gate?", "zone_id": "gate"})
    assert chat.status_code == 200
    assert chat.json()["answer_mode"] == "grounded-structured"
    assert chat.json()["citations"][0]["event_id"]

    first_event_id = response.json()["events"][0]["event_id"]
    second_video_id = client.post("/api/v1/videos", files={"file": ("second.mp4", b"data", "video/mp4")}).json()["video_id"]
    second_process = client.post(
        f"/api/v1/videos/{second_video_id}/process",
        json={
            "zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
            "observations": [
                {"timestamp": 0, "objects": [{"object_id": "track-2", "x": 20, "y": 5, "label": "person"}]},
                {"timestamp": 1, "objects": [{"object_id": "track-2", "x": 5, "y": 5, "label": "person"}]},
            ],
        },
    )
    assert second_process.status_code == 202

    scoped_chat = client.post(
        "/api/v1/chat",
        json={"message": "Which person entered zone gate?", "video_id": video_id},
    )
    assert scoped_chat.status_code == 200
    assert [item["event_id"] for item in scoped_chat.json()["citations"]] == [first_event_id]

    mismatched_event_chat = client.post(
        "/api/v1/chat",
        json={"message": "Show this event", "event_id": first_event_id, "video_id": second_video_id},
    )
    assert mismatched_event_chat.status_code == 200
    assert mismatched_event_chat.json()["citations"] == []

    tracks = client.get(f"/api/v1/videos/{video_id}/tracks?track_id=track-1")
    assert tracks.status_code == 200
    assert len(tracks.json()["tracks"]) == 2

    alert_id = response.json()["alerts"][0]["alert_id"]
    acknowledged = client.post(f"/api/v1/alerts/{alert_id}/acknowledge")
    assert acknowledged.json()["status"] == "acknowledged"


def test_grounded_chat_scopes_camera_runs_without_mixing_track_ids(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)

    for camera_id, session_id, object_id in (
        ("front", "session-a", "track-1"),
        ("back", "session-b", "track-1"),
    ):
        store.upsert_camera(
            {
                "camera_id": camera_id,
                "display_name": camera_id.title(),
                "source": {"kind": "file", "path": f"{camera_id}.mp4"},
            }
        )
        store.save_monitoring_report(
            CameraMonitoringReport(
                camera_id,
                session_id,
                MonitoringStatus.COMPLETED,
                track_scope=f"camera:{camera_id}:session:{session_id}",
            )
        )
        video_id = camera_run_video_id(camera_id, session_id)
        store.add_video(video_id, f"{camera_id}.mp4", f"{camera_id}.mp4", "2026-01-01T00:00:00+00:00")
        store.add_events(
            video_id,
            [
                {
                    "event_id": f"event-{camera_id}",
                    "event_type": "enter",
                    "zone_id": "gate",
                    "object_id": object_id,
                    "object_label": "person",
                    "track_scope": f"camera:{camera_id}:session:{session_id}",
                    "timestamp_seconds": 1.0,
                }
            ],
        )

    response = client.post(
        "/api/v1/chat",
        json={
            "message": "Which person entered zone gate?",
            "camera_id": "front",
            "session_id": "session-a",
        },
    )
    assert response.status_code == 200
    assert [item["event_id"] for item in response.json()["citations"]] == ["event-front"]
    assert response.json()["query_plan"]["camera_id"] == "front"
    assert response.json()["query_plan"]["session_id"] == "session-a"

    mismatched = client.post(
        "/api/v1/chat",
        json={"message": "Which person entered zone gate?", "camera_id": "front", "session_id": "session-b"},
    )
    assert mismatched.status_code == 200
    assert mismatched.json()["citations"] == []
