import numpy as np

from video_intelligence.model_adapters import YOLODetector
from video_intelligence.observables import (
    extract_observable_appearance,
    normalize_yolo_label,
    object_category_for_label,
)
from video_intelligence.pipeline import CentroidTracker, DetectedObject


def test_supported_yolo_labels_are_normalized_and_classified():
    expected = {
        "person": ("person", "person"),
        "motorbike": ("motorcycle", "vehicle"),
        "bike": ("bicycle", "vehicle"),
        "pickup-truck": ("truck", "vehicle"),
        "vehicle": ("vehicle", "vehicle"),
    }
    for raw, result in expected.items():
        canonical, category = normalize_yolo_label(raw), object_category_for_label(raw)
        assert (canonical, category) == result
    assert normalize_yolo_label("dog") == "dog"
    assert object_category_for_label("dog") is None


def test_track_ids_are_local_to_explicit_video_session_scope():
    detection = DetectedObject("person", 10, 10, 25, category="person")
    first = CentroidTracker(track_scope="camera-a/video-1").update((detection,))[0]
    second = CentroidTracker(track_scope="camera-b/video-2").update((detection,))[0]

    assert first.object_id == second.object_id == "track-1"
    assert first.track_scope == "camera-a/video-1"
    assert second.track_scope == "camera-b/video-2"


def test_appearance_extraction_is_deterministic_and_coarse_for_synthetic_crop():
    frame = np.zeros((20, 10, 3), dtype=np.uint8)
    frame[:10] = (0, 0, 255)  # BGR red upper garment cue.
    frame[10:] = (255, 0, 0)  # BGR blue lower garment cue.

    appearance = extract_observable_appearance(frame, (0, 0, 10, 20), "person")
    assert appearance is not None
    assert appearance.dominant_color in {"red", "blue"}
    assert appearance.upper_color == "red"
    assert appearance.lower_color == "blue"
    assert appearance.estimated is True
    assert appearance.biometric is False


def test_yolo_adapter_attaches_normalized_label_and_crop_metadata():
    class FakeScalar:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    class FakeBox:
        xyxy = [type("Coordinates", (), {"tolist": lambda self: [0, 0, 10, 20]})()]
        conf = [FakeScalar(0.9)]
        cls = [FakeScalar(1)]

    class FakeResult:
        names = {1: "motorbike"}
        boxes = [FakeBox()]

    class FakeModel:
        def predict(self, frame, **kwargs):
            return [FakeResult()]

    detector = object.__new__(YOLODetector)
    detector.model = FakeModel()
    detector.confidence = 0.25
    detector.device = "cpu"
    frame = np.zeros((20, 10, 3), dtype=np.uint8)
    frame[:, :] = (0, 255, 0)

    detections = detector.detect(frame)
    assert detections[0].label == "motorcycle"
    assert detections[0].category == "vehicle"
    assert detections[0].appearance is not None
    assert detections[0].appearance.vehicle_color == "green"
    assert detections[0].appearance.vehicle_type == "motorcycle"
