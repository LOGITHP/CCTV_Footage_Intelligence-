"""SQLAlchemy-backed authoritative store.

The class deliberately exposes the same small repository surface as the local
SQLite ``Store``.  That lets the existing API select PostgreSQL by setting
``DATABASE_URL`` without changing any route contract.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .schema import (
    AlertRecord,
    AlertRuleRecord,
    Base,
    ChatMessageRecord,
    CameraRecord,
    EventRecord,
    JobRecord,
    MonitoringAnomalyRecord,
    MonitoringReportRecord,
    TrackObservationRecord,
    VideoRecord,
)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


class PostgresStore:
    """Repository adapter for PostgreSQL (and SQLAlchemy test databases)."""

    def __init__(
        self,
        database_url: str,
        *,
        engine: Engine | None = None,
        create_schema: bool = True,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required for PostgresStore")
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
        if engine is None:
            engine_options: dict[str, Any] = {"future": True, "pool_pre_ping": True}
            if database_url.startswith("sqlite") and ":memory:" in database_url:
                engine_options.update({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool})
            elif database_url.startswith("postgresql"):
                engine_options.update({"connect_args": {"connect_timeout": 10}})
            engine = create_engine(database_url, **engine_options)
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        if create_schema:
            Base.metadata.create_all(bind=engine)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self._sessions() as session:
            yield session

    @staticmethod
    def _video_dict(record: VideoRecord) -> dict[str, Any]:
        return {
            "video_id": record.video_id,
            "filename": record.filename,
            "media_path": record.media_path,
            "media_object_key": record.media_object_key,
            "created_at": record.created_at,
        }

    @staticmethod
    def _job_dict(record: JobRecord) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "video_id": record.video_id,
            "status": record.status,
            "error": record.error,
            "created_at": record.created_at,
        }

    @staticmethod
    def _event_dict(record: EventRecord) -> dict[str, Any]:
        return {
            "event_id": record.event_id,
            "video_id": record.video_id,
            "event_type": record.event_type,
            "zone_id": record.zone_id,
            "object_id": record.object_id,
            "object_label": record.object_label,
            "track_scope": record.track_scope,
            "timestamp_seconds": record.timestamp_seconds,
            "evidence_status": record.evidence_status,
            "evidence_path": record.evidence_path,
        }

    @staticmethod
    def _camera_dict(record: CameraRecord) -> dict[str, Any]:
        return {
            "camera_id": record.camera_id,
            "display_name": record.display_name,
            "source": {"kind": record.source_kind, "path": record.source_path},
            "enabled": bool(record.enabled),
            "zones": json.loads(record.zones_json or "[]"),
            "processing_options": json.loads(record.processing_options_json or "{}"),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _report_dict(record: MonitoringReportRecord) -> dict[str, Any]:
        return {
            "camera_id": record.camera_id,
            "session_id": record.session_id,
            "status": record.status,
            "frame_count": record.frame_count,
            "event_count": record.event_count,
            "last_error": record.last_error,
            "track_scope": record.track_scope,
            "source_kind": record.source_kind,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "latest_media_timestamp_seconds": record.latest_media_timestamp_seconds,
            "last_frame_at": record.last_frame_at,
            "frames_dropped": record.frames_dropped,
            "reconnect_count": record.reconnect_count,
            "worker_mode": record.worker_mode,
            "created_at": record.created_at,
        }

    @staticmethod
    def _anomaly_dict(record: MonitoringAnomalyRecord) -> dict[str, Any]:
        return {
            "anomaly_id": record.anomaly_id,
            "anomaly_type": record.anomaly_type,
            "camera_id": record.camera_id,
            "session_id": record.session_id,
            "reason": record.reason,
            "severity": record.severity,
            "timestamp_seconds": record.timestamp_seconds,
            "related_event_ids": json.loads(record.related_event_ids_json or "[]"),
            "related_track_ids": json.loads(record.related_track_ids_json or "[]"),
            "source_quality": record.source_quality,
            "orchestration_timestamp": record.orchestration_timestamp,
            "details": json.loads(record.details_json or "{}"),
            "created_at": record.created_at,
        }

    def close(self) -> None:
        self.engine.dispose()

    # Camera/monitoring persistence -------------------------------------------------

    def upsert_camera(
        self,
        camera: Any,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        value = camera.model_dump() if hasattr(camera, "model_dump") else (dict(camera) if isinstance(camera, dict) else None)
        if value is None:
            source = camera.source
            options = camera.processing_options
            option_values = {
                name: getattr(options, name)
                for name in ("every", "threshold", "min_area", "clip_padding", "device", "model_profile", "tracker_profile", "track_scope")
                if hasattr(options, name)
            }
            option_values["zones"] = [
                {"zone_id": zone.zone_id, "polygon": [list(point) for point in zone.polygon]}
                for zone in getattr(options, "zones", ())
            ]
            value = {
                "camera_id": camera.camera_id,
                "display_name": camera.display_name,
                "source": {"kind": getattr(source.kind, "value", str(source.kind)), "path": str(source.path)},
                "enabled": camera.enabled,
                "zones": [
                    {"zone_id": zone.zone_id, "polygon": [list(point) for point in zone.polygon]}
                    for zone in getattr(camera, "zones", ())
                ],
                "processing_options": option_values,
            }
        source = value.get("source") or {"kind": value.get("source_kind", "file"), "path": value.get("path", value.get("url"))}
        options = value.get("processing_options", value.get("processing", {})) or {}
        source_kind = str(source.get("kind", source.get("source_kind", "file"))).strip().lower()
        source_path = str(source.get("path") or source.get("url") or "").strip()
        camera_id = str(value.get("camera_id", "")).strip()
        if not camera_id or not str(value.get("display_name", "")).strip() or not source_path:
            raise ValueError("camera_id, display_name, and source path are required")
        from datetime import datetime, timezone

        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._session() as session:
            with session.begin():
                record = session.get(CameraRecord, camera_id)
                if record is None:
                    record = CameraRecord(
                        camera_id=camera_id,
                        display_name=str(value["display_name"]).strip(),
                        source_kind=source_kind,
                        source_path=source_path,
                        enabled=bool(value.get("enabled", True)),
                        zones_json=json.dumps(value.get("zones") or [], sort_keys=True),
                        processing_options_json=json.dumps(options, sort_keys=True),
                        created_at=created_at or stamp,
                        updated_at=stamp,
                    )
                    session.add(record)
                else:
                    record.display_name = str(value["display_name"]).strip()
                    record.source_kind = source_kind
                    record.source_path = source_path
                    record.enabled = bool(value.get("enabled", True))
                    record.zones_json = json.dumps(value.get("zones") or [], sort_keys=True)
                    record.processing_options_json = json.dumps(options, sort_keys=True)
                    record.updated_at = stamp
        result = self.get_camera(camera_id)
        if result is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("camera insert did not persist")
        return result

    save_camera = upsert_camera
    create_camera = upsert_camera
    add_camera = upsert_camera
    update_camera = upsert_camera

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            record = session.get(CameraRecord, camera_id)
            return self._camera_dict(record) if record else None

    def list_cameras(self) -> list[dict[str, Any]]:
        with self._session() as session:
            return [self._camera_dict(item) for item in session.scalars(select(CameraRecord).order_by(CameraRecord.created_at, CameraRecord.camera_id)).all()]

    def delete_camera(self, camera_id: str) -> bool:
        with self._session() as session:
            with session.begin():
                record = session.get(CameraRecord, camera_id)
                if record is None:
                    return False
                session.execute(delete(MonitoringAnomalyRecord).where(MonitoringAnomalyRecord.camera_id == camera_id))
                session.execute(delete(MonitoringReportRecord).where(MonitoringReportRecord.camera_id == camera_id))
                session.delete(record)
                return True

    def save_monitoring_report(self, report: Any, *, created_at: str | None = None) -> dict[str, Any]:
        value = report.as_dict() if hasattr(report, "as_dict") else dict(report)
        from datetime import datetime, timezone

        stamp = created_at or value.get("finished_at") or value.get("started_at") or datetime.now(timezone.utc).isoformat()
        with self._session() as session:
            with session.begin():
                record = session.get(MonitoringReportRecord, (value["camera_id"], value["session_id"]))
                if record is None:
                    record = MonitoringReportRecord(camera_id=value["camera_id"], session_id=value["session_id"], created_at=stamp)
                    session.add(record)
                record.status = str(_enum_value(value.get("status", "unknown")))
                record.frame_count = int(value.get("frame_count", 0))
                record.event_count = int(value.get("event_count", 0))
                record.last_error = value.get("last_error")
                record.track_scope = value.get("track_scope")
                record.source_kind = _enum_value(value.get("source_kind"))
                record.started_at = value.get("started_at")
                record.finished_at = value.get("finished_at")
                record.latest_media_timestamp_seconds = value.get("latest_media_timestamp_seconds")
                record.last_frame_at = value.get("last_frame_at")
                record.frames_dropped = int(value.get("frames_dropped", 0))
                record.reconnect_count = int(value.get("reconnect_count", 0))
                record.worker_mode = value.get("worker_mode")
        result = self.get_monitoring_report(value["camera_id"], value["session_id"])
        if result is None:  # pragma: no cover
            raise RuntimeError("monitoring report did not persist")
        return result

    add_monitoring_report = save_monitoring_report
    save_report = save_monitoring_report

    def get_monitoring_report(self, camera_id: str, session_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            record = session.get(MonitoringReportRecord, (camera_id, session_id))
            return self._report_dict(record) if record else None

    def list_monitoring_reports(self, *, camera_id: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        statement = select(MonitoringReportRecord).order_by(MonitoringReportRecord.created_at, MonitoringReportRecord.camera_id, MonitoringReportRecord.session_id)
        if camera_id:
            statement = statement.where(MonitoringReportRecord.camera_id == camera_id)
        if session_id:
            statement = statement.where(MonitoringReportRecord.session_id == session_id)
        with self._session() as session:
            return [self._report_dict(item) for item in session.scalars(statement).all()]

    list_reports = list_monitoring_reports
    get_reports = list_monitoring_reports
    get_monitoring_reports = list_monitoring_reports

    def save_monitoring_anomaly(self, anomaly: Any, *, created_at: str | None = None) -> dict[str, Any]:
        value = anomaly.as_dict() if hasattr(anomaly, "as_dict") else dict(anomaly)
        from datetime import datetime, timezone

        stamp = created_at or value.get("orchestration_timestamp") or datetime.now(timezone.utc).isoformat()
        with self._session() as session:
            with session.begin():
                record = session.get(MonitoringAnomalyRecord, value["anomaly_id"])
                if record is None:
                    record = MonitoringAnomalyRecord(anomaly_id=value["anomaly_id"], created_at=stamp)
                    session.add(record)
                record.anomaly_type = _enum_value(value.get("anomaly_type", value.get("type", "processing_failure")))
                record.camera_id = value["camera_id"]
                record.session_id = value["session_id"]
                record.reason = value["reason"]
                record.severity = _enum_value(value.get("severity", "medium"))
                record.timestamp_seconds = value.get("timestamp_seconds")
                record.related_event_ids_json = json.dumps(value.get("related_event_ids", value.get("event_ids", [])))
                record.related_track_ids_json = json.dumps(value.get("related_track_ids", value.get("track_ids", [])))
                record.source_quality = _enum_value(value.get("source_quality", "derived"))
                record.orchestration_timestamp = value.get("orchestration_timestamp")
                record.details_json = json.dumps(value.get("details", {}), sort_keys=True)
        result = self.get_monitoring_anomaly(value["anomaly_id"])
        if result is None:  # pragma: no cover
            raise RuntimeError("monitoring anomaly did not persist")
        return result

    add_monitoring_anomaly = save_monitoring_anomaly

    def get_monitoring_anomaly(self, anomaly_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            record = session.get(MonitoringAnomalyRecord, anomaly_id)
            return self._anomaly_dict(record) if record else None

    def list_monitoring_anomalies(self, *, camera_id: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        statement = select(MonitoringAnomalyRecord).order_by(MonitoringAnomalyRecord.created_at.desc(), MonitoringAnomalyRecord.anomaly_id)
        if camera_id:
            statement = statement.where(MonitoringAnomalyRecord.camera_id == camera_id)
        if session_id:
            statement = statement.where(MonitoringAnomalyRecord.session_id == session_id)
        with self._session() as session:
            return [self._anomaly_dict(item) for item in session.scalars(statement).all()]

    list_anomalies = list_monitoring_anomalies
    get_monitoring_anomalies = list_monitoring_anomalies

    def add_video(
        self,
        video_id: str,
        filename: str,
        media_path: str,
        created_at: str,
        *,
        media_object_key: str | None = None,
        content_type: str | None = None,
        size_bytes: int | None = None,
        checksum_sha256: str | None = None,
    ) -> dict[str, Any]:
        with self._session() as session:
            with session.begin():
                session.add(
                    VideoRecord(
                        video_id=video_id,
                        filename=filename,
                        media_path=media_path,
                        media_object_key=media_object_key,
                        created_at=created_at,
                        content_type=content_type,
                        size_bytes=size_bytes,
                        checksum_sha256=checksum_sha256,
                    )
                )
        result = self.get_video(video_id)
        if result is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("video insert did not persist")
        return result

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            record = session.get(VideoRecord, video_id)
            return self._video_dict(record) if record else None

    def list_videos(self) -> list[dict[str, Any]]:
        with self._session() as session:
            return [self._video_dict(item) for item in session.scalars(select(VideoRecord).order_by(VideoRecord.created_at)).all()]

    def list_video_artifacts(self, video_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            return [
                {"event_id": item.event_id, "evidence_path": item.evidence_path}
                for item in session.scalars(
                    select(EventRecord).where(
                        EventRecord.video_id == video_id,
                        EventRecord.evidence_path.is_not(None),
                    )
                ).all()
            ]

    def delete_video(self, video_id: str) -> bool:
        """Delete authoritative descendants for an explicitly guarded purge."""

        with self._session() as session:
            with session.begin():
                event_ids = list(
                    session.scalars(
                        select(EventRecord.event_id).where(EventRecord.video_id == video_id)
                    ).all()
                )
                if event_ids:
                    session.execute(delete(AlertRecord).where(AlertRecord.event_id.in_(event_ids)))
                    session.execute(delete(EventRecord).where(EventRecord.event_id.in_(event_ids)))
                session.execute(delete(TrackObservationRecord).where(TrackObservationRecord.video_id == video_id))
                session.execute(delete(JobRecord).where(JobRecord.video_id == video_id))
                result = session.execute(delete(VideoRecord).where(VideoRecord.video_id == video_id))
                return bool(result.rowcount)

    def add_job(self, job_id: str, video_id: str, status: str, created_at: str) -> dict[str, Any]:
        with self._session() as session:
            with session.begin():
                session.add(JobRecord(job_id=job_id, video_id=video_id, status=status, created_at=created_at))
        result = self.get_job(job_id)
        if result is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("job insert did not persist")
        return result

    def update_job(self, job_id: str, status: str, error: str | None = None) -> dict[str, Any] | None:
        with self._session() as session:
            with session.begin():
                record = session.get(JobRecord, job_id)
                if record is not None:
                    record.status = status
                    record.error = error
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            record = session.get(JobRecord, job_id)
            return self._job_dict(record) if record else None

    def add_events(self, video_id: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with self._session() as session:
            with session.begin():
                for event in events:
                    if session.get(EventRecord, event["event_id"]) is not None:
                        continue
                    session.add(
                        EventRecord(
                            event_id=event["event_id"],
                            video_id=video_id,
                            event_type=event["event_type"],
                            zone_id=event["zone_id"],
                            object_id=event["object_id"],
                            object_label=event.get("object_label", "object"),
                            track_scope=event.get("track_scope", "session"),
                            timestamp_seconds=event["timestamp_seconds"],
                            evidence_status=event.get("evidence_status", "pending"),
                            evidence_path=event.get("evidence_path"),
                        )
                    )

    def update_evidence(self, event_id: str, path: str) -> None:
        with self._session() as session:
            with session.begin():
                record = session.get(EventRecord, event_id)
                if record is not None:
                    record.evidence_status = "ready"
                    record.evidence_path = path

    def search_events(
        self,
        *,
        video_id: str | None = None,
        object_id: str | None = None,
        zone_id: str | None = None,
        object_label: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(EventRecord)
        if video_id:
            statement = statement.where(EventRecord.video_id == video_id)
        if object_id:
            statement = statement.where(EventRecord.object_id == object_id)
        if zone_id:
            statement = statement.where(EventRecord.zone_id == zone_id)
        if object_label:
            statement = statement.where(EventRecord.object_label == object_label)
        if event_type:
            statement = statement.where(EventRecord.event_type == event_type)
        statement = statement.order_by(EventRecord.timestamp_seconds)
        with self._session() as session:
            records = session.scalars(statement).all()
            return [self._event_dict(record) for record in records]

    def add_observations(self, video_id: str, observations: tuple[Any, ...], *, frame_index_start: int = 0) -> None:
        with self._session() as session:
            with session.begin():
                for offset, frame in enumerate(observations):
                    frame_index = frame_index_start + offset
                    for item in frame.objects:
                        session.add(
                            TrackObservationRecord(
                                video_id=video_id,
                                track_id=item.object_id,
                                frame_index=frame_index,
                                timestamp_seconds=frame.timestamp,
                                x=item.x,
                                y=item.y,
                                object_label=item.label,
                                confidence=item.confidence,
                                track_scope=item.track_scope,
                                object_category=item.category,
                                bbox_json=json.dumps(list(item.bbox)) if item.bbox is not None else None,
                                appearance_json=json.dumps(item.appearance.as_dict()) if item.appearance is not None else None,
                            )
                        )

    def search_tracks(
        self,
        *,
        video_id: str | None = None,
        track_id: str | None = None,
        track_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(TrackObservationRecord)
        if video_id:
            statement = statement.where(TrackObservationRecord.video_id == video_id)
        if track_id:
            statement = statement.where(TrackObservationRecord.track_id == track_id)
        if track_scope:
            statement = statement.where(TrackObservationRecord.track_scope == track_scope)
        statement = statement.order_by(TrackObservationRecord.timestamp_seconds)
        with self._session() as session:
            return [
                {
                    "observation_id": record.observation_id,
                    "video_id": record.video_id,
                    "track_id": record.track_id,
                    "frame_index": record.frame_index,
                    "timestamp_seconds": record.timestamp_seconds,
                    "x": record.x,
                    "y": record.y,
                    "object_label": record.object_label,
                    "confidence": record.confidence,
                    "track_scope": record.track_scope,
                    "object_category": record.object_category,
                    "bbox": json.loads(record.bbox_json) if record.bbox_json else None,
                    "appearance": json.loads(record.appearance_json) if record.appearance_json else None,
                }
                for record in session.scalars(statement).all()
            ]

    def add_chat_message(self, message_id: str, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]], created_at: str) -> dict[str, Any]:
        with self._session() as session:
            with session.begin():
                session.add(ChatMessageRecord(message_id=message_id, conversation_id=conversation_id, role=role, content=content, citations_json=json.dumps(citations), created_at=created_at))
        return {"message_id": message_id, "conversation_id": conversation_id, "role": role, "content": content, "citations": citations, "created_at": created_at}

    def add_alert_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        with self._session() as session:
            with session.begin():
                session.add(AlertRuleRecord(**rule))
        return rule

    def list_alert_rules(self) -> list[dict[str, Any]]:
        with self._session() as session:
            return [
                {"rule_id": item.rule_id, "name": item.name, "event_type": item.event_type, "zone_id": item.zone_id, "object_label": item.object_label, "enabled": item.enabled, "cooldown_seconds": item.cooldown_seconds, "created_at": item.created_at}
                for item in session.scalars(select(AlertRuleRecord).order_by(AlertRuleRecord.created_at.desc())).all()
            ]

    def create_alerts_for_events(self, events: list[dict[str, Any]], *, created_at: str) -> list[dict[str, Any]]:
        created = []
        for event in events:
            for rule in self.list_alert_rules():
                if not rule["enabled"] or (rule["event_type"] and rule["event_type"] != event["event_type"]) or (rule["zone_id"] and rule["zone_id"] != event["zone_id"]) or (rule["object_label"] and rule["object_label"] != event.get("object_label", "object")):
                    continue
                alert_id = f"alert-{rule['rule_id']}-{event['event_id']}"
                message = f"{rule['name']}: {event['event_type']} in {event['zone_id']} at {event['timestamp_seconds']:.3f}s"
                with self._session() as session:
                    with session.begin():
                        if session.get(AlertRecord, alert_id) is None:
                            session.add(AlertRecord(alert_id=alert_id, rule_id=rule["rule_id"], event_id=event["event_id"], status="open", message=message, created_at=created_at))
                created.append({"alert_id": alert_id, "rule_id": rule["rule_id"], "event_id": event["event_id"], "status": "open", "message": message, "created_at": created_at})
        return created

    def list_alerts(self, *, status: str | None = None) -> list[dict[str, Any]]:
        statement = select(AlertRecord).order_by(AlertRecord.created_at.desc())
        if status:
            statement = statement.where(AlertRecord.status == status)
        with self._session() as session:
            return [{"alert_id": item.alert_id, "rule_id": item.rule_id, "event_id": item.event_id, "status": item.status, "message": item.message, "created_at": item.created_at, "acknowledged_at": item.acknowledged_at} for item in session.scalars(statement).all()]

    def acknowledge_alert(self, alert_id: str, acknowledged_at: str) -> dict[str, Any] | None:
        with self._session() as session:
            with session.begin():
                item = session.get(AlertRecord, alert_id)
                if item is None:
                    return None
                item.status = "acknowledged"
                item.acknowledged_at = acknowledged_at
        return next((item for item in self.list_alerts() if item["alert_id"] == alert_id), None)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            record = session.get(EventRecord, event_id)
            return self._event_dict(record) if record else None
