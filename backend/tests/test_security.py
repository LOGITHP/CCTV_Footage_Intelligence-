from __future__ import annotations

from dataclasses import replace
import json

import numpy as np
import pytest

from video_intelligence.audit import AuditLogRepository, AuditLogService
from video_intelligence.auth import (
    AuthenticationService,
    ExpiredTokenError,
    InvalidCredentialsError,
    JWTManager,
    PasswordHasher,
    Role,
    TokenError,
)
from video_intelligence.privacy import PrivacyPolicy, redact_frame, redact_public_metadata
from video_intelligence.rate_limit import RateLimitPolicy, RateLimiter, RateLimitExceeded
from video_intelligence.rbac import Permission, AuthorizationError, authorize_permission, authorize_roles
from video_intelligence.security_config import SecuritySettings
from video_intelligence.signed_evidence import (
    EvidenceAccessDenied,
    EvidenceTokenError,
    EvidenceTokenSigner,
    build_signed_evidence_url,
    verify_signed_evidence_url,
)
from video_intelligence.security import JWTService, RateLimitConfig, SignedEvidenceToken


def _hasher() -> PasswordHasher:
    return PasswordHasher(iterations=100_000)


def test_security_package_exposes_stable_aliases():
    assert JWTService is JWTManager
    assert RateLimitConfig.__name__ == "RateLimitPolicy"
    assert SignedEvidenceToken.__name__ == "EvidenceTokenSigner"


def test_password_hashing_and_local_role_authentication_are_offline():
    hasher = _hasher()
    repository = __import__("video_intelligence.auth", fromlist=["InMemoryUserRepository"]).InMemoryUserRepository(
        hasher=hasher
    )
    jwt = JWTManager("s" * 32, ttl_seconds=60, clock=lambda: 1_000)
    auth = AuthenticationService(repository, jwt, hasher=hasher)

    principal = auth.register_user("operator", "correct horse battery staple", Role.OPERATOR)
    assert principal.role is Role.OPERATOR
    assert hasher.verify_password("correct horse battery staple", repository.get_by_username("operator").password_hash)
    assert not hasher.verify_password("wrong", repository.get_by_username("operator").password_hash)

    session = auth.login("operator", "correct horse battery staple")
    assert session.user.user_id == principal.user_id
    assert auth.principal_from_token(session.access_token).role is Role.OPERATOR
    with pytest.raises(InvalidCredentialsError):
        auth.login("operator", "wrong")
    repository.set_active(principal.user_id, False)
    with pytest.raises(InvalidCredentialsError):
        auth.login("operator", "correct horse battery staple")


def test_jwt_rejects_tampering_and_expiry():
    manager = JWTManager("j" * 32, ttl_seconds=10, clock=lambda: 100)
    token = manager.create_access_token("user-1", Role.REVIEWER, username="reviewer", now=100)
    claims = manager.decode_access_token(token, now=105)
    assert claims["sub"] == "user-1"
    assert claims["role"] == "reviewer"

    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}.{signature}"
    with pytest.raises(TokenError):
        manager.decode_access_token(tampered, now=105)
    with pytest.raises(ExpiredTokenError):
        manager.decode_access_token(token, now=111)
    with pytest.raises(TokenError):
        manager.decode_access_token(token, expected_subject="different", now=105)


def test_security_settings_require_secret_in_production_and_parse_policy():
    with pytest.raises(ValueError, match="JWT_SECRET"):
        SecuritySettings.from_env({"APP_ENV": "production"}, allow_ephemeral_secret=False)
    settings = SecuritySettings.from_env(
        {
            "APP_ENV": "production",
            "JWT_SECRET": "x" * 32,
            "JWT_TTL_SECONDS": "120",
            "SECURITY_POLICY": json.dumps(
                {"auth_required": True, "redact_public_derivatives": True, "evidence_retention_days": 14}
            ),
        },
        allow_ephemeral_secret=False,
    )
    assert settings.jwt.ttl_seconds == 120
    assert settings.policy.require_authentication is True
    assert settings.policy.evidence_retention_days == 14


def test_rbac_permission_and_role_helpers_fail_closed():
    reviewer = __import__("video_intelligence.auth", fromlist=["UserPrincipal"]).UserPrincipal(
        "u1", "reviewer", Role.REVIEWER
    )
    operator = replace(reviewer, role=Role.OPERATOR)
    authorize_roles(reviewer, Role.REVIEWER)
    with pytest.raises(AuthorizationError):
        authorize_roles(reviewer, Role.ADMIN)
    authorize_permission(operator, Permission.VIDEO_PROCESS)
    with pytest.raises(AuthorizationError):
        authorize_permission(reviewer, Permission.VIDEO_PROCESS)


def test_signed_evidence_grants_are_scoped_and_short_lived():
    clock = [1_000.0]
    signer = EvidenceTokenSigner("e" * 32, ttl_seconds=30, clock=lambda: clock[0])
    token = signer.sign("event-1", "videos/event-1/derivative.mp4", video_id="video-1", subject="u1")
    grant = signer.verify(token, event_id="event-1", evidence_key="videos/event-1/derivative.mp4", subject="u1")
    assert grant.visibility == "derivative"
    assert grant.video_id == "video-1"
    with pytest.raises(EvidenceTokenError):
        signer.verify(token, event_id="event-2")
    with pytest.raises(EvidenceTokenError):
        signer.verify(token, subject="u2")
    clock[0] = 1_031
    with pytest.raises(EvidenceTokenError):
        signer.verify(token)

    url = build_signed_evidence_url(
        "https://example.test/evidence/event-1?download=1",
        signer,
        "event-1",
        "videos/event-1/derivative.mp4",
        now=clock[0],
    )
    # The newly-issued token is independently verified from the query string.
    assert verify_signed_evidence_url(url, signer, event_id="event-1").event_id == "event-1"
    with pytest.raises(EvidenceAccessDenied):
        signer.sign("event-1", "videos/event-1/original.mp4", visibility="original")


def test_audit_repository_sanitizes_secrets_and_supports_query():
    repository = AuditLogRepository(":memory:")
    service = AuditLogService(repository)
    entry = service.record(
        "evidence.read",
        actor="u1",
        resource_type="event",
        resource_id="event-1",
        metadata={"token": "do-not-store", "nested": {"password": "also-do-not-store", "ok": True}},
    )
    assert entry.metadata == {"token": "[REDACTED]", "nested": {"password": "[REDACTED]", "ok": True}}
    stored = repository.get(entry.audit_id)
    assert stored is not None
    assert stored.metadata["token"] == "[REDACTED]"
    assert service.query(resource_type="event")[0].resource_id == "event-1"


def test_rate_limiter_is_configurable_and_key_scoped():
    now = [0.0]
    policy = RateLimitPolicy(default=__import__("video_intelligence.rate_limit", fromlist=["RateLimitRule"]).RateLimitRule(2, 10))
    limiter = RateLimiter(policy, clock=lambda: now[0])
    assert limiter.allow("ip:a")
    assert limiter.allow("ip:a")
    assert not limiter.allow("ip:a")
    assert limiter.allow("ip:b")
    with pytest.raises(RateLimitExceeded) as error:
        limiter.enforce("ip:a")
    assert error.value.retry_after == 10
    now[0] = 10.1
    assert limiter.allow("ip:a")


def test_public_derivative_redaction_fails_closed_and_removes_paths():
    policy = PrivacyPolicy()
    metadata = redact_public_metadata(
        {
            "event_id": "event-1",
            "media_path": "/srv/private/original.mp4",
            "evidence_path": "/srv/private/clip.mp4",
            "object_id": "track-1",
            "event_type": "enter",
        },
        policy=policy,
    )
    assert metadata["privacy_redacted"] is True
    assert "media_path" not in metadata
    assert "evidence_path" not in metadata
    assert "object_id" not in metadata

    frame = np.full((8, 8, 3), 255, dtype=np.uint8)
    fully_masked = redact_frame(frame, policy=policy)
    assert fully_masked.max() == 0
    partially_masked = redact_frame(frame, [(2, 2, 3, 3)], policy=PrivacyPolicy(redaction_method="solid"))
    assert partially_masked[3, 3].max() == 0
    assert partially_masked[0, 0].max() == 255
