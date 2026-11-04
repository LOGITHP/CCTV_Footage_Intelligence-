"""Standalone VI Sentry Telegram Alert & Bot Server.

This production edge microservice manages:
1. Fast, secured outbound security alert dispatching to authorized Telegram chats (/api/alerts).
2. Broadcast alert dispatching to full security officer fleets (/api/alerts/broadcast).
3. Inbound operator webhook processing (/telegram/webhook) with autonomous commands
   (/start, /status, /cameras, /alerts) and LangGraph / Muse Spark 1.3 AI Copilot grounding.
4. Edge health and readiness probes (/health, /health/ready).
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("telegram_bot_server")

BACKEND_SRC = Path(__file__).resolve().parents[1] / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from video_intelligence.operations.telegram import TelegramBotClient  # noqa: E402


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _allowed_chat_ids() -> tuple[str, ...]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        default_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        return (default_chat,) if default_chat else ()
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _client() -> TelegramBotClient:
    return TelegramBotClient(
        _required("TELEGRAM_BOT_TOKEN"),
        api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
        timeout_seconds=float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10")),
    )


def _authorized(chat_id: str) -> bool:
    allowed = _allowed_chat_ids()
    return not allowed or chat_id in allowed


def _query_backend(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    backend_url = os.getenv("VIDEO_INTELLIGENCE_API_URL", "http://video-intelligence:8000").rstrip("/")
    headers = {"Content-Type": "application/json"}
    token = os.getenv("VIDEO_INTELLIGENCE_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(f"{backend_url}{path}", data=data, headers=headers, method=method)
    timeout = float(os.getenv("VIDEO_INTELLIGENCE_API_TIMEOUT_SECONDS", "30"))
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:
        logger.warning("Backend query to %s failed: %s", path, error)
        raise RuntimeError(f"Main Video Intelligence API error ({path}): {error}") from error


def _ask_backend(question: str) -> str:
    try:
        body = _query_backend("/api/v1/chat", method="POST", payload={"message": question, "agent_mode": "auto"})
        answer = body.get("answer") if isinstance(body, Mapping) else None
        return str(answer or "No response returned by the Video Intelligence API.")
    except Exception as err:
        return f"⚠️ <b>AI Sentry Copilot Unavailable:</b> {html.escape(str(err))}"


def _fetch_backend_snapshot(path: str = "/api/v1/live/snapshot") -> bytes | None:
    backend_url = os.getenv("VIDEO_INTELLIGENCE_API_URL", "http://video-intelligence:8000").rstrip("/")
    headers = {}
    token = os.getenv("VIDEO_INTELLIGENCE_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{backend_url}{path}", headers=headers, method="GET")
    try:
        with urlopen(request, timeout=10.0) as response:  # noqa: S310
            return response.read()
    except Exception as exc:
        logger.warning("Failed to fetch snapshot from %s: %s", path, exc)
        return None


def _format_alert(body: Mapping[str, Any]) -> str:
    """Format structured alert into high-priority Telegram HTML dispatch."""
    rule = body.get("rule_name") or body.get("rule_id")
    # If the payload is just a simple message string without alert metadata
    if not rule and "message" in body and not any(k in body for k in ("severity", "camera_id", "zone_id")):
        return str(body["message"])

    severity = str(body.get("severity", "WARNING")).upper()
    icon = "🚨" if severity in {"CRITICAL", "HIGH"} else ("⚠️" if severity == "WARNING" else "ℹ️")
    rule_name = html.escape(str(rule or "Perimeter Rule Infraction"))
    camera = html.escape(str(body.get("camera_name") or body.get("camera_id") or "Primary Sentry"))
    zone = html.escape(str(body.get("zone_id") or "Sector Alpha"))
    timestamp = html.escape(str(body.get("timestamp") or body.get("created_at") or "Real-Time Ingress"))
    details = html.escape(str(body.get("details") or body.get("message") or body.get("body") or ""))
    evidence_url = str(body.get("evidence_url") or body.get("evidence_path") or "").strip()

    lines = [
        f"{icon} <b>VI SENTRY · SECURITY ALERT DISPATCH</b>",
        f"<b>Severity:</b> <code>{severity}</code>",
        f"<b>Incident:</b> <b>{rule_name}</b>",
        f"<b>Camera:</b> <code>{camera}</code> | <b>Zone:</b> <code>{zone}</code>",
        f"<b>Timecode:</b> {timestamp}",
    ]
    if details:
        lines.append(f"<b>Telemetry:</b> {details}")
    if evidence_url:
        lines.append(f"<b>Evidence Clip:</b> <a href=\"{html.escape(evidence_url)}\">Review Video Recording</a>")

    lines.append("\n<i>Automated alert generated by VI Sentry Autonomous Dispatch Core</i>")
    return "\n".join(lines)


app = FastAPI(title="VI Sentry Telegram Alert & Bot Server", version="2.0.0")


@app.get("/health")
@app.get("/health/ready")
def health() -> dict[str, Any]:
    bot_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    webhook_secret_set = bool(os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip())
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    allowed_chats = _allowed_chat_ids()
    backend_url = os.getenv("VIDEO_INTELLIGENCE_API_URL", "http://video-intelligence:8000")
    
    return {
        "status": "ok",
        "service": "vi-sentry-telegram-bot-server",
        "bot_configured": bot_configured,
        "webhook_secret_configured": webhook_secret_set,
        "default_chat_id": chat_id,
        "allowed_chat_ids_count": len(allowed_chats),
        "backend_url": backend_url,
    }


@app.post("/telegram/webhook")
async def webhook(request: FastAPIRequest) -> dict[str, Any]:
    secret = _required("TELEGRAM_WEBHOOK_SECRET")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, supplied):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("message") if isinstance(update, Mapping) else None
    chat = message.get("chat") if isinstance(message, Mapping) else None
    raw_text = message.get("text") if isinstance(message, Mapping) else None
    chat_id = str(chat.get("id", "")).strip() if isinstance(chat, Mapping) else ""

    if not chat_id or raw_text is None:
        return {"status": "ignored", "reason": "No text message in update"}

    if not _authorized(chat_id):
        logger.warning("Rejected message from unauthorized Telegram chat ID: %s", chat_id)
        return {"status": "ignored", "reason": "Chat is not authorized"}

    text = str(raw_text).strip()
    client = _client()

    # Built-in Tactical Commands
    if text in {"/start", "/help"}:
        welcome = (
            "🛡️ <b>VI SENTRY · COMMAND CORE</b>\n\n"
            "Autonomous CCTV & AI Video Intelligence Bot.\n\n"
            "<b>Available Commands:</b>\n"
            "• <code>/status</code> — System health & backend connectivity\n"
            "• <code>/cameras</code> — Fleet camera streams & status\n"
            "• <code>/alerts</code> — Recent perimeter security alerts\n"
            "• <code>/help</code> — Show this commands menu\n\n"
            "💬 <i>You can also ask free-form surveillance questions directly (e.g. 'Any intrusion at loading bay?') to query the AI Sentry Copilot.</i>"
        )
        client.send_message(chat_id, welcome, parse_mode="HTML")
        return {"status": "responded", "command": text, "chat_id": chat_id}

    if text == "/status":
        try:
            status_data = _query_backend("/health/ready")
            store_name = status_data.get("store", "Active")
            reply = (
                "✅ <b>VI Sentry System Status:</b>\n"
                f"• <b>VMS Daemon:</b> <code>ONLINE</code>\n"
                f"• <b>Database Store:</b> <code>{html.escape(store_name)}</code>\n"
                "• <b>AI Models:</b> Meta SAM 3.1 & Muse Spark 1.3 Active"
            )
        except Exception as e:
            reply = f"⚠️ <b>Backend Health Warning:</b> {html.escape(str(e))}"
        client.send_message(chat_id, reply, parse_mode="HTML")
        return {"status": "responded", "command": text, "chat_id": chat_id}

    if text == "/cameras":
        try:
            cams = _query_backend("/api/v1/cameras")
            cam_list = cams.get("cameras", []) if isinstance(cams, Mapping) else cams
            if not cam_list:
                reply = "📹 <b>Camera Fleet:</b> No cameras currently registered."
            else:
                lines = [f"📹 <b>Registered Cameras ({len(cam_list)}):</b>"]
                for c in cam_list:
                    name = html.escape(c.get("display_name") or c.get("camera_id", "Unnamed"))
                    state = "🟢 Enabled" if c.get("enabled", True) else "🔴 Disabled"
                    lines.append(f"• <b>{name}</b> — {state}")
                reply = "\n".join(lines)
        except Exception as e:
            reply = f"⚠️ <b>Cannot fetch camera list:</b> {html.escape(str(e))}"
        client.send_message(chat_id, reply, parse_mode="HTML")
        return {"status": "responded", "command": text, "chat_id": chat_id}

    if text == "/alerts":
        try:
            alerts_data = _query_backend("/api/v1/alerts")
            alerts = alerts_data.get("alerts", []) if isinstance(alerts_data, Mapping) else alerts_data
            if not alerts:
                reply = "🛡️ <b>Recent Alerts:</b> No active perimeter intrusions or infractions recorded."
            else:
                lines = [f"🚨 <b>Recent Alerts ({min(5, len(alerts))}):</b>"]
                for a in alerts[:5]:
                    r = html.escape(str(a.get("rule_name") or a.get("rule_id") or "Alert"))
                    sev = html.escape(str(a.get("severity", "WARNING")).upper())
                    ts = html.escape(str(a.get("created_at") or a.get("timestamp") or ""))
                    lines.append(f"• [{sev}] <b>{r}</b> {ts}")
                reply = "\n".join(lines)
        except Exception as e:
            reply = f"⚠️ <b>Cannot fetch alerts:</b> {html.escape(str(e))}"
        client.send_message(chat_id, reply, parse_mode="HTML")
        return {"status": "responded", "command": text, "chat_id": chat_id}

    # Free-form question: grounded reasoning through main Copilot with evidence footage snapshot
    answer = _ask_backend(text)
    snapshot_bytes = _fetch_backend_snapshot("/api/v1/live/snapshot")
    if snapshot_bytes:
        photo_caption = answer[:1000] if len(answer) > 1000 else answer
        sent = False
        try:
            client.send_photo(chat_id, snapshot_bytes, caption=photo_caption, parse_mode="HTML")
            sent = True
        except Exception:
            try:
                # Retry without strict HTML parsing in case answer contained unescaped characters
                client.send_photo(chat_id, snapshot_bytes, caption=photo_caption, parse_mode=None)
                sent = True
            except Exception as photo_err:
                logger.warning("send_photo failed, falling back to message: %s", photo_err)

        if sent:
            if len(answer) > 1000:
                remainder = answer[1000:]
                try:
                    client.send_message(chat_id, remainder, parse_mode="HTML")
                except Exception:
                    client.send_message(chat_id, remainder)
            return {"status": "responded_with_snapshot", "chat_id": chat_id}

    try:
        client.send_message(chat_id, answer, parse_mode="HTML")
    except Exception:
        client.send_message(chat_id, answer)
    return {"status": "responded", "chat_id": chat_id}


@app.post("/api/alerts")
async def alert(request: FastAPIRequest) -> dict[str, Any]:
    expected = os.getenv("TELEGRAM_ALERT_SHARED_SECRET", "").strip()
    if expected:
        supplied = request.headers.get("X-Telegram-Alert-Secret", "")
        auth_header = request.headers.get("Authorization", "")
        bearer_secret = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        if not (hmac.compare_digest(expected, supplied) or hmac.compare_digest(expected, bearer_secret)):
            raise HTTPException(status_code=403, detail="Invalid Telegram alert shared secret")

    body = await request.json()
    if not isinstance(body, Mapping):
        raise HTTPException(status_code=400, detail="Invalid JSON body; mapping expected")

    formatted_text = _format_alert(body)
    target_chat_id = str(body.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    if not target_chat_id:
        raise HTTPException(
            status_code=503,
            detail="TELEGRAM_CHAT_ID is not configured and no chat_id was provided in the alert payload",
        )

    client = _client()
    try:
        res = client.send_message(target_chat_id, formatted_text, parse_mode="HTML")
        logger.info("Alert dispatched to Telegram chat %s", target_chat_id)
        return {"status": "sent", "chat_id": target_chat_id, "telegram_message_id": res.get("result", {}).get("message_id")}
    except Exception as exc:
        logger.error("Failed to send alert to Telegram chat %s: %s", target_chat_id, exc)
        raise HTTPException(status_code=502, detail=f"Telegram delivery failed: {exc}") from exc


@app.post("/api/alerts/broadcast")
async def broadcast_alert(request: FastAPIRequest) -> dict[str, Any]:
    expected = os.getenv("TELEGRAM_ALERT_SHARED_SECRET", "").strip()
    if expected:
        supplied = request.headers.get("X-Telegram-Alert-Secret", "")
        auth_header = request.headers.get("Authorization", "")
        bearer_secret = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        if not (hmac.compare_digest(expected, supplied) or hmac.compare_digest(expected, bearer_secret)):
            raise HTTPException(status_code=403, detail="Invalid Telegram alert shared secret")

    body = await request.json()
    if not isinstance(body, Mapping):
        raise HTTPException(status_code=400, detail="Invalid JSON body; mapping expected")

    formatted_text = _format_alert(body)
    recipients = _allowed_chat_ids()

    if not recipients:
        raise HTTPException(status_code=503, detail="No authorized recipient chat IDs configured for broadcast")

    client = _client()
    delivered = []
    failed = []

    for chat_id in recipients:
        try:
            client.send_message(chat_id, formatted_text, parse_mode="HTML")
            delivered.append(chat_id)
        except Exception as exc:
            logger.warning("Failed broadcast to %s: %s", chat_id, exc)
            failed.append({"chat_id": chat_id, "error": str(exc)})

    return {
        "status": "broadcast_complete",
        "delivered_count": len(delivered),
        "failed_count": len(failed),
        "delivered": delivered,
        "failed": failed,
    }
