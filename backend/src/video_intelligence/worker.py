"""Celery application entrypoint for durable processing deployments.

The HTTP API remains inline by default. Operators can run this module after
enabling Redis/Celery and then route processing through a task dispatcher. The
task is intentionally small and imports the API lazily so offline imports and
tests do not require a broker connection.
"""

from __future__ import annotations

import os

from .infrastructure.config import load_settings


def create_celery_app():
    try:
        from celery import Celery
    except ImportError as exc:
        raise RuntimeError("install celery to run the worker") from exc
    settings = load_settings()
    if not settings.queue.broker_url:
        raise RuntimeError("REDIS_URL or CELERY_BROKER_URL is required to run the worker")
    app = Celery("video_intelligence", broker=settings.queue.broker_url, backend=settings.queue.result_backend)
    app.conf.task_default_queue = settings.queue.task_default_queue
    app.conf.task_always_eager = settings.queue.task_always_eager
    app.conf.beat_schedule = {
        "reconcile-outbox": {
            "task": "video_intelligence.reconcile_outbox",
            "schedule": 10.0,
            "kwargs": {"limit": 100, "max_batches": 1},
        },
    }

    @app.task(bind=True, name="video_intelligence.healthcheck", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
    def healthcheck(self):
        return {"status": "ok", "worker": self.request.id}

    # Task registration is lazy and does not construct a database, object
    # storage client, or notification channel until a task actually runs.
    from .operations.tasks import register_celery_tasks

    register_celery_tasks(app)

    return app


celery_app = None
if os.getenv("CELERY_IMPORT_APP", "false").lower() in {"1", "true", "yes"}:
    celery_app = create_celery_app()
