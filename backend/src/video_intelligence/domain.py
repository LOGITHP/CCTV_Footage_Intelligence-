"""Deterministic, dependency-free domain logic for the first evidence slice.

The domain deliberately deals in media timestamps and structured observations;
it does not infer time from a nominal frame rate or perform detection itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from numbers import Real
from typing import Iterable, Tuple

from .observables import ObservableAppearance, normalize_yolo_label, object_category_for_label


class TimestampError(ValueError):
    """Raised when a media timestamp cannot be normalized safely."""


def normalize_timestamp(timestamp: Real, *, time_base: Real = 1.0) -> float:
    """Convert a non-negative media tick value to seconds.

    ``time_base`` is seconds per tick (for example, ``1/90000`` for a 90 kHz
    clock). This function never derives time from a frame rate.
    """
    if isinstance(timestamp, bool) or not isinstance(timestamp, Real):
        raise TimestampError("timestamp must be a real number")
    if isinstance(time_base, bool) or not isinstance(time_base, Real):
        raise TimestampError("time_base must be a real number")
    timestamp_value = float(timestamp)
    base_value = float(time_base)
    if not math.isfinite(timestamp_value) or timestamp_value < 0:
        raise TimestampError("timestamp must be finite and non-negative")
    if not math.isfinite(base_value) or base_value <= 0:
        raise TimestampError("time_base must be finite and positive")
    return timestamp_value * base_value


def normalize_timestamps(
    timestamps: Iterable[Real], *, time_base: Real = 1.0
) -> Tuple[float, ...]:
    """Normalize timestamps while preserving variable frame timing.

    Input order is meaningful and must be non-decreasing. Equal timestamps
    are valid (e.g. multiple streams sharing a presentation timestamp).
    """
    normalized = tuple(normalize_timestamp(value, time_base=time_base) for value in timestamps)
    if any(right < left for left, right in zip(normalized, normalized[1:])):
        raise TimestampError("timestamps must be non-decreasing")
    return normalized


@dataclass(frozen=True)
class ObjectObservation:
    object_id: str
    x: float
    y: float
    label: str = "object"
    confidence: float | None = None
    bbox: Tuple[float, float, float, float] | None = None
    category: str | None = None
    appearance: ObservableAppearance | None = None
    track_scope: str = "session"
    segmentation_polygon: Tuple[Tuple[float, float], ...] | None = None

    def __post_init__(self) -> None:
        if not self.object_id:
            raise ValueError("object_id must not be empty")
        if not (math.isfinite(self.x) and math.isfinite(self.y)):
            raise ValueError("object coordinates must be finite")
        if not self.label:
            raise ValueError("label must not be empty")
        canonical_label = normalize_yolo_label(self.label)
        if canonical_label != self.label:
            object.__setattr__(self, "label", canonical_label)
        inferred_category = object_category_for_label(canonical_label)
        if self.category is None and inferred_category is not None:
            object.__setattr__(self, "category", inferred_category)
        if self.category not in {None, "person", "vehicle"}:
            raise ValueError("category must be person, vehicle, or None")
        if not self.track_scope:
            raise ValueError("track_scope must not be empty")
        if self.appearance is not None and not isinstance(self.appearance, ObservableAppearance):
            raise TypeError("appearance must be ObservableAppearance or None")
        if self.confidence is not None and not (0 <= self.confidence <= 1):
            raise ValueError("confidence must be between 0 and 1")
        if self.bbox is not None:
            if len(self.bbox) != 4 or any(not math.isfinite(float(value)) for value in self.bbox):
                raise ValueError("bbox must contain four finite coordinates")
            left, top, right, bottom = (float(value) for value in self.bbox)
            if right < left or bottom < top:
                raise ValueError("bbox must have right >= left and bottom >= top")


@dataclass(frozen=True)
class FrameObservation:
    timestamp: Real
    objects: Tuple[ObjectObservation, ...]

    def __post_init__(self) -> None:
        scoped_object_ids = [(item.track_scope, item.object_id) for item in self.objects]
        if len(scoped_object_ids) != len(set(scoped_object_ids)):
            raise ValueError("an observation cannot contain duplicate object IDs")


@dataclass(frozen=True)
class Zone:
    zone_id: str
    polygon: Tuple[Tuple[float, float], ...]

    def __post_init__(self) -> None:
        if not self.zone_id:
            raise ValueError("zone_id must not be empty")
        if len(self.polygon) < 3:
            raise ValueError("a zone needs at least three polygon points")
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in self.polygon):
            raise ValueError("zone coordinates must be finite")

    def contains(self, x: float, y: float) -> bool:
        """Return whether a point is inside or on the polygon boundary."""
        inside = False
        points = self.polygon
        for index, (x1, y1) in enumerate(points):
            x2, y2 = points[(index + 1) % len(points)]
            cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
            if abs(cross) <= 1e-12 and min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2):
                return True
            if (y1 > y) != (y2 > y):
                crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < crossing_x:
                    inside = not inside
        return inside


@dataclass(frozen=True)
class ZoneEvent:
    event_id: str
    event_type: str
    zone_id: str
    object_id: str
    timestamp_seconds: float
    object_label: str = "object"
    track_scope: str = "session"


def generate_zone_events(
    observations: Iterable[FrameObservation],
    zones: Iterable[Zone],
    *,
    time_base: Real = 1.0,
) -> Tuple[ZoneEvent, ...]:
    """Generate stable enter/exit events from timestamped observations.

    The same input yields byte-for-byte-equivalent dataclass values. Objects
    and zones are sorted by ID, making output independent of mapping/list order.
    """
    frames = tuple(observations)
    timestamps = normalize_timestamps((frame.timestamp for frame in frames), time_base=time_base)
    ordered_zones = tuple(sorted(zones, key=lambda zone: zone.zone_id))
    events = []
    previous = {}
    for frame, timestamp in zip(frames, timestamps):
        for item in sorted(frame.objects, key=lambda value: (value.track_scope, value.object_id)):
            for zone in ordered_zones:
                key = (item.track_scope, item.object_id, zone.zone_id)
                current = zone.contains(item.x, item.y)
                prior = previous.get(key)
                if prior is None:
                    if current:
                        event_type = "enter"
                        identity = f"{item.track_scope}|{zone.zone_id}|{item.object_id}|{event_type}|{timestamp:.9f}"
                        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                        events.append(ZoneEvent(event_id, event_type, zone.zone_id, item.object_id, timestamp, item.label, item.track_scope))
                elif prior != current:
                    event_type = "enter" if current else "exit"
                    identity = f"{item.track_scope}|{zone.zone_id}|{item.object_id}|{event_type}|{timestamp:.9f}"
                    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                    events.append(ZoneEvent(event_id, event_type, zone.zone_id, item.object_id, timestamp, item.label, item.track_scope))
                previous[key] = current
    return tuple(events)
