"""Durable operations for uploaded-video processing.

The modules in this package deliberately keep integrations behind small
protocols.  The default configuration is local SQLite, no-op notifications,
dry-run retention, and an in-memory metrics sink, so importing the package is
safe in an offline test environment.
"""

from .backup import BackupManager, BackupManifest, BackupRestoreService, backup_database, backup_sqlite, restore_sqlite
from .metrics import InMemoryMetrics, MetricsHooks, NoopMetrics, PrometheusMetrics, build_metrics
from .notifications import (
    EmailNotifier,
    LocalNotifier,
    NoopNotifier,
    NotificationDeliveryWorker,
    NotificationDispatcher,
    NotificationMessage,
    NotificationWorker,
    WebhookNotifier,
    build_notification_worker,
)
from .outbox import OutboxStore, ReconciliationService
from .processing import ProcessingOptions, UploadedVideoProcessor
from .retention import RetentionService

__all__ = [
    "InMemoryMetrics",
    "BackupManifest",
    "BackupManager",
    "BackupRestoreService",
    "MetricsHooks",
    "NoopMetrics",
    "PrometheusMetrics",
    "EmailNotifier",
    "LocalNotifier",
    "NoopNotifier",
    "NotificationDeliveryWorker",
    "NotificationDispatcher",
    "NotificationMessage",
    "NotificationWorker",
    "ProcessingOptions",
    "RetentionService",
    "OutboxStore",
    "ReconciliationService",
    "UploadedVideoProcessor",
    "WebhookNotifier",
    "build_notification_worker",
    "backup_database",
    "backup_sqlite",
    "build_metrics",
    "restore_sqlite",
]
