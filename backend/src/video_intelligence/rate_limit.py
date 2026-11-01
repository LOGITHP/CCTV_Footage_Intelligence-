"""Configurable local sliding-window rate limiting.

The limiter is intentionally process-local for the offline deployment.  A
future multi-worker deployment can keep the same policy/decision interface and
replace the counter store with Redis without changing route code.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
import threading
import time
from typing import Callable, Mapping

from fastapi import HTTPException, Request, status


class RateLimitConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    requests: int = 60
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests < 1:
            raise RateLimitConfigurationError("rate limit requests must be at least 1")
        if self.window_seconds <= 0:
            raise RateLimitConfigurationError("rate limit window must be positive")


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    default: RateLimitRule = field(default_factory=RateLimitRule)
    upload: RateLimitRule = field(default_factory=lambda: RateLimitRule(requests=5, window_seconds=60.0))
    trust_proxy_headers: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RateLimitPolicy":
        env = os.environ if environ is None else environ
        built_in = cls()
        default = built_in.default
        upload = built_in.upload
        raw = (env.get("RATE_LIMIT_POLICY") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as error:
                raise RateLimitConfigurationError("RATE_LIMIT_POLICY must be a JSON object") from error
            if not isinstance(parsed, dict):
                raise RateLimitConfigurationError("RATE_LIMIT_POLICY must be a JSON object")
            default = cls._rule_from_value(parsed.get("default", parsed))
            if "upload" in parsed:
                upload = cls._rule_from_value(parsed["upload"])
        requests = env.get("RATE_LIMIT_REQUESTS")
        window = env.get("RATE_LIMIT_WINDOW_SECONDS")
        if requests:
            try:
                default = RateLimitRule(int(requests), default.window_seconds)
            except ValueError as error:
                raise RateLimitConfigurationError("RATE_LIMIT_REQUESTS must be an integer") from error
        if window:
            try:
                default = RateLimitRule(default.requests, float(window))
            except ValueError as error:
                raise RateLimitConfigurationError("RATE_LIMIT_WINDOW_SECONDS must be numeric") from error
        upload_requests = env.get("UPLOAD_RATE_LIMIT_REQUESTS")
        upload_window = env.get("UPLOAD_RATE_LIMIT_WINDOW_SECONDS")
        if upload_requests:
            try:
                upload = RateLimitRule(int(upload_requests), upload.window_seconds)
            except ValueError as error:
                raise RateLimitConfigurationError("UPLOAD_RATE_LIMIT_REQUESTS must be an integer") from error
        if upload_window:
            try:
                upload = RateLimitRule(upload.requests, float(upload_window))
            except ValueError as error:
                raise RateLimitConfigurationError("UPLOAD_RATE_LIMIT_WINDOW_SECONDS must be numeric") from error
        trust_proxy = (env.get("TRUST_PROXY_HEADERS") or "").strip().lower() in {"1", "true", "yes", "on"}
        return cls(default=default, upload=upload, trust_proxy_headers=trust_proxy)

    @staticmethod
    def _rule_from_value(value: object) -> RateLimitRule:
        if not isinstance(value, dict):
            raise RateLimitConfigurationError("each rate-limit rule must be an object")
        requests = value.get("requests", value.get("limit", 60))
        window = value.get("window_seconds", value.get("window", 60))
        try:
            return RateLimitRule(int(requests), float(window))
        except (TypeError, ValueError) as error:
            raise RateLimitConfigurationError("rate-limit requests/window must be numeric") from error

    def rule(self, name: str) -> RateLimitRule:
        if name == "default":
            return self.default
        if name == "upload":
            return self.upload
        raise KeyError(f"unknown rate-limit rule: {name}")


RateLimitConfig = RateLimitPolicy


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    key: str
    rule: str
    limit: int
    remaining: int
    retry_after: float


class RateLimitExceeded(Exception):
    """Raised by ``RateLimiter.enforce`` after a request is refused."""

    def __init__(self, decision: RateLimitDecision) -> None:
        self.decision = decision
        super().__init__(f"rate limit exceeded for {decision.key}; retry in {decision.retry_after:.3f}s")

    @property
    def retry_after(self) -> float:
        return self.decision.retry_after

    @property
    def retry_after_seconds(self) -> float:
        return self.decision.retry_after


class RateLimiter:
    """Thread-safe in-memory sliding-window limiter."""

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        max_requests: int | None = None,
        window_seconds: float | None = None,
        limit: int | None = None,
        window: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_requests is None:
            max_requests = limit
        if window_seconds is None:
            window_seconds = window
        if max_requests is not None or window_seconds is not None:
            built_in = RateLimitPolicy()
            base = policy.default if policy is not None else built_in.default
            selected = RateLimitRule(
                max_requests if max_requests is not None else base.requests,
                window_seconds if window_seconds is not None else base.window_seconds,
            )
            policy = RateLimitPolicy(
                default=selected,
                upload=policy.upload if policy is not None else built_in.upload,
                trust_proxy_headers=policy.trust_proxy_headers if policy is not None else False,
            )
        self.policy = policy or RateLimitPolicy()
        self._clock = clock or time.monotonic
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.RLock()

    def _rule(self, rule: str | RateLimitRule) -> RateLimitRule:
        return rule if isinstance(rule, RateLimitRule) else self.policy.rule(rule)

    def check(
        self,
        key: str,
        *,
        rule: str = "default",
        cost: int = 1,
        now: float | None = None,
    ) -> RateLimitDecision:
        if not key or not str(key).strip():
            raise ValueError("rate-limit key is required")
        if cost < 1:
            raise ValueError("rate-limit cost must be at least 1")
        selected = self._rule(rule)
        current = self._clock() if now is None else now
        bucket_key = (str(key), rule if isinstance(rule, str) else "custom")
        with self._lock:
            events = self._events.setdefault(bucket_key, deque())
            cutoff = current - selected.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            used = len(events)
            if used + cost <= selected.requests:
                for _ in range(cost):
                    events.append(current)
                return RateLimitDecision(
                    allowed=True,
                    key=str(key),
                    rule=rule if isinstance(rule, str) else "custom",
                    limit=selected.requests,
                    remaining=max(0, selected.requests - used - cost),
                    retry_after=0.0,
                )
            retry_after = max(0.0, events[0] + selected.window_seconds - current) if events else selected.window_seconds
            return RateLimitDecision(
                allowed=False,
                key=str(key),
                rule=rule if isinstance(rule, str) else "custom",
                limit=selected.requests,
                remaining=0,
                retry_after=retry_after,
            )

    def allow(self, key: str, *, rule: str = "default", cost: int = 1, now: float | None = None) -> bool:
        return self.check(key, rule=rule, cost=cost, now=now).allowed

    is_allowed = allow

    def enforce(self, key: str, *, rule: str = "default", cost: int = 1, now: float | None = None) -> RateLimitDecision:
        decision = self.check(key, rule=rule, cost=cost, now=now)
        if not decision.allowed:
            raise RateLimitExceeded(decision)
        return decision

    def reset(self, key: str | None = None, *, rule: str | None = None) -> None:
        with self._lock:
            if key is None and rule is None:
                self._events.clear()
                return
            for bucket_key in list(self._events):
                if (key is None or bucket_key[0] == key) and (rule is None or bucket_key[1] == rule):
                    del self._events[bucket_key]

    def remaining(self, key: str, *, rule: str = "default", now: float | None = None) -> int:
        """Return the current remaining request budget without consuming it."""

        selected = self._rule(rule)
        current = self._clock() if now is None else now
        bucket_key = (str(key), rule if isinstance(rule, str) else "custom")
        with self._lock:
            events = self._events.get(bucket_key, deque())
            cutoff = current - selected.window_seconds
            used = sum(1 for timestamp in events if timestamp > cutoff)
        return max(0, selected.requests - used)


def client_key(request: Request, *, user_id: str | None = None, trust_proxy_headers: bool = False) -> str:
    """Build a conservative limiter key; forwarded headers are opt-in."""

    if user_id:
        return f"user:{user_id}"
    if trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            forwarded_host = forwarded.split(",", 1)[0].strip()
            return f"ip:{forwarded_host}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def rate_limit_dependency(
    limiter: RateLimiter | None = None,
    *,
    rule: str = "default",
    user_id_getter: Callable[[Request], str | None] | None = None,
) -> Callable[[Request], RateLimitDecision]:
    """Return a FastAPI dependency that responds with HTTP 429 on overflow."""

    selected_limiter = limiter or RateLimiter(RateLimitPolicy.from_env())

    def dependency(request: Request) -> RateLimitDecision:
        user_id = user_id_getter(request) if user_id_getter else None
        key = client_key(request, user_id=user_id, trust_proxy_headers=selected_limiter.policy.trust_proxy_headers)
        decision = selected_limiter.check(key, rule=rule)
        if not decision.allowed:
            retry_after = str(max(1, int(decision.retry_after + 0.999)))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": retry_after},
            )
        return decision

    dependency.__name__ = f"rate_limit_{rule}"
    return dependency
