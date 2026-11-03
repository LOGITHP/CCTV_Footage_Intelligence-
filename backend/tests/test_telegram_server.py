from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest
from starlette.testclient import TestClient

import sys
from pathlib import Path
BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telegram_bot_server.server import app


@pytest.fixture
def client():
    return TestClient(app)


def test_telegram_server_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "vi-sentry-telegram-bot-server"


def test_alert_endpoint_requires_secret(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_SHARED_SECRET", "super-secret-123")
    
    # Missing header
    res = client.post("/api/alerts", json={"message": "intruder"})
    assert res.status_code == 403

    # Incorrect header
    res = client.post("/api/alerts", json={"message": "intruder"}, headers={"X-Telegram-Alert-Secret": "wrong"})
    assert res.status_code == 403


def test_alert_endpoint_sends_formatted_alert(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_SHARED_SECRET", "super-secret-123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-token-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")

    mock_client = MagicMock()
    mock_client.send_message.return_value = {"ok": True, "result": {"message_id": 999}}
    monkeypatch.setattr("telegram_bot_server.server._client", lambda: mock_client)

    payload = {
        "rule_name": "Perimeter Breach",
        "severity": "CRITICAL",
        "camera_name": "Gate Camera 1",
        "zone_id": "zone-gate",
        "details": "Person crossed restricted boundary",
        "evidence_url": "https://example.com/clip.mp4"
    }

    res = client.post(
        "/api/alerts",
        json=payload,
        headers={"X-Telegram-Alert-Secret": "super-secret-123"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "sent"
    assert data["chat_id"] == "123456789"
    assert data["telegram_message_id"] == 999

    mock_client.send_message.assert_called_once()
    args, kwargs = mock_client.send_message.call_args
    assert args[0] == "123456789"
    sent_text = args[1]
    assert "Perimeter Breach" in sent_text
    assert "CRITICAL" in sent_text
    assert "Gate Camera 1" in sent_text
    assert "zone-gate" in sent_text
    assert kwargs.get("parse_mode") == "HTML"


def test_broadcast_alert_endpoint(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_SHARED_SECRET", "super-secret-123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-token-123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")

    mock_client = MagicMock()
    monkeypatch.setattr("telegram_bot_server.server._client", lambda: mock_client)

    res = client.post(
        "/api/alerts/broadcast",
        json={"rule_name": "Facility Evac", "severity": "HIGH"},
        headers={"X-Telegram-Alert-Secret": "super-secret-123"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "broadcast_complete"
    assert data["delivered_count"] == 2
    assert "111" in data["delivered"]
    assert "222" in data["delivered"]
    assert mock_client.send_message.call_count == 2


def test_webhook_commands(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "webhook-secret-xyz")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock-token-123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")

    mock_client = MagicMock()
    monkeypatch.setattr("telegram_bot_server.server._client", lambda: mock_client)

    # 1. /start command
    res = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 42}, "text": "/start"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret-xyz"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "responded"
    assert res.json()["command"] == "/start"
    mock_client.send_message.assert_called_once()
    assert "VI SENTRY" in mock_client.send_message.call_args[0][1]

    # 2. Reject unauthorized chat
    res = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 999}, "text": "/status"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret-xyz"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
