"""Portable transactional-outbox storage and reconciliation.

The implementation uses SQLAlchemy's portable ORM for both SQLite and
PostgreSQL.  It never starts a broker or contacts an integration by itself;
the caller supplies handlers to :class:`ReconciliationService`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ..infrastructure.schema import OutboxRecord
from .metrics import MetricsHooks


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        value = _now()
    if isinstance(value, str):
        return value
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    message_id: str
    topic: str
    aggregate_type: str | None
    aggregate_id: str | None
    dedupe_key: str | None
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: str
    locked_at: str | None
    processed_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


def _message(record: OutboxRecord) -> OutboxMessage:
    payload = json.loads(record.payload_json)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return OutboxMessage(
        message_id=record.message_id,
        topic=record.topic,
        aggregate_type=record.aggregate_type,
        aggregate_id=record.aggregate_id,
        dedupe_key=record.dedupe_key,
        payload=payload,
        status=record.status,
        attempts=record.attempts,
        available_at=_timestamp(record.available_at),
        locked_at=_timestamp(record.locked_at) if record.locked_at else None,
        processed_at=_timestamp(record.processed_at) if record.processed_at else None,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class OutboxStore:
    """SQLite/PostgreSQL-compatible repository for outbox messages."""

    def __init__(
        self,
        database: str | Path | Engine | None = None,
        *,
        database_url: str | None = None,
        path: str | Path | None = None,
        sqlite_path: str | Path | None = None,
        engine: Engine | None = None,
        create_schema: bool = True,
        metrics: MetricsHooks | None = None,
    ) -> None:
        if database is None:
            database = database_url or path or sqlite_path or ":memory:"
        elif database_url is not None or path is not None or sqlite_path is not None:
            raise TypeError("pass one of database, database_url, path, or sqlite_path")
        self._owns_engine = engine is None and not isinstance(database, Engine)
        self.engine = engine or self._build_engine(database)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.metrics = metrics or MetricsHooks()
        if create_schema:
            OutboxRecord.__table__.create(bind=self.engine, checkfirst=True)

    @staticmethod
    def _build_engine(database: str | Path | Engine) -> Engine:
        if isinstance(database, Engine):
            return database
        value = str(database)
        if value == ":memory:":
            return create_engine(
                "sqlite+pysqlite:///:memory:",
                future=True,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        if "://" not in value:
            path = Path(value)
            if str(path) == ":memory:":
                return create_engine(
                    "sqlite+pysqlite:///:memory:",
                    future=True,
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool,
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            value = f"sqlite+pysqlite:///{path.as_posix()}"
        options: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if value.startswith("sqlite") and ":memory:" in value:
            options.update({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool})
        return create_engine(value, **options)

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def __enter__(self) -> "OutboxStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @staticmethod
    def _record_dict(record: OutboxRecord) -> dict[str, Any]:
        return {
            "message_id": record.message_id,
            "topic": record.topic,
            "aggregate_type": record.aggregate_type,
            "aggregate_id": record.aggregate_id,
            "dedupe_key": record.dedupe_key,
            "payload": _message(record).payload,
            "status": record.status,
            "attempts": record.attempts,
            "available_at": _timestamp(record.available_at),
            "locked_at": _timestamp(record.locked_at) if record.locked_at else None,
            "processed_at": _timestamp(record.processed_at) if record.processed_at else None,
            "last_error": record.last_error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def enqueue(
        self,
        topic: str,
        payload: Mapping[str, Any],
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        dedupe_key: str | None = None,
        message_id: str | None = None,
        available_at: datetime | str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Insert one pending message, returning an existing dedupe match."""

        now = _timestamp(created_at)
        available = _timestamp(available_at or now)
        message_id = message_id or str(uuid4())
        with self._sessions() as session:
            with session.begin():
                if dedupe_key:
                    existing = session.scalar(
                        select(OutboxRecord).where(OutboxRecord.dedupe_key == dedupe_key)
                    )
                    if existing is not None:
                        return self._record_dict(existing)
                record = OutboxRecord(
                    message_id=message_id,
                    topic=topic,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    dedupe_key=dedupe_key,
                    payload_json=json.dumps(dict(payload), sort_keys=True, separators=(",", ":")),
                    status="pending",
                    attempts=0,
                    available_at=available,
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
            self.metrics.outbox_enqueued(topic)
            return self._record_dict(record)

    publish = enqueue
    enqueue_event = enqueue

    def get(self, message_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.get(OutboxRecord, message_id)
            return self._record_dict(record) if record else None

    def list(self, *, status: str | None = None, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        statement = select(OutboxRecord).order_by(OutboxRecord.created_at)
        if status:
            statement = statement.where(OutboxRecord.status == status)
        if aggregate_id:
            statement = statement.where(OutboxRecord.aggregate_id == aggregate_id)
        with self._sessions() as session:
            return [self._record_dict(item) for item in session.scalars(statement).all()]

    def depth(self) -> int:
        with self._sessions() as session:
            statement = select(OutboxRecord).where(
                OutboxRecord.status.in_(("pending", "processing"))
            )
            return len(session.scalars(statement).all())

    def reset_expired_leases(self, *, now: datetime | str | None = None) -> int:
        current = _timestamp(now)
        changed = 0
        with self._sessions() as session:
            with session.begin():
                records = session.scalars(
                    select(OutboxRecord).where(
                        OutboxRecord.status == "processing",
                        OutboxRecord.locked_at.is_not(None),
                        OutboxRecord.locked_at < current,
                    )
                ).all()
                for record in records:
                    record.status = "pending"
                    record.locked_at = None
                    record.available_at = current
                    record.updated_at = current
                    changed += 1
        return changed

    def claim(
        self,
        *,
        limit: int = 50,
        lease_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        current_dt = _parse_timestamp(_timestamp(now))
        current = _timestamp(current_dt)
        lease_until = _timestamp(current_dt + timedelta(seconds=max(1, lease_seconds)))
        self.reset_expired_leases(now=current)
        claimed: list[dict[str, Any]] = []
        with self._sessions() as session:
            with session.begin():
                statement = (
                    select(OutboxRecord)
                    .where(
                        # Retries are represented as pending with a future
                        # ``available_at``.  A terminal failed message is not
                        # picked up again until an operator explicitly resets
                        # it, preventing a permanently bad payload from
                        # spinning a worker.
                        OutboxRecord.status == "pending",
                        OutboxRecord.available_at <= current,
                    )
                    .order_by(OutboxRecord.created_at)
                    .limit(limit)
                )
                if self.engine.dialect.name == "postgresql":
                    statement = statement.with_for_update(skip_locked=True)
                records = session.scalars(statement).all()
                for record in records:
                    record.status = "processing"
                    record.attempts += 1
                    record.locked_at = lease_until
                    record.updated_at = current
                    claimed.append(self._record_dict(record))
        return claimed

    def mark_delivered(
        self,
        message_id: str,
        *,
        processed_at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        current = _timestamp(processed_at)
        with self._sessions() as session:
            with session.begin():
                record = session.get(OutboxRecord, message_id)
                if record is None:
                    return None
                record.status = "delivered"
                record.locked_at = None
                record.processed_at = current
                record.updated_at = current
                record.last_error = None
            return self._record_dict(record)

    def mark_failed(
        self,
        message_id: str,
        error: str,
        *,
        retry_after_seconds: int | float | None = 60,
        processed_at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        current_dt = _parse_timestamp(_timestamp(processed_at))
        current = _timestamp(current_dt)
        if retry_after_seconds is None:
            status = "failed"
            available = current
        else:
            status = "pending"
            available = _timestamp(current_dt + timedelta(seconds=max(0, retry_after_seconds)))
        with self._sessions() as session:
            with session.begin():
                record = session.get(OutboxRecord, message_id)
                if record is None:
                    return None
                record.status = status
                record.locked_at = None
                record.available_at = available
                record.last_error = str(error)[:4000]
                record.updated_at = current
            return self._record_dict(record)

    def delete_for_aggregate(self, aggregate_id: str) -> int:
        """Delete queued side effects for an aggregate during explicit purge."""

        with self._sessions() as session:
            with session.begin():
                records = session.scalars(
                    select(OutboxRecord).where(OutboxRecord.aggregate_id == aggregate_id)
                ).all()
                count = len(records)
                for record in records:
                    session.delete(record)
                return count


Handler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    claimed: int
    delivered: int
    failed: int
    skipped: int


class ReconciliationService:
    """Replay pending outbox messages through injected, testable handlers."""

    def __init__(
        self,
        outbox: OutboxStore,
        handlers: Mapping[str, Handler] | None = None,
        *,
        default_handler: Handler | None = None,
        metrics: MetricsHooks | None = None,
        lease_seconds: int = 300,
        retry_after_seconds: int = 60,
    ) -> None:
        self.outbox = outbox
        self.handlers = dict(handlers or {})
        self.default_handler = default_handler
        self.metrics = metrics or MetricsHooks()
        self.lease_seconds = lease_seconds
        self.retry_after_seconds = retry_after_seconds

    def reconcile_once(self, *, limit: int = 50, now: datetime | str | None = None) -> ReconciliationReport:
        claimed = self.outbox.claim(limit=limit, lease_seconds=self.lease_seconds, now=now)
        delivered = failed = skipped = 0
        self.metrics.outbox_depth(self.outbox.depth())
        for record in claimed:
            handler = self.handlers.get(record["topic"], self.default_handler)
            if handler is None:
                self.outbox.mark_failed(
                    record["message_id"],
                    f"no handler registered for topic {record['topic']}",
                    retry_after_seconds=None,
                    processed_at=now,
                )
                self.metrics.outbox_failed(record["topic"])
                skipped += 1
                continue
            try:
                handler(record)
            except Exception as exc:  # delivery must be retried, not lost
                self.outbox.mark_failed(
                    record["message_id"],
                    str(exc),
                    retry_after_seconds=self.retry_after_seconds,
                    processed_at=now,
                )
                self.metrics.outbox_failed(record["topic"])
                failed += 1
            else:
                self.outbox.mark_delivered(record["message_id"], processed_at=now)
                self.metrics.outbox_delivered(record["topic"])
                delivered += 1
        self.metrics.outbox_depth(self.outbox.depth())
        return ReconciliationReport(len(claimed), delivered, failed, skipped)

    def reconcile(self, *, limit: int = 50, max_batches: int = 1) -> ReconciliationReport:
        """Run bounded batches without spinning forever on a failing message."""

        total = ReconciliationReport(0, 0, 0, 0)
        for _ in range(max(1, max_batches)):
            report = self.reconcile_once(limit=limit)
            total = ReconciliationReport(
                total.claimed + report.claimed,
                total.delivered + report.delivered,
                total.failed + report.failed,
                total.skipped + report.skipped,
            )
            if report.claimed == 0:
                break
        return total

    # ``run_once`` reads naturally in worker code and keeps the service easy to
    # adopt without coupling callers to the implementation's first name.
    run_once = reconcile_once


# Repository terminology is common in deployments that already have a service
# layer; keep the concrete implementation available under both names.
OutboxRepository = OutboxStore
