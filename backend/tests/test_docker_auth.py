import os
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

import video_intelligence.api as api_module
from video_intelligence.storage import Store
from video_intelligence.auth import SQLiteUserRepository, AuthenticationService, JWTManager, Role
from video_intelligence.security_config import load_security_settings


def test_jwt_auth_enforcement_and_media_volume(monkeypatch, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    auth_db = tmp_path / "auth.sqlite3"

    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "super_secret_jwt_key_at_least_32_bytes_long!")
    monkeypatch.setenv("MEDIA_ROOT", str(media_dir))
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(auth_db))

    # Reload security settings with auth required
    sec_settings = load_security_settings()
    user_repo = SQLiteUserRepository(str(auth_db))
    jwt_mgr = JWTManager.from_settings(sec_settings.jwt)
    auth_service = AuthenticationService(user_repo, jwt_mgr)

    monkeypatch.setattr(api_module, "security_settings", sec_settings)
    monkeypatch.setattr(api_module, "user_repository", user_repo)
    monkeypatch.setattr(api_module, "authentication_service", auth_service)
    monkeypatch.setattr(api_module, "store", Store(":memory:"))
    monkeypatch.setattr(api_module, "media_root", media_dir)

    client = TestClient(api_module.app)

    # 1. Unauthenticated request to /api/v1/auth/me must fail with 401
    me_res = client.get("/api/v1/auth/me")
    assert me_res.status_code == 401
    assert "authentication required" in me_res.json()["detail"].lower()

    # 2. Register first admin user
    reg_res = client.post(
        "/api/v1/auth/register",
        json={"username": "meta_admin", "password": "securepassword123", "role": "admin"},
    )
    assert reg_res.status_code == 201
    assert reg_res.json()["username"] == "meta_admin"

    # 3. Log in to acquire JWT access token
    login_res = client.post(
        "/api/v1/auth/login",
        json={"username": "meta_admin", "password": "securepassword123"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    assert token and len(token.split(".")) == 3

    # 4. Authenticated request with Bearer token succeeds
    auth_headers = {"Authorization": f"Bearer {token}"}
    me_auth_res = client.get("/api/v1/auth/me", headers=auth_headers)
    assert me_auth_res.status_code == 200
    assert me_auth_res.json()["username"] == "meta_admin"

    # 5. Upload actual video file into the configured media volume
    upload_res = client.post(
        "/api/v1/videos",
        files={"file": ("cctv_recording.mp4", b"synthetic_video_payload_bytes", "video/mp4")},
        headers=auth_headers,
    )
    assert upload_res.status_code == 201
    video_id = upload_res.json()["video_id"]

    # Verify actual video file is persisted on disk in the media volume path
    stored_files = list(media_dir.glob(f"{video_id}-*"))
    assert len(stored_files) == 1
    assert stored_files[0].read_bytes() == b"synthetic_video_payload_bytes"
