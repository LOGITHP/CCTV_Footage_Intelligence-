"""Environment-driven security settings.

The security helpers deliberately have no dependency on the FastAPI app.  They
can therefore be used by workers, management commands, and the offline SQLite
test path without creating network clients or importing route modules.

Production deployments must provide ``JWT_SECRET`` (or the equivalent
``VIDEO_INTELLIGENCE_JWT_SECRET``).  Development and test processes get a
process-local random secret when no secret is supplied; that keeps an omitted
secret from becoming a committed or predictable credential while still making
the local path convenient.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import secrets
from typing import Mapping


class SecurityConfigurationError(ValueError):
    """Raised when security settings would be unsafe or ambiguous."""


# Development settings may be loaded more than once by the API, worker, and
# signed-evidence helpers. Keep the process-local fallback stable for the life
# of the process so a token issued from one settings snapshot remains verifiable
# by another snapshot, while production still requires an explicit secret.
_EPHEMERAL_JWT_SECRET = secrets.token_urlsafe(32)


def _first(environ: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SecurityConfigurationError(f"{name} must be a boolean value")


def _integer(
    environ: Mapping[str, str],
    names: tuple[str, ...],
    default: int,
    *,
    minimum: int = 0,
) -> int:
    value = _first(environ, *names)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise SecurityConfigurationError(f"{names[0]} must be an integer") from error
    if parsed < minimum:
        raise SecurityConfigurationError(f"{names[0]} must be at least {minimum}")
    return parsed


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Access and privacy policy shared by all security helpers.

    ``require_authentication`` follows the application environment: production
    and staging default to enabled, while an unset/development environment
    remains usable for the existing local test path.  Public derivatives are
    always redacted by default and originals remain restricted.
    """

    require_authentication: bool = False
    redact_public_derivatives: bool = True
    allow_original_evidence: bool = False
    evidence_retention_days: int = 30
    fail_closed_redaction: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SecurityPolicy":
        env = os.environ if environ is None else environ
        app_env = (_first(env, "APP_ENV") or "development").lower()
        require_auth = app_env in {"production", "prod", "staging"}
        redact = True
        allow_originals = False
        retention_days = 30
        fail_closed = True

        raw_policy = _first(env, "SECURITY_POLICY", "VIDEO_SECURITY_POLICY")
        if raw_policy:
            normalized = raw_policy.lower()
            if normalized in {"strict", "production", "prod"}:
                require_auth = True
            elif normalized in {"development", "dev", "local"}:
                require_auth = False
            elif normalized in {"permissive", "open"}:
                # This mode is explicitly opt-in and still preserves the safe
                # redaction default unless each relaxation is separately set.
                require_auth = False
            else:
                try:
                    parsed = json.loads(raw_policy)
                except json.JSONDecodeError as error:
                    raise SecurityConfigurationError(
                        "SECURITY_POLICY must be strict, development, or a JSON object"
                    ) from error
                if not isinstance(parsed, dict):
                    raise SecurityConfigurationError("SECURITY_POLICY JSON must be an object")
                require_auth = bool(
                    parsed.get("require_authentication", parsed.get("auth_required", require_auth))
                )
                redact = bool(
                    parsed.get("redact_public_derivatives", parsed.get("redaction_enabled", redact))
                )
                allow_originals = bool(
                    parsed.get("allow_original_evidence", parsed.get("allow_originals", allow_originals))
                )
                retention_days = int(parsed.get("evidence_retention_days", retention_days))
                fail_closed = bool(parsed.get("fail_closed_redaction", fail_closed))

        require_auth = _boolean(env, "AUTH_REQUIRED", require_auth)
        redact = _boolean(
            env,
            "PRIVACY_REDACTION_ENABLED",
            _boolean(env, "PUBLIC_DERIVATIVE_REDACTION", redact),
        )
        allow_originals = _boolean(env, "ALLOW_ORIGINAL_EVIDENCE", allow_originals)
        fail_closed = _boolean(env, "PRIVACY_FAIL_CLOSED", fail_closed)
        retention_days = _integer(
            env,
            ("EVIDENCE_RETENTION_DAYS", "PRIVACY_RETENTION_DAYS"),
            retention_days,
            minimum=1,
        )
        return cls(
            require_authentication=require_auth,
            redact_public_derivatives=redact,
            allow_original_evidence=allow_originals,
            evidence_retention_days=retention_days,
            fail_closed_redaction=fail_closed,
        )


@dataclass(frozen=True, slots=True)
class JWTSettings:
    """JWT signing configuration with a deliberately short default TTL."""

    secret: str
    ttl_seconds: int = 900
    issuer: str | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        allow_ephemeral: bool = True,
    ) -> "JWTSettings":
        env = os.environ if environ is None else environ
        secret = _first(env, "JWT_SECRET", "VIDEO_INTELLIGENCE_JWT_SECRET", "AUTH_JWT_SECRET")
        app_env = (_first(env, "APP_ENV") or "development").lower()
        if not secret:
            if not allow_ephemeral or app_env in {"production", "prod", "staging"}:
                raise SecurityConfigurationError(
                    "JWT_SECRET must be supplied in production/staging environments"
                )
            # A process-local value is safe for offline development and cannot
            # accidentally become a shared credential through source control.
            secret = _EPHEMERAL_JWT_SECRET
        if len(secret.encode("utf-8")) < 32:
            raise SecurityConfigurationError("JWT_SECRET must contain at least 32 bytes")
        ttl_seconds = _integer(
            env,
            ("JWT_TOKEN_TTL_SECONDS", "JWT_TTL_SECONDS", "JWT_EXPIRY_SECONDS"),
            900,
            minimum=1,
        )
        issuer = _first(env, "JWT_ISSUER")
        return cls(secret=secret, ttl_seconds=ttl_seconds, issuer=issuer)


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    """Complete settings snapshot for local auth and evidence controls."""

    jwt: JWTSettings
    policy: SecurityPolicy

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        allow_ephemeral_secret: bool = True,
    ) -> "SecuritySettings":
        env = os.environ if environ is None else environ
        return cls(
            jwt=JWTSettings.from_env(env, allow_ephemeral=allow_ephemeral_secret),
            policy=SecurityPolicy.from_env(env),
        )


def load_security_settings(*, allow_ephemeral_secret: bool = True) -> SecuritySettings:
    """Load a fresh settings snapshot from process environment."""

    return SecuritySettings.from_env(allow_ephemeral_secret=allow_ephemeral_secret)
