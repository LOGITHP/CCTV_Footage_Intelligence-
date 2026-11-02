from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservableAppearanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dominant_color: str | None = None
    upper_color: str | None = None
    lower_color: str | None = None
    vehicle_color: str | None = None
    vehicle_type: str | None = None
    source: Literal["detection_crop"] = "detection_crop"
    estimated: Literal[True] = True
    biometric: Literal[False] = False


class ObservationObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_id: str = Field(min_length=1)
    x: float
    y: float
    label: str = "object"
    confidence: float | None = Field(default=None, ge=0, le=1)
    bbox: tuple[float, float, float, float] | None = None
    category: Literal["person", "vehicle"] | None = None
    track_scope: str | None = Field(default=None, min_length=1)
    appearance: ObservableAppearanceInput | None = None


class ObservationFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: float
    objects: list[ObservationObject]


class ZoneInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    zone_id: str = Field(min_length=1)
    polygon: list[tuple[float, float]] = Field(min_length=3)
    label: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_zone_dict(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "polygon" not in data:
            for key, val in list(data.items()):
                if key in ("zone_id", "label"):
                    continue
                if isinstance(val, (list, tuple)) and len(val) >= 3:
                    if all(isinstance(pt, (list, tuple)) and len(pt) >= 2 for pt in val):
                        data["polygon"] = val
                        if not data.get("label") and key not in ("coords", "coordinates", "points", "boundary"):
                            data["label"] = str(key)
                        break
        return data


class CameraSourceRequest(BaseModel):
    """Strict camera source envelope shared by register and update routes.

    The frontend uses ``path`` for both local files and RTSP URLs.  ``url`` is
    also accepted for API clients that prefer the more explicit RTSP spelling;
    exactly one of the two fields must be supplied and the backend normalizes
    it to ``source.path`` in responses.
    """

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["file", "rtsp", "device"]
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    url: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_location(self) -> "CameraSourceRequest":
        if (self.path is None) == (self.url is None):
            raise ValueError("source must provide exactly one of path or url")
        value = self.path or self.url
        if not value or not value.strip():
            raise ValueError("source path or url must not be empty")
        return self

    @property
    def location(self) -> str:
        return self.path or self.url or ""


class CameraProcessingOptionsRequest(BaseModel):
    """Validated subset of the existing finite pipeline options.

    The anomaly fields are orchestration-only settings.  They remain in the
    durable camera definition but are not passed to the media pipeline.
    """

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    zones: list[ZoneInput] = Field(default_factory=list)
    every: int = Field(default=1, ge=1)
    threshold: int = Field(default=200, ge=0, le=255)
    min_area: int = Field(default=25, ge=1)
    clip_padding: float = Field(default=5.0, ge=0)
    device: Literal["cpu", "gpu"] = "cpu"
    model_profile: Literal["baseline", "yolo", "sam", "sam3.1", "sam2"] = "baseline"
    tracker_profile: Literal["centroid", "bytetrack", "sam", "sam3.1", "sam2"] = "centroid"
    track_scope: str | None = Field(default=None, min_length=1, max_length=255)
    baseline_count: float | dict[str, float] | None = None
    count_spike_ratio: float = Field(default=2.0, ge=1.0)
    count_spike_min_delta: float = Field(default=1.0, ge=0)
    dwell_threshold_seconds: float | None = Field(default=30.0, ge=0)
    expected_labels: list[str] = Field(default_factory=list)
    expected_categories: list[str] = Field(default_factory=list)
    silence_threshold_seconds: float | None = Field(default=30.0, ge=0)

    @model_validator(mode="after")
    def validate_baseline(self) -> "CameraProcessingOptionsRequest":
        if self.baseline_count is not None:
            if isinstance(self.baseline_count, dict):
                if any(not str(key).strip() or value < 0 for key, value in self.baseline_count.items()):
                    raise ValueError("baseline_count mapping keys must be non-empty and values non-negative")
            elif self.baseline_count < 0:
                raise ValueError("baseline_count must be non-negative")
        return self


class CameraRequest(BaseModel):
    """Strict persisted camera registration payload."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    camera_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    source: CameraSourceRequest
    enabled: bool = True
    zones: list[ZoneInput] = Field(default_factory=list)
    processing_options: CameraProcessingOptionsRequest = Field(default_factory=CameraProcessingOptionsRequest)


class CameraUpdateRequest(BaseModel):
    """Strict partial update payload for an existing camera."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    source: CameraSourceRequest | None = None
    enabled: bool | None = None
    zones: list[ZoneInput] | None = None
    processing_options: CameraProcessingOptionsRequest | None = None


class CameraRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    continuous: bool = False
    max_frames: int | None = Field(default=None, ge=1, le=1_000_000)
    max_seconds: float | None = Field(default=None, gt=0, le=86_400)


# Descriptive aliases used by integrations that name the operation explicitly.
CameraCreateRequest = CameraRequest
CameraPatchRequest = CameraUpdateRequest
CameraSourceInput = CameraSourceRequest


class ProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[ObservationFrame] = []
    zones: list[ZoneInput] = []
    time_base: float = Field(default=1.0, gt=0)


class MediaProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zones: list[ZoneInput] = []
    every: int = Field(default=1, ge=1)
    threshold: int = Field(default=200, ge=0, le=255)
    min_area: int = Field(default=25, ge=1)
    clip_padding: float = Field(default=5.0, ge=0)
    device: str = Field(default="cpu", pattern="^(cpu|gpu)$")
    model_profile: str = Field(default="baseline", pattern="^(baseline|yolo|sam|sam3\\.1|sam2)$")
    tracker_profile: str = Field(default="centroid", pattern="^(centroid|bytetrack|sam|sam3\\.1|sam2)$")
    track_scope: str | None = Field(default=None, min_length=1)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_id: str | None = None
    zone_id: str | None = None
    object_label: str | None = None
    event_type: str | None = None
    query: str | None = None


class SemanticIndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modality: str = Field(default="events", pattern="^(events|frames)$")
    every: int = Field(default=1, ge=1)


class SemanticSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=100)
    video_id: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    video_id: str | None = None
    camera_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    event_id: str | None = None
    object_id: str | None = None
    zone_id: str | None = None


class AlertRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    event_type: str | None = None
    zone_id: str | None = None
    object_label: str | None = None
    enabled: bool = True
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=1024)
    role: str = Field(default="reviewer", pattern="^(admin|operator|reviewer)$")
