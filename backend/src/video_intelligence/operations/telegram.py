"""Telegram webhook integration for alerts and grounded operator questions."""

from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from ..chat import answer_live_query, answer_query
from ..infrastructure.config import NotificationSettings


TelegramRequest = Any


def _default_request(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is explicit configuration
        return int(response.status), response.read()


class TelegramBotClient:
    """Small Bot API client with injectable transport for offline tests."""

    def __init__(
        self,
        bot_token: str,
        *,
        api_base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 10.0,
        request: TelegramRequest | None = None,
    ) -> None:
        cleaned_token = re.sub(r"\s+", "", str(bot_token))
        if not cleaned_token:
            raise ValueError("Telegram bot token is required")
        parsed = urlparse(api_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Telegram API base URL must be an absolute http(s) URL")
        self._token = cleaned_token
        self._base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._request = request or _default_request

    def _call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = f"{self._base_url}/bot{self._token}/{method}"
        status, raw = self._request(
            endpoint,
            json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json"},
            self._timeout,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"Telegram API returned HTTP {status}")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Telegram API returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            detail = response.get("description", "Telegram API rejected the request") if isinstance(response, dict) else "Telegram API rejected the request"
            raise RuntimeError(str(detail))
        return response

    def send_message(self, chat_id: str, text: str, *, parse_mode: str | None = None) -> dict[str, Any]:
        if not str(chat_id).strip():
            raise ValueError("Telegram chat ID is required")
        if not text.strip():
            text = " "
        results = []
        for start in range(0, len(text), 4096):
            payload = {"chat_id": str(chat_id), "text": text[start : start + 4096]}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            results.append(self._call("sendMessage", payload))
        return results[-1] if results else self._call("sendMessage", {"chat_id": str(chat_id), "text": " "})

    def send_photo(
        self,
        chat_id: str,
        photo: bytes | str | Any,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send a visual evidence snapshot to a Telegram chat."""
        if not str(chat_id).strip():
            raise ValueError("Telegram chat ID is required")

        caption_str = str(caption or "").strip()
        first_cap = caption_str[:1024] if caption_str else None
        remainder = caption_str[1024:].strip() if len(caption_str) > 1024 else ""

        # Case 1: Photo is an absolute URL
        if isinstance(photo, str) and (photo.startswith("http://") or photo.startswith("https://")):
            payload: dict[str, Any] = {"chat_id": str(chat_id), "photo": photo}
            if first_cap:
                payload["caption"] = first_cap
            if parse_mode:
                payload["parse_mode"] = parse_mode
            res = self._call("sendPhoto", payload)
            if remainder:
                self.send_message(chat_id, remainder, parse_mode=parse_mode)
            return res

        # Case 2: Photo is bytes or file-like object
        photo_bytes: bytes
        filename = "snapshot.jpg"
        if isinstance(photo, (str, bytes, bytearray)):
            if isinstance(photo, str):
                from pathlib import Path
                p = Path(photo)
                if p.is_file():
                    photo_bytes = p.read_bytes()
                    filename = p.name
                else:
                    raise FileNotFoundError(f"Snapshot image file not found: {photo}")
            else:
                photo_bytes = bytes(photo)
        elif hasattr(photo, "read"):
            photo_bytes = photo.read()
        else:
            raise ValueError("Photo must be URL string, bytes, or file path")

        boundary = f"----TelegramBoundary{uuid4().hex}"
        body = bytearray()

        def add_field(name: str, val: str) -> None:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(val.encode("utf-8"))
            body.extend(b"\r\n")

        add_field("chat_id", str(chat_id))
        if first_cap:
            add_field("caption", first_cap)
        if parse_mode:
            add_field("parse_mode", parse_mode)

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(b"Content-Type: image/jpeg\r\n\r\n")
        body.extend(photo_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        endpoint = f"{self._base_url}/bot{self._token}/sendPhoto"
        status_code, raw = self._request(
            endpoint,
            bytes(body),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            self._timeout,
        )
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"Telegram sendPhoto API returned HTTP {status_code}")
        try:
            response = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Telegram API returned invalid JSON on sendPhoto") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            detail = response.get("description", "sendPhoto rejected") if isinstance(response, dict) else "sendPhoto rejected"
            raise RuntimeError(str(detail))

        if remainder:
            self.send_message(chat_id, remainder, parse_mode=parse_mode)
        return response

    def set_webhook(self, url: str, *, secret_token: str | None = None) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Telegram webhook URL must be an absolute HTTPS URL")
        payload: dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._call("setWebhook", payload)


class TelegramBotService:
    """Authenticate Telegram updates and answer grounded operator questions."""

    def __init__(
        self,
        store: Any,
        settings: NotificationSettings,
        *,
        client: TelegramBotClient | None = None,
    ) -> None:
        if not settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        self.store = store
        self.settings = settings
        self.client = client or TelegramBotClient(
            settings.telegram_bot_token,
            api_base_url=settings.telegram_api_base_url,
            timeout_seconds=settings.webhook_timeout_seconds,
        )

    @property
    def allowed_chat_ids(self) -> tuple[str, ...]:
        configured = tuple(str(value).strip() for value in self.settings.telegram_allowed_chat_ids if str(value).strip())
        if configured:
            return configured
        if self.settings.telegram_chat_id:
            return (self.settings.telegram_chat_id.strip(),)
        return ()

    def verify_secret(self, supplied_secret: str | None) -> bool:
        expected = self.settings.telegram_webhook_secret
        return bool(expected and supplied_secret and hmac.compare_digest(expected, supplied_secret))

    def _authorized(self, chat_id: str) -> bool:
        allowed = self.allowed_chat_ids
        return not allowed or chat_id in allowed

    @staticmethod
    def _message(update: Mapping[str, Any]) -> tuple[str, str, str | None] | None:
        raw_message = update.get("message") or update.get("edited_message")
        if not isinstance(raw_message, Mapping):
            return None
        chat = raw_message.get("chat")
        text = raw_message.get("text")
        if not isinstance(chat, Mapping) or text is None:
            return None
        chat_id = str(chat.get("id", "")).strip()
        message_id = str(raw_message.get("message_id", uuid4())).strip()
        if not chat_id:
            return None
        return chat_id, str(text).strip(), message_id

    @staticmethod
    def _live_question(text: str) -> bool:
        return bool(
            re.search(
                r"\b(live|camera|stream|fleet|alert|breach|unusual|anomal|tripwire|online|offline)\b",
                text,
                re.IGNORECASE,
            )
        )

    def _answer(self, text: str) -> dict[str, Any]:
        if text.lower() in {"/start", "/help"}:
            return {
                "conversation_id": "telegram-help",
                "answer": (
                    "VI Sentry Telegram assistant. Ask about camera status, alerts, unusual activity, "
                    "or the latest recording. Responses are grounded in stored video evidence."
                ),
                "citations": [],
                "matches": [],
                "answer_mode": "telegram-help",
            }
        if self._live_question(text):
            return answer_live_query(self.store, text)
        videos = self.store.list_videos() if hasattr(self.store, "list_videos") else []
        latest_video_id = videos[-1].get("video_id") if videos else None
        if latest_video_id:
            return answer_query(self.store, text, video_id=str(latest_video_id))
        return answer_live_query(self.store, text)

    def _fetch_snapshot(self, citations: list[Any] | None = None) -> bytes | None:
        """Retrieve evidence snapshot from citations or latest surveillance footage."""
        try:
            if citations:
                for c in citations:
                    if isinstance(c, dict):
                        artifact = c.get("artifact") or c.get("evidence_path")
                        if artifact:
                            from pathlib import Path
                            p = Path(artifact)
                            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                                return p.read_bytes()
            import cv2
            from pathlib import Path
            media_root = Path(os.getenv("MEDIA_ROOT", "data/media"))
            for sub in ["live_streams", "videos", "."]:
                folder = media_root / sub if sub != "." else media_root
                if folder.is_dir():
                    candidates = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                    for cand in candidates:
                        cap = cv2.VideoCapture(str(cand))
                        if cap.isOpened():
                            ret, frame = cap.read()
                            cap.release()
                            if ret and frame is not None:
                                _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                                return buf.tobytes()
        except Exception:
            pass
        return None

    def handle_update(self, update: Mapping[str, Any]) -> dict[str, Any]:
        message = self._message(update)
        if message is None:
            return {"status": "ignored", "reason": "update has no text message"}
        chat_id, text, message_id = message
        if not self._authorized(chat_id):
            return {"status": "ignored", "reason": "chat is not authorized", "chat_id": chat_id}
        result = self._answer(text)
        conversation_id = f"telegram:{chat_id}"
        citations = list(result.get("citations", []))
        if hasattr(self.store, "add_chat_message"):
            created_at = datetime.now(timezone.utc).isoformat()
            self.store.add_chat_message(f"telegram-user:{chat_id}:{message_id}", conversation_id, "user", text, citations, created_at)
            self.store.add_chat_message(f"telegram-assistant:{chat_id}:{message_id}", conversation_id, "assistant", str(result.get("answer", "")), citations, datetime.now(timezone.utc).isoformat())
        answer = str(result.get("answer") or "No grounded answer was available.")
        photo_bytes = self._fetch_snapshot(citations)
        if photo_bytes:
            try:
                caption = answer[:1000] if len(answer) > 1000 else answer
                self.client.send_photo(chat_id, photo_bytes, caption=caption)
                if len(answer) > 1000:
                    self.client.send_message(chat_id, answer[1000:])
                return {
                    "status": "responded_with_snapshot",
                    "chat_id": chat_id,
                    "conversation_id": conversation_id,
                    "answer_mode": result.get("answer_mode", "grounded-structured"),
                    "citation_count": len(citations),
                }
            except Exception:
                pass

        self.client.send_message(chat_id, answer)
        return {
            "status": "responded",
            "chat_id": chat_id,
            "conversation_id": conversation_id,
            "answer_mode": result.get("answer_mode", "grounded-structured"),
            "citation_count": len(citations),
        }


__all__ = ["TelegramBotClient", "TelegramBotService"]
