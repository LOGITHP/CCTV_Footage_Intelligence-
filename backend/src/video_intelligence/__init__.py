"""Pure domain primitives for timestamped video observations."""

from .domain import (
    FrameObservation,
    ObjectObservation,
    Zone,
    ZoneEvent,
    TimestampError,
    generate_zone_events,
    normalize_timestamp,
    normalize_timestamps,
)
from .media import (
    FrameSample,
    MediaProbeError,
    MediaProbeResult,
    MediaTimestampError,
    MediaToolError,
    MediaToolNotFoundError,
    VideoStreamInfo,
    probe_media,
    sample_frames,
)
from .pipeline import BrightBlobDetector, CentroidTracker, PipelineError, process_video
from .observables import (
    ObservableAppearance,
    extract_observable_appearance,
    normalize_yolo_label,
    object_category_for_label,
)

__all__ = [
    "FrameObservation",
    "ObjectObservation",
    "TimestampError",
    "Zone",
    "ZoneEvent",
    "generate_zone_events",
    "normalize_timestamp",
    "normalize_timestamps",
    "FrameSample",
    "MediaProbeError",
    "MediaProbeResult",
    "MediaTimestampError",
    "MediaToolError",
    "MediaToolNotFoundError",
    "VideoStreamInfo",
    "probe_media",
    "sample_frames",
    "BrightBlobDetector",
    "CentroidTracker",
    "PipelineError",
    "process_video",
    "ObservableAppearance",
    "extract_observable_appearance",
    "normalize_yolo_label",
    "object_category_for_label",
]
