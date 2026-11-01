from __future__ import annotations

from datetime import datetime, timezone

import pytest

from video_intelligence.domain import FrameObservation, ObjectObservation
from video_intelligence.infrastructure.config import NotificationSettings, RetentionSettings
from video_intelligence.operations.backup import backup_sqlite, restore_sqlite
from video_intelligence.operations.metrics import InMemoryMetrics, MetricsHooks, build_metrics
from video_intelligence.operations.notifications import (
    NotificationDispatcher,
    NotificationMessage,
    WebhookNotifier,
    build_notification_dispatcher,
)
from video_intelligence.operations.outbox import OutboxStore, ReconciliationService
from video_intelligence.operations.processing import UploadedVideoProcessor
from video_intelligence.operations.retention import RetentionService
from video_intelligence.operations.tasks import register_celery_tasks
from video_intelligence.storage import Store


def test_outbox_deduplicates_and_reconciles_with_fake_handler(tmp_path):
    outbox = OutboxStore(tmp_path / "outbox.sqlite3")
    try:
        first = outbox.enqueue("test.topic", {"value": 1}, dedupe_key="same-event")
        duplicate = outbox.enqueue("test.topic", {"value": 2}, dedupe_key="same-event")
        assert duplicate["message_id"] == first["message_id"]

        delivered = []
        report = ReconciliationService(
            outbox,
            {"test.topic": lambda record: delivered.append(record["payload"])},
        ).reconcile_once()
        assert report.claimed == report.delivered == 1
        assert delivered == [{"value": 1}]
        assert outbox.get(first["message_id"])["status"] == "delivered"
    finally:
        outbox.close()


def test_prometheus_hooks_support_labeled_and_unlabeled_metrics():
    metrics = build_metrics(enabled=True)
    metrics.job_started()
    metrics.outbox_depth(2)
    metrics.backup_created()
    assert b"video_intelligence_outbox_depth" in metrics.sink.render()


def test_notification_local_and_webhook_fakes_are_opt_in(tmp_path):
    local_path = tmp_path / "notifications.jsonl"
    local = build_notification_dispatcher(NotificationSettings(mode="local", local_path=str(local_path)))
    result = local.deliver(NotificationMessage("n1", "local", subject="Alert", body="hello"))
    assert result.status == "delivered"
    assert '"notification_id": "n1"' in local_path.read_text(encoding="utf-8")

    calls = []
    webhook = WebhookNotifier(
        "https://example.invalid/hook",
        sender=lambda url, body, headers, timeout: calls.append((url, body, timeout)) or 202,
    )
    assert webhook.send(NotificationMessage("n2", "webhook")).status == "delivered"
    assert calls[0][0] == "https://example.invalid/hook"

    noop = NotificationDispatcher().deliver(NotificationMessage("n3", "unknown"))
    assert noop.status == "skipped"


def test_uploaded_video_processor_uses_fake_pipeline_and_outbox(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    outbox = OutboxStore(tmp_path / "store.sqlite3")
    metrics = InMemoryMetrics()
    video_path = tmp_path / "upload.mp4"
    video_path.write_bytes(b"fixture")
    store.add_video("video-1", "upload.mp4", str(video_path), "2026-01-01T00:00:00+00:00")

    def runner(path, options):
        assert path == str(video_path)
        assert options.every == 2
        return {
            "observations": (FrameObservation(0.0, (ObjectObservation("track-1", 1, 2),)),),
            "events": [{
                "event_id": "event-1",
                "event_type": "enter",
                "zone_id": "gate",
                "object_id": "track-1",
                "object_label": "object",
                "timestamp_seconds": 0.0,
            }],
        }

    try:
        processor = UploadedVideoProcessor(
            store,
            media_root=tmp_path / "media",
            pipeline_runner=runner,
            clip_creator=lambda source, destination, **kwargs: destination,
            outbox=outbox,
            metrics=MetricsHooks(metrics),
        )
        result = processor.process("video-1", job_id="job-1", options={"every": 2})
        assert result["status"] == "completed"
        assert result["event_count"] == 1
        assert store.get_job("job-1")["status"] == "completed"
        assert outbox.list(status="pending")[0]["topic"] == "video.processed"
        assert any(key[0] == "jobs_completed_total" for key in metrics.counters)
    finally:
        outbox.close()
        store.close()


def test_uploaded_video_processor_resolves_windows_media_path_in_worker(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    media_root = tmp_path / "media"
    media_root.mkdir()
    source = media_root / "upload.mp4"
    source.write_bytes(b"fixture")
    store.add_video(
        "video-legacy",
        "upload.mp4",
        r"C:\\app\\data\\media\\upload.mp4",
        "2026-01-01T00:00:00+00:00",
    )
    observed_paths = []

    try:
        processor = UploadedVideoProcessor(
            store,
            media_root=media_root,
            pipeline_runner=lambda path, options: observed_paths.append(path) or {"observations": (), "events": []},
        )
        result = processor.process("video-legacy", job_id="job-legacy")
        assert result["status"] == "completed"
        assert observed_paths == [str(source.resolve())]
    finally:
        store.close()


def test_uploaded_video_processor_enqueues_configured_alert_notifications(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    outbox = OutboxStore(tmp_path / "store.sqlite3")
    video_path = tmp_path / "upload.mp4"
    video_path.write_bytes(b"fixture")
    store.add_video("video-1", "upload.mp4", str(video_path), "2026-01-01T00:00:00+00:00")
    store.add_alert_rule(
        {
            "rule_id": "rule-1",
            "name": "Gate entry",
            "event_type": "enter",
            "zone_id": "gate",
            "object_label": None,
            "enabled": True,
            "cooldown_seconds": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    try:
        processor = UploadedVideoProcessor(
            store,
            media_root=tmp_path / "media",
            pipeline_runner=lambda path, options: {
                "observations": (),
                "events": [{
                    "event_id": "event-1",
                    "event_type": "enter",
                    "zone_id": "gate",
                    "object_id": "track-1",
                    "object_label": "person",
                    "timestamp_seconds": 0.0,
                }],
            },
            clip_creator=lambda source, destination, **kwargs: destination,
            outbox=outbox,
            notification_channels=("local",),
        )
        result = processor.process("video-1", job_id="job-1")
        assert result["alerts"][0]["alert_id"] == "alert-rule-1-event-1"
        notifications = outbox.list(status="pending", aggregate_id="alert-rule-1-event-1")
        assert [item["topic"] for item in notifications] == ["notification.send"]
        assert notifications[0]["payload"]["channel"] == "local"
    finally:
        outbox.close()
        store.close()


def test_retention_is_dry_run_until_both_gates_are_explicit(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    original = media_root / "original.mp4"
    evidence = media_root / "evidence.mp4"
    original.write_bytes(b"original")
    evidence.write_bytes(b"evidence")
    store = Store(tmp_path / "retention.sqlite3")
    store.add_video("video-1", "original.mp4", str(original), "2020-01-01T00:00:00+00:00")
    store.add_events(
        "video-1",
        [{
            "event_id": "event-1",
            "event_type": "enter",
            "zone_id": "gate",
            "object_id": "track-1",
            "timestamp_seconds": 1.0,
        }],
    )
    store.update_evidence("event-1", str(evidence))
    try:
        safe = RetentionService(store, settings=RetentionSettings(media_root=str(media_root)))
        planned = safe.run(now=datetime(2026, 1, 1, tzinfo=timezone.utc), execute=True, confirm=True)
        assert planned.deleted == 0
        assert original.exists() and evidence.exists()

        enabled = RetentionService(
            store,
            settings=RetentionSettings(
                days=30,
                dry_run=False,
                delete_enabled=True,
                media_root=str(media_root),
            ),
        )
        deleted = enabled.run(now=datetime(2026, 1, 1, tzinfo=timezone.utc), execute=True, confirm=True)
        assert deleted.deleted == 1
        assert not original.exists() and not evidence.exists()
        assert store.get_video("video-1") is None
    finally:
        store.close()


def test_sqlite_backup_restore_is_consistent_and_non_overwriting(tmp_path):
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backups" / "snapshot.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    store = Store(source)
    store.add_video("video-1", "fixture.mp4", "fixture.mp4", "2026-01-01T00:00:00+00:00")
    store.close()
    manifest = backup_sqlite(source, backup)
    assert manifest.sha256
    with pytest.raises(FileExistsError):
        backup_sqlite(source, backup)
    restore_sqlite(backup, restored)
    restored_store = Store(restored)
    try:
        assert restored_store.get_video("video-1")["filename"] == "fixture.mp4"
    finally:
        restored_store.close()


def test_celery_processing_task_is_lazy_and_uses_a_fake_processor():
    celery = pytest.importorskip("celery")
    app = celery.Celery("offline", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    calls = []

    class FakeProcessor:
        def process(self, video_id, **kwargs):
            calls.append((video_id, kwargs))
            return {"status": "completed", "video_id": video_id}

    tasks = register_celery_tasks(app, processor_factory=lambda: FakeProcessor())
    result = tasks["process_uploaded_video"].apply(
        args=["video-1"], kwargs={"job_id": "job-1", "options": {"every": 2}}
    )
    assert result.get() == {"status": "completed", "video_id": "video-1"}
    assert calls == [("video-1", {"job_id": "job-1", "options": {"every": 2}})]
