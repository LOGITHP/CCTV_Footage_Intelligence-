"""Environment-driven configuration for optional production infrastructure.

No infrastructure client is created while this module is imported.  This is
important for the offline SQLite test path and for commands such as ``--help``
that should not attempt to connect to a service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping


def _first(environ: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _boolean(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _integer(environ: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def _float(environ: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def _csv(environ: Mapping[str, str], name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = environ.get(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Authoritative event database settings.

    ``url`` is intentionally optional.  When it is empty, the local
    ``sqlite_path`` is used by the factory and no PostgreSQL driver is needed.
    """

    url: str | None = None
    sqlite_path: str = "data/video_intelligence.sqlite3"


@dataclass(frozen=True, slots=True)
class ObjectStorageSettings:
    """S3-compatible or local filesystem object storage settings."""

    backend: str = "local"
    local_root: str = "data/media"
    endpoint_url: str | None = None
    bucket: str = "video-intelligence"
    region: str = "us-east-1"
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)
    presign_ttl_seconds: int = 900


@dataclass(frozen=True, slots=True)
class VectorStorageSettings:
    """Qdrant settings, with a deterministic in-memory fallback."""

    url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    collection: str = "video-events"
    vector_size: int = 384
    distance: str = "Cosine"
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class QueueSettings:
    """Redis/Celery settings for long-running processing jobs."""

    redis_url: str | None = None
    broker_url: str | None = None
    result_backend: str | None = None
    task_always_eager: bool = False
    task_default_queue: str = "video-processing"


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    """Notification channels selected explicitly from the environment.

    ``mode=noop`` is the safe default.  A channel is never contacted merely by
    importing the application; delivery workers must be run explicitly.
    """

    mode: str = "noop"
    channels: tuple[str, ...] = ()
    local_path: str | None = None
    webhook_url: str | None = field(default=None, repr=False)
    webhook_timeout_seconds: float = 5.0
    email_host: str | None = None
    email_port: int = 25
    email_username: str | None = field(default=None, repr=False)
    email_password: str | None = field(default=None, repr=False)
    email_from: str | None = None
    email_to: str | None = None
    email_starttls: bool = False
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    telegram_allowed_chat_ids: tuple[str, ...] = ()
    telegram_webhook_secret: str | None = field(default=None, repr=False)
    telegram_api_base_url: str = "https://api.telegram.org"


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """Retention policy with deletion disabled and dry-run enabled by default."""

    days: int = 30
    dry_run: bool = True
    delete_enabled: bool = False
    media_root: str = "data/media"


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """Environment-configured backup destination and overwrite guard."""

    root: str = "data/backups"
    database_path: str = "data/video_intelligence.sqlite3"
    allow_overwrite: bool = False


@dataclass(frozen=True, slots=True)
class MetricsSettings:
    """Metrics export configuration.

    The hooks work with an in-memory sink in offline mode.  Prometheus export is
    opt-in so a local test run does not require a metrics collector.
    """

    enabled: bool = False
    namespace: str = "video_intelligence"


@dataclass(frozen=True, slots=True)
class InfrastructureSettings:
    """Complete infrastructure configuration loaded from process variables."""

    database: DatabaseSettings
    object_storage: ObjectStorageSettings
    vector_storage: VectorStorageSettings
    queue: QueueSettings
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    metrics: MetricsSettings = field(default_factory=MetricsSettings)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "InfrastructureSettings":
        if environ is None:
            for candidate in (Path(".env"), Path("../.env"), Path(__file__).resolve().parents[4] / ".env"):
                if candidate.is_file():
                    try:
                        for line in candidate.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                k, v = k.strip(), v.strip()
                                if k and k not in os.environ:
                                    os.environ[k] = v
                    except Exception:
                        pass
                    break
        env = os.environ if environ is None else environ

        database = DatabaseSettings(
            url=_first(env, "DATABASE_URL"),
            sqlite_path=_first(env, "DATABASE_PATH") or "data/video_intelligence.sqlite3",
        )

        endpoint_url = _first(env, "S3_ENDPOINT_URL", "MINIO_ENDPOINT")
        if endpoint_url and "://" not in endpoint_url:
            endpoint_url = f"http://{endpoint_url}"
        storage_backend = (_first(env, "OBJECT_STORAGE_BACKEND") or "local").lower()
        if storage_backend not in {"local", "s3", "minio"}:
            raise ValueError("OBJECT_STORAGE_BACKEND must be local, s3, or minio")
        object_storage = ObjectStorageSettings(
            backend="s3" if storage_backend == "minio" else storage_backend,
            local_root=_first(env, "OBJECT_STORAGE_ROOT", "MEDIA_ROOT") or "data/media",
            endpoint_url=endpoint_url,
            bucket=_first(env, "S3_BUCKET", "MINIO_BUCKET") or "video-intelligence",
            region=_first(env, "S3_REGION", "AWS_DEFAULT_REGION") or "us-east-1",
            access_key_id=_first(env, "S3_ACCESS_KEY_ID", "MINIO_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
            secret_access_key=_first(
                env,
                "S3_SECRET_ACCESS_KEY",
                "MINIO_SECRET_KEY",
                "AWS_SECRET_ACCESS_KEY",
            ),
            presign_ttl_seconds=_integer(env, "S3_PRESIGN_TTL_SECONDS", 900, minimum=1),
        )

        qdrant_url = _first(env, "QDRANT_URL")
        if qdrant_url is None:
            qdrant_host = _first(env, "QDRANT_HOST")
            if qdrant_host:
                qdrant_port = _first(env, "QDRANT_PORT") or "6333"
                qdrant_url = qdrant_host if "://" in qdrant_host else f"http://{qdrant_host}:{qdrant_port}"
        vector_storage = VectorStorageSettings(
            url=qdrant_url,
            api_key=_first(env, "QDRANT_API_KEY"),
            collection=_first(env, "QDRANT_COLLECTION") or "video-events",
            vector_size=_integer(env, "QDRANT_VECTOR_SIZE", 384, minimum=1),
            distance=_first(env, "QDRANT_DISTANCE") or "Cosine",
            timeout_seconds=float(_first(env, "QDRANT_TIMEOUT_SECONDS") or "10"),
        )

        redis_url = _first(env, "REDIS_URL")
        queue = QueueSettings(
            redis_url=redis_url,
            broker_url=_first(env, "CELERY_BROKER_URL") or redis_url,
            result_backend=_first(env, "CELERY_RESULT_BACKEND") or redis_url,
            task_always_eager=_boolean(env, "CELERY_TASK_ALWAYS_EAGER", default=False),
            task_default_queue=_first(env, "CELERY_TASK_DEFAULT_QUEUE") or "video-processing",
        )

        notification_mode = (_first(env, "NOTIFICATION_MODE") or "noop").lower()
        if notification_mode not in {"noop", "local", "webhook", "email", "telegram", "multi"}:
            raise ValueError("NOTIFICATION_MODE must be noop, local, webhook, email, telegram, or multi")
        notifications = NotificationSettings(
            mode=notification_mode,
            channels=_csv(env, "NOTIFICATION_CHANNELS"),
            local_path=_first(env, "NOTIFICATION_LOCAL_PATH"),
            webhook_url=_first(env, "NOTIFICATION_WEBHOOK_URL"),
            webhook_timeout_seconds=_float(env, "NOTIFICATION_WEBHOOK_TIMEOUT_SECONDS", 5.0, minimum=0.1),
            email_host=_first(env, "NOTIFICATION_EMAIL_HOST"),
            email_port=_integer(env, "NOTIFICATION_EMAIL_PORT", 25, minimum=1),
            email_username=_first(env, "NOTIFICATION_EMAIL_USERNAME"),
            email_password=_first(env, "NOTIFICATION_EMAIL_PASSWORD"),
            email_from=_first(env, "NOTIFICATION_EMAIL_FROM"),
            email_to=_first(env, "NOTIFICATION_EMAIL_TO"),
            email_starttls=_boolean(env, "NOTIFICATION_EMAIL_STARTTLS", default=False),
            telegram_bot_token=_first(env, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_first(env, "TELEGRAM_CHAT_ID"),
            telegram_allowed_chat_ids=_csv(env, "TELEGRAM_ALLOWED_CHAT_IDS"),
            telegram_webhook_secret=_first(env, "TELEGRAM_WEBHOOK_SECRET"),
            telegram_api_base_url=_first(env, "TELEGRAM_API_BASE_URL") or "https://api.telegram.org",
        )
        retention = RetentionSettings(
            days=_integer(env, "RETENTION_DAYS", 30, minimum=1),
            dry_run=_boolean(env, "RETENTION_DRY_RUN", default=True),
            delete_enabled=_boolean(env, "RETENTION_DELETE_ENABLED", default=False),
            media_root=_first(env, "RETENTION_MEDIA_ROOT", "MEDIA_ROOT") or "data/media",
        )
        backup = BackupSettings(
            root=_first(env, "BACKUP_ROOT") or "data/backups",
            database_path=_first(env, "BACKUP_DATABASE_PATH", "DATABASE_PATH") or "data/video_intelligence.sqlite3",
            allow_overwrite=_boolean(env, "BACKUP_ALLOW_OVERWRITE", default=False),
        )
        metrics = MetricsSettings(
            enabled=_boolean(env, "PROMETHEUS_ENABLED", default=False)
            or _boolean(env, "METRICS_ENABLED", default=False),
            namespace=_first(env, "PROMETHEUS_NAMESPACE") or "video_intelligence",
        )
        return cls(
            database=database,
            object_storage=object_storage,
            vector_storage=vector_storage,
            queue=queue,
            notifications=notifications,
            retention=retention,
            backup=backup,
            metrics=metrics,
        )


def load_settings() -> InfrastructureSettings:
    """Return a fresh settings snapshot from the current process environment."""

    return InfrastructureSettings.from_env()


def local_storage_path(settings: ObjectStorageSettings) -> Path:
    """Return the local object root as a ``Path`` for adapter construction."""

    return Path(settings.local_root)
