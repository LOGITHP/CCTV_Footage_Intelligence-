"""Deterministic, explainable unusual-activity detection.

The detector in this module consumes structured observations/events that have
already been produced by the video pipeline.  It deliberately does not read a
clock, derive timestamps from FPS, or persist anything.  Evidence timestamps
are therefore always media timestamps supplied by the caller.  An optional
orchestration timestamp is carried through only when it is explicitly passed.

The public functions are intentionally usable without the FastAPI layer.  A
future retrieval service can call :func:`filter_anomalies` or
:func:`anomalies_for_scope` over persisted :class:`AnomalyRecord` mappings
without changing the chat service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any

from .domain import FrameObservation, ObjectObservation, ZoneEvent
from .observables import normalize_yolo_label


class AnomalyType(str, Enum):
    """Stable identifiers for the supported explainable rules."""

    OBJECT_COUNT_SPIKE = "object_count_spike"
    DWELL_DURATION = "dwell_duration"
    UNEXPECTED_LABEL = "unexpected_label"
    UNEXPECTED_CATEGORY = "unexpected_category"
    CAMERA_SILENCE = "camera_silence"
    PROCESSING_FAILURE = "processing_failure"


class AnomalySeverity(str, Enum):
    """Severity is a routing hint, not a claim about intent or identity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SourceQuality(str, Enum):
    """How directly the anomaly is supported by the supplied input."""

    OBSERVED = "observed"
    DERIVED = "derived"
    REPORTED = "reported"


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _clean_component(value: Any, *, fallback: str = "unknown", limit: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)([^/@\s]+):([^/@\s]+)@", r"\1[REDACTED]@", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    return text[:limit]


def _safe_number(value: Any, *, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a real number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a real number") from exc
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return result


def _normalize_category(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().lower().replace("_", " ").replace("-", " ").split())
    return text or None


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    """Thresholds and allow-lists for deterministic anomaly rules.

    ``baseline_count`` accepts one total baseline or a mapping keyed by a
    canonical label/category.  ``object_count_baseline`` is an explicit alias
    for callers that prefer the longer name.  A count rule is disabled when no
    baseline is supplied; an empty observation list is never treated as camera
    silence unless a health report says so explicitly.
    """

    baseline_count: float | Mapping[str, float] | None = None
    object_count_baseline: float | Mapping[str, float] | None = None
    count_spike_ratio: float = 2.0
    count_spike_min_delta: float = 1.0
    dwell_threshold_seconds: float | None = 30.0
    expected_labels: Iterable[str] = field(default_factory=frozenset)
    expected_categories: Iterable[str] = field(default_factory=frozenset)
    silence_threshold_seconds: float | None = 30.0

    def __post_init__(self) -> None:
        if self.baseline_count is not None:
            _validate_baseline(self.baseline_count, "baseline_count")
        if self.object_count_baseline is not None:
            _validate_baseline(self.object_count_baseline, "object_count_baseline")
        object.__setattr__(self, "count_spike_ratio", _safe_number(self.count_spike_ratio, name="count_spike_ratio", minimum=1.0))
        object.__setattr__(self, "count_spike_min_delta", _safe_number(self.count_spike_min_delta, name="count_spike_min_delta"))
        if self.dwell_threshold_seconds is not None:
            object.__setattr__(self, "dwell_threshold_seconds", _safe_number(self.dwell_threshold_seconds, name="dwell_threshold_seconds"))
        if self.silence_threshold_seconds is not None:
            object.__setattr__(self, "silence_threshold_seconds", _safe_number(self.silence_threshold_seconds, name="silence_threshold_seconds"))
        object.__setattr__(
            self,
            "expected_labels",
            frozenset(normalize_yolo_label(str(value)) for value in (self.expected_labels or ())),
        )
        object.__setattr__(
            self,
            "expected_categories",
            frozenset(_normalize_category(value) for value in (self.expected_categories or ()) if _normalize_category(value)),
        )

    @property
    def resolved_baseline(self) -> float | Mapping[str, float] | None:
        return self.baseline_count if self.baseline_count is not None else self.object_count_baseline


def _validate_baseline(value: float | Mapping[str, float], name: str) -> None:
    if isinstance(value, Mapping):
        for key, baseline in value.items():
            if not str(key).strip():
                raise ValueError(f"{name} keys must not be empty")
            _safe_number(baseline, name=f"{name}[{key!r}]")
        return
    _safe_number(value, name=name)


@dataclass(frozen=True, slots=True)
class AnomalyRecord:
    """Typed, serializable anomaly evidence record."""

    anomaly_id: str
    anomaly_type: AnomalyType
    camera_id: str
    session_id: str
    reason: str
    severity: AnomalySeverity
    timestamp_seconds: float | None = None
    related_event_ids: tuple[str, ...] = ()
    related_track_ids: tuple[str, ...] = ()
    source_quality: SourceQuality = SourceQuality.DERIVED
    orchestration_timestamp: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.anomaly_id).strip():
            raise ValueError("anomaly_id must not be empty")
        if not str(self.camera_id).strip() or not str(self.session_id).strip():
            raise ValueError("camera_id and session_id must not be empty")
        if not str(self.reason).strip():
            raise ValueError("reason must not be empty")
        object.__setattr__(self, "anomaly_type", AnomalyType(_enum_value(self.anomaly_type)))
        object.__setattr__(self, "severity", AnomalySeverity(_enum_value(self.severity)))
        object.__setattr__(self, "source_quality", SourceQuality(_enum_value(self.source_quality)))
        if self.timestamp_seconds is not None:
            object.__setattr__(self, "timestamp_seconds", _safe_number(self.timestamp_seconds, name="timestamp_seconds"))
        object.__setattr__(self, "related_event_ids", _unique_strings(self.related_event_ids))
        object.__setattr__(self, "related_track_ids", _unique_strings(self.related_track_ids))
        object.__setattr__(self, "details", dict(self.details or {}))

    @property
    def scope(self) -> str:
        return camera_session_scope(self.camera_id, self.session_id)

    @property
    def evidence_timestamp_seconds(self) -> float | None:
        """Alias making the media-only evidence boundary explicit."""

        return self.timestamp_seconds

    @property
    def event_ids(self) -> tuple[str, ...]:
        return self.related_event_ids

    @property
    def track_ids(self) -> tuple[str, ...]:
        return self.related_track_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "anomaly_type": self.anomaly_type.value,
            "camera_id": self.camera_id,
            "session_id": self.session_id,
            "scope": self.scope,
            "reason": self.reason,
            "severity": self.severity.value,
            "timestamp_seconds": self.timestamp_seconds,
            "related_event_ids": list(self.related_event_ids),
            "related_track_ids": list(self.related_track_ids),
            "source_quality": self.source_quality.value,
            "orchestration_timestamp": self.orchestration_timestamp,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class CameraHealthReport:
    """Explicit camera health input for silence/failure rules.

    ``silence_seconds`` and ``timestamp_seconds`` are caller-supplied values.
    The detector never compares a media timestamp with the current wall clock.
    """

    camera_id: str
    session_id: str
    status: str = "completed"
    silence_seconds: float | None = None
    processing_failure: bool = False
    failure_reason: str | None = None
    timestamp_seconds: float | None = None
    orchestration_timestamp: str | None = None

    def __post_init__(self) -> None:
        if not str(self.camera_id).strip() or not str(self.session_id).strip():
            raise ValueError("camera_id and session_id must not be empty")
        if self.silence_seconds is not None:
            object.__setattr__(self, "silence_seconds", _safe_number(self.silence_seconds, name="silence_seconds"))
        if self.timestamp_seconds is not None:
            object.__setattr__(self, "timestamp_seconds", _safe_number(self.timestamp_seconds, name="timestamp_seconds"))
        object.__setattr__(self, "status", _enum_value(self.status).strip().lower())


@dataclass(frozen=True, slots=True)
class _ObservedObject:
    object_id: str
    label: str
    category: str | None
    track_scope: str


@dataclass(frozen=True, slots=True)
class _ObservedFrame:
    timestamp_seconds: float
    objects: tuple[_ObservedObject, ...]


@dataclass(frozen=True, slots=True)
class _ObservedEvent:
    event_id: str | None
    event_type: str
    object_id: str | None
    timestamp_seconds: float | None
    track_scope: str | None


def camera_session_scope(camera_id: str, session_id: str) -> str:
    """Return the same stable camera/session scope convention used by T1."""

    camera = str(camera_id).strip()
    session = str(session_id).strip()
    if not camera or not session:
        raise ValueError("camera_id and session_id must not be empty")
    return f"camera:{camera}:session:{session}"


def _unique_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _coerce_object(value: ObjectObservation | Mapping[str, Any]) -> _ObservedObject:
    if isinstance(value, ObjectObservation):
        return _ObservedObject(
            object_id=str(value.object_id),
            label=normalize_yolo_label(value.label),
            category=_normalize_category(value.category),
            track_scope=str(value.track_scope),
        )
    if not isinstance(value, Mapping):
        raise TypeError("observation objects must be ObjectObservation values or mappings")
    object_id = str(value.get("object_id", value.get("track_id", ""))).strip()
    if not object_id:
        raise ValueError("observation object_id must not be empty")
    label = normalize_yolo_label(str(value.get("label", value.get("object_label", "object"))))
    track_scope = str(value.get("track_scope") or "session").strip()
    if not track_scope:
        raise ValueError("observation track_scope must not be empty")
    return _ObservedObject(object_id, label, _normalize_category(value.get("category")), track_scope)


def _coerce_frame(value: FrameObservation | Mapping[str, Any]) -> _ObservedFrame:
    if isinstance(value, FrameObservation):
        timestamp = value.timestamp
        objects = value.objects
    elif isinstance(value, Mapping):
        timestamp = value.get("timestamp_seconds", value.get("timestamp"))
        objects = value.get("objects") or ()
    else:
        raise TypeError("observations must be FrameObservation values or mappings")
    timestamp_seconds = _safe_number(timestamp, name="observation timestamp_seconds")
    normalized_objects = tuple(sorted((_coerce_object(item) for item in objects), key=lambda item: (item.track_scope, item.object_id, item.label)))
    return _ObservedFrame(timestamp_seconds, normalized_objects)


def _coerce_event(value: ZoneEvent | Mapping[str, Any]) -> _ObservedEvent:
    if isinstance(value, ZoneEvent):
        return _ObservedEvent(
            event_id=str(value.event_id),
            event_type=str(value.event_type).strip().lower(),
            object_id=str(value.object_id),
            timestamp_seconds=_safe_number(value.timestamp_seconds, name="event timestamp_seconds"),
            track_scope=str(value.track_scope),
        )
    if not isinstance(value, Mapping):
        raise TypeError("events must be ZoneEvent values or mappings")
    raw_timestamp = value.get("timestamp_seconds", value.get("timestamp"))
    timestamp = None if raw_timestamp is None else _safe_number(raw_timestamp, name="event timestamp_seconds")
    raw_object_id = value.get("object_id", value.get("track_id"))
    object_id = str(raw_object_id).strip() if raw_object_id is not None else None
    return _ObservedEvent(
        event_id=str(value["event_id"]).strip() if value.get("event_id") else None,
        event_type=str(value.get("event_type", "")).strip().lower(),
        object_id=object_id or None,
        timestamp_seconds=timestamp,
        track_scope=str(value["track_scope"]).strip() if value.get("track_scope") else None,
    )


def _normalize_inputs(
    observations: Iterable[FrameObservation | Mapping[str, Any]],
    events: Iterable[ZoneEvent | Mapping[str, Any]],
) -> tuple[tuple[_ObservedFrame, ...], tuple[_ObservedEvent, ...]]:
    frames = tuple(sorted((_coerce_frame(item) for item in observations), key=lambda item: item.timestamp_seconds))
    normalized_events = tuple(
        sorted(
            (_coerce_event(item) for item in events),
            key=lambda item: (item.timestamp_seconds is None, item.timestamp_seconds or 0.0, item.event_id or ""),
        )
    )
    return frames, normalized_events


def _event_ids_for(
    events: tuple[_ObservedEvent, ...],
    *,
    object_id: str | None = None,
    track_scope: str | None = None,
    timestamp_seconds: float | None = None,
) -> tuple[str, ...]:
    result: list[str] = []
    for event in events:
        if not event.event_id:
            continue
        if object_id is not None and event.object_id != object_id:
            continue
        if track_scope is not None and event.track_scope not in {None, track_scope}:
            continue
        if timestamp_seconds is not None and (event.timestamp_seconds is None or not math.isclose(event.timestamp_seconds, timestamp_seconds, abs_tol=1e-9)):
            continue
        result.append(event.event_id)
    return _unique_strings(sorted(result))


def _anomaly_id(
    anomaly_type: AnomalyType,
    camera_id: str,
    session_id: str,
    timestamp_seconds: float | None,
    related_event_ids: Iterable[str],
    related_track_ids: Iterable[str],
    details: Mapping[str, Any],
) -> str:
    identity = {
        "type": anomaly_type.value,
        "camera_id": camera_id,
        "session_id": session_id,
        "timestamp_seconds": timestamp_seconds,
        "event_ids": list(related_event_ids),
        "track_ids": list(related_track_ids),
        "details": dict(details),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    return f"anomaly-{digest}"


def _make_anomaly(
    anomaly_type: AnomalyType,
    *,
    camera_id: str,
    session_id: str,
    reason: str,
    severity: AnomalySeverity,
    timestamp_seconds: float | None,
    related_event_ids: Iterable[str] = (),
    related_track_ids: Iterable[str] = (),
    source_quality: SourceQuality,
    orchestration_timestamp: str | None,
    details: Mapping[str, Any] | None = None,
) -> AnomalyRecord:
    event_ids = _unique_strings(related_event_ids)
    track_ids = _unique_strings(related_track_ids)
    safe_details = dict(details or {})
    return AnomalyRecord(
        anomaly_id=_anomaly_id(anomaly_type, camera_id, session_id, timestamp_seconds, event_ids, track_ids, safe_details),
        anomaly_type=anomaly_type,
        camera_id=camera_id,
        session_id=session_id,
        reason=_clean_component(reason),
        severity=severity,
        timestamp_seconds=timestamp_seconds,
        related_event_ids=event_ids,
        related_track_ids=track_ids,
        source_quality=source_quality,
        orchestration_timestamp=orchestration_timestamp,
        details=safe_details,
    )


def _baseline_entries(baseline: float | Mapping[str, float] | None) -> tuple[tuple[str | None, float], ...]:
    if baseline is None:
        return ()
    if isinstance(baseline, Mapping):
        return tuple(sorted(((str(key).strip(), _safe_number(value, name=f"baseline[{key!r}]")) for key, value in baseline.items()), key=lambda item: item[0] or ""))
    return ((None, _safe_number(baseline, name="baseline_count")),)


def _count_for_key(objects: tuple[_ObservedObject, ...], key: str | None) -> int:
    if key is None or key.lower() in {"all", "total", "*"}:
        return len(objects)
    normalized = normalize_yolo_label(key)
    if _normalize_category(key) in {"person", "vehicle"}:
        return sum(1 for item in objects if item.category == _normalize_category(key))
    return sum(1 for item in objects if item.label == normalized)


def _severity_for_count(ratio: float) -> AnomalySeverity:
    return AnomalySeverity.HIGH if ratio >= 3.0 or math.isinf(ratio) else AnomalySeverity.MEDIUM


def _detect_count_spikes(
    frames: tuple[_ObservedFrame, ...],
    events: tuple[_ObservedEvent, ...],
    *,
    camera_id: str,
    session_id: str,
    config: AnomalyConfig,
    orchestration_timestamp: str | None,
) -> tuple[AnomalyRecord, ...]:
    anomalies: list[AnomalyRecord] = []
    for key, baseline in _baseline_entries(config.resolved_baseline):
        candidates: list[tuple[int, float, _ObservedFrame]] = []
        for frame in frames:
            count = _count_for_key(frame.objects, key)
            delta = count - baseline
            ratio = (count / baseline) if baseline > 0 else (math.inf if count > 0 else 0.0)
            if delta >= config.count_spike_min_delta and (ratio >= config.count_spike_ratio or (baseline == 0 and count > 0)):
                candidates.append((count, ratio, frame))
        if not candidates:
            continue
        count, ratio, frame = max(candidates, key=lambda item: (item[0], item[1], -item[2].timestamp_seconds))
        label = "" if key is None or key.lower() in {"all", "total", "*"} else f" for {normalize_yolo_label(key)}"
        ratio_text = "unbounded" if math.isinf(ratio) else f"{ratio:.2f}x"
        tracks = sorted({item.object_id for item in frame.objects})
        details = {
            "baseline_key": key or "total",
            "baseline_count": baseline,
            "observed_count": count,
            "ratio": None if math.isinf(ratio) else round(ratio, 6),
            "minimum_ratio": config.count_spike_ratio,
        }
        anomalies.append(
            _make_anomaly(
                AnomalyType.OBJECT_COUNT_SPIKE,
                camera_id=camera_id,
                session_id=session_id,
                reason=f"Object count spike{label}: observed {count} versus baseline {baseline:g} ({ratio_text}).",
                severity=_severity_for_count(ratio),
                timestamp_seconds=frame.timestamp_seconds,
                related_event_ids=_event_ids_for(events, timestamp_seconds=frame.timestamp_seconds),
                related_track_ids=tracks,
                source_quality=SourceQuality.DERIVED,
                orchestration_timestamp=orchestration_timestamp,
                details=details,
            )
        )
    return tuple(anomalies)


def _detect_dwell(
    frames: tuple[_ObservedFrame, ...],
    events: tuple[_ObservedEvent, ...],
    *,
    camera_id: str,
    session_id: str,
    config: AnomalyConfig,
    orchestration_timestamp: str | None,
) -> tuple[AnomalyRecord, ...]:
    threshold = config.dwell_threshold_seconds
    if threshold is None:
        return ()
    timestamps: dict[tuple[str, str], list[float]] = {}
    for frame in frames:
        for item in frame.objects:
            timestamps.setdefault((item.track_scope, item.object_id), []).append(frame.timestamp_seconds)
    for event in events:
        if event.object_id and event.timestamp_seconds is not None and event.event_type in {"enter", "exit"}:
            timestamps.setdefault((event.track_scope or "session", event.object_id), []).append(event.timestamp_seconds)

    anomalies: list[AnomalyRecord] = []
    for (track_scope, object_id), values in sorted(timestamps.items()):
        if len(values) < 2:
            continue
        first = min(values)
        last = max(values)
        duration = last - first
        if duration <= threshold:
            continue
        related_events = _event_ids_for(events, object_id=object_id, track_scope=track_scope)
        details = {
            "duration_seconds": round(duration, 6),
            "threshold_seconds": threshold,
            "track_scope": track_scope,
        }
        anomalies.append(
            _make_anomaly(
                AnomalyType.DWELL_DURATION,
                camera_id=camera_id,
                session_id=session_id,
                reason=f"Dwell duration exceeded threshold: track {object_id} remained {duration:.3f}s (threshold {threshold:.3f}s).",
                severity=AnomalySeverity.HIGH if duration >= threshold * 2 else AnomalySeverity.MEDIUM,
                timestamp_seconds=last,
                related_event_ids=related_events,
                related_track_ids=(object_id,),
                source_quality=SourceQuality.DERIVED,
                orchestration_timestamp=orchestration_timestamp,
                details=details,
            )
        )
    return tuple(anomalies)


def _detect_unexpected(
    frames: tuple[_ObservedFrame, ...],
    events: tuple[_ObservedEvent, ...],
    *,
    camera_id: str,
    session_id: str,
    config: AnomalyConfig,
    orchestration_timestamp: str | None,
) -> tuple[AnomalyRecord, ...]:
    if not config.expected_labels and not config.expected_categories:
        return ()
    first_seen: dict[tuple[AnomalyType, str, str, str, str], tuple[float, _ObservedObject]] = {}
    for frame in frames:
        for item in frame.objects:
            label_bad = bool(config.expected_labels) and item.label not in config.expected_labels
            category = item.category or "unknown"
            category_bad = bool(config.expected_categories) and category not in config.expected_categories
            if label_bad:
                key = (AnomalyType.UNEXPECTED_LABEL, item.track_scope, item.object_id, item.label, category)
                first_seen.setdefault(key, (frame.timestamp_seconds, item))
            if category_bad:
                key = (AnomalyType.UNEXPECTED_CATEGORY, item.track_scope, item.object_id, item.label, category)
                first_seen.setdefault(key, (frame.timestamp_seconds, item))

    anomalies: list[AnomalyRecord] = []
    for (anomaly_type, track_scope, object_id, label, category), (timestamp, item) in sorted(
        first_seen.items(), key=lambda entry: (entry[1][0], entry[0][0].value, entry[0][1], entry[0][2], entry[0][3])
    ):
        if anomaly_type is AnomalyType.UNEXPECTED_LABEL:
            expected = ", ".join(sorted(config.expected_labels))
            reason = f"Unexpected object label '{_clean_component(label)}' observed; expected labels: {expected}."
            details = {"label": label, "expected_labels": sorted(config.expected_labels), "track_scope": track_scope}
        else:
            expected = ", ".join(sorted(config.expected_categories))
            reason = f"Unexpected object category '{_clean_component(category)}' observed; expected categories: {expected}."
            details = {"category": category, "expected_categories": sorted(config.expected_categories), "track_scope": track_scope}
        anomalies.append(
            _make_anomaly(
                anomaly_type,
                camera_id=camera_id,
                session_id=session_id,
                reason=reason,
                severity=AnomalySeverity.MEDIUM,
                timestamp_seconds=timestamp,
                related_event_ids=_event_ids_for(events, object_id=object_id, track_scope=track_scope),
                related_track_ids=(item.object_id,),
                source_quality=SourceQuality.OBSERVED,
                orchestration_timestamp=orchestration_timestamp,
                details=details,
            )
        )
    return tuple(anomalies)


def _health_value(report: Any, name: str, default: Any = None) -> Any:
    if isinstance(report, Mapping):
        return report.get(name, default)
    return getattr(report, name, default)


def _coerce_health_report(report: CameraHealthReport | Mapping[str, Any] | Any, *, camera_id: str, session_id: str) -> CameraHealthReport:
    if isinstance(report, CameraHealthReport):
        health = report
    else:
        report_camera = _health_value(report, "camera_id")
        report_session = _health_value(report, "session_id")
        if report_camera is not None and str(report_camera).strip() != camera_id:
            raise ValueError("health report camera_id does not match anomaly scope")
        if report_session is not None and str(report_session).strip() != session_id:
            raise ValueError("health report session_id does not match anomaly scope")
        status = _enum_value(_health_value(report, "status", "completed"))
        failure_reason = _health_value(report, "failure_reason", _health_value(report, "last_error"))
        timestamp = _health_value(report, "timestamp_seconds", _health_value(report, "last_media_timestamp_seconds"))
        silence = _health_value(report, "silence_seconds", _health_value(report, "silent_for_seconds"))
        if silence is None:
            last_media = _health_value(report, "last_media_timestamp_seconds")
            current_media = _health_value(report, "current_media_timestamp_seconds")
            if last_media is not None and current_media is not None:
                silence = _safe_number(current_media, name="current_media_timestamp_seconds") - _safe_number(last_media, name="last_media_timestamp_seconds")
                if silence < 0:
                    raise ValueError("current media timestamp must not precede last media timestamp")
        explicit_silence = _health_value(report, "silence", False)
        if explicit_silence and silence is None:
            silence = 0.0
            status = "silent"
        failure = bool(_health_value(report, "processing_failure", False)) or bool(failure_reason)
        health = CameraHealthReport(
            camera_id=camera_id,
            session_id=session_id,
            status=status,
            silence_seconds=silence,
            processing_failure=failure,
            failure_reason=str(failure_reason) if failure_reason is not None else None,
            timestamp_seconds=timestamp,
            orchestration_timestamp=_health_value(report, "orchestration_timestamp", _health_value(report, "finished_at")),
        )
    if health.camera_id != camera_id or health.session_id != session_id:
        raise ValueError("health report scope does not match anomaly scope")
    return health


def _detect_health(
    report: CameraHealthReport | Mapping[str, Any] | Any,
    *,
    camera_id: str,
    session_id: str,
    config: AnomalyConfig,
    orchestration_timestamp: str | None,
) -> tuple[AnomalyRecord, ...]:
    health = _coerce_health_report(report, camera_id=camera_id, session_id=session_id)
    orchestration = orchestration_timestamp if orchestration_timestamp is not None else health.orchestration_timestamp
    status = health.status
    failures = {"failed", "failure", "processing_failed", "processing_failure", "error"}
    anomalies: list[AnomalyRecord] = []
    if health.processing_failure or bool(health.failure_reason) or status in failures:
        status_text = _clean_component(status)
        reason = f"Processing failure reported for camera {camera_id} (status '{status_text}')."
        if health.failure_reason:
            reason = f"{reason} {_clean_component(health.failure_reason)}"
        anomalies.append(
            _make_anomaly(
                AnomalyType.PROCESSING_FAILURE,
                camera_id=camera_id,
                session_id=session_id,
                reason=reason,
                severity=AnomalySeverity.HIGH,
                timestamp_seconds=health.timestamp_seconds,
                source_quality=SourceQuality.REPORTED,
                orchestration_timestamp=orchestration,
                details={"status": status, "error_present": bool(health.failure_reason)},
            )
        )

    silence_explicit = status in {"silent", "silenced"} or health.silence_seconds is not None
    threshold = config.silence_threshold_seconds
    if silence_explicit and (
        status in {"silent", "silenced"}
        or threshold is None
        or (health.silence_seconds is not None and health.silence_seconds > threshold)
    ):
        duration = health.silence_seconds
        duration_text = "reported" if duration is None else f"{duration:.3f}s"
        threshold_text = "no threshold" if threshold is None else f"{threshold:.3f}s"
        anomalies.append(
            _make_anomaly(
                AnomalyType.CAMERA_SILENCE,
                camera_id=camera_id,
                session_id=session_id,
                reason=f"Camera silence reported: no media activity for {duration_text} (threshold {threshold_text}).",
                severity=AnomalySeverity.HIGH,
                timestamp_seconds=health.timestamp_seconds,
                source_quality=SourceQuality.REPORTED,
                orchestration_timestamp=orchestration,
                details={"silence_seconds": duration, "threshold_seconds": threshold},
            )
        )
    return tuple(anomalies)


def detect_anomalies(
    observations: Iterable[FrameObservation | Mapping[str, Any]] = (),
    events: Iterable[ZoneEvent | Mapping[str, Any]] = (),
    *,
    camera_id: str,
    session_id: str,
    config: AnomalyConfig | None = None,
    health_report: CameraHealthReport | Mapping[str, Any] | Any | None = None,
    orchestration_timestamp: str | None = None,
    baseline_count: float | Mapping[str, float] | None = None,
    dwell_threshold_seconds: float | None = None,
    expected_labels: Iterable[str] | None = None,
    expected_categories: Iterable[str] | None = None,
    silence_threshold_seconds: float | None = None,
) -> tuple[AnomalyRecord, ...]:
    """Run all configured rules for one explicit camera/session scope."""

    camera = str(camera_id).strip()
    session = str(session_id).strip()
    camera_session_scope(camera, session)
    resolved = config or AnomalyConfig()
    overrides: dict[str, Any] = {}
    if baseline_count is not None:
        overrides["baseline_count"] = baseline_count
    if dwell_threshold_seconds is not None:
        overrides["dwell_threshold_seconds"] = dwell_threshold_seconds
    if expected_labels is not None:
        overrides["expected_labels"] = frozenset(expected_labels)
    if expected_categories is not None:
        overrides["expected_categories"] = frozenset(expected_categories)
    if silence_threshold_seconds is not None:
        overrides["silence_threshold_seconds"] = silence_threshold_seconds
    if overrides:
        from dataclasses import replace

        resolved = replace(resolved, **overrides)
    frames, normalized_events = _normalize_inputs(observations, events)
    result = list(_detect_count_spikes(frames, normalized_events, camera_id=camera, session_id=session, config=resolved, orchestration_timestamp=orchestration_timestamp))
    result.extend(_detect_dwell(frames, normalized_events, camera_id=camera, session_id=session, config=resolved, orchestration_timestamp=orchestration_timestamp))
    result.extend(_detect_unexpected(frames, normalized_events, camera_id=camera, session_id=session, config=resolved, orchestration_timestamp=orchestration_timestamp))
    if health_report is not None:
        result.extend(_detect_health(health_report, camera_id=camera, session_id=session, config=resolved, orchestration_timestamp=orchestration_timestamp))
    result.sort(key=lambda item: (item.timestamp_seconds is None, item.timestamp_seconds if item.timestamp_seconds is not None else math.inf, item.anomaly_type.value, item.anomaly_id))
    unique: dict[str, AnomalyRecord] = {}
    for anomaly in result:
        unique.setdefault(anomaly.anomaly_id, anomaly)
    return tuple(unique.values())


def detect_object_count_spikes(
    observations: Iterable[FrameObservation | Mapping[str, Any]],
    *,
    camera_id: str,
    session_id: str,
    baseline_count: float | Mapping[str, float],
    events: Iterable[ZoneEvent | Mapping[str, Any]] = (),
    config: AnomalyConfig | None = None,
    orchestration_timestamp: str | None = None,
) -> tuple[AnomalyRecord, ...]:
    """Run only the object-count rule."""

    frames, normalized_events = _normalize_inputs(observations, events)
    resolved = config or AnomalyConfig()
    from dataclasses import replace

    resolved = replace(resolved, baseline_count=baseline_count)
    return _detect_count_spikes(frames, normalized_events, camera_id=str(camera_id).strip(), session_id=str(session_id).strip(), config=resolved, orchestration_timestamp=orchestration_timestamp)


def detect_dwell_anomalies(
    observations: Iterable[FrameObservation | Mapping[str, Any]],
    *,
    camera_id: str,
    session_id: str,
    dwell_threshold_seconds: float,
    events: Iterable[ZoneEvent | Mapping[str, Any]] = (),
    config: AnomalyConfig | None = None,
    orchestration_timestamp: str | None = None,
) -> tuple[AnomalyRecord, ...]:
    """Run only the dwell-duration rule."""

    frames, normalized_events = _normalize_inputs(observations, events)
    resolved = config or AnomalyConfig()
    from dataclasses import replace

    resolved = replace(resolved, dwell_threshold_seconds=dwell_threshold_seconds)
    return _detect_dwell(frames, normalized_events, camera_id=str(camera_id).strip(), session_id=str(session_id).strip(), config=resolved, orchestration_timestamp=orchestration_timestamp)


def detect_unexpected_objects(
    observations: Iterable[FrameObservation | Mapping[str, Any]],
    *,
    camera_id: str,
    session_id: str,
    expected_labels: Iterable[str] = (),
    expected_categories: Iterable[str] = (),
    events: Iterable[ZoneEvent | Mapping[str, Any]] = (),
    orchestration_timestamp: str | None = None,
) -> tuple[AnomalyRecord, ...]:
    """Run only unexpected-label/category rules."""

    frames, normalized_events = _normalize_inputs(observations, events)
    config = AnomalyConfig(expected_labels=frozenset(expected_labels), expected_categories=frozenset(expected_categories))
    return _detect_unexpected(frames, normalized_events, camera_id=str(camera_id).strip(), session_id=str(session_id).strip(), config=config, orchestration_timestamp=orchestration_timestamp)


def detect_health_anomalies(
    health_report: CameraHealthReport | Mapping[str, Any] | Any,
    *,
    camera_id: str,
    session_id: str,
    silence_threshold_seconds: float | None = 30.0,
    orchestration_timestamp: str | None = None,
) -> tuple[AnomalyRecord, ...]:
    """Run only explicit camera-silence/processing-failure rules."""

    return _detect_health(
        health_report,
        camera_id=str(camera_id).strip(),
        session_id=str(session_id).strip(),
        config=AnomalyConfig(silence_threshold_seconds=silence_threshold_seconds),
        orchestration_timestamp=orchestration_timestamp,
    )


def _coerce_anomaly(value: AnomalyRecord | Mapping[str, Any]) -> AnomalyRecord:
    if isinstance(value, AnomalyRecord):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("anomalies must be AnomalyRecord values or mappings")
    return AnomalyRecord(
        anomaly_id=str(value.get("anomaly_id", "")),
        anomaly_type=value.get("anomaly_type", value.get("type", "processing_failure")),
        camera_id=str(value.get("camera_id", "")),
        session_id=str(value.get("session_id", "")),
        reason=str(value.get("reason", "anomaly")),
        severity=value.get("severity", "medium"),
        timestamp_seconds=value.get("timestamp_seconds"),
        related_event_ids=tuple(value.get("related_event_ids") or value.get("event_ids") or ()),
        related_track_ids=tuple(value.get("related_track_ids") or value.get("track_ids") or ()),
        source_quality=value.get("source_quality", "derived"),
        orchestration_timestamp=value.get("orchestration_timestamp"),
        details=value.get("details", {}),
    )


def filter_anomalies(
    anomalies: Iterable[AnomalyRecord | Mapping[str, Any]],
    *,
    camera_id: str | None = None,
    session_id: str | None = None,
    anomaly_type: AnomalyType | str | None = None,
    severity: AnomalySeverity | str | None = None,
    source_quality: SourceQuality | str | None = None,
) -> tuple[AnomalyRecord, ...]:
    """Filter typed/mapping records without mixing camera/session scopes."""

    camera = str(camera_id).strip() if camera_id is not None else None
    session = str(session_id).strip() if session_id is not None else None
    type_value = _enum_value(anomaly_type) if anomaly_type is not None else None
    severity_value = _enum_value(severity) if severity is not None else None
    quality_value = _enum_value(source_quality) if source_quality is not None else None
    result = []
    for value in anomalies:
        item = _coerce_anomaly(value)
        if camera is not None and item.camera_id != camera:
            continue
        if session is not None and item.session_id != session:
            continue
        if type_value is not None and item.anomaly_type.value != type_value:
            continue
        if severity_value is not None and item.severity.value != severity_value:
            continue
        if quality_value is not None and item.source_quality.value != quality_value:
            continue
        result.append(item)
    return tuple(result)


def anomalies_for_scope(
    anomalies: Iterable[AnomalyRecord | Mapping[str, Any]],
    camera_id: str,
    session_id: str,
) -> tuple[AnomalyRecord, ...]:
    """Return only anomalies from one exact camera/session scope."""

    return filter_anomalies(anomalies, camera_id=camera_id, session_id=session_id)


def anomalies_for_camera(
    anomalies: Iterable[AnomalyRecord | Mapping[str, Any]], camera_id: str
) -> tuple[AnomalyRecord, ...]:
    return filter_anomalies(anomalies, camera_id=camera_id)


def anomalies_for_session(
    anomalies: Iterable[AnomalyRecord | Mapping[str, Any]], session_id: str
) -> tuple[AnomalyRecord, ...]:
    return filter_anomalies(anomalies, session_id=session_id)


def filter_anomalies_by_scope(
    anomalies: Iterable[AnomalyRecord | Mapping[str, Any]],
    *,
    camera_id: str | None = None,
    session_id: str | None = None,
) -> tuple[AnomalyRecord, ...]:
    return filter_anomalies(anomalies, camera_id=camera_id, session_id=session_id)


def anomaly_notification_payloads(
    anomalies: Iterable[AnomalyRecord | Mapping[str, Any]],
    *,
    channels: Iterable[str],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Build secret-free notification mappings compatible with the outbox.

    The nested payload keeps the existing ``alert_id``/``event_id`` shape as
    aliases while adding the required anomaly, camera, and session scope.  Raw
    ``details`` and orchestration metadata are intentionally omitted.
    """

    normalized_channels = tuple(str(channel).strip().lower() for channel in channels if str(channel).strip())
    payloads: list[tuple[str, dict[str, Any]]] = []
    for anomaly in (_coerce_anomaly(value) for value in anomalies):
        first_event = anomaly.related_event_ids[0] if anomaly.related_event_ids else None
        for channel in normalized_channels:
            notification_id = f"anomaly:{anomaly.anomaly_id}:{channel}"
            payload = {
                "anomaly_id": anomaly.anomaly_id,
                "alert_id": anomaly.anomaly_id,
                "anomaly_type": anomaly.anomaly_type.value,
                "camera_id": anomaly.camera_id,
                "session_id": anomaly.session_id,
                "scope": anomaly.scope,
                "reason": anomaly.reason,
                "severity": anomaly.severity.value,
                "timestamp_seconds": anomaly.timestamp_seconds,
                "related_event_ids": list(anomaly.related_event_ids),
                "related_track_ids": list(anomaly.related_track_ids),
                "event_id": first_event,
                "source_quality": anomaly.source_quality.value,
                "status": "open",
            }
            yield_payload = {
                "notification_id": notification_id,
                "channel": channel,
                "camera_id": anomaly.camera_id,
                "session_id": anomaly.session_id,
                "subject": "Video intelligence anomaly",
                "body": f"{anomaly.reason} Camera {anomaly.camera_id}, session {anomaly.session_id}.",
                "payload": payload,
            }
            payloads.append((channel, yield_payload))
    return tuple(payloads)


# Names that read naturally at the integration boundary.
notification_payloads_for_anomalies = anomaly_notification_payloads
to_notification_payloads = anomaly_notification_payloads
notification_payloads = anomaly_notification_payloads


__all__ = [
    "AnomalyConfig",
    "AnomalyRecord",
    "AnomalySeverity",
    "AnomalyType",
    "CameraHealthReport",
    "SourceQuality",
    "anomalies_for_camera",
    "anomalies_for_scope",
    "anomalies_for_session",
    "anomaly_notification_payloads",
    "camera_session_scope",
    "detect_anomalies",
    "detect_dwell_anomalies",
    "detect_health_anomalies",
    "detect_object_count_spikes",
    "detect_unexpected_objects",
    "filter_anomalies",
    "filter_anomalies_by_scope",
    "notification_payloads_for_anomalies",
    "notification_payloads",
    "to_notification_payloads",
]
