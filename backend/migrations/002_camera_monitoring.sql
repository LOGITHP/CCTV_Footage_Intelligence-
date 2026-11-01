-- Multi-camera registry, live-worker reports, scoped observations, and anomaly
-- records. Run after 001_initial.sql for existing PostgreSQL deployments.

ALTER TABLE events ADD COLUMN IF NOT EXISTS object_label VARCHAR(255) NOT NULL DEFAULT 'object';
ALTER TABLE events ADD COLUMN IF NOT EXISTS track_scope VARCHAR(255) NOT NULL DEFAULT 'session';
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_frame_index INTEGER;

ALTER TABLE track_observations ADD COLUMN IF NOT EXISTS track_scope VARCHAR(255) NOT NULL DEFAULT 'session';
ALTER TABLE track_observations ADD COLUMN IF NOT EXISTS object_category VARCHAR(32);
ALTER TABLE track_observations ADD COLUMN IF NOT EXISTS bbox_json TEXT;
ALTER TABLE track_observations ADD COLUMN IF NOT EXISTS appearance_json TEXT;

CREATE TABLE IF NOT EXISTS cameras (
  camera_id VARCHAR(128) PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  source_kind VARCHAR(16) NOT NULL,
  source_path TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  zones_json TEXT NOT NULL DEFAULT '[]',
  processing_options_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitoring_reports (
  camera_id VARCHAR(128) NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE,
  session_id VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL,
  frame_count INTEGER NOT NULL DEFAULT 0,
  event_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  track_scope VARCHAR(255),
  source_kind VARCHAR(16),
  started_at TEXT,
  finished_at TEXT,
  latest_media_timestamp_seconds DOUBLE PRECISION,
  last_frame_at TEXT,
  frames_dropped INTEGER NOT NULL DEFAULT 0,
  reconnect_count INTEGER NOT NULL DEFAULT 0,
  worker_mode VARCHAR(32),
  created_at TEXT NOT NULL,
  PRIMARY KEY (camera_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_monitoring_reports_camera_created
  ON monitoring_reports(camera_id, created_at);

CREATE TABLE IF NOT EXISTS monitoring_anomalies (
  anomaly_id VARCHAR(128) PRIMARY KEY,
  anomaly_type VARCHAR(64) NOT NULL,
  camera_id VARCHAR(128) NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE,
  session_id VARCHAR(255) NOT NULL,
  reason TEXT NOT NULL,
  severity VARCHAR(16) NOT NULL,
  timestamp_seconds DOUBLE PRECISION,
  related_event_ids_json TEXT NOT NULL DEFAULT '[]',
  related_track_ids_json TEXT NOT NULL DEFAULT '[]',
  source_quality VARCHAR(16) NOT NULL,
  orchestration_timestamp TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitoring_anomalies_scope
  ON monitoring_anomalies(camera_id, session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_track_observations_scope
  ON track_observations(video_id, track_scope, track_id, timestamp_seconds);
