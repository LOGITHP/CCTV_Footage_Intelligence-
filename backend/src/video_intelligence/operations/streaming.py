"""Bounded live-camera monitoring for RTSP and local capture devices.

The live worker is deliberately explicit about source timing.  It only creates
media-timestamped observations/events when the capture backend supplies a
finite, monotonic ``CAP_PROP_POS_MSEC`` value.  Frames without a source
timestamp may still be decoded and counted for health, but are not turned into
grounded evidence events.  This avoids inventing UTC or media time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from threading import Event, RLock
from typing import Any, Callable, Mapping, cast

from ..anomalies import anomaly_notification_payloads, detect_anomalies
from ..cameras import CameraDefinition, CameraMonitoringReport, CameraSourceKind, MonitoringStatus, camera_track_scope
from ..domain import FrameObservation, generate_zone_events
from ..model_adapters import ModelSettings, build_detector, build_tracker, settings_from_env
from ..pipeline import BrightBlobDetector, Detector, Tracker
from .monitoring import CameraMonitoringWorker, _safe_error
from .notifications import notification_payloads
from .outbox import OutboxStore
from .processing import ProcessingOptions


Clock = Callable[[], datetime | str]
CaptureFactory = Callable[[str | int], Any]
ComponentFactory = Callable[[ProcessingOptions, str], tuple[Detector, Tracker]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_capture_factory(source: str | int) -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("opencv-python-headless is required for live camera monitoring") from exc
    return cv2.VideoCapture(source)


def default_component_factory(options: ProcessingOptions, scope: str) -> tuple[Detector, Tracker]:
    environment = settings_from_env()
    settings = ModelSettings(
        profile=options.model_profile,
        device=options.device,
        yolo_model=environment.yolo_model,
        sam_model=environment.sam_model,
        siglip_model=environment.siglip_model,
        bge_model=environment.bge_model,
        qwen_model=environment.qwen_model,
        meta_api_key=environment.meta_api_key,
        meta_api_url=environment.meta_api_url,
        meta_model=environment.meta_model,
        model_cache=environment.model_cache,
    )
    detector = build_detector(settings)
    if options.model_profile == "baseline":
        detector = BrightBlobDetector(threshold=options.threshold, min_area=options.min_area)
    tracker = build_tracker(settings, profile=options.tracker_profile, track_scope=scope)
    return detector, tracker


def _clock_value(clock: Clock) -> str:
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stream monitoring clock must return an aware datetime")
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError("stream monitoring clock must return datetime or string")


def _source_argument(camera: CameraDefinition) -> str | int:
    if camera.source.kind is CameraSourceKind.DEVICE:
        try:
            return int(str(camera.source.path).strip())
        except ValueError as exc:
            raise ValueError("device camera source must use a numeric device index") from exc
    return str(camera.source.path)


def _source_timestamp(capture: Any, last_timestamp: float | None) -> float | None:
    try:
        import cv2

        raw = float(capture.get(cv2.CAP_PROP_POS_MSEC))
    except (AttributeError, TypeError, ValueError, ImportError):
        return None
    timestamp = raw / 1000.0
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    # A live stream must advance its source timestamp. Repeated values are
    # treated as unavailable rather than turned into fabricated media time.
    if last_timestamp is not None and timestamp <= last_timestamp:
        return None
    return timestamp


class CameraStreamMonitoringWorker:
    """Monitor one live source until stop, bounded limit, or source failure."""

    _locks: dict[tuple[int, str, str], RLock] = {}
    _locks_guard = RLock()

    def __init__(
        self,
        store: Any,
        *,
        outbox: OutboxStore | None = None,
        notification_channels: tuple[str, ...] | list[str] = (),
        capture_factory: CaptureFactory = default_capture_factory,
        component_factory: ComponentFactory = default_component_factory,
        clock: Clock = _now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        reconnect_attempts: int = 3,
        reconnect_backoff_seconds: float = 0.25,
        worker_mode: str = "inline-live",
    ) -> None:
        self.store = store
        self.outbox = outbox
        self.notification_channels = tuple(str(item).strip().lower() for item in notification_channels if str(item).strip())
        self.capture_factory = capture_factory
        self.component_factory = component_factory
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.reconnect_attempts = max(0, int(reconnect_attempts))
        self.reconnect_backoff_seconds = max(0.0, float(reconnect_backoff_seconds))
        self.worker_mode = worker_mode

    @classmethod
    def _lock_for(cls, store: Any, camera_id: str, session_id: str) -> RLock:
        key = (id(store), camera_id, session_id)
        with cls._locks_guard:
            return cls._locks.setdefault(key, RLock())

    def _camera(self, camera_id: str) -> tuple[CameraDefinition, Mapping[str, Any]]:
        value = self.store.get_camera(camera_id)
        if value is None:
            raise KeyError("camera not found")
        return CameraDefinition.from_mapping(value), value

    @staticmethod
    def _capture_open(capture: Any) -> bool:
        try:
            return bool(capture.isOpened())
        except AttributeError:
            return True

    @staticmethod
    def _report_dict(report: CameraMonitoringReport) -> dict[str, Any]:
        return report.as_dict()

    def _save(self, report: CameraMonitoringReport) -> dict[str, Any]:
        return self.store.save_monitoring_report(report, created_at=report.last_frame_at or report.finished_at or report.started_at or _clock_value(self.clock))

    def _readback(self, camera_id: str, session_id: str) -> dict[str, Any] | None:
        report = self.store.get_monitoring_report(camera_id, session_id)
        if report is None:
            return None
        result = dict(report)
        anomalies = self.store.list_monitoring_anomalies(camera_id=camera_id, session_id=session_id)
        result["anomalies"] = anomalies
        result["anomaly_count"] = len(anomalies)
        return result

    def _notify(self, alerts: list[Mapping[str, Any]], anomalies: tuple[Any, ...], *, video_id: str, created_at: str | None) -> None:
        if self.outbox is None:
            return
        for _channel, payload in notification_payloads(alerts, video_id=video_id, channels=self.notification_channels):
            self.outbox.enqueue(
                "notification.send",
                payload,
                aggregate_type="alert",
                aggregate_id=str(payload["payload"]["alert_id"]),
                dedupe_key=f"notification:{payload['notification_id']}",
                created_at=created_at,
            )
        for _channel, payload in anomaly_notification_payloads(anomalies, channels=self.notification_channels):
            self.outbox.enqueue(
                "notification.send",
                payload,
                aggregate_type="anomaly",
                aggregate_id=str(payload["payload"]["anomaly_id"]),
                dedupe_key=f"notification:{payload['notification_id']}",
                created_at=created_at,
            )

    def run(
        self,
        camera_id: str,
        *,
        session_id: str,
        stop_event: Event | None = None,
        max_frames: int | None = None,
        max_seconds: float | None = None,
    ) -> dict[str, Any]:
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        stop_event = stop_event or Event()
        lock = self._lock_for(self.store, camera_id, session_id)
        with lock:
            existing = self.store.get_monitoring_report(camera_id, session_id)
            if existing and str(existing.get("status")) in {"running", "starting", "completed", "failed", "stopped", "disabled"}:
                return self._readback(camera_id, session_id) or dict(existing)
            camera, camera_value = self._camera(camera_id)
            scope = camera_track_scope(camera_id, session_id)
            if not camera.enabled:
                return self._save(CameraMonitoringReport(camera_id, session_id, MonitoringStatus.DISABLED, track_scope=scope, source_kind=camera.source.kind, worker_mode=self.worker_mode))
            if camera.source.kind not in {CameraSourceKind.RTSP, CameraSourceKind.DEVICE}:
                raise ValueError("live monitoring requires an rtsp or device source")

            started_at = _clock_value(self.clock)
            report = CameraMonitoringReport(
                camera_id=camera_id,
                session_id=session_id,
                status=MonitoringStatus.STARTING,
                track_scope=scope,
                source_kind=camera.source.kind,
                started_at=started_at,
                worker_mode=self.worker_mode,
            )
            self._save(report)
            video_id = CameraMonitoringWorker(self.store).session_video_id(camera_id, session_id)
            if self.store.get_video(video_id) is None:
                self.store.add_video(video_id, Path(str(camera.source.path)).name or video_id, str(camera.source.path), started_at)

            capture = None
            frame_count = event_count = frames_dropped = reconnect_count = 0
            last_timestamp: float | None = None
            previous: FrameObservation | None = None
            recent_observations: deque[FrameObservation] = deque(maxlen=120)
            recent_events: deque[dict[str, Any]] = deque(maxlen=120)
            started_monotonic = self.monotonic()
            try:
                capture = self.capture_factory(_source_argument(camera))
                if not self._capture_open(capture):
                    raise RuntimeError("live camera source could not be opened")
                report = replace(report, status=MonitoringStatus.RUNNING)
                self._save(report)
                options = camera.processing_options
                detector, tracker = self.component_factory(options, scope)
                effective_zones = camera.zones or options.zones
                frame_index = 0
                while not stop_event.is_set():
                    if max_frames is not None and frame_count >= max_frames:
                        break
                    if max_seconds is not None and self.monotonic() - started_monotonic >= max_seconds:
                        break
                    ok, frame = capture.read()
                    if not ok:
                        if reconnect_count >= self.reconnect_attempts:
                            raise RuntimeError("live camera source stopped producing frames")
                        reconnect_count += 1
                        report = replace(
                            report,
                            status=MonitoringStatus.DEGRADED,
                            reconnect_count=reconnect_count,
                            last_frame_at=_clock_value(self.clock),
                        )
                        self._save(report)
                        if self.reconnect_backoff_seconds:
                            self.sleep(self.reconnect_backoff_seconds)
                        try:
                            capture.release()
                        except AttributeError:
                            pass
                        capture = self.capture_factory(_source_argument(camera))
                        if not self._capture_open(capture):
                            continue
                        report = replace(report, status=MonitoringStatus.RUNNING, reconnect_count=reconnect_count)
                        self._save(report)
                        continue
                    if frame_index % options.every != 0:
                        frames_dropped += 1
                        frame_index += 1
                        continue
                    frame_index += 1
                    detections = detector.detect(frame)
                    tracks = tracker.update(detections)
                    timestamp = _source_timestamp(capture, last_timestamp)
                    if timestamp is None:
                        frames_dropped += 1
                        frame_count += 1
                        report = replace(report, frame_count=frame_count, frames_dropped=frames_dropped, reconnect_count=reconnect_count, last_frame_at=_clock_value(self.clock))
                        self._save(report)
                        continue
                    last_timestamp = timestamp
                    observation = FrameObservation(cast(Any, timestamp), tuple(tracks))
                    frame_count += 1
                    new_events: list[dict[str, Any]] = []
                    if effective_zones:
                        comparison = (previous, observation) if previous is not None else (observation,)
                        for event in generate_zone_events(comparison, effective_zones):
                            new_events.append({**event.__dict__, "track_scope": scope})
                    previous = observation
                    recent_observations.append(observation)
                    recent_events.extend(new_events)
                    self.store.add_observations(video_id, (observation,), frame_index_start=frame_count - 1)
                    if new_events:
                        self.store.add_events(video_id, new_events)
                        event_count += len(new_events)
                    alerts: list[Mapping[str, Any]] = self.store.create_alerts_for_events(new_events, created_at=_clock_value(self.clock)) if new_events else []
                    anomalies = detect_anomalies(
                        tuple(recent_observations),
                        tuple(recent_events),
                        camera_id=camera_id,
                        session_id=session_id,
                        config=CameraMonitoringWorker(self.store)._anomaly_config(camera_value),
                        orchestration_timestamp=_clock_value(self.clock),
                    )
                    for anomaly in anomalies:
                        self.store.save_monitoring_anomaly(anomaly, created_at=anomaly.orchestration_timestamp)
                    self._notify(alerts, anomalies, video_id=video_id, created_at=_clock_value(self.clock))
                    report = replace(
                        report,
                        status=MonitoringStatus.RUNNING,
                        frame_count=frame_count,
                        event_count=event_count,
                        latest_media_timestamp_seconds=timestamp,
                        last_frame_at=_clock_value(self.clock),
                        frames_dropped=frames_dropped,
                        reconnect_count=reconnect_count,
                    )
                    self._save(report)
                status = MonitoringStatus.STOPPED if stop_event.is_set() else MonitoringStatus.COMPLETED
                report = replace(report, status=status, finished_at=_clock_value(self.clock), frame_count=frame_count, event_count=event_count, frames_dropped=frames_dropped, reconnect_count=reconnect_count)
            except Exception as exc:
                report = replace(report, status=MonitoringStatus.FAILED, last_error=_safe_error(exc), finished_at=_clock_value(self.clock), frame_count=frame_count, event_count=event_count, frames_dropped=frames_dropped, reconnect_count=reconnect_count)
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except AttributeError:
                        pass
            self._save(report)
            result = self._readback(camera_id, session_id) or self._report_dict(report)
            result["video_id"] = video_id
            return result


__all__ = ["CameraStreamMonitoringWorker", "default_capture_factory", "default_component_factory"]
