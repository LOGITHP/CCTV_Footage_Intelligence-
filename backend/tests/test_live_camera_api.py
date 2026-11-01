from __future__ import annotations

import time

from fastapi.testclient import TestClient

import video_intelligence.api as api_module
from video_intelligence.cameras import CameraSourceKind
from video_intelligence.domain import ObjectObservation
from video_intelligence.operations.streaming import CameraStreamMonitoringWorker
from video_intelligence.pipeline import DetectedObject
from video_intelligence.storage import Store


class Capture:
    def __init__(self):
        self.index = 0

    def isOpened(self):
        return True

    def read(self):
        if self.index >= 2:
            return False, None
        value = object()
        self.index += 1
        return True, value

    def get(self, _property):
        return float((self.index - 1) * 1000)

    def release(self):
        return None


class Detector:
    def detect(self, _frame):
        return (DetectedObject("person", 5.0, 5.0, 25.0, category="person"),)


class Tracker:
    def __init__(self, scope):
        self.scope = scope

    def update(self, detections):
        return tuple(ObjectObservation("track-1", item.x, item.y, label=item.label, category=item.category, track_scope=self.scope) for item in detections)


def test_continuous_device_route_starts_and_persists_live_report(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    store.upsert_camera(
        {
            "camera_id": "device-0",
            "display_name": "Device 0",
            "source": {"kind": "device", "path": "0"},
        }
    )
    worker = CameraStreamMonitoringWorker(
        store,
        capture_factory=lambda _source: Capture(),
        component_factory=lambda _options, scope: (Detector(), Tracker(scope)),
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(api_module, "_camera_stream_worker", lambda: worker)
    client = TestClient(api_module.app)

    started = client.post("/api/v1/cameras/device-0/run", json={"continuous": True, "max_frames": 2})

    assert started.status_code == 200
    assert started.json()["worker_mode"] == "inline-live"
    for _ in range(20):
        reports = client.get("/api/v1/cameras/device-0/reports").json()["reports"]
        if reports and reports[-1]["status"] in {"completed", "failed", "stopped"}:
            break
        time.sleep(0.01)
    assert reports[-1]["status"] == "completed"
    assert reports[-1]["source_kind"] == CameraSourceKind.DEVICE.value
    assert reports[-1]["frame_count"] == 2
