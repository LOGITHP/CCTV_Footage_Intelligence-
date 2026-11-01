"""Inline and Celery task dispatchers."""

from __future__ import annotations

from typing import Any

from .config import QueueSettings


class QueueUnavailable(RuntimeError):
    pass


class InlineDispatcher:
    mode = "inline"

    def dispatch(self, task_name: str, **payload: Any) -> str:
        return f"inline:{task_name}"


class CeleryDispatcher:
    mode = "celery"

    def __init__(self, settings: QueueSettings) -> None:
        if not settings.broker_url:
            raise QueueUnavailable("REDIS_URL or CELERY_BROKER_URL is required for Celery")
        try:
            from celery import Celery
        except ImportError as exc:
            raise QueueUnavailable("install celery to enable background workers") from exc
        self.app = Celery("video_intelligence", broker=settings.broker_url, backend=settings.result_backend)
        self.app.conf.task_always_eager = settings.task_always_eager
        self.app.conf.task_default_queue = settings.task_default_queue

    def dispatch(self, task_name: str, **payload: Any) -> str:
        result = self.app.send_task(task_name, kwargs=payload)
        return str(result.id)


def build_dispatcher(settings: QueueSettings):
    return CeleryDispatcher(settings) if settings.broker_url else InlineDispatcher()
