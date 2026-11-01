"""Durable camera monitoring worker.

The worker owns the camera-run side effects that used to live in the HTTP
module.  It loads the authoritative camera row for every execution, scopes all
observations and events to one camera/session pair, and persists a terminal
report only after the finite pipeline and its side effects have completed.

No FastAPI imports are allowed here.  The service is usable by both the local
sync route and a Celery task with injected stores/runners for tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path, PureWindowsPath
from threading import RLock
from typing import Any, Callable, Mapping

from ..anomalies import AnomalyConfig, anomaly_notification_payloads, detect_anomalies
from ..cameras import (
    CameraConfigurationError,
    CameraDefinition,
    CameraMonitoringReport,
    CameraSourceKind,
    MonitoringStatus,
    camera_track_scope,
)
from ..domain import FrameObservation, ObjectObservation, generate_zone_events
from ..observables import ObservableAppearance
from ..pipeline import PipelineResult
from .notifications import notification_payloads
from .outbox import OutboxStore
from .processing import ProcessingOptions, default_pipeline_runner


Clock = Callable[[], datetime | str]
PipelineRunner = Callable[[str, ProcessingOptions], PipelineResult | Mapping[str, Any]]

_TERMINAL_STATUSES = {
    MonitoringStatus.COMPLETED.value,
    MonitoringStatus.FAILED.value,
    MonitoringStatus.STOPPED.value,
    MonitoringStatus.DISABLED.value,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clock_value(clock: Clock) -> str:
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("monitoring clock must return an aware datetime")
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError("monitoring clock must return datetime or string")


def _safe_error(error: BaseException) -> str:
    """Return a public-safe failure category without source paths/secrets."""

    text = str(error).lower()
    if "rtsp" in text or "live source" in text or "not supported" in text:
        return "live camera sources are not supported by the finite camera worker"
    if "unavailable" in text or "not found" in text or "no such file" in text:
        return "camera source is unavailable"
    if isinstance(error, CameraConfigurationError):
        return "camera configuration is invalid"
    if isinstance(error, ValueError) and "event" in text:
        return "camera pipeline returned malformed event data"
    if isinstance(error, ValueError) and "observation" in text:
        return "camera pipeline returned malformed observation data"
    return "camera processing failed"


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(vars(value))
    except TypeError as exc:
        raise TypeError("pipeline events must be mappings or event objects") from exc


def _finite_media_timestamp(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite non-negative media timestamp")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite non-negative media timestamp") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative media timestamp")
    return result


def _source_name(source_path: str) -> str:
    return Path(source_path).name or PureWindowsPath(source_path).name or "camera-run"


class CameraMonitoringWorker:
    """Run and durably complete one finite camera/session execution.

    ``store`` must provide the existing camera/report/video/observation/event/
    alert methods exposed by :class:`Store` and :class:`PostgresStore`.
    ``pipeline_runner`` and ``outbox`` are intentionally injectable so a
    Celery task can construct infrastructure lazily and tests need no broker or
    model downloads.
    """

    _run_locks: dict[tuple[int, str, str], RLock] = {}
    _run_locks_guard = RLock()

    def __init__(
        self,
        store: Any,
        *,
        pipeline_runner: PipelineRunner | None = None,
        outbox: OutboxStore | None = None,
        notification_channels: tuple[str, ...] | list[str] = (),
        media_root: str | Path | None = None,
        clock: Clock = _now,
    ) -> None:
        self.store = store
        self.pipeline_runner = pipeline_runner or default_pipeline_runner
        self.outbox = outbox
        self.notification_channels = tuple(str(item).strip().lower() for item in notification_channels if str(item).strip())
        self.media_root = Path(media_root) if media_root is not None else None
        self.clock = clock

    @classmethod
    def _lock_for(cls, store: Any, camera_id: str, session_id: str) -> RLock:
        key = (id(store), camera_id, session_id)
        with cls._run_locks_guard:
            lock = cls._run_locks.get(key)
            if lock is None:
                lock = RLock()
                cls._run_locks[key] = lock
            return lock

    def camera_definition(self, camera_id: str) -> tuple[CameraDefinition, Mapping[str, Any]]:
        value = self.store.get_camera(camera_id)
        if value is None:
            raise KeyError("camera not found")
        try:
            return CameraDefinition.from_mapping(value), value
        except (CameraConfigurationError, TypeError, ValueError) as exc:
            raise CameraConfigurationError("camera configuration is invalid") from exc

    @staticmethod
    def _report_dict(value: Any) -> dict[str, Any]:
        return value.as_dict() if hasattr(value, "as_dict") else dict(value)

    def _readback(self, camera_id: str, session_id: str) -> dict[str, Any] | None:
        report = self.store.get_monitoring_report(camera_id, session_id)
        if report is None:
            return None
        payload = dict(report)
        anomalies = self.store.list_monitoring_anomalies(camera_id=camera_id, session_id=session_id)
        payload["anomalies"] = anomalies
        payload["anomaly_count"] = len(anomalies)
        video_id = self.session_video_id(camera_id, session_id)
        if self.store.get_video(video_id) is not None:
            payload["video_id"] = video_id
        return payload

    def session_video_id(self, camera_id: str, session_id: str) -> str:
        digest = hashlib.sha256(f"{camera_id}:{session_id}".encode("utf-8")).hexdigest()[:32]
        return f"camera-run-{digest}"

    def readback(self, camera_id: str, session_id: str) -> dict[str, Any] | None:
        """Return the durable report and its persisted anomaly/video summary."""

        return self._readback(camera_id, session_id)

    def queue(self, camera_id: str, session_id: str) -> tuple[dict[str, Any], bool]:
        """Durably claim a run for dispatch without executing the pipeline.

        The boolean is false when an existing queued/running/terminal report
        already owns this camera/session pair, which prevents duplicate task
        dispatches from retrying HTTP requests.
        """

        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        lock = self._lock_for(self.store, camera_id, session_id)
        with lock:
            camera, _ = self.camera_definition(camera_id)
            scope = camera_track_scope(camera_id, session_id)
            existing = self.store.get_monitoring_report(camera_id, session_id)
            if existing is not None:
                return dict(existing), False
            report = CameraMonitoringReport(
                camera_id=camera.camera_id,
                session_id=session_id,
                status=MonitoringStatus.QUEUED,
                track_scope=scope,
                source_kind=camera.source.kind,
            )
            persisted = self.store.save_monitoring_report(report, created_at=_clock_value(self.clock))
            return persisted, True

    def mark_failed(self, camera_id: str, session_id: str, *, reason: str = "camera task could not be queued") -> dict[str, Any]:
        camera, camera_value = self.camera_definition(camera_id)
        report = CameraMonitoringReport(
            camera_id=camera_id,
            session_id=session_id,
            status=MonitoringStatus.FAILED,
            last_error=reason,
            track_scope=camera_track_scope(camera_id, session_id),
            source_kind=camera.source.kind,
            finished_at=_clock_value(self.clock),
        )
        self.store.save_monitoring_report(report, created_at=report.finished_at)
        try:
            self._persist_side_effects(
                camera_value,
                camera,
                report,
                (),
                [],
                video_id=self.session_video_id(camera_id, session_id),
            )
        except Exception:
            # The failed report is authoritative even if anomaly/outbox
            # persistence is unavailable during queue failure handling.
            pass
        return self._readback(camera_id, session_id) or self._report_dict(report)

    def _resolve_source_path(self, camera: CameraDefinition) -> Path:
        raw = str(camera.source.path)
        candidates = [Path(raw)]
        if self.media_root is not None:
            names = {Path(raw).name, PureWindowsPath(raw).name}
            candidates.extend(self.media_root / name for name in names if name)
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        raise FileNotFoundError("camera source is unavailable")

    @staticmethod
    def _result_parts(result: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        if result is None:
            return (), ()
        if isinstance(result, Mapping):
            return tuple(result.get("observations") or ()), tuple(result.get("events") or ())
        return tuple(getattr(result, "observations", ()) or ()), tuple(getattr(result, "events", ()) or ())

    @staticmethod
    def _object(value: Any, *, scope: str) -> ObjectObservation:
        if isinstance(value, ObjectObservation):
            return replace(value, track_scope=scope)
        if not isinstance(value, Mapping):
            raise TypeError("pipeline observation objects must be mappings")
        raw_id = value.get("object_id", value.get("track_id"))
        if raw_id is None or not str(raw_id).strip():
            raise ValueError("pipeline observation object_id is required")
        raw_appearance = value.get("appearance")
        if raw_appearance is not None and not isinstance(raw_appearance, ObservableAppearance):
            if not isinstance(raw_appearance, Mapping):
                raise TypeError("pipeline observation appearance must be a mapping")
            raw_appearance = ObservableAppearance(**dict(raw_appearance))
        try:
            return ObjectObservation(
                object_id=str(raw_id).strip(),
                x=float(value["x"]),
                y=float(value["y"]),
                label=str(value.get("label", value.get("object_label", "object"))),
                confidence=value.get("confidence"),
                bbox=tuple(value["bbox"]) if value.get("bbox") is not None else None,
                category=value.get("category"),
                appearance=raw_appearance,
                track_scope=scope,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("pipeline observation is malformed") from exc

    @classmethod
    def _observations(cls, values: tuple[Any, ...], *, scope: str) -> tuple[FrameObservation, ...]:
        result: list[FrameObservation] = []
        for value in values:
            if isinstance(value, FrameObservation):
                timestamp = _finite_media_timestamp(value.timestamp, field="observation timestamp_seconds")
                objects = tuple(cls._object(item, scope=scope) for item in value.objects)
            else:
                if not isinstance(value, Mapping):
                    raise TypeError("pipeline observations must be FrameObservation values or mappings")
                raw_timestamp = value.get("timestamp_seconds", value.get("timestamp"))
                if raw_timestamp is None:
                    raise ValueError("pipeline observation timestamp_seconds is required")
                timestamp = _finite_media_timestamp(raw_timestamp, field="observation timestamp_seconds")
                objects = tuple(cls._object(item, scope=scope) for item in (value.get("objects") or ()))
            result.append(FrameObservation(timestamp, objects))
        return tuple(result)

    @staticmethod
    def _events(values: tuple[Any, ...], *, scope: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw in values:
            value = _as_mapping(raw)
            required = ("event_id", "event_type", "zone_id", "object_id")
            if any(value.get(name) is None or not str(value.get(name)).strip() for name in required):
                raise ValueError("pipeline event requires event_id, event_type, zone_id, and object_id")
            timestamp = value.get("timestamp_seconds", value.get("timestamp"))
            if timestamp is None:
                raise ValueError("pipeline event timestamp_seconds is required")
            event_scope = value.get("track_scope")
            if event_scope is not None and str(event_scope).strip() not in {"", scope, "session"}:
                raise ValueError("pipeline event track_scope does not match camera session")
            result.append(
                {
                    **value,
                    "event_id": str(value["event_id"]).strip(),
                    "event_type": str(value["event_type"]).strip(),
                    "zone_id": str(value["zone_id"]).strip(),
                    "object_id": str(value["object_id"]).strip(),
                    "object_label": str(value.get("object_label") or "object"),
                    "track_scope": scope,
                    "timestamp_seconds": _finite_media_timestamp(timestamp, field="event timestamp_seconds"),
                }
            )
        return result

    @staticmethod
    def _anomaly_config(value: Mapping[str, Any]) -> AnomalyConfig:
        options = value.get("processing_options") or {}
        allowed = {
            key: options[key]
            for key in (
                "baseline_count",
                "object_count_baseline",
                "count_spike_ratio",
                "count_spike_min_delta",
                "dwell_threshold_seconds",
                "expected_labels",
                "expected_categories",
                "silence_threshold_seconds",
            )
            if key in options
        }
        try:
            return AnomalyConfig(**allowed)
        except (TypeError, ValueError):
            return AnomalyConfig()

    def _persist_video(
        self,
        camera: CameraDefinition,
        report: CameraMonitoringReport,
        observations: tuple[FrameObservation, ...],
        events: list[dict[str, Any]],
    ) -> str:
        video_id = self.session_video_id(camera.camera_id, report.session_id)
        if self.store.get_video(video_id) is None:
            self.store.add_video(
                video_id,
                _source_name(camera.source_path),
                camera.source_path,
                report.finished_at or _clock_value(self.clock),
            )
            if observations:
                self.store.add_observations(video_id, observations)
            if events:
                self.store.add_events(video_id, events)
        return video_id

    def _persist_side_effects(
        self,
        camera_value: Mapping[str, Any],
        camera: CameraDefinition,
        report: CameraMonitoringReport,
        observations: tuple[FrameObservation, ...],
        events: list[dict[str, Any]],
        *,
        video_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        alerts = self.store.create_alerts_for_events(events, created_at=report.finished_at or _clock_value(self.clock)) if events else []
        health = None
        if report.status is MonitoringStatus.FAILED and report.last_error:
            health = {
                "camera_id": report.camera_id,
                "session_id": report.session_id,
                "status": report.status.value,
                "processing_failure": True,
                "failure_reason": report.last_error,
                "finished_at": report.finished_at,
            }
        anomalies = detect_anomalies(
            observations,
            events,
            camera_id=report.camera_id,
            session_id=report.session_id,
            config=self._anomaly_config(camera_value),
            health_report=health,
            orchestration_timestamp=report.finished_at,
        )
        persisted_anomalies = [
            self.store.save_monitoring_anomaly(item, created_at=report.finished_at or _clock_value(self.clock))
            for item in anomalies
        ]
        if self.outbox is not None:
            for _channel, payload in notification_payloads(
                alerts,
                video_id=video_id,
                channels=self.notification_channels,
            ):
                self.outbox.enqueue(
                    "notification.send",
                    payload,
                    aggregate_type="alert",
                    aggregate_id=str(payload["payload"]["alert_id"]),
                    dedupe_key=f"notification:{payload['notification_id']}",
                    created_at=report.finished_at,
                )
            for _channel, payload in anomaly_notification_payloads(anomalies, channels=self.notification_channels):
                self.outbox.enqueue(
                    "notification.send",
                    payload,
                    aggregate_type="anomaly",
                    aggregate_id=str(payload["payload"]["anomaly_id"]),
                    dedupe_key=f"notification:{payload['notification_id']}",
                    created_at=report.finished_at,
                )
        return alerts, persisted_anomalies

    def run(self, camera_id: str, *, session_id: str) -> dict[str, Any]:
        """Run one finite camera source, returning durable status/readback."""

        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        lock = self._lock_for(self.store, camera_id, session_id)
        with lock:
            existing = self.store.get_monitoring_report(camera_id, session_id)
            if existing is not None:
                status = str(existing.get("status", ""))
                if status in _TERMINAL_STATUSES or status == MonitoringStatus.RUNNING.value:
                    return self._readback(camera_id, session_id) or dict(existing)
            camera, camera_value = self.camera_definition(camera_id)
            scope = camera_track_scope(camera_id, session_id)
            if not camera.enabled:
                report = CameraMonitoringReport(
                    camera_id=camera_id,
                    session_id=session_id,
                    status=MonitoringStatus.DISABLED,
                    track_scope=scope,
                    source_kind=camera.source.kind,
                )
                self.store.save_monitoring_report(report, created_at=_clock_value(self.clock))
                return self._readback(camera_id, session_id) or self._report_dict(report)

            started_at = _clock_value(self.clock)
            running = CameraMonitoringReport(
                camera_id=camera_id,
                session_id=session_id,
                status=MonitoringStatus.RUNNING,
                track_scope=scope,
                source_kind=camera.source.kind,
                started_at=started_at,
            )
            self.store.save_monitoring_report(running, created_at=started_at)

            try:
                if camera.source.kind is not CameraSourceKind.FILE:
                    raise RuntimeError("live camera source is not supported by the finite worker")
                source_path = self._resolve_source_path(camera)
                effective_zones = camera.zones or camera.processing_options.zones
                options = replace(camera.processing_options, zones=effective_zones, track_scope=scope)
                pipeline_result = self.pipeline_runner(str(source_path), options)
                raw_observations, raw_events = self._result_parts(pipeline_result)
                observations = self._observations(raw_observations, scope=scope)
                events = self._events(raw_events, scope=scope)
                if not events and effective_zones:
                    events = [
                        {
                            "event_id": item.event_id,
                            "event_type": item.event_type,
                            "zone_id": item.zone_id,
                            "object_id": item.object_id,
                            "object_label": item.object_label,
                            "track_scope": item.track_scope,
                            "timestamp_seconds": item.timestamp_seconds,
                        }
                        for item in generate_zone_events(observations, effective_zones)
                    ]
                finished_at = _clock_value(self.clock)
                completed = CameraMonitoringReport(
                    camera_id=camera_id,
                    session_id=session_id,
                    status=MonitoringStatus.COMPLETED,
                    frame_count=len(observations),
                    event_count=len(events),
                    track_scope=scope,
                    source_kind=camera.source.kind,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                video_id = self._persist_video(camera, completed, observations, events)
                alerts, _anomalies = self._persist_side_effects(camera_value, camera, completed, observations, events, video_id=video_id)
                self.store.save_monitoring_report(completed, created_at=finished_at)
                result = self._readback(camera_id, session_id) or self._report_dict(completed)
                result["alerts"] = alerts
                result["video_id"] = video_id
                return result
            except Exception as exc:
                finished_at = _clock_value(self.clock)
                failed = CameraMonitoringReport(
                    camera_id=camera_id,
                    session_id=session_id,
                    status=MonitoringStatus.FAILED,
                    last_error=_safe_error(exc),
                    track_scope=scope,
                    source_kind=camera.source.kind,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                self.store.save_monitoring_report(failed, created_at=finished_at)
                # A failed execution is itself anomaly evidence.  Persisting it
                # is best effort; the report remains the authoritative failure.
                try:
                    persisted = self._persist_side_effects(camera_value, camera, failed, (), [], video_id=self.session_video_id(camera_id, session_id))[1]
                except Exception:
                    persisted = []
                result = self._readback(camera_id, session_id) or self._report_dict(failed)
                result["anomalies"] = persisted or result.get("anomalies", [])
                result["anomaly_count"] = len(result["anomalies"])
                return result

    execute = run


__all__ = ["CameraMonitoringWorker"]
