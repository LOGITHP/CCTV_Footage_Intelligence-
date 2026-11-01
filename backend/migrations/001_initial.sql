-- Reference PostgreSQL schema for deployments that use DATABASE_URL.
-- The development PostgresStore can create equivalent tables through SQLAlchemy;
-- production installations should run this file through a migration tool.
CREATE TABLE IF NOT EXISTS videos (
  video_id VARCHAR(64) PRIMARY KEY,
  filename TEXT NOT NULL,
  media_path TEXT NOT NULL,
  media_object_key TEXT,
  content_type VARCHAR(255),
  size_bytes BIGINT,
  checksum_sha256 VARCHAR(64),
  created_at VARCHAR(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id VARCHAR(64) PRIMARY KEY,
  video_id VARCHAR(64) NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  status VARCHAR(32) NOT NULL,
  error TEXT,
  created_at VARCHAR(64) NOT NULL,
  updated_at VARCHAR(64)
);
CREATE TABLE IF NOT EXISTS events (
  event_id VARCHAR(64) PRIMARY KEY,
  video_id VARCHAR(64) NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  event_type VARCHAR(64) NOT NULL,
  zone_id VARCHAR(255) NOT NULL,
  object_id VARCHAR(255) NOT NULL,
  object_label VARCHAR(255) NOT NULL DEFAULT 'object',
  timestamp_seconds DOUBLE PRECISION NOT NULL,
  evidence_status VARCHAR(32) NOT NULL DEFAULT 'pending',
  evidence_path TEXT,
  source_frame_index INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_video_time ON events(video_id, timestamp_seconds);
CREATE INDEX IF NOT EXISTS idx_events_object_zone_time ON events(object_id, zone_id, timestamp_seconds);
CREATE TABLE IF NOT EXISTS track_observations (
  observation_id BIGSERIAL PRIMARY KEY,
  video_id VARCHAR(64) NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  track_id VARCHAR(255) NOT NULL,
  frame_index INTEGER NOT NULL,
  timestamp_seconds DOUBLE PRECISION NOT NULL,
  x DOUBLE PRECISION NOT NULL,
  y DOUBLE PRECISION NOT NULL,
  object_label VARCHAR(255) NOT NULL,
  confidence DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_track_observations_track ON track_observations(video_id, track_id, timestamp_seconds);
CREATE TABLE IF NOT EXISTS alert_rules (
  rule_id VARCHAR(64) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  event_type VARCHAR(64),
  zone_id VARCHAR(255),
  object_label VARCHAR(255),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  cooldown_seconds INTEGER NOT NULL DEFAULT 0,
  created_at VARCHAR(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id VARCHAR(128) PRIMARY KEY,
  rule_id VARCHAR(64) NOT NULL REFERENCES alert_rules(rule_id) ON DELETE CASCADE,
  event_id VARCHAR(64) NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  status VARCHAR(32) NOT NULL DEFAULT 'open',
  message TEXT NOT NULL,
  created_at VARCHAR(64) NOT NULL,
  acknowledged_at VARCHAR(64),
  UNIQUE(rule_id, event_id)
);
CREATE TABLE IF NOT EXISTS chat_messages (
  message_id VARCHAR(64) PRIMARY KEY,
  conversation_id VARCHAR(64) NOT NULL,
  role VARCHAR(32) NOT NULL,
  content TEXT NOT NULL,
  citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_messages (
  message_id VARCHAR(128) PRIMARY KEY,
  topic VARCHAR(255) NOT NULL,
  aggregate_type VARCHAR(64),
  aggregate_id VARCHAR(128),
  dedupe_key VARCHAR(255) UNIQUE,
  payload_json TEXT NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  locked_at TEXT,
  processed_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status_available ON outbox_messages(status, available_at);
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate ON outbox_messages(aggregate_type, aggregate_id);

-- Local-auth/RBAC and append-only security audit records.  Password hashes are
-- PBKDF2 records; plaintext passwords and bearer/evidence tokens are never
-- persisted.
CREATE TABLE IF NOT EXISTS security_users (
  user_id VARCHAR(64) PRIMARY KEY,
  username VARCHAR(120) NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role VARCHAR(16) NOT NULL CHECK (role IN ('admin', 'operator', 'reviewer')),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at VARCHAR(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
  audit_id VARCHAR(64) PRIMARY KEY,
  occurred_at VARCHAR(64) NOT NULL,
  action VARCHAR(255) NOT NULL,
  outcome VARCHAR(64) NOT NULL,
  actor_id VARCHAR(64),
  actor_role VARCHAR(16),
  resource_type VARCHAR(128),
  resource_id VARCHAR(255),
  ip_address VARCHAR(255),
  user_agent TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit_logs(resource_type, resource_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id, occurred_at);
