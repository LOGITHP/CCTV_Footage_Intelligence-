"""Uploaded-video processing service used by Celery tasks and offline fakes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from ..domain import Zone
from ..evidence import create_clip
from ..infrastructure.config import InfrastructureSettings, load_settings
from ..infrastructure.postgres import PostgresStore
from ..model_adapters import ModelSettings, build_detector, build_tracker, settings_from_env
from ..pipeline import BrightBlobDetector, PipelineResult, process_video as run_media_pipeline
from ..storage import Store
from .metrics import MetricsHooks, build_metrics
from .notifications import configured_notification_channels, notification_payloads
from .outbox import OutboxStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ProcessingOptions:
    zones: tuple[Zone, ...] = ()
    every: int = 1
    threshold: int = 200
    min_area: int = 25
    clip_padding: float = 5.0
    device: str = "cpu"
    model_profile: str = "baseline"
    tracker_profile: str = "centroid"
    track_scope: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ProcessingOptions":
        data = dict(value or {})
        raw_zones = data.get("zones") or ()
        zones = tuple(
            zone if isinstance(zone, Zone) else Zone(str(zone["zone_id"]), tuple(tuple(point) for point in zone["polygon"]))
            for zone in raw_zones
        )
        raw_stride = data.get("every") if data.get("every") is not None else data.get("stride", 1)
        options = cls(
            zones=zones,
            every=int(raw_stride),
            threshold=int(data.get("threshold", 200)),
            min_area=int(data.get("min_area", 25)),
            clip_padding=float(data.get("clip_padding", 5.0)),
            device=str(data.get("device", "cpu")),
            model_profile=str(data.get("model_profile", "baseline")),
            tracker_profile=str(data.get("tracker_profile", "centroid")),
            track_scope=str(data["track_scope"]) if data.get("track_scope") else None,
        )
        if options.every < 1:
            raise ValueError("every must be at least 1")
        if not 0 <= options.threshold <= 255 or options.min_area < 1:
            raise ValueError("threshold must be 0..255 and min_area must be positive")
        if options.clip_padding < 0:
            raise ValueError("clip_padding must be non-negative")
        if options.device not in {"cpu", "gpu"}:
            raise ValueError("device must be cpu or gpu")
        if options.model_profile not in {"baseline", "sam", "sam3.1", "sam2"}:
            raise ValueError("model_profile must be baseline or sam")
        if options.tracker_profile not in {"centroid", "sam", "sam3.1", "sam2"}:
            raise ValueError("tracker_profile must be centroid or sam")
        if options.track_scope is not None and not options.track_scope:
            raise ValueError("track_scope must not be empty")
        return options


PipelineRunner = Callable[[str, ProcessingOptions], PipelineResult | Mapping[str, Any]]
ClipCreator = Callable[..., str | Path]


def default_pipeline_runner(path: str, options: ProcessingOptions) -> PipelineResult:
    model_environment = settings_from_env()
    model_settings = ModelSettings(
        profile=options.model_profile,
        device=options.device,
        yolo_model=model_environment.yolo_model,
        sam_model=model_environment.sam_model,
        siglip_model=model_environment.siglip_model,
        bge_model=model_environment.bge_model,
        qwen_model=model_environment.qwen_model,
        meta_api_key=model_environment.meta_api_key,
        meta_api_url=model_environment.meta_api_url,
        meta_model=model_environment.meta_model,
        baseten_api_key=model_environment.baseten_api_key,
        baseten_sam_endpoint=model_environment.baseten_sam_endpoint,
        baseten_siglip_endpoint=model_environment.baseten_siglip_endpoint,
        baseten_bge_endpoint=model_environment.baseten_bge_endpoint,
        model_cache=model_environment.model_cache,
    )
    detector = build_detector(model_settings)
    tracker = build_tracker(model_settings, profile=options.tracker_profile, track_scope=options.track_scope or "session")
    if options.model_profile == "baseline":
        detector = BrightBlobDetector(threshold=options.threshold, min_area=options.min_area)
    return run_media_pipeline(
        path,
        options.zones,
        every=options.every,
        detector=detector,
        tracker=tracker,
        device=options.device,
        track_scope=options.track_scope or "session",
    )


def _result_parts(result: PipelineResult | Mapping[str, Any]) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
    if isinstance(result, Mapping):
        observations = tuple(result.get("observations") or ())
        raw_events = result.get("events") or ()
    else:
        observations = tuple(result.observations)
        raw_events = result.events
    events: list[dict[str, Any]] = []
    for event in raw_events:
        if isinstance(event, Mapping):
            events.append(dict(event))
        else:
            events.append(dict(vars(event)))
    return observations, events


class UploadedVideoProcessor:
    """Process one stored upload with injectable runners and side effects."""

    def __init__(
        self,
        store: Any,
        *,
        media_root: str | Path = "data/media",
        pipeline_runner: PipelineRunner | None = None,
        clip_creator: ClipCreator = create_clip,
        outbox: OutboxStore | None = None,
        metrics: MetricsHooks | None = None,
        notification_channels: tuple[str, ...] = (),
        clock: Callable[[], str] = _now,
    ) -> None:
        self.store = store
        self.media_root = Path(media_root)
        self.pipeline_runner = pipeline_runner or default_pipeline_runner
        self.clip_creator = clip_creator
        self.outbox = outbox
        self.metrics = metrics or MetricsHooks()
        self.notification_channels = tuple(notification_channels)
        self.clock = clock

    def _resolve_media_path(self, raw_path: str | Path) -> Path:
        """Resolve a stored upload across host/container path changes."""

        raw_text = str(raw_path)
        candidates = [Path(raw_text)]
        fallback_names = {Path(raw_text).name, PureWindowsPath(raw_text).name}
        candidates.extend(self.media_root / name for name in fallback_names if name)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        raise FileNotFoundError(f"uploaded video is not available in the active media store: {raw_path}")

    def process(
        self,
        video_id: str,
        *,
        job_id: str | None = None,
        options: Mapping[str, Any] | ProcessingOptions | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        operation = "processing"
        self.metrics.job_started(operation=operation)
        video = self.store.get_video(video_id)
        if video is None:
            self.metrics.job_failed(operation=operation)
            raise ValueError(f"video not found: {video_id}")
        resolved_job_id = job_id or str(uuid4())
        existing_job = self.store.get_job(resolved_job_id) if hasattr(self.store, "get_job") else None
        if existing_job is None and hasattr(self.store, "add_job"):
            self.store.add_job(resolved_job_id, video_id, "processing", self.clock())
        elif hasattr(self.store, "update_job"):
            self.store.update_job(resolved_job_id, "processing")
        try:
            parsed_options = options if isinstance(options, ProcessingOptions) else ProcessingOptions.from_mapping(options)
            parsed_options = replace(parsed_options, track_scope=parsed_options.track_scope or video_id)
            source_path = self._resolve_media_path(video["media_path"])
            result = self.pipeline_runner(str(source_path), parsed_options)
            observations, events = _result_parts(result)
            if observations and hasattr(self.store, "add_observations"):
                self.store.add_observations(video_id, observations)
            if events and hasattr(self.store, "add_events"):
                self.store.add_events(video_id, events)
            for event in events:
                if parsed_options.clip_padding < 0:
                    continue
                event_id = event.get("event_id")
                if not event_id:
                    continue
                destination = self.media_root / "evidence" / f"{event_id}.mp4"
                clip = self.clip_creator(
                    source_path,
                    destination,
                    timestamp_seconds=float(event["timestamp_seconds"]),
                    padding=parsed_options.clip_padding,
                )
                event["evidence_status"] = "ready"
                event["evidence_path"] = str(clip)
                if hasattr(self.store, "update_evidence"):
                    self.store.update_evidence(event_id, str(clip))
            alerts = []
            if events and hasattr(self.store, "create_alerts_for_events"):
                alerts = self.store.create_alerts_for_events(events, created_at=self.clock())
            if hasattr(self.store, "update_job"):
                self.store.update_job(resolved_job_id, "completed")
            if self.outbox is not None:
                self.outbox.enqueue(
                    "video.processed",
                    {"video_id": video_id, "job_id": resolved_job_id, "event_count": len(events)},
                    aggregate_type="video",
                    aggregate_id=video_id,
                    dedupe_key=f"video.processed:{resolved_job_id}",
                    created_at=self.clock(),
                )
                for channel, notification in notification_payloads(
                    alerts,
                    video_id=video_id,
                    channels=self.notification_channels,
                ):
                    self.outbox.enqueue(
                        "notification.send",
                        notification,
                        aggregate_type="alert",
                        aggregate_id=str(notification["payload"]["alert_id"]),
                        dedupe_key=f"notification:{notification['notification_id']}",
                        created_at=self.clock(),
                    )
            self.metrics.job_completed(operation=operation)
            self.metrics.processing_seconds(time.monotonic() - started)
            return {
                "job_id": resolved_job_id,
                "video_id": video_id,
                "status": "completed",
                "frame_count": len(observations),
                "event_count": len(events),
                "events": events,
                "alerts": alerts,
            }
        except Exception as exc:
            if hasattr(self.store, "update_job"):
                self.store.update_job(resolved_job_id, "failed", str(exc))
            self.metrics.job_failed(operation=operation)
            self.metrics.processing_seconds(time.monotonic() - started)
            raise


def build_store(settings: InfrastructureSettings | None = None) -> Any:
    settings = settings or load_settings()
    if settings.database.url:
        return PostgresStore(settings.database.url)
    return Store(settings.database.sqlite_path)


def build_default_processor() -> UploadedVideoProcessor:
    settings = load_settings()
    store = build_store(settings)
    outbox = OutboxStore(settings.database.url or settings.database.sqlite_path)
    return UploadedVideoProcessor(
        store,
        media_root=settings.object_storage.local_root,
        outbox=outbox,
        metrics=build_metrics(enabled=settings.metrics.enabled, namespace=settings.metrics.namespace),
        notification_channels=configured_notification_channels(settings.notifications),
    )
