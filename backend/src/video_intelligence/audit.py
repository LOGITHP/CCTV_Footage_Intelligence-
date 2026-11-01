"""Append-only audit logging for authentication and uploaded-video access.

The repository works with either a local SQLite path or an existing SQLite
connection.  That makes it suitable for the normal local database and for
``:memory:`` tests without a network service.  Metadata is recursively
sanitized before serialization so bearer tokens, passwords, and key material
cannot be copied into the audit trail by accident.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping
from uuid import uuid4

from .auth import UserPrincipal


_SENSITIVE_KEY = re.compile(
    r"(?:pass(?:word)?|secret|token|authorization|cookie|api[_-]?key|access[_-]?key|private[_-]?key|jwt)",
    re.IGNORECASE,
)
_MAX_TEXT = 2048


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_audit_value(value: Any, *, key: str | None = None) -> Any:
    """Return JSON-safe metadata with secret-looking fields redacted."""

    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): sanitize_audit_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_audit_value(item) for item in value]
    if isinstance(value, bytes):
        return "[BINARY_REDACTED]"
    if isinstance(value, str):
        # Avoid persisting copied bearer/JWT/evidence tokens even when a caller
        # supplied them under an otherwise innocuous metadata key.
        if value.lower().startswith(("bearer ", "jwt ")) or (
            value.count(".") == 2 and len(value) > 40
        ):
            return "[REDACTED]"
        return value[:_MAX_TEXT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_TEXT]


@dataclass(frozen=True, slots=True)
class AuditLogEntry:
    audit_id: str
    occurred_at: str
    action: str
    outcome: str
    actor_id: str | None = None
    actor_role: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    metadata: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["metadata"] = dict(self.metadata or {})
        return result

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


# A shorter alias used by some callers.
AuditEvent = AuditLogEntry


class AuditLogRepository:
    """SQLite audit repository with an append/query interface."""

    def __init__(
        self,
        path: str | Path | sqlite3.Connection = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if isinstance(path, sqlite3.Connection) and connection is None:
            connection = path
            path = ":memory:"
        self._owns_connection = connection is None
        if connection is None:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, check_same_thread=False)
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                audit_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                actor_id TEXT,
                actor_role TEXT,
                resource_type TEXT,
                resource_id TEXT,
                ip_address TEXT,
                user_agent TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit_logs(resource_type, resource_id, occurred_at)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id, occurred_at)"
        )
        self.connection.commit()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> AuditLogEntry:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return AuditLogEntry(
            audit_id=row["audit_id"],
            occurred_at=row["occurred_at"],
            action=row["action"],
            outcome=row["outcome"],
            actor_id=row["actor_id"],
            actor_role=row["actor_role"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            ip_address=row["ip_address"],
            user_agent=row["user_agent"],
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def append(self, entry: AuditLogEntry) -> AuditLogEntry:
        metadata = sanitize_audit_value(dict(entry.metadata or {}))
        if not isinstance(metadata, dict):  # pragma: no cover - mapping input guarantees this
            metadata = {}
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO audit_logs
                (audit_id, occurred_at, action, outcome, actor_id, actor_role,
                 resource_type, resource_id, ip_address, user_agent, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.audit_id,
                    entry.occurred_at,
                    entry.action,
                    entry.outcome,
                    entry.actor_id,
                    entry.actor_role,
                    entry.resource_type,
                    entry.resource_id,
                    entry.ip_address,
                    entry.user_agent[:_MAX_TEXT] if entry.user_agent else None,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                ),
            )
            self.connection.commit()
        return AuditLogEntry(
            audit_id=entry.audit_id,
            occurred_at=entry.occurred_at,
            action=entry.action,
            outcome=entry.outcome,
            actor_id=entry.actor_id,
            actor_role=entry.actor_role,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            ip_address=entry.ip_address,
            user_agent=entry.user_agent[:_MAX_TEXT] if entry.user_agent else None,
            metadata=metadata,
        )

    def get(self, audit_id: str) -> AuditLogEntry | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM audit_logs WHERE audit_id = ?", (audit_id,)).fetchone()
        return self._row_to_entry(row) if row else None

    def list(
        self,
        *,
        actor_id: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
    ) -> list[AuditLogEntry]:
        if not 1 <= limit <= 1000:
            raise ValueError("audit log limit must be between 1 and 1000")
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("actor_id", actor_id),
            ("action", action),
            ("resource_type", resource_type),
            ("resource_id", resource_id),
            ("outcome", outcome),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM audit_logs{where} ORDER BY occurred_at DESC, audit_id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def purge_before(self, occurred_before: str) -> int:
        """Delete old entries after retention has been enforced by policy."""

        with self._lock:
            cursor = self.connection.execute("DELETE FROM audit_logs WHERE occurred_at < ?", (occurred_before,))
            self.connection.commit()
            return cursor.rowcount


SQLiteAuditLogRepository = AuditLogRepository
InMemoryAuditLogRepository = AuditLogRepository
AuditLogStore = AuditLogRepository


class AuditLogService:
    """Create sanitized audit entries for auth, evidence, and media actions."""

    def __init__(self, repository: AuditLogRepository) -> None:
        self.repository = repository

    @staticmethod
    def _actor_fields(actor: UserPrincipal | str | None) -> tuple[str | None, str | None]:
        if isinstance(actor, UserPrincipal):
            return actor.user_id, actor.role.value
        if actor is None:
            return None, None
        return str(actor), None

    def record(
        self,
        action: str,
        *,
        actor: UserPrincipal | str | None = None,
        outcome: str = "success",
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
        audit_id: str | None = None,
    ) -> AuditLogEntry:
        if not action or not action.strip():
            raise ValueError("audit action is required")
        if not outcome or not outcome.strip():
            raise ValueError("audit outcome is required")
        actor_id, actor_role = self._actor_fields(actor)
        entry = AuditLogEntry(
            audit_id=audit_id or str(uuid4()),
            occurred_at=occurred_at or _utc_now(),
            action=action.strip(),
            outcome=outcome.strip(),
            actor_id=actor_id,
            actor_role=actor_role,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata=sanitize_audit_value(dict(metadata or {})),
        )
        return self.repository.append(entry)

    # Explicit alias for call sites that use event terminology.
    log = record
    record_event = record

    def query(self, **filters: Any) -> list[AuditLogEntry]:
        return self.repository.list(**filters)
