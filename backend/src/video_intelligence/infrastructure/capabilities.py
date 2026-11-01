from __future__ import annotations

from typing import Any

from .config import InfrastructureSettings, load_settings


def infrastructure_capabilities(settings: InfrastructureSettings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    database = "postgresql-configured" if settings.database.url else "sqlite"
    return {
        "database": database,
        "object_store": settings.object_storage.backend,
        "vector_store": "qdrant-configured" if settings.vector_storage.url else "disabled",
        "queue": "celery-configured" if settings.queue.broker_url else "inline",
        "external_services_required": bool(
            settings.database.url or settings.object_storage.backend == "s3" or settings.vector_storage.url or settings.queue.broker_url
        ),
    }
