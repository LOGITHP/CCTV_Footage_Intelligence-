"""Small SQLite-backed authoritative store for the initial vertical slice."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any


def _timestamp(value: str | None = None) -> str:
    return value or datetime.now(timezone.utc).isoformat()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _zone_mapping(zone: Any) -> dict[str, Any]:
    if isinstance(zone, dict):
        return {"zone_id": str(zone["zone_id"]), "polygon": [list(point) for point in zone["polygon"]]}
    return {"zone_id": str(zone.zone_id), "polygon": [list(point) for point in zone.polygon]}


def _camera_mapping(camera: Any) -> dict[str, Any]:
    """Convert CameraDefinition/request-like values to durable JSON fields."""

    if hasattr(camera, "model_dump"):
        camera = camera.model_dump(mode="json")
    if isinstance(camera, dict):
        value = dict(camera)
        source = value.get("source") or {
            "kind": value.get("source_kind", "file"),
            "path": value.get("path", value.get("url")),
        }
        options = value.get("processing_options", value.get("processing", {})) or {}
        zones = value.get("zones") or []
        return {
            "camera_id": str(value.get("camera_id", "")).strip(),
            "display_name": str(value.get("display_name", value.get("name", ""))).strip(),
            "source_kind": str(source.get("kind", source.get("source_kind", "file"))).strip().lower(),
            "source_path": str(source.get("path") or source.get("url") or "").strip(),
            "enabled": bool(value.get("enabled", True)),
            "zones": [_zone_mapping(zone) for zone in zones],
            "processing_options": dict(options) if isinstance(options, dict) else {},
        }
    source = camera.source
    options = camera.processing_options
    option_values = {
        name: getattr(options, name)
        for name in (
            "every", "threshold", "min_area", "clip_padding", "device",
            "model_profile", "tracker_profile", "track_scope",
        )
        if hasattr(options, name)
    }
    option_values["zones"] = [_zone_mapping(zone) for zone in getattr(options, "zones", ())]
    return {
        "camera_id": str(camera.camera_id).strip(),
        "display_name": str(camera.display_name).strip(),
        "source_kind": getattr(source.kind, "value", str(source.kind)),
        "source_path": str(source.path),
        "enabled": bool(camera.enabled),
        "zones": [_zone_mapping(zone) for zone in getattr(camera, "zones", ())],
        "processing_options": option_values,
    }


class Store:
    def __init__(self, path: str | Path = "data/video_intelligence.sqlite3") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                media_path TEXT NOT NULL,
                media_object_key TEXT,
                content_type TEXT,
                size_bytes INTEGER,
                checksum_sha256 TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(video_id),
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(video_id),
                event_type TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                object_id TEXT NOT NULL,
                object_label TEXT NOT NULL DEFAULT 'object',
                track_scope TEXT NOT NULL DEFAULT 'session',
                timestamp_seconds REAL NOT NULL,
                evidence_status TEXT NOT NULL DEFAULT 'pending',
                evidence_path TEXT,
                source_frame_index INTEGER
            );
            CREATE TABLE IF NOT EXISTS frames (
                frame_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES videos(video_id),
                frame_index INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL,
                timestamp_quality TEXT NOT NULL,
                UNIQUE(video_id, frame_index)
            );
            CREATE TABLE IF NOT EXISTS track_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL REFERENCES videos(video_id),
                track_id TEXT NOT NULL,
                frame_index INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                object_label TEXT NOT NULL,
                confidence REAL,
                track_scope TEXT NOT NULL DEFAULT 'session',
                object_category TEXT,
                bbox_json TEXT,
                appearance_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_track_observations_track ON track_observations(video_id, track_id, timestamp_seconds);
            CREATE INDEX IF NOT EXISTS idx_track_observations_scope ON track_observations(video_id, track_scope, track_id, timestamp_seconds);
            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                citations_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_rules (
                rule_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                event_type TEXT,
                zone_id TEXT,
                object_label TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                rule_id TEXT NOT NULL REFERENCES alert_rules(rule_id),
                event_id TEXT NOT NULL REFERENCES events(event_id),
                status TEXT NOT NULL DEFAULT 'open',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                acknowledged_at TEXT,
                UNIQUE(rule_id, event_id)
            );
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                delivery_id TEXT PRIMARY KEY,
                alert_id TEXT NOT NULL REFERENCES alerts(alert_id),
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cameras (
                camera_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_path TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                zones_json TEXT NOT NULL DEFAULT '[]',
                processing_options_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitoring_reports (
                camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                frame_count INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                track_scope TEXT,
                source_kind TEXT,
                started_at TEXT,
                finished_at TEXT,
                latest_media_timestamp_seconds REAL,
                last_frame_at TEXT,
                frames_dropped INTEGER NOT NULL DEFAULT 0,
                reconnect_count INTEGER NOT NULL DEFAULT 0,
                worker_mode TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (camera_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_monitoring_reports_camera_created
                ON monitoring_reports(camera_id, created_at);
            CREATE TABLE IF NOT EXISTS monitoring_anomalies (
                anomaly_id TEXT PRIMARY KEY,
                anomaly_type TEXT NOT NULL,
                camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                severity TEXT NOT NULL,
                timestamp_seconds REAL,
                related_event_ids_json TEXT NOT NULL DEFAULT '[]',
                related_track_ids_json TEXT NOT NULL DEFAULT '[]',
                source_quality TEXT NOT NULL,
                orchestration_timestamp TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_monitoring_anomalies_scope
                ON monitoring_anomalies(camera_id, session_id, created_at);
            CREATE TABLE IF NOT EXISTS outbox_messages (
                message_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                aggregate_type TEXT,
                aggregate_id TEXT,
                dedupe_key TEXT UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                locked_at TEXT,
                processed_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_status_available
                ON outbox_messages(status, available_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_aggregate
                ON outbox_messages(aggregate_type, aggregate_id);
            """
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(events)")}
        if "evidence_path" not in columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN evidence_path TEXT")
        if "object_label" not in columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN object_label TEXT NOT NULL DEFAULT 'object'")
        if "source_frame_index" not in columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN source_frame_index INTEGER")
        if "track_scope" not in columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN track_scope TEXT NOT NULL DEFAULT 'session'")
        observation_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(track_observations)")}
        for name, definition in (
            ("track_scope", "TEXT NOT NULL DEFAULT 'session'"),
            ("object_category", "TEXT"),
            ("bbox_json", "TEXT"),
            ("appearance_json", "TEXT"),
        ):
            if name not in observation_columns:
                self.connection.execute(f"ALTER TABLE track_observations ADD COLUMN {name} {definition}")
        video_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(videos)")}
        for name, definition in (
            ("media_object_key", "TEXT"),
            ("content_type", "TEXT"),
            ("size_bytes", "INTEGER"),
            ("checksum_sha256", "TEXT"),
        ):
            if name not in video_columns:
                self.connection.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
        report_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(monitoring_reports)")}
        for name, definition in (
            ("latest_media_timestamp_seconds", "REAL"),
            ("last_frame_at", "TEXT"),
            ("frames_dropped", "INTEGER NOT NULL DEFAULT 0"),
            ("reconnect_count", "INTEGER NOT NULL DEFAULT 0"),
            ("worker_mode", "TEXT"),
        ):
            if name not in report_columns:
                self.connection.execute(f"ALTER TABLE monitoring_reports ADD COLUMN {name} {definition}")
        self.connection.commit()

    # Camera/monitoring persistence -------------------------------------------------

    def upsert_camera(
        self,
        camera: Any,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        value = _camera_mapping(camera)
        if not value["camera_id"] or not value["display_name"] or not value["source_path"]:
            raise ValueError("camera_id, display_name, and source path are required")
        stamp = _timestamp(updated_at)
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT created_at FROM cameras WHERE camera_id = ?", (value["camera_id"],)
            ).fetchone()
            created = existing[0] if existing else _timestamp(created_at)
            self.connection.execute(
                """INSERT INTO cameras
                (camera_id, display_name, source_kind, source_path, enabled, zones_json,
                 processing_options_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(camera_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    source_kind = excluded.source_kind,
                    source_path = excluded.source_path,
                    enabled = excluded.enabled,
                    zones_json = excluded.zones_json,
                    processing_options_json = excluded.processing_options_json,
                    updated_at = excluded.updated_at""",
                (
                    value["camera_id"], value["display_name"], value["source_kind"], value["source_path"],
                    int(value["enabled"]), json.dumps(value["zones"], sort_keys=True),
                    json.dumps(value["processing_options"], sort_keys=True), created, stamp,
                ),
            )
        result = self.get_camera(value["camera_id"])
        if result is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("camera insert did not persist")
        return result

    save_camera = upsert_camera
    create_camera = upsert_camera
    add_camera = upsert_camera
    update_camera = upsert_camera

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        return {
            "camera_id": value["camera_id"],
            "display_name": value["display_name"],
            "source": {"kind": value["source_kind"], "path": value["source_path"]},
            "enabled": bool(value["enabled"]),
            "zones": json.loads(value["zones_json"] or "[]"),
            "processing_options": json.loads(value["processing_options_json"] or "{}"),
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
        }

    def list_cameras(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute("SELECT camera_id FROM cameras ORDER BY created_at, camera_id").fetchall()
        return [camera for row in rows if (camera := self.get_camera(row[0])) is not None]

    def delete_camera(self, camera_id: str) -> bool:
        with self._lock, self.connection:
            result = self.connection.execute("DELETE FROM cameras WHERE camera_id = ?", (camera_id,))
            return result.rowcount > 0

    def save_monitoring_report(self, report: Any, *, created_at: str | None = None) -> dict[str, Any]:
        value = report.as_dict() if hasattr(report, "as_dict") else dict(report)
        stamp = _timestamp(created_at or value.get("finished_at") or value.get("started_at"))
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO monitoring_reports
                (camera_id, session_id, status, frame_count, event_count, last_error,
                 track_scope, source_kind, started_at, finished_at,
                 latest_media_timestamp_seconds, last_frame_at, frames_dropped,
                 reconnect_count, worker_mode, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(camera_id, session_id) DO UPDATE SET
                    status = excluded.status, frame_count = excluded.frame_count,
                    event_count = excluded.event_count, last_error = excluded.last_error,
                    track_scope = excluded.track_scope, source_kind = excluded.source_kind,
                    started_at = excluded.started_at, finished_at = excluded.finished_at,
                    latest_media_timestamp_seconds = excluded.latest_media_timestamp_seconds,
                    last_frame_at = excluded.last_frame_at, frames_dropped = excluded.frames_dropped,
                    reconnect_count = excluded.reconnect_count, worker_mode = excluded.worker_mode""",
                (
                    value["camera_id"], value["session_id"], _enum_value(value.get("status", "unknown")),
                    int(value.get("frame_count", 0)), int(value.get("event_count", 0)), value.get("last_error"),
                    value.get("track_scope"), _enum_value(value.get("source_kind")), value.get("started_at"),
                    value.get("finished_at"), value.get("latest_media_timestamp_seconds"), value.get("last_frame_at"),
                    int(value.get("frames_dropped", 0)), int(value.get("reconnect_count", 0)), value.get("worker_mode"), stamp,
                ),
            )
        result = self.get_monitoring_report(value["camera_id"], value["session_id"])
        if result is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("monitoring report did not persist")
        return result

    add_monitoring_report = save_monitoring_report
    save_report = save_monitoring_report

    def get_monitoring_report(self, camera_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM monitoring_reports WHERE camera_id = ? AND session_id = ?",
                (camera_id, session_id),
            ).fetchone()
        return dict(row) if row else None

    def list_monitoring_reports(
        self, *, camera_id: str | None = None, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses, values = [], []
        if camera_id:
            clauses.append("camera_id = ?")
            values.append(camera_id)
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM monitoring_reports" + where + " ORDER BY created_at, camera_id, session_id", values
            ).fetchall()
        return [dict(row) for row in rows]

    list_reports = list_monitoring_reports
    get_reports = list_monitoring_reports
    get_monitoring_reports = list_monitoring_reports

    def save_monitoring_anomaly(self, anomaly: Any, *, created_at: str | None = None) -> dict[str, Any]:
        value = anomaly.as_dict() if hasattr(anomaly, "as_dict") else dict(anomaly)
        stamp = _timestamp(created_at or value.get("orchestration_timestamp"))
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO monitoring_anomalies
                (anomaly_id, anomaly_type, camera_id, session_id, reason, severity,
                 timestamp_seconds, related_event_ids_json, related_track_ids_json,
                 source_quality, orchestration_timestamp, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(anomaly_id) DO UPDATE SET
                    reason = excluded.reason, severity = excluded.severity,
                    timestamp_seconds = excluded.timestamp_seconds,
                    related_event_ids_json = excluded.related_event_ids_json,
                    related_track_ids_json = excluded.related_track_ids_json,
                    source_quality = excluded.source_quality,
                    orchestration_timestamp = excluded.orchestration_timestamp,
                    details_json = excluded.details_json""",
                (
                    value["anomaly_id"], _enum_value(value.get("anomaly_type", value.get("type", "processing_failure"))),
                    value["camera_id"], value["session_id"], value["reason"], _enum_value(value.get("severity", "medium")),
                    value.get("timestamp_seconds"), json.dumps(value.get("related_event_ids", value.get("event_ids", []))),
                    json.dumps(value.get("related_track_ids", value.get("track_ids", []))),
                    _enum_value(value.get("source_quality", "derived")), value.get("orchestration_timestamp"),
                    json.dumps(value.get("details", {}), sort_keys=True), stamp,
                ),
            )
        result = self.get_monitoring_anomaly(value["anomaly_id"])
        if result is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("monitoring anomaly did not persist")
        return result

    add_monitoring_anomaly = save_monitoring_anomaly

    def get_monitoring_anomaly(self, anomaly_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM monitoring_anomalies WHERE anomaly_id = ?", (anomaly_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        return self._public_anomaly(value)

    @staticmethod
    def _public_anomaly(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "anomaly_id": value["anomaly_id"], "anomaly_type": value["anomaly_type"],
            "camera_id": value["camera_id"], "session_id": value["session_id"],
            "reason": value["reason"], "severity": value["severity"],
            "timestamp_seconds": value["timestamp_seconds"],
            "related_event_ids": json.loads(value["related_event_ids_json"] or "[]"),
            "related_track_ids": json.loads(value["related_track_ids_json"] or "[]"),
            "source_quality": value["source_quality"],
            "orchestration_timestamp": value["orchestration_timestamp"],
            "details": json.loads(value["details_json"] or "{}"), "created_at": value["created_at"],
        }

    def list_monitoring_anomalies(
        self, *, camera_id: str | None = None, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses, values = [], []
        if camera_id:
            clauses.append("camera_id = ?")
            values.append(camera_id)
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM monitoring_anomalies" + where + " ORDER BY created_at DESC, anomaly_id", values
            ).fetchall()
        return [self._public_anomaly(dict(row)) for row in rows]

    list_anomalies = list_monitoring_anomalies
    get_monitoring_anomalies = list_monitoring_anomalies

    def close(self) -> None:
        self.connection.close()

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
        self.connection.execute(
            """INSERT INTO videos
            (video_id, filename, media_path, media_object_key, content_type, size_bytes, checksum_sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (video_id, filename, media_path, media_object_key, content_type, size_bytes, checksum_sha256, created_at),
        )
        self.connection.commit()
        return self.get_video(video_id)

    def update_evidence(self, event_id: str, path: str) -> None:
        self.connection.execute(
            "UPDATE events SET evidence_status = 'ready', evidence_path = ? WHERE event_id = ?",
            (path, event_id),
        )
        self.connection.commit()

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        return dict(row) if row else None

    def list_videos(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM videos ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def list_video_artifacts(self, video_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT event_id, evidence_path FROM events WHERE video_id = ? AND evidence_path IS NOT NULL",
            (video_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_video(self, video_id: str) -> bool:
        """Delete a video and all local authoritative descendants.

        RetentionService is the guarded caller.  Keeping this operation on the
        store makes deletion propagation explicit and testable for SQLite.
        """

        with self.connection:
            event_ids = [
                row[0]
                for row in self.connection.execute(
                    "SELECT event_id FROM events WHERE video_id = ?", (video_id,)
                ).fetchall()
            ]
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                self.connection.execute(
                    f"DELETE FROM notification_deliveries WHERE alert_id IN "
                    f"(SELECT alert_id FROM alerts WHERE event_id IN ({placeholders}))",
                    event_ids,
                )
                self.connection.execute(
                    f"DELETE FROM alerts WHERE event_id IN ({placeholders})", event_ids
                )
                self.connection.execute(
                    f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids
                )
            self.connection.execute("DELETE FROM track_observations WHERE video_id = ?", (video_id,))
            self.connection.execute("DELETE FROM frames WHERE video_id = ?", (video_id,))
            self.connection.execute("DELETE FROM jobs WHERE video_id = ?", (video_id,))
            cursor = self.connection.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
            return cursor.rowcount > 0

    def add_job(self, job_id: str, video_id: str, status: str, created_at: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO jobs(job_id, video_id, status, created_at) VALUES (?, ?, ?, ?)",
            (job_id, video_id, status, created_at),
        )
        self.connection.commit()
        return self.get_job(job_id)

    def update_job(self, job_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        self.connection.execute("UPDATE jobs SET status = ?, error = ? WHERE job_id = ?", (status, error, job_id))
        self.connection.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add_events(self, video_id: str, events: list[dict[str, Any]]) -> None:
        self.connection.executemany(
            """INSERT OR IGNORE INTO events
            (event_id, video_id, event_type, zone_id, object_id, object_label, track_scope, timestamp_seconds, source_frame_index)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    event["event_id"], video_id, event["event_type"], event["zone_id"], event["object_id"],
                    event.get("object_label", "object"), event.get("track_scope", "session"), event["timestamp_seconds"], event.get("source_frame_index"),
                )
                for event in events
            ],
        )
        self.connection.commit()

    def add_observations(self, video_id: str, observations: tuple[Any, ...], *, frame_index_start: int = 0) -> None:
        for offset, frame in enumerate(observations):
            frame_index = frame_index_start + offset
            frame_id = f"{video_id}:{frame_index}"
            self.connection.execute(
                "INSERT OR IGNORE INTO frames(frame_id, video_id, frame_index, timestamp_seconds, timestamp_quality) VALUES (?, ?, ?, ?, ?)",
                (frame_id, video_id, frame_index, frame.timestamp, "source_media"),
            )
            self.connection.executemany(
                """INSERT INTO track_observations
                (video_id, track_id, frame_index, timestamp_seconds, x, y, object_label, confidence, track_scope, object_category, bbox_json, appearance_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        video_id,
                        item.object_id,
                        frame_index,
                        frame.timestamp,
                        item.x,
                        item.y,
                        item.label,
                        item.confidence,
                        item.track_scope,
                        item.category,
                        json.dumps(list(item.bbox)) if item.bbox is not None else None,
                        json.dumps(item.appearance.as_dict()) if item.appearance is not None else None,
                    )
                    for item in frame.objects
                ],
            )
        self.connection.commit()

    def search_tracks(
        self,
        *,
        video_id: str | None = None,
        track_id: str | None = None,
        track_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, values = [], []
        if video_id:
            clauses.append("video_id = ?")
            values.append(video_id)
        if track_id:
            clauses.append("track_id = ?")
            values.append(track_id)
        if track_scope:
            clauses.append("track_scope = ?")
            values.append(track_scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute("SELECT * FROM track_observations" + where + " ORDER BY timestamp_seconds", values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["bbox"] = json.loads(item.pop("bbox_json")) if item.get("bbox_json") else None
            item["appearance"] = json.loads(item.pop("appearance_json")) if item.get("appearance_json") else None
            result.append(item)
        return result

    def add_chat_message(self, message_id: str, conversation_id: str, role: str, content: str, citations: list[dict[str, Any]], created_at: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO chat_messages(message_id, conversation_id, role, content, citations_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, role, content, json.dumps(citations), created_at),
        )
        self.connection.commit()
        return {"message_id": message_id, "conversation_id": conversation_id, "role": role, "content": content, "citations": citations, "created_at": created_at}

    def add_alert_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        self.connection.execute(
            """INSERT INTO alert_rules(rule_id, name, event_type, zone_id, object_label, enabled, cooldown_seconds, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (rule["rule_id"], rule["name"], rule.get("event_type"), rule.get("zone_id"), rule.get("object_label"), int(rule.get("enabled", True)), rule.get("cooldown_seconds", 0), rule["created_at"]),
        )
        self.connection.commit()
        return rule

    def list_alert_rules(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM alert_rules ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_alerts_for_events(self, events: list[dict[str, Any]], *, created_at: str) -> list[dict[str, Any]]:
        rules = self.list_alert_rules()
        created = []
        for event in events:
            for rule in rules:
                if not rule["enabled"]:
                    continue
                if rule["event_type"] and rule["event_type"] != event["event_type"]:
                    continue
                if rule["zone_id"] and rule["zone_id"] != event["zone_id"]:
                    continue
                if rule["object_label"] and rule["object_label"] != event.get("object_label", "object"):
                    continue
                alert_id = f"alert-{rule['rule_id']}-{event['event_id']}"
                message = f"{rule['name']}: {event['event_type']} in {event['zone_id']} at {event['timestamp_seconds']:.3f}s"
                self.connection.execute(
                    "INSERT OR IGNORE INTO alerts(alert_id, rule_id, event_id, status, message, created_at) VALUES (?, ?, ?, 'open', ?, ?)",
                    (alert_id, rule["rule_id"], event["event_id"], message, created_at),
                )
                created.append({"alert_id": alert_id, "rule_id": rule["rule_id"], "event_id": event["event_id"], "status": "open", "message": message, "created_at": created_at})
        self.connection.commit()
        return created

    def list_alerts(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT * FROM alerts WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM alerts ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert(self, alert_id: str, acknowledged_at: str) -> dict[str, Any] | None:
        self.connection.execute("UPDATE alerts SET status = 'acknowledged', acknowledged_at = ? WHERE alert_id = ?", (acknowledged_at, alert_id))
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        return dict(row) if row else None

    def delete_alert_rule(self, rule_id: str) -> bool:
        self.connection.execute("DELETE FROM alerts WHERE rule_id = ?", (rule_id,))
        cursor = self.connection.execute("DELETE FROM alert_rules WHERE rule_id = ?", (rule_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def delete_alert(self, alert_id: str) -> bool:
        cursor = self.connection.execute("DELETE FROM alerts WHERE alert_id = ?", (alert_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def clear_alerts(self) -> int:
        cursor = self.connection.execute("DELETE FROM alerts")
        self.connection.commit()
        return cursor.rowcount

    def search_events(
        self,
        *,
        video_id: str | None = None,
        object_id: str | None = None,
        zone_id: str | None = None,
        object_label: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, values = [], []
        if video_id:
            clauses.append("video_id = ?")
            values.append(video_id)
        if object_id:
            clauses.append("object_id = ?")
            values.append(object_id)
        if zone_id:
            clauses.append("zone_id = ?")
            values.append(zone_id)
        if object_label:
            clauses.append("object_label = ?")
            values.append(object_label)
        if event_type:
            clauses.append("event_type = ?")
            values.append(event_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute("SELECT * FROM events" + where + " ORDER BY timestamp_seconds", values).fetchall()
        return [dict(row) for row in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
