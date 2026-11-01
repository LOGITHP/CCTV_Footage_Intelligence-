from __future__ import annotations

from threading import Event

from video_intelligence.cameras import CameraSourceKind, MonitoringStatus
from video_intelligence.domain import ObjectObservation
from video_intelligence.operations.streaming import CameraStreamMonitoringWorker
from video_intelligence.pipeline import DetectedObject
from video_intelligence.storage import Store


class FakeCapture:
    def __init__(self, frames):
        self.frames = list(frames)
        self.index = 0
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self.index >= len(self.frames):
            return False, None
        frame = self.frames[self.index]
        self.index += 1
        return True, frame

    def get(self, _property):
        return float((self.index - 1) * 1000)

    def release(self):
        self.released = True


class FakeDetector:
    def __init__(self):
        self.index = 0

    def detect(self, _frame):
        x = 20.0 if self.index == 0 else 5.0
        self.index += 1
        return (DetectedObject("person", x, 5.0, 100.0, category="person"),)


class FakeTracker:
    def __init__(self, scope):
        self.scope = scope
        self.next_id = 1

    def update(self, detections):
        return tuple(
            ObjectObservation(
                f"track-{self.next_id}",
                item.x,
                item.y,
                label=item.label,
                category=item.category,
                track_scope=self.scope,
            )
            for item in detections
        )


def _worker(store, captures, *, stop_event=None):
    detector = FakeDetector()
    return CameraStreamMonitoringWorker(
        store,
        capture_factory=lambda _source: captures.pop(0),
        component_factory=lambda _options, scope: (detector, FakeTracker(scope)),
        sleep=lambda _seconds: None,
    )


def test_live_device_worker_persists_frames_events_and_terminal_report():
    store = Store(":memory:")
    store.upsert_camera(
        {
            "camera_id": "front",
            "display_name": "Front",
            "source": {"kind": "device", "path": "0"},
            "zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
        }
    )
    captures = [FakeCapture([object(), object(), object()])]

    result = _worker(store, captures).run("front", session_id="live-1", max_frames=3)

    assert result["status"] == MonitoringStatus.COMPLETED.value
    assert result["source_kind"] == CameraSourceKind.DEVICE.value
    assert result["frame_count"] == 3
    assert result["event_count"] == 1
    video_id = result["video_id"]
    assert len(store.search_tracks(video_id=video_id)) == 3
    assert store.search_events(video_id=video_id)[0]["event_type"] == "enter"


def test_live_worker_honors_stop_event_and_marks_report_stopped():
    store = Store(":memory:")
    store.upsert_camera(
        {
            "camera_id": "front",
            "display_name": "Front",
            "source": {"kind": "device", "path": "0"},
        }
    )
    stop_event = Event()

    class StopDetector(FakeDetector):
        def detect(self, frame):
            result = super().detect(frame)
            stop_event.set()
            return result

    detector = StopDetector()
    worker = CameraStreamMonitoringWorker(
        store,
        capture_factory=lambda _source: FakeCapture([object(), object()]),
        component_factory=lambda _options, scope: (detector, FakeTracker(scope)),
        sleep=lambda _seconds: None,
    )

    result = worker.run("front", session_id="live-stop", stop_event=stop_event, max_frames=10)

    assert result["status"] == MonitoringStatus.STOPPED.value
    assert result["frame_count"] == 1
