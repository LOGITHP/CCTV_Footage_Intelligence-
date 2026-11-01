from __future__ import annotations

from fastapi.testclient import TestClient

import video_intelligence.api as api_module
from video_intelligence.audit import AuditLogRepository, AuditLogService
from video_intelligence.auth import AuthenticationService, InMemoryUserRepository, JWTManager, PasswordHasher
from video_intelligence.rate_limit import RateLimitPolicy, RateLimitRule, RateLimiter
from video_intelligence.security_config import JWTSettings, SecurityPolicy, SecuritySettings
from video_intelligence.storage import Store


def _configure_required_auth(monkeypatch, *, tmp_path):
    hasher = PasswordHasher(iterations=100_000)
    users = InMemoryUserRepository(hasher=hasher)
    secret = "s" * 32
    jwt = JWTManager(secret, ttl_seconds=600)
    monkeypatch.setattr(api_module, "user_repository", users)
    monkeypatch.setattr(api_module, "authentication_service", AuthenticationService(users, jwt, hasher=hasher))
    monkeypatch.setattr(
        api_module,
        "security_settings",
        SecuritySettings(
            jwt=JWTSettings(secret=secret, ttl_seconds=600),
            policy=SecurityPolicy(require_authentication=True),
        ),
    )
    monkeypatch.setattr(api_module, "audit_service", AuditLogService(AuditLogRepository(":memory:")))
    monkeypatch.setattr(
        api_module,
        "rate_limiter",
        RateLimiter(RateLimitPolicy(default=RateLimitRule(requests=1000, window_seconds=60))),
    )
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    monkeypatch.setattr(api_module, "media_root", tmp_path)


def test_required_auth_bootstrap_login_and_rbac(monkeypatch, tmp_path):
    _configure_required_auth(monkeypatch, tmp_path=tmp_path)
    client = TestClient(api_module.app)

    assert client.get("/api/v1/auth/me").status_code == 401

    register_admin = client.post(
        "/api/v1/auth/register",
        json={"username": "admin", "password": "correct horse battery staple", "role": "admin"},
    )
    assert register_admin.status_code == 201

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["role"] == "admin"

    register_reviewer = client.post(
        "/api/v1/auth/register",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"username": "reviewer", "password": "another correct password", "role": "reviewer"},
    )
    assert register_reviewer.status_code == 201
    reviewer_login = client.post(
        "/api/v1/auth/login",
        json={"username": "reviewer", "password": "another correct password"},
    )
    reviewer_token = reviewer_login.json()["access_token"]

    denied = client.post(
        "/api/v1/videos",
        headers={"Authorization": f"Bearer {reviewer_token}"},
        files={"file": ("fixture.mp4", b"data", "video/mp4")},
    )
    assert denied.status_code == 403

    accepted = client.post(
        "/api/v1/videos",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("fixture.mp4", b"data", "video/mp4")},
    )
    assert accepted.status_code == 201


def test_signed_evidence_url_downloads_only_the_persisted_derivative(monkeypatch, tmp_path):
    _configure_required_auth(monkeypatch, tmp_path=tmp_path)
    client = TestClient(api_module.app)

    register = client.post(
        "/api/v1/auth/register",
        json={"username": "admin", "password": "correct horse battery staple", "role": "admin"},
    )
    assert register.status_code == 201
    token = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    upload = client.post("/api/v1/videos", headers=headers, files={"file": ("fixture.mp4", b"data", "video/mp4")})
    video_id = upload.json()["video_id"]
    process = client.post(
        f"/api/v1/videos/{video_id}/process",
        headers=headers,
        json={
            "zones": [{"zone_id": "gate", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
            "observations": [
                {"timestamp": 0, "objects": [{"object_id": "track-1", "x": 20, "y": 5}]},
                {"timestamp": 1, "objects": [{"object_id": "track-1", "x": 5, "y": 5}]},
            ],
        },
    )
    assert process.status_code == 202
    event_id = process.json()["events"][0]["event_id"]

    evidence = tmp_path / "evidence" / "event-1.mp4"
    evidence.parent.mkdir()
    evidence.write_bytes(b"private derivative bytes")
    api_module.store.update_evidence(event_id, str(evidence))

    signed = client.get(f"/api/v1/evidence/{event_id}/url", headers=headers)
    assert signed.status_code == 200
    download = client.get(signed.json()["url"])
    assert download.status_code == 200
    assert download.content == b"private derivative bytes"

    tampered = signed.json()["url"].replace("token=", "token=x", 1)
    assert client.get(tampered).status_code == 403
