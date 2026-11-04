from __future__ import annotations

import json

from video_intelligence.infrastructure.config import InfrastructureSettings
from video_intelligence.operations.notifications import NotificationMessage, TelegramNotifier
from video_intelligence.operations.telegram import TelegramBotService


def test_telegram_notifier_delivers_alert_without_network():
    calls = []

    def sender(url, body, headers, timeout):
        calls.append((url, json.loads(body.decode()), headers, timeout))
        return 200, b'{"ok": true}'

    notifier = TelegramNotifier("token-for-test", "chat-42", sender=sender)
    result = notifier.send(
        NotificationMessage(
            notification_id="alert-1:telegram",
            channel="telegram",
            subject="Alert",
            body="Unusual activity detected.",
        )
    )

    assert result.status == "delivered"
    assert result.detail == "chat-42"
    assert calls[0][1] == {"chat_id": "chat-42", "text": "Unusual activity detected."}
    assert "/sendMessage" in calls[0][0]


def test_telegram_bot_service_answers_authorized_live_question():
    class Store:
        def __init__(self):
            self.messages = []

        def list_cameras(self):
            return [{"camera_id": "front", "display_name": "Front", "enabled": True, "zones": []}]

        def list_alerts(self):
            return []

        def list_alert_rules(self):
            return []

        def add_chat_message(self, *args):
            self.messages.append(args)

    class Client:
        def __init__(self):
            self.sent = []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))

    settings = InfrastructureSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "token-for-test",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_WEBHOOK_SECRET": "secret-for-test",
        }
    ).notifications
    client = Client()
    service = TelegramBotService(Store(), settings, client=client)

    result = service.handle_update(
        {"message": {"message_id": 7, "chat": {"id": 42}, "text": "Are any cameras online?"}}
    )

    assert result["status"] == "responded"
    assert client.sent and client.sent[0][0] == "42"
    assert "Live Surveillance Fleet Overview" in client.sent[0][1]


def test_telegram_bot_service_rejects_unauthorized_chat():
    settings = InfrastructureSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "token-for-test",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_WEBHOOK_SECRET": "secret-for-test",
        }
    ).notifications
    service = TelegramBotService(object(), settings, client=object())

    result = service.handle_update(
        {"message": {"message_id": 8, "chat": {"id": 99}, "text": "show alerts"}}
    )

    assert result["status"] == "ignored"
    assert result["reason"] == "chat is not authorized"


def test_telegram_settings_keep_token_out_of_repr():
    settings = InfrastructureSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "secret-token",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42,43",
        }
    )
    assert settings.notifications.telegram_allowed_chat_ids == ("42", "43")
    assert "secret-token" not in repr(settings)


def test_telegram_webhook_requires_secret_and_dispatches_update(monkeypatch):
    import video_intelligence.api as api_module
    from fastapi.testclient import TestClient

    settings = InfrastructureSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "token-for-test",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_WEBHOOK_SECRET": "secret-for-test",
        }
    )
    monkeypatch.setattr(api_module, "load_settings", lambda: settings)

    class Service:
        def __init__(self, _store, _settings):
            pass

        def handle_update(self, update):
            return {"status": "responded", "update_id": update.get("update_id")}

    monkeypatch.setattr(api_module, "TelegramBotService", Service)
    client = TestClient(api_module.app)
    body = {"update_id": 9, "message": {"chat": {"id": 42}, "text": "status"}}

    assert client.post("/api/v1/integrations/telegram/webhook", json=body).status_code == 403
    response = client.post(
        "/api/v1/integrations/telegram/webhook",
        json=body,
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-for-test"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "responded", "update_id": 9}


def test_telegram_bot_client_send_photo():
    from video_intelligence.operations.telegram import TelegramBotClient

    calls = []

    def mock_request(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 200, b'{"ok": true, "result": {"message_id": 123}}'

    client = TelegramBotClient("test-bot-token", request=mock_request)

    # 1. Send photo via URL
    res_url = client.send_photo("42", "https://example.com/snapshot.jpg", caption="Live Evidence")
    assert res_url["ok"] is True
    assert "/sendPhoto" in calls[0][0]
    assert calls[0][2]["Content-Type"] == "application/json"

    # 2. Send photo via Bytes
    test_jpeg_bytes = b"\xff\xd8\xff\xe0testjpegdata\xff\xd9"
    res_bytes = client.send_photo("42", test_jpeg_bytes, caption="Frame Evidence")
    assert res_bytes["ok"] is True
    assert "/sendPhoto" in calls[1][0]
    assert "multipart/form-data" in calls[1][2]["Content-Type"]
    assert b"testjpegdata" in calls[1][1]
