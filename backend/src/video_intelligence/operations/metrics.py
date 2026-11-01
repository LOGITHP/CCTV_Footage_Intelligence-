"""Prometheus-compatible metrics hooks with an offline fallback.

The processing and delivery services depend on ``MetricsHooks`` rather than on
the Prometheus client directly.  This keeps unit tests deterministic and lets
operators enable a real registry only when ``PROMETHEUS_ENABLED`` is set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from threading import Lock
from typing import Mapping, Protocol


Labels = Mapping[str, str]


class MetricSink(Protocol):
    def increment(self, name: str, value: float = 1.0, labels: Labels | None = None) -> None: ...

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None: ...

    def set(self, name: str, value: float, labels: Labels | None = None) -> None: ...


class NoopMetrics:
    """A metrics sink that intentionally performs no work."""

    def increment(self, name: str, value: float = 1.0, labels: Labels | None = None) -> None:
        return None

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None:
        return None

    def set(self, name: str, value: float, labels: Labels | None = None) -> None:
        return None


@dataclass
class InMemoryMetrics:
    """Small fake-friendly sink used by offline tests and local diagnostics."""

    counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(default_factory=dict)
    observations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = field(default_factory=dict)
    gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(default_factory=dict)

    @staticmethod
    def _key(name: str, labels: Labels | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))

    def increment(self, name: str, value: float = 1.0, labels: Labels | None = None) -> None:
        key = self._key(name, labels)
        self.counters[key] = self.counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None:
        key = self._key(name, labels)
        self.observations.setdefault(key, []).append(value)

    def set(self, name: str, value: float, labels: Labels | None = None) -> None:
        self.gauges[self._key(name, labels)] = value


class PrometheusMetrics:
    """Metric sink backed by ``prometheus_client``.

    The dependency is optional at import time.  Construction raises a clear
    error when metrics were explicitly enabled without installing it.
    """

    def __init__(self, *, namespace: str = "video_intelligence", registry=None) -> None:
        try:
            from prometheus_client import CollectorRegistry
            from prometheus_client import Counter, Gauge, Histogram
        except ImportError as exc:  # pragma: no cover - depends on installation
            raise RuntimeError("install prometheus-client to enable Prometheus metrics") from exc
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.namespace = namespace
        self._counter_type = Counter
        self._gauge_type = Gauge
        self._histogram_type = Histogram
        self._counters = {}
        self._gauges = {}
        self._histograms = {}
        self._lock = Lock()

    @staticmethod
    def _label_names(labels: Labels | None) -> tuple[str, ...]:
        return tuple(sorted(str(key) for key in (labels or {}).keys()))

    def _metric_name(self, name: str) -> str:
        return name.removesuffix("_total")

    @staticmethod
    def _child(metric, labels: Labels):
        return metric.labels(**labels) if labels else metric

    def increment(self, name: str, value: float = 1.0, labels: Labels | None = None) -> None:
        labels = labels or {}
        key = (name, self._label_names(labels))
        with self._lock:
            metric = self._counters.get(key)
            if metric is None:
                metric = self._counter_type(
                    self._metric_name(name),
                    name.replace("_", " "),
                    labelnames=key[1],
                    namespace=self.namespace,
                    registry=self.registry,
                )
                self._counters[key] = metric
        self._child(metric, labels).inc(value)

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None:
        labels = labels or {}
        key = (name, self._label_names(labels))
        with self._lock:
            metric = self._histograms.get(key)
            if metric is None:
                metric = self._histogram_type(
                    name,
                    name.replace("_", " "),
                    labelnames=key[1],
                    namespace=self.namespace,
                    registry=self.registry,
                )
                self._histograms[key] = metric
        self._child(metric, labels).observe(value)

    def set(self, name: str, value: float, labels: Labels | None = None) -> None:
        labels = labels or {}
        key = (name, self._label_names(labels))
        with self._lock:
            metric = self._gauges.get(key)
            if metric is None:
                metric = self._gauge_type(
                    name,
                    name.replace("_", " "),
                    labelnames=key[1],
                    namespace=self.namespace,
                    registry=self.registry,
                )
                self._gauges[key] = metric
        self._child(metric, labels).set(value)

    def render(self) -> bytes:
        """Return the standard Prometheus text exposition payload."""

        from prometheus_client import generate_latest

        return generate_latest(self.registry)


@dataclass
class MetricsHooks:
    """Stable operation-level hooks used by all durable services."""

    sink: MetricSink = field(default_factory=NoopMetrics)

    def increment(self, name: str, value: float = 1.0, labels: Labels | None = None) -> None:
        self.sink.increment(name, value, labels)

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None:
        self.sink.observe(name, value, labels)

    def set(self, name: str, value: float, labels: Labels | None = None) -> None:
        self.sink.set(name, value, labels)

    def job_started(self, *, operation: str = "processing") -> None:
        self.increment("jobs_started_total", labels={"operation": operation})

    def job_completed(self, *, operation: str = "processing") -> None:
        self.increment("jobs_completed_total", labels={"operation": operation})

    def job_failed(self, *, operation: str = "processing") -> None:
        self.increment("jobs_failed_total", labels={"operation": operation})

    def processing_seconds(self, value: float) -> None:
        self.observe("processing_seconds", value)

    def outbox_enqueued(self, topic: str) -> None:
        self.increment("outbox_enqueued_total", labels={"topic": topic})

    def outbox_delivered(self, topic: str) -> None:
        self.increment("outbox_delivered_total", labels={"topic": topic})

    def outbox_failed(self, topic: str) -> None:
        self.increment("outbox_failed_total", labels={"topic": topic})

    def outbox_depth(self, value: int) -> None:
        self.set("outbox_depth", value)

    def notification_delivered(self, channel: str) -> None:
        self.increment("notifications_delivered_total", labels={"channel": channel})

    def notification_failed(self, channel: str) -> None:
        self.increment("notifications_failed_total", labels={"channel": channel})

    def retention_planned(self, value: int) -> None:
        self.set("retention_planned", value)

    def backup_created(self) -> None:
        self.increment("backups_created_total")


def build_metrics(*, enabled: bool | None = None, namespace: str = "video_intelligence") -> MetricsHooks:
    """Build a metrics hook from explicit input or environment settings."""

    if enabled is None:
        enabled = os.getenv("PROMETHEUS_ENABLED", os.getenv("METRICS_ENABLED", "false")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if not enabled:
        return MetricsHooks(NoopMetrics())
    try:
        return MetricsHooks(PrometheusMetrics(namespace=namespace))
    except RuntimeError:
        # Metrics are observability, not a reason to make video processing fail.
        return MetricsHooks(NoopMetrics())
