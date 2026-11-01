"""SQLAlchemy schema for the authoritative PostgreSQL store.

The tables intentionally mirror the local SQLite store while adding durable
object metadata fields.  ``Base.metadata`` can be supplied directly to Alembic
or used by the adapter's development-only ``create_all`` path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base imported by an Alembic ``env.py``."""


class VideoRecord(Base):
    __tablename__ = "videos"

    video_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    # This is a local path in the fallback and an object key in S3 deployments.
    media_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class JobRecord(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    video_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("videos.video_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EventRecord(Base):
    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    video_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("videos.video_id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    object_label: Mapped[str] = mapped_column(String(255), nullable=False, default="object")
    track_scope: Mapped[str] = mapped_column(String(255), nullable=False, default="session")
    timestamp_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class TrackObservationRecord(Base):
    __tablename__ = "track_observations"

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(String(64), ForeignKey("videos.video_id", ondelete="CASCADE"), nullable=False)
    track_id: Mapped[str] = mapped_column(String(255), nullable=False)
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    object_label: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    track_scope: Mapped[str] = mapped_column(String(255), nullable=False, default="session")
    object_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bbox_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    appearance_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AlertRuleRecord(Base):
    __tablename__ = "alert_rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zone_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AlertRecord(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(64), ForeignKey("alert_rules.rule_id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CameraRecord(Base):
    __tablename__ = "cameras"

    camera_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    zones_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    processing_options_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class MonitoringReportRecord(Base):
    __tablename__ = "monitoring_reports"

    camera_id: Mapped[str] = mapped_column(String(128), ForeignKey("cameras.camera_id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_media_timestamp_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_frame_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frames_dropped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class MonitoringAnomalyRecord(Base):
    __tablename__ = "monitoring_anomalies"

    anomaly_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    anomaly_type: Mapped[str] = mapped_column(String(64), nullable=False)
    camera_id: Mapped[str] = mapped_column(String(128), ForeignKey("cameras.camera_id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    related_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    related_track_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    orchestration_timestamp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class OutboxRecord(Base):
    """Durable message awaiting delivery to a worker or integration.

    Payloads are stored as text rather than a database-specific JSON type so
    the exact same model can be created in SQLite and PostgreSQL.  ``message_id``
    is generated by the application and ``dedupe_key`` makes retries idempotent.
    """

    __tablename__ = "outbox_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    aggregate_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[str] = mapped_column(String(64), nullable=False)
    locked_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


Index("ix_jobs_video_id", JobRecord.video_id)
Index("ix_events_video_id_timestamp", EventRecord.video_id, EventRecord.timestamp_seconds)
Index("ix_events_object_zone_timestamp", EventRecord.object_id, EventRecord.zone_id, EventRecord.timestamp_seconds)
Index("ix_track_observations_track", TrackObservationRecord.video_id, TrackObservationRecord.track_id, TrackObservationRecord.timestamp_seconds)
Index("ix_track_observations_scope", TrackObservationRecord.video_id, TrackObservationRecord.track_scope, TrackObservationRecord.track_id, TrackObservationRecord.timestamp_seconds)
Index("ix_outbox_status_available", OutboxRecord.status, OutboxRecord.available_at)
Index("ix_outbox_aggregate", OutboxRecord.aggregate_type, OutboxRecord.aggregate_id)
Index("ix_monitoring_reports_camera_created", MonitoringReportRecord.camera_id, MonitoringReportRecord.created_at)
Index("ix_monitoring_anomalies_scope", MonitoringAnomalyRecord.camera_id, MonitoringAnomalyRecord.session_id, MonitoringAnomalyRecord.created_at)


def metadata() -> Any:
    """Return metadata through a function for straightforward Alembic imports."""

    return Base.metadata
