"""In-memory multi-camera monitoring orchestration.

This module is intentionally small and synchronous.  A :class:`CameraSupervisor`
validates configured finite file sources, gives every camera run a unique
camera/session track scope, and delegates decoding and inference to the existing
video pipeline through an injectable runner.  RTSP/live sources are represented
explicitly but are rejected by the finite-source runner instead of being
reported as running.

The supervisor does not add wall-clock timestamps to observations or events.
``started_at`` and ``finished_at`` on reports are optional orchestration
metadata supplied by the injected clock; media timestamps remain the pipeline's
source timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from .domain import Zone
from .pipeline import PipelineResult
from .operations.processing import ProcessingOptions, default_pipeline_runner


class CameraConfigurationError(ValueError):
    """Raised when a camera definition or update is invalid."""


class CameraSourceError(RuntimeError):
    """Raised when a configured camera source cannot be run honestly."""


class CameraSourceKind(str, Enum):
    """Source kinds supported by camera orchestration."""

    FILE = "file"
    RTSP = "rtsp"
    DEVICE = "device"


class MonitoringStatus(str, Enum):
    """Lifecycle states for one camera/session run."""

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class CameraSource:
    """An explicit camera source.

    ``path`` is a local file path for ``file`` sources, a URL for ``rtsp``
    sources, or a numeric device index for ``device`` sources.  The finite
    supervisor still handles only files; live sources are handled by the
    explicit stream worker.
    """

    kind: CameraSourceKind
    path: str | Path

    def __post_init__(self) -> None:
        raw_kind = self.kind.value if isinstance(self.kind, CameraSourceKind) else str(self.kind).strip().lower()
        try:
            normalized_kind = CameraSourceKind(raw_kind)
        except ValueError as exc:
            raise CameraConfigurationError(
                f"unsupported camera source kind: {raw_kind!r}; expected file or rtsp"
            ) from exc
        if self.path is None:
            raise CameraConfigurationError("camera source path must not be empty")
        normalized_path = str(self.path).strip()
        if not normalized_path:
            raise CameraConfigurationError("camera source path must not be empty")
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "path", normalized_path)

    @classmethod
    def file(cls, path: str | Path) -> "CameraSource":
        return cls(CameraSourceKind.FILE, path)

    @classmethod
    def rtsp(cls, url: str) -> "CameraSource":
        return cls(CameraSourceKind.RTSP, url)

    @classmethod
    def device(cls, index: int | str = 0) -> "CameraSource":
        return cls(CameraSourceKind.DEVICE, str(index))

    @property
    def is_finite(self) -> bool:
        return self.kind is CameraSourceKind.FILE


def _coerce_zones(raw_zones: Iterable[Zone | Mapping[str, Any]] | None) -> tuple[Zone, ...]:
    zones = raw_zones or ()
    result: list[Zone] = []
    for zone in zones:
        if isinstance(zone, Zone):
            result.append(zone)
            continue
        if not isinstance(zone, Mapping):
            raise CameraConfigurationError("camera zones must be Zone objects or mappings")
        try:
            result.append(
                Zone(
                    str(zone["zone_id"]),
                    tuple(tuple(point) for point in zone["polygon"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CameraConfigurationError("invalid camera zone definition") from exc
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CameraDefinition:
    """One explicit camera configuration held by :class:`CameraRegistry`."""

    camera_id: str
    display_name: str
    # The runtime accepts mappings and plain paths as a convenience; the
    # normalized value stored on every instance is always CameraSource.
    source: CameraSource
    enabled: bool = True
    zones: tuple[Zone, ...] = ()
    # Mappings are accepted at runtime and normalized in __post_init__.
    processing_options: ProcessingOptions = field(default_factory=ProcessingOptions)

    def __post_init__(self) -> None:
        camera_id = str(self.camera_id).strip()
        display_name = str(self.display_name).strip()
        if not camera_id:
            raise CameraConfigurationError("camera_id must not be empty")
        if not display_name:
            raise CameraConfigurationError("display_name must not be empty")

        source: Any = self.source
        if isinstance(source, CameraSource):
            normalized_source = source
        elif isinstance(source, Mapping):
            source_kind = source.get("kind", source.get("source_kind", "file"))
            source_path = source.get("path", source.get("url"))
            normalized_source = CameraSource(source_kind, source_path)
        else:
            normalized_source = CameraSource.file(source)

        options = self.processing_options
        normalized_options = (
            options if isinstance(options, ProcessingOptions) else ProcessingOptions.from_mapping(options)
        )
        normalized_zones = _coerce_zones(self.zones)
        object.__setattr__(self, "camera_id", camera_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "source", normalized_source)
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "zones", normalized_zones)
        object.__setattr__(self, "processing_options", normalized_options)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CameraDefinition":
        """Build a camera definition from a plain configuration mapping."""

        source: Any = value.get("source")
        if source is None:
            source = {
                "kind": value.get("source_kind", "file"),
                "path": value.get("path", value.get("url")),
            }
        options = value.get("processing_options", value.get("processing", {}))
        if isinstance(source, CameraSource):
            normalized_source = source
        elif isinstance(source, Mapping):
            normalized_source = CameraSource(
                source.get("kind", source.get("source_kind", "file")),
                source.get("path", source.get("url")),
            )
        else:
            normalized_source = CameraSource.file(source)
        return cls(
            camera_id=str(value.get("camera_id", "")),
            display_name=str(value.get("display_name", value.get("name", ""))),
            source=normalized_source,
            enabled=bool(value.get("enabled", True)),
            zones=_coerce_zones(value.get("zones")),
            processing_options=options,
        )

    @property
    def source_path(self) -> str:
        """Return the configured file path or live URL."""

        return str(self.source.path)


@dataclass(frozen=True, slots=True)
class CameraMonitoringReport:
    """Structured result for one camera and session."""

    camera_id: str
    session_id: str
    status: MonitoringStatus
    frame_count: int = 0
    event_count: int = 0
    last_error: str | None = None
    track_scope: str | None = None
    source_kind: CameraSourceKind | None = None
    started_at: str | None = None
    finished_at: str | None = None
    latest_media_timestamp_seconds: float | None = None
    last_frame_at: str | None = None
    frames_dropped: int = 0
    reconnect_count: int = 0
    worker_mode: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "frame_count": self.frame_count,
            "event_count": self.event_count,
            "last_error": self.last_error,
            "track_scope": self.track_scope,
            "source_kind": self.source_kind.value if self.source_kind else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latest_media_timestamp_seconds": self.latest_media_timestamp_seconds,
            "last_frame_at": self.last_frame_at,
            "frames_dropped": self.frames_dropped,
            "reconnect_count": self.reconnect_count,
            "worker_mode": self.worker_mode,
        }


# A shorter name for callers that do not need the camera-specific spelling.
MonitoringReport = CameraMonitoringReport


def camera_track_scope(camera_id: str, session_id: str) -> str:
    """Return the stable, non-global scope used by one camera/session run."""

    if not str(camera_id).strip() or not str(session_id).strip():
        raise ValueError("camera_id and session_id must not be empty")
    return f"camera:{camera_id}:session:{session_id}"


class CameraRegistry:
    """Thread-safe in-memory camera definitions."""

    def __init__(self, cameras: Iterable[CameraDefinition | Mapping[str, Any]] = ()) -> None:
        self._lock = RLock()
        self._cameras: dict[str, CameraDefinition] = {}
        for camera in cameras:
            self.register(camera)

    def register(
        self,
        camera: CameraDefinition | Mapping[str, Any],
        *,
        replace_existing: bool = False,
    ) -> CameraDefinition:
        normalized = camera if isinstance(camera, CameraDefinition) else CameraDefinition.from_mapping(camera)
        with self._lock:
            if normalized.camera_id in self._cameras and not replace_existing:
                raise CameraConfigurationError(f"camera already registered: {normalized.camera_id}")
            self._cameras[normalized.camera_id] = normalized
        return normalized

    def get(self, camera_id: str) -> CameraDefinition:
        with self._lock:
            try:
                return self._cameras[camera_id]
            except KeyError as exc:
                raise KeyError(f"camera not registered: {camera_id}") from exc

    def list(self) -> tuple[CameraDefinition, ...]:
        with self._lock:
            return tuple(self._cameras.values())

    def update(self, camera_id: str, **changes: Any) -> CameraDefinition:
        allowed = {"display_name", "source", "enabled", "zones", "processing_options"}
        unknown = set(changes) - allowed
        if unknown:
            raise CameraConfigurationError(f"unsupported camera update fields: {sorted(unknown)}")
        with self._lock:
            current = self.get(camera_id)
            updated = replace(current, **changes)
            self._cameras[camera_id] = updated
            return updated

    def remove(self, camera_id: str) -> CameraDefinition:
        with self._lock:
            try:
                return self._cameras.pop(camera_id)
            except KeyError as exc:
                raise KeyError(f"camera not registered: {camera_id}") from exc


PipelineRunner = Callable[[str, ProcessingOptions], PipelineResult | Mapping[str, Any]]
Clock = Callable[[], datetime | str | None]
SessionIdFactory = Callable[[], str]


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def _clock_value(clock: Clock) -> str | None:
    value = clock()
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("camera report clock must return an aware datetime")
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError("camera report clock must return datetime, string, or None")


def _result_counts(result: PipelineResult | Mapping[str, Any]) -> tuple[int, int]:
    if isinstance(result, Mapping):
        observations = result.get("observations") or ()
        events = result.get("events") or ()
        frame_count = result.get("frame_count", len(tuple(observations)))
        event_count = result.get("event_count", len(tuple(events)))
    else:
        frame_count = len(result.observations)
        event_count = len(result.events)
    try:
        return max(0, int(frame_count)), max(0, int(event_count))
    except (TypeError, ValueError) as exc:
        raise ValueError("pipeline result frame_count and event_count must be integers") from exc


class CameraSupervisor:
    """Run configured finite camera sources through the existing pipeline."""

    def __init__(
        self,
        registry: CameraRegistry | None = None,
        *,
        pipeline_runner: PipelineRunner = default_pipeline_runner,
        clock: Clock = _utc_clock,
        session_id_factory: SessionIdFactory | None = None,
    ) -> None:
        self.registry = registry or CameraRegistry()
        self.pipeline_runner = pipeline_runner
        self.clock = clock
        self.session_id_factory = session_id_factory or (lambda: uuid4().hex)
        self._lock = RLock()
        self._active: set[tuple[str, str]] = set()
        self._stop_requested: set[tuple[str, str]] = set()
        self._reports: dict[tuple[str, str], CameraMonitoringReport] = {}

    def list_cameras(self) -> tuple[CameraDefinition, ...]:
        return self.registry.list()

    def register_camera(
        self,
        camera: CameraDefinition | Mapping[str, Any],
        *,
        replace_existing: bool = False,
    ) -> CameraDefinition:
        return self.registry.register(camera, replace_existing=replace_existing)

    def update_camera(self, camera_id: str, **changes: Any) -> CameraDefinition:
        return self.registry.update(camera_id, **changes)

    def disable(self, camera_id: str) -> CameraDefinition:
        camera = self.registry.update(camera_id, enabled=False)
        self.stop(camera_id)
        return camera

    def enable(self, camera_id: str) -> CameraDefinition:
        return self.registry.update(camera_id, enabled=True)

    def stop(self, camera_id: str, session_id: str | None = None) -> bool:
        """Request a running session to stop after its injected runner returns.

        The existing pipeline runner is a finite synchronous call and has no
        cancellation protocol.  Therefore this method cannot interrupt a
        runner that is already inside OpenCV; the final report is marked
        ``stopped`` once that call returns.  Disabling a camera prevents future
        runs and also requests an active run to stop.
        """

        with self._lock:
            keys = {
                key
                for key in self._active
                if key[0] == camera_id and (session_id is None or key[1] == session_id)
            }
            self._stop_requested.update(keys)
            return bool(keys)

    def get_report(self, camera_id: str, session_id: str) -> CameraMonitoringReport | None:
        with self._lock:
            return self._reports.get((camera_id, session_id))

    def list_reports(self) -> tuple[CameraMonitoringReport, ...]:
        with self._lock:
            return tuple(self._reports.values())

    def _new_session_id(self) -> str:
        session_id = str(self.session_id_factory()).strip()
        if not session_id:
            raise ValueError("session_id_factory returned an empty session ID")
        return session_id

    def _store_report(self, report: CameraMonitoringReport) -> CameraMonitoringReport:
        with self._lock:
            self._reports[(report.camera_id, report.session_id)] = report
        return report

    def run(self, camera_id: str, *, session_id: str | None = None) -> CameraMonitoringReport:
        """Run one configured camera source to completion or a truthful failure."""

        camera = self.registry.get(camera_id)
        resolved_session_id = str(session_id).strip() if session_id is not None else self._new_session_id()
        if not resolved_session_id:
            raise ValueError("session_id must not be empty")
        scope = camera_track_scope(camera.camera_id, resolved_session_id)
        key = (camera.camera_id, resolved_session_id)

        if not camera.enabled:
            return self._store_report(
                CameraMonitoringReport(
                    camera_id=camera.camera_id,
                    session_id=resolved_session_id,
                    status=MonitoringStatus.DISABLED,
                    track_scope=scope,
                    source_kind=camera.source.kind,
                )
            )

        started_at = _clock_value(self.clock)
        with self._lock:
            if key in self._active:
                raise RuntimeError(f"camera session is already running: {camera.camera_id}/{resolved_session_id}")
            if key in self._stop_requested:
                self._stop_requested.discard(key)
                return self._store_report(
                    CameraMonitoringReport(
                        camera_id=camera.camera_id,
                        session_id=resolved_session_id,
                        status=MonitoringStatus.STOPPED,
                        track_scope=scope,
                        source_kind=camera.source.kind,
                        started_at=started_at,
                        finished_at=_clock_value(self.clock),
                    )
                )
            self._active.add(key)
            self._reports[key] = CameraMonitoringReport(
                camera_id=camera.camera_id,
                session_id=resolved_session_id,
                status=MonitoringStatus.RUNNING,
                track_scope=scope,
                source_kind=camera.source.kind,
                started_at=started_at,
            )

        frame_count = 0
        event_count = 0
        last_error: str | None = None
        status = MonitoringStatus.COMPLETED
        try:
            source = camera.source
            if source.kind is not CameraSourceKind.FILE:
                raise CameraSourceError(
                    f"live source kind {source.kind.value!r} is configured but not supported by the finite camera runner"
                )
            source_path = Path(source.path).expanduser()
            if not source_path.is_file():
                raise CameraSourceError("camera source is unavailable")

            effective_zones = camera.zones or camera.processing_options.zones
            options = replace(
                camera.processing_options,
                zones=effective_zones,
                track_scope=scope,
            )
            result = self.pipeline_runner(str(source_path), options)
            frame_count, event_count = _result_counts(result)
            with self._lock:
                if key in self._stop_requested:
                    status = MonitoringStatus.STOPPED
        except Exception as exc:
            with self._lock:
                stopped = key in self._stop_requested
            status = MonitoringStatus.STOPPED if stopped else MonitoringStatus.FAILED
            last_error = str(exc)
        finally:
            finished_at = _clock_value(self.clock)
            with self._lock:
                self._active.discard(key)
                self._stop_requested.discard(key)
            report = CameraMonitoringReport(
                camera_id=camera.camera_id,
                session_id=resolved_session_id,
                status=status,
                frame_count=frame_count,
                event_count=event_count,
                last_error=last_error,
                track_scope=scope,
                source_kind=camera.source.kind,
                started_at=started_at,
                finished_at=finished_at,
            )
            return self._store_report(report)

    def run_all(self, *, session_id: str | None = None) -> dict[str, CameraMonitoringReport]:
        """Run every currently registered camera using one shared session ID."""

        resolved_session_id = str(session_id).strip() if session_id is not None else self._new_session_id()
        if not resolved_session_id:
            raise ValueError("session_id must not be empty")
        return {
            camera.camera_id: self.run(camera.camera_id, session_id=resolved_session_id)
            for camera in self.registry.list()
        }


__all__ = [
    "CameraConfigurationError",
    "CameraDefinition",
    "CameraMonitoringReport",
    "CameraRegistry",
    "CameraSource",
    "CameraSourceError",
    "CameraSourceKind",
    "CameraSupervisor",
    "MonitoringReport",
    "MonitoringStatus",
    "PipelineRunner",
    "camera_track_scope",
]
