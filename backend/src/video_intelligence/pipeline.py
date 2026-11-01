"""CPU baseline video pipeline used before model adapters are introduced.

The detector is deliberately explicit: it finds bright connected regions and is
useful for deterministic fixtures, not a claim of general-purpose AI detection.
Decoder timestamps are read from the media backend and validated as source-time
values; nominal FPS is never used to synthesize timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
from typing import Iterator, Protocol

from .domain import FrameObservation, ObjectObservation, Zone, generate_zone_events
from .observables import ObservableAppearance, object_category_for_label


class PipelineError(RuntimeError):
    pass


class Detector(Protocol):
    """Model adapter boundary: detectors consume frames and return observations."""

    def detect(self, frame) -> tuple["DetectedObject", ...]: ...


class Tracker(Protocol):
    def update(self, detections: tuple["DetectedObject", ...]) -> tuple[ObjectObservation, ...]: ...


@lru_cache(maxsize=1)
def runtime_capabilities() -> dict[str, object]:
    """Report capabilities without claiming that the baseline is a production model."""
    try:
        import cv2
        cuda_devices = int(cv2.cuda.getCudaEnabledDeviceCount())
    except Exception:
        cuda_devices = 0
    try:
        import torch

        torch_devices = int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
    except Exception:
        torch_devices = 0
    gpu_count = max(cuda_devices, torch_devices)
    return {
        "default_device": "cpu",
        "devices": {"cpu": {"available": True}, "gpu": {"available": gpu_count > 0, "count": gpu_count}},
        "detector": "bright-blob-baseline",
        "detector_quality": "fixture-baseline-not-production-model",
    }


def require_device(device: str) -> None:
    if device not in {"cpu", "gpu"}:
        raise PipelineError("device must be cpu or gpu")
    if device == "gpu" and not runtime_capabilities()["devices"]["gpu"]["available"]:  # type: ignore[index]
        raise PipelineError("GPU processing requested but CUDA is unavailable")


@dataclass(frozen=True)
class DetectedObject:
    label: str
    x: float
    y: float
    area: float
    confidence: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    category: str | None = None
    appearance: ObservableAppearance | None = None
    segmentation_polygon: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class PipelineResult:
    observations: tuple[FrameObservation, ...]
    events: tuple


@dataclass(frozen=True)
class FramePrediction:
    """One decoded source-time frame with raw detections and tracked objects."""

    frame_index: int
    timestamp_seconds: float
    detections: tuple[DetectedObject, ...]
    tracks: tuple[ObjectObservation, ...]


class BrightBlobDetector:
    """Deterministic OpenCV detector for high-contrast fixture objects."""

    def __init__(self, *, threshold: int = 200, min_area: int = 25, label: str = "object") -> None:
        if not 0 <= threshold <= 255 or min_area < 1:
            raise ValueError("threshold must be 0..255 and min_area must be positive")
        self.threshold = threshold
        self.min_area = min_area
        self.label = label

    def detect(self, frame) -> tuple[DetectedObject, ...]:
        try:
            import cv2
        except ImportError as exc:
            raise PipelineError("opencv-python-headless and numpy are required for media processing") from exc
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, self.threshold, 255, cv2.THRESH_BINARY)
        count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        detections = []
        for index in range(1, count):
            area = float(stats[index, cv2.CC_STAT_AREA])
            if area >= self.min_area:
                x, y = (float(value) for value in centroids[index])
                left = float(stats[index, cv2.CC_STAT_LEFT])
                top = float(stats[index, cv2.CC_STAT_TOP])
                width = float(stats[index, cv2.CC_STAT_WIDTH])
                height = float(stats[index, cv2.CC_STAT_HEIGHT])
                detections.append(DetectedObject(self.label, x, y, area, bbox=(left, top, left + width, top + height)))
        return tuple(detections)


class CentroidTracker:
    def __init__(self, *, max_distance: float = 80.0, track_scope: str = "session") -> None:
        if not track_scope:
            raise ValueError("track_scope must not be empty")
        self.max_distance = max_distance
        self.track_scope = track_scope
        self._tracks: dict[str, tuple[float, float]] = {}
        self._next_id = 1

    def update(self, detections: tuple[DetectedObject, ...]) -> tuple[ObjectObservation, ...]:
        available = set(self._tracks)
        result = []
        for detection in detections:
            best_id, best_distance = None, self.max_distance
            for track_id in available:
                previous = self._tracks[track_id]
                distance = math.hypot(detection.x - previous[0], detection.y - previous[1])
                if distance <= best_distance:
                    best_id, best_distance = track_id, distance
            if best_id is None:
                best_id = f"track-{self._next_id}"
                self._next_id += 1
            available.discard(best_id)
            self._tracks[best_id] = (detection.x, detection.y)
            result.append(
                ObjectObservation(
                    object_id=best_id,
                    x=detection.x,
                    y=detection.y,
                    label=detection.label,
                    confidence=detection.confidence,
                    bbox=detection.bbox,
                    category=detection.category or object_category_for_label(detection.label),
                    appearance=detection.appearance,
                    track_scope=self.track_scope,
                )
            )
        return tuple(result)


def decode_observations(
    path: str | Path,
    *,
    every: int = 1,
    detector: Detector | None = None,
    tracker: Tracker | None = None,
    track_scope: str = "session",
) -> Iterator[FrameObservation]:
    """Decode frames with OpenCV and yield validated source-time observations."""

    for prediction in decode_predictions(path, every=every, detector=detector, tracker=tracker, track_scope=track_scope):
        yield FrameObservation(prediction.timestamp_seconds, prediction.tracks)


def decode_predictions(
    path: str | Path,
    *,
    every: int = 1,
    detector: Detector | None = None,
    tracker: Tracker | None = None,
    track_scope: str = "session",
) -> Iterator[FramePrediction]:
    """Decode source-time frames while retaining detector boxes for evaluation."""

    if every < 1:
        raise ValueError("every must be at least 1")
    try:
        import cv2
    except ImportError as exc:
        raise PipelineError("opencv-python-headless is required for media processing") from exc
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise PipelineError(f"unable to open video: {path}")
    detector = detector or BrightBlobDetector()
    tracker = tracker or CentroidTracker(track_scope=track_scope)
    index = 0
    last_timestamp = -1.0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if index % every == 0:
                timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
                if not math.isfinite(timestamp) or timestamp < 0 or timestamp < last_timestamp:
                    raise PipelineError(f"invalid or discontinuous decoder timestamp at frame {index}")
                last_timestamp = timestamp
                detections = detector.detect(frame)
                yield FramePrediction(index, timestamp, detections, tracker.update(detections))
            index += 1
    finally:
        capture.release()


def process_video(
    path: str | Path,
    zones: tuple[Zone, ...],
    *,
    every: int = 1,
    detector: Detector | None = None,
    tracker: Tracker | None = None,
    device: str = "cpu",
    track_scope: str = "session",
) -> PipelineResult:
    require_device(device)
    observations = tuple(
        decode_observations(
            path,
            every=every,
            detector=detector,
            tracker=tracker,
            track_scope=track_scope,
        )
    )
    return PipelineResult(observations, generate_zone_events(observations, zones))
