import json
from pathlib import Path

from fastapi.testclient import TestClient

import video_intelligence.api as api_module
from video_intelligence.storage import Store


def test_process_search_and_readback(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)

    upload = client.post("/api/v1/videos", files={"file": ("fixture.mp4", b"not-a-real-video", "video/mp4")})
    assert upload.status_code == 201
    video_id = upload.json()["video_id"]

    process = client.post(
        f"/api/v1/videos/{video_id}/process",
        json={
            "zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
            "observations": [
                {"timestamp": 0, "objects": [{"object_id": "car-1", "x": 20, "y": 5}]},
                {"timestamp": 9000, "objects": [{"object_id": "car-1", "x": 5, "y": 5}]},
            ],
            "time_base": 1 / 90000,
        },
    )
    assert process.status_code == 202
    assert process.json()["events"][0]["timestamp_seconds"] == 0.1

    result = client.post("/api/v1/search", json={"object_id": "car-1", "zone_id": "gate"})
    assert result.status_code == 200
    assert result.json()["matches"][0]["event_type"] == "enter"

    scoped_events = client.get(f"/api/v1/events?video_id={video_id}")
    assert scoped_events.status_code == 200
    assert [item["video_id"] for item in scoped_events.json()["events"]] == [video_id]

    job = client.get(f"/api/v1/jobs/{process.json()['job_id']}")
    assert job.json()["status"] == "completed"


def test_process_observation_attributes_survive_storage_and_track_api(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)
    video_id = client.post(
        "/api/v1/videos",
        files={"file": ("fixture.mp4", b"not-a-real-video", "video/mp4")},
    ).json()["video_id"]

    response = client.post(
        f"/api/v1/videos/{video_id}/process",
        json={
            "observations": [{
                "timestamp": 0,
                "objects": [{
                    "object_id": "track-1",
                    "x": 5,
                    "y": 5,
                    "label": "car",
                    "bbox": [0, 0, 10, 10],
                    "track_scope": "camera-a/session-1",
                    "appearance": {
                        "dominant_color": "blue",
                        "vehicle_color": "blue",
                        "vehicle_type": "car",
                    },
                }],
            }],
        },
    )
    assert response.status_code == 202
    track = client.get(
        f"/api/v1/videos/{video_id}/tracks?track_id=track-1&track_scope=camera-a%2Fsession-1"
    ).json()["tracks"][0]
    assert track["track_scope"] == "camera-a/session-1"
    assert track["object_category"] == "vehicle"
    assert track["bbox"] == [0.0, 0.0, 10.0, 10.0]
    assert track["appearance"]["estimated"] is True
    assert track["appearance"]["biometric"] is False


def test_probe_endpoint_returns_media_metadata(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)
    upload = client.post("/api/v1/videos", files={"file": ("fixture.mp4", b"bytes", "video/mp4")})
    video_id = upload.json()["video_id"]

    class Result:
        format_name = "mov,mp4"
        duration_seconds = 2.0
        streams = ()

    monkeypatch.setattr(api_module, "probe_media", lambda path: Result())
    response = client.post(f"/api/v1/videos/{video_id}/probe")
    assert response.status_code == 200
    assert response.json()["duration_seconds"] == 2.0


def test_process_media_persists_evidence_reference(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)
    upload = client.post("/api/v1/videos", files={"file": ("fixture.mp4", b"bytes", "video/mp4")})
    video_id = upload.json()["video_id"]

    from video_intelligence.domain import FrameObservation, ObjectObservation, ZoneEvent
    from video_intelligence.pipeline import PipelineResult

    observations = (
        FrameObservation(0.0, (ObjectObservation("track-1", 20, 5),)),
        FrameObservation(1.0, (ObjectObservation("track-1", 5, 5),)),
    )
    event = ZoneEvent("event-1", "enter", "gate", "track-1", 1.0)
    monkeypatch.setattr(api_module, "run_media_pipeline", lambda *args, **kwargs: PipelineResult(observations, (event,)))
    evidence = tmp_path / "evidence" / "event-1.mp4"
    evidence.parent.mkdir()
    evidence.write_bytes(b"clip")
    monkeypatch.setattr(api_module, "create_clip", lambda *args, **kwargs: evidence)

    response = client.post(
        f"/api/v1/videos/{video_id}/process-media",
        json={"zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}]},
    )
    assert response.status_code == 202
    assert response.json()["events"][0]["evidence_status"] == "ready"
    assert client.get("/api/v1/events").json()["events"][0]["evidence_artifact"] == "event-1.mp4"
    event_id = response.json()["events"][0]["event_id"]
    evidence_response = client.get(f"/api/v1/evidence/{event_id}").json()
    assert evidence_response["status"] == "ready"
    assert evidence_response["artifact"] == "event-1.mp4"
    assert evidence_response["download_available"] is False


def test_upload_and_process_media_fixture_produces_expected_events(monkeypatch, tmp_path):
    import cv2
    import numpy as np

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "zone_crossing_expected.json").read_text(encoding="utf-8")
    )

    source = tmp_path / "known-fixture.avi"
    width, height = fixture["frame_size"]
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), fixture["fps"], (width, height))
    assert writer.isOpened()
    for item in fixture["frames"]:
        x, y = item["object_center"]
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[y - 7 : y + 8, x - 8 : x + 8] = 255
        writer.write(frame)
    writer.release()

    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path / "uploads")

    def fake_clip(_source, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fixture-clip")
        return destination

    monkeypatch.setattr(api_module, "create_clip", fake_clip)
    client = TestClient(api_module.app)
    upload = client.post(
        "/api/v1/videos",
        files={"file": (source.name, source.read_bytes(), "video/x-msvideo")},
    )
    assert upload.status_code == 201
    video_id = upload.json()["video_id"]

    process = client.post(
        f"/api/v1/videos/{video_id}/process-media",
        json={"zones": [fixture["zone"]]},
    )
    assert process.status_code == 202
    payload = process.json()
    assert payload["status"] == "completed"
    actual = [
        {
            "event_type": event["event_type"],
            "zone_id": event["zone_id"],
            "object_id": event["object_id"],
            "timestamp_seconds": round(event["timestamp_seconds"], 1),
        }
        for event in payload["events"]
    ]
    assert actual == fixture["expected_events"]
    assert all(event["evidence_status"] == "ready" for event in payload["events"])
    evidence = client.get(f"/api/v1/evidence/{payload['events'][0]['event_id']}")
    assert evidence.status_code == 200
    assert evidence.json()["status"] == "ready"
    assert evidence.json()["download_available"] is False


def test_frontend_is_served_from_api():
    response = TestClient(api_module.app).get("/")
    assert response.status_code == 200
    assert "Video Intelligence Console" in response.text


def test_system_status_exposes_runtime_capabilities(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    response = TestClient(api_module.app).get("/api/v1/system/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["runtime"]["default_device"] == "cpu"
    assert "gpu" in payload["runtime"]["devices"]


def test_upload_rejects_oversized_media_and_removes_partial_file(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 3)
    response = TestClient(api_module.app).post(
        "/api/v1/videos", files={"file": ("fixture.mp4", b"1234", "video/mp4")}
    )
    assert response.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_gpu_processing_fails_clearly_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    import video_intelligence.pipeline as pipeline_module
    monkeypatch.setattr(
        pipeline_module,
        "runtime_capabilities",
        lambda: {"devices": {"gpu": {"available": False}}},
    )
    client = TestClient(api_module.app)
    upload = client.post("/api/v1/videos", files={"file": ("fixture.mp4", b"bytes", "video/mp4")})
    response = client.post(
        f"/api/v1/videos/{upload.json()['video_id']}/process-media",
        json={"device": "gpu"},
    )
    assert response.status_code == 422
    assert "CUDA is unavailable" in response.json()["detail"]


def test_process_media_marks_job_failed_when_uploaded_file_is_missing(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)
    upload = client.post("/api/v1/videos", files={"file": ("missing.mp4", b"bytes", "video/mp4")})
    assert upload.status_code == 201
    video_id = upload.json()["video_id"]
    video = store.get_video(video_id)
    assert video is not None
    Path(video["media_path"]).unlink()

    response = client.post(f"/api/v1/videos/{video_id}/process-media", json={})
    assert response.status_code == 422
    assert "not available in the active media store" in response.json()["detail"]
    job = store.connection.execute(
        "SELECT status, error FROM jobs WHERE video_id = ? ORDER BY created_at DESC LIMIT 1",
        (video_id,),
    ).fetchone()
    assert tuple(job) == ("failed", response.json()["detail"])


def test_semantic_routes_fail_closed_without_reviewed_local_manifest(monkeypatch):
    monkeypatch.delenv("MODEL_MANIFEST", raising=False)
    response = TestClient(api_module.app).post(
        "/api/v1/semantic-search",
        json={"query": "person entered the gate"},
    )
    assert response.status_code == 503
    assert "MODEL_MANIFEST" in response.json()["detail"]


def test_probe_resolves_legacy_host_path_inside_active_media_root(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    source = tmp_path / "legacy-upload.webm"
    source.write_bytes(b"fixture")
    store.add_video(
        "video-legacy",
        "legacy-upload.webm",
        r"C:\\Program Files\\Git\\app\\data\\media\\legacy-upload.webm",
        "2026-01-01T00:00:00+00:00",
    )

    class Result:
        format_name = "webm"
        duration_seconds = 3.0
        streams = ()

    monkeypatch.setattr(api_module, "probe_media", lambda path: Result())
    response = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(api_module.app).post(
        "/api/v1/videos/video-legacy/probe"
    )
    assert response.status_code == 200
    assert response.json()["format_name"] == "webm"


def test_list_videos_and_stream_video_route(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    client = TestClient(api_module.app)

    # Upload video fixture
    payload = b"fake-cctv-surveillance-mp4-stream-data"
    upload = client.post("/api/v1/videos", files={"file": ("cctv_test.mp4", payload, "video/mp4")})
    assert upload.status_code == 201
    video_id = upload.json()["video_id"]

    # Test GET /api/v1/videos
    list_resp = client.get("/api/v1/videos")
    assert list_resp.status_code == 200
    videos = list_resp.json()["videos"]
    assert any(v["video_id"] == video_id for v in videos)

    # Test GET /api/v1/videos/{video_id}/stream
    stream_resp = client.get(f"/api/v1/videos/{video_id}/stream")
    assert stream_resp.status_code == 200
    assert "video/mp4" in stream_resp.headers["content-type"]
    assert stream_resp.content == payload

    # Test HTTP Range request (scrubbing/seeking)
    range_resp = client.get(f"/api/v1/videos/{video_id}/stream", headers={"Range": "bytes=0-3"})
    assert range_resp.status_code == 206
    assert range_resp.content == payload[:4]

    # Test 404 for nonexistent video
    assert client.get("/api/v1/videos/nonexistent-id/stream").status_code == 404
