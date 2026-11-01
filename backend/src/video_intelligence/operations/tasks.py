"""Celery task definitions for processing and durable operational workers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Mapping

from ..infrastructure.config import load_settings
from ..infrastructure.object_storage import build_object_storage
from .backup import BackupRestoreService
from .metrics import build_metrics
from .monitoring import CameraMonitoringWorker
from .streaming import CameraStreamMonitoringWorker
from .notifications import build_notification_worker, configured_notification_channels
from .outbox import OutboxStore, ReconciliationService
from .processing import UploadedVideoProcessor, build_default_processor, build_store
from .retention import RetentionService


PROCESS_VIDEO_TASK = "video_intelligence.process_uploaded_video"
PROCESS_CAMERA_TASK = "video_intelligence.process_camera"
PROCESS_CAMERA_STREAM_TASK = "video_intelligence.process_camera_stream"
DELIVER_NOTIFICATION_TASK = "video_intelligence.deliver_notification"
RECONCILE_OUTBOX_TASK = "video_intelligence.reconcile_outbox"
RETENTION_TASK = "video_intelligence.retention_cleanup"
BACKUP_TASK = "video_intelligence.backup_database"


def register_celery_tasks(
    app: Any,
    *,
    processor_factory: Callable[[], UploadedVideoProcessor] | None = None,
    camera_worker_factory: Callable[[], CameraMonitoringWorker] | None = None,
    camera_stream_worker_factory: Callable[[], CameraStreamMonitoringWorker] | None = None,
    notification_worker_factory: Callable[[], Any] | None = None,
    reconciliation_factory: Callable[[], ReconciliationService] | None = None,
    retention_factory: Callable[[], RetentionService] | None = None,
    backup_factory: Callable[[], BackupRestoreService] | None = None,
):
    """Register lazy task functions on an already-created Celery app.

    Factories are injectable so ``task.apply`` can be tested entirely with
    in-memory stores and fakes.  The default factories create infrastructure
    only when the corresponding worker actually runs.
    """

    processor_factory = processor_factory or build_default_processor
    camera_worker_factory = camera_worker_factory or _default_camera_worker
    camera_stream_worker_factory = camera_stream_worker_factory or _default_camera_stream_worker
    notification_worker_factory = notification_worker_factory or _default_notification_worker
    reconciliation_factory = reconciliation_factory or _default_reconciliation_service
    retention_factory = retention_factory or _default_retention_service
    backup_factory = backup_factory or _default_backup_service

    @app.task(
        bind=True,
        name=PROCESS_VIDEO_TASK,
        autoretry_for=(RuntimeError,),
        retry_backoff=True,
        max_retries=3,
    )
    def process_uploaded_video(self, video_id: str, job_id: str | None = None, options: Mapping[str, Any] | None = None):
        processor = processor_factory()
        return processor.process(video_id, job_id=job_id, options=options)

    @app.task(bind=True, name=PROCESS_CAMERA_TASK)
    def process_camera(self, camera_id: str, session_id: str):
        worker = camera_worker_factory()
        return worker.run(camera_id, session_id=session_id)

    @app.task(bind=True, name=PROCESS_CAMERA_STREAM_TASK)
    def process_camera_stream(self, camera_id: str, session_id: str, max_frames: int | None = None, max_seconds: float | None = None):
        worker = camera_stream_worker_factory()
        return worker.run(camera_id, session_id=session_id, max_frames=max_frames, max_seconds=max_seconds)

    @app.task(
        bind=True,
        name=DELIVER_NOTIFICATION_TASK,
        autoretry_for=(RuntimeError,),
        retry_backoff=True,
        max_retries=5,
    )
    def deliver_notification(self, payload: Mapping[str, Any]):
        worker = notification_worker_factory()
        return worker.deliver({"payload": dict(payload)})

    @app.task(
        bind=True,
        name=RECONCILE_OUTBOX_TASK,
        autoretry_for=(RuntimeError,),
        retry_backoff=True,
        max_retries=3,
    )
    def reconcile_outbox(self, limit: int = 50, max_batches: int = 1):
        report = reconciliation_factory().reconcile(limit=limit, max_batches=max_batches)
        return asdict(report)

    @app.task(
        bind=True,
        name=RETENTION_TASK,
        autoretry_for=(RuntimeError,),
        retry_backoff=True,
        max_retries=2,
    )
    def retention_cleanup(execute: bool = False, confirm: bool = False):
        report = retention_factory().run(execute=execute, confirm=confirm)
        return report.as_dict()

    @app.task(
        bind=True,
        name=BACKUP_TASK,
        autoretry_for=(RuntimeError,),
        retry_backoff=True,
        max_retries=2,
    )
    def backup_database(destination: str | None = None):
        manifest = backup_factory().backup(destination=destination)
        return manifest.as_dict()

    return {
        "process_uploaded_video": process_uploaded_video,
        "process_camera": process_camera,
        "process_camera_stream": process_camera_stream,
        "deliver_notification": deliver_notification,
        "reconcile_outbox": reconcile_outbox,
        "retention_cleanup": retention_cleanup,
        "backup_database": backup_database,
    }


def register_processing_tasks(app: Any, **kwargs: Any):
    """Compatibility alias for callers interested only in video processing."""

    return register_celery_tasks(app, **kwargs)["process_uploaded_video"]


def dispatch_uploaded_video(dispatcher: Any, video_id: str, *, job_id: str | None = None, options: Mapping[str, Any] | None = None) -> str:
    """Queue one processing task through any dispatcher implementing ``dispatch``."""

    return dispatcher.dispatch(PROCESS_VIDEO_TASK, video_id=video_id, job_id=job_id, options=dict(options or {}))


def dispatch_camera(dispatcher: Any, camera_id: str, session_id: str) -> str:
    """Queue one durable camera/session run through the configured dispatcher."""

    return dispatcher.dispatch(PROCESS_CAMERA_TASK, camera_id=camera_id, session_id=session_id)


def dispatch_camera_stream(
    dispatcher: Any,
    camera_id: str,
    session_id: str,
    *,
    max_frames: int | None = None,
    max_seconds: float | None = None,
) -> str:
    """Queue one long-running live camera task through the configured worker."""

    return dispatcher.dispatch(
        PROCESS_CAMERA_STREAM_TASK,
        camera_id=camera_id,
        session_id=session_id,
        max_frames=max_frames,
        max_seconds=max_seconds,
    )


def _default_camera_worker() -> CameraMonitoringWorker:
    settings = load_settings()
    store = build_store(settings)
    outbox = OutboxStore(settings.database.url or settings.database.sqlite_path)
    return CameraMonitoringWorker(
        store,
        outbox=outbox,
        media_root=settings.object_storage.local_root,
        notification_channels=configured_notification_channels(settings.notifications),
    )


def _default_camera_stream_worker() -> CameraStreamMonitoringWorker:
    settings = load_settings()
    store = build_store(settings)
    outbox = OutboxStore(settings.database.url or settings.database.sqlite_path)
    return CameraStreamMonitoringWorker(
        store,
        outbox=outbox,
        notification_channels=configured_notification_channels(settings.notifications),
        worker_mode="celery-live",
    )


def _default_notification_worker():
    settings = load_settings()
    return build_notification_worker(
        settings.notifications,
        metrics=build_metrics(enabled=settings.metrics.enabled, namespace=settings.metrics.namespace),
    )


def _default_reconciliation_service() -> ReconciliationService:
    settings = load_settings()
    outbox = OutboxStore(settings.database.url or settings.database.sqlite_path)
    worker = _default_notification_worker()
    metrics = build_metrics(enabled=settings.metrics.enabled, namespace=settings.metrics.namespace)
    return ReconciliationService(
        outbox,
        handlers={"notification.send": worker.deliver},
        metrics=metrics,
    )


def _default_retention_service() -> RetentionService:
    settings = load_settings()
    store = build_store(settings)
    object_storage = None
    if (
        settings.object_storage.backend == "s3"
        and settings.retention.delete_enabled
        and not settings.retention.dry_run
    ):
        object_storage = build_object_storage(settings.object_storage)
    outbox = OutboxStore(settings.database.url or settings.database.sqlite_path)
    return RetentionService(
        store,
        settings=settings.retention,
        object_storage=object_storage,
        outbox=outbox,
        metrics=build_metrics(enabled=settings.metrics.enabled, namespace=settings.metrics.namespace),
    )


def _default_backup_service() -> BackupRestoreService:
    settings = load_settings()
    return BackupRestoreService(
        settings.backup,
        metrics=build_metrics(enabled=settings.metrics.enabled, namespace=settings.metrics.namespace),
    )
