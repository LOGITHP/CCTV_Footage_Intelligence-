"""Safe notification channel abstractions and delivery worker.

No channel is contacted unless it is selected in configuration.  The default
``noop`` channel records no credentials and performs no I/O.  Tests can inject
webhook or SMTP fakes without a network connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
import json
from pathlib import Path
import smtplib
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..infrastructure.config import NotificationSettings
from .metrics import MetricsHooks


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """Serializable notification payload used by outbox and Celery workers."""

    notification_id: str
    channel: str
    recipient: str | None = None
    subject: str = "Video intelligence notification"
    body: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NotificationMessage":
        payload = value.get("payload", {})
        return cls(
            notification_id=str(value.get("notification_id") or value.get("message_id") or "notification"),
            channel=str(value.get("channel") or "noop"),
            recipient=str(value["recipient"]) if value.get("recipient") is not None else None,
            subject=str(value.get("subject") or "Video intelligence notification"),
            body=str(value.get("body") or ""),
            payload=dict(payload) if isinstance(payload, Mapping) else {"value": payload},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "channel": self.channel,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    notification_id: str
    channel: str
    status: str
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "channel": self.channel,
            "status": self.status,
            "detail": self.detail,
        }


class NotificationChannel(Protocol):
    name: str

    def send(self, message: NotificationMessage) -> DeliveryResult: ...


class NoopNotifier:
    """Explicit no-op channel used by local/offline deployments."""

    name = "noop"

    def send(self, message: NotificationMessage) -> DeliveryResult:
        return DeliveryResult(message.notification_id, self.name, "skipped", "notification mode is noop")


class LocalNotifier:
    """Append JSONL notifications to a caller-configured local path."""

    name = "local"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, message: NotificationMessage) -> DeliveryResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(message.as_dict(), sort_keys=True) + "\n")
        return DeliveryResult(message.notification_id, self.name, "delivered", str(self.path))


WebhookSender = Callable[[str, bytes, Mapping[str, str], float], int]


def _default_webhook_sender(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is explicit env configuration
        return int(response.status)


class WebhookNotifier:
    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 5.0,
        sender: WebhookSender | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("webhook URL must be an absolute http(s) URL")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.sender = sender or _default_webhook_sender
        self.headers = {"Content-Type": "application/json", **dict(headers or {})}

    def send(self, message: NotificationMessage) -> DeliveryResult:
        status_code = self.sender(
            self.url,
            json.dumps(message.as_dict(), sort_keys=True).encode("utf-8"),
            self.headers,
            self.timeout_seconds,
        )
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"webhook returned HTTP {status_code}")
        return DeliveryResult(message.notification_id, self.name, "delivered", f"HTTP {status_code}")


SMTPFactory = Callable[[str, int, float], smtplib.SMTP]


def _default_smtp_factory(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host=host, port=port, timeout=timeout)


class EmailNotifier:
    name = "email"

    def __init__(
        self,
        host: str,
        *,
        port: int = 25,
        sender_address: str,
        recipient: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
        timeout_seconds: float = 5.0,
        smtp_factory: SMTPFactory | None = None,
    ) -> None:
        if not host or not sender_address or not recipient:
            raise ValueError("email host, sender address, and recipient are required")
        self.host = host
        self.port = port
        self.sender_address = sender_address
        self.recipient = recipient
        self.username = username
        self.password = password
        self.starttls = starttls
        self.timeout_seconds = timeout_seconds
        self.smtp_factory = smtp_factory or _default_smtp_factory

    def send(self, message: NotificationMessage) -> DeliveryResult:
        email = EmailMessage()
        email["From"] = self.sender_address
        email["To"] = message.recipient or self.recipient
        email["Subject"] = message.subject
        email.set_content(message.body)
        client = self.smtp_factory(self.host, self.port, self.timeout_seconds)
        try:
            if self.starttls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password or "")
            client.send_message(email)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
        return DeliveryResult(message.notification_id, self.name, "delivered", email["To"])


# Descriptive aliases for callers that use the channel terminology.
NoopChannel = NoopNotifier
LocalChannel = LocalNotifier
WebhookChannel = WebhookNotifier
EmailChannel = EmailNotifier


class NotificationDispatcher:
    """Select a configured channel for each outbox payload."""

    def __init__(self, channels: Mapping[str, NotificationChannel] | None = None, *, metrics: MetricsHooks | None = None) -> None:
        self.channels = dict(channels or {"noop": NoopNotifier()})
        if "noop" not in self.channels:
            self.channels["noop"] = NoopNotifier()
        self.metrics = metrics or MetricsHooks()

    def deliver(self, message: NotificationMessage | Mapping[str, Any]) -> DeliveryResult:
        notification = message if isinstance(message, NotificationMessage) else NotificationMessage.from_mapping(message)
        channel = self.channels.get(notification.channel, self.channels["noop"])
        try:
            result = channel.send(notification)
        except Exception:
            self.metrics.notification_failed(getattr(channel, "name", notification.channel))
            raise
        if result.status == "delivered":
            self.metrics.notification_delivered(result.channel)
        return result


class NotificationDeliveryWorker:
    """Outbox/Celery-friendly worker around :class:`NotificationDispatcher`."""

    topic = "notification.send"

    def __init__(self, dispatcher: NotificationDispatcher) -> None:
        self.dispatcher = dispatcher

    def deliver(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = record.get("payload", record)
        if not isinstance(payload, Mapping):
            raise ValueError("notification outbox payload must be an object")
        return self.dispatcher.deliver(payload).as_dict()


def build_notification_dispatcher(
    settings: NotificationSettings,
    *,
    webhook_sender: WebhookSender | None = None,
    smtp_factory: SMTPFactory | None = None,
    metrics: MetricsHooks | None = None,
) -> NotificationDispatcher:
    """Create channels without performing any delivery."""

    selected = tuple(settings.channels)
    if not selected:
        selected = (settings.mode,) if settings.mode != "multi" else ()
    channels: dict[str, NotificationChannel] = {"noop": NoopNotifier()}
    for name in selected:
        normalized = name.lower()
        if normalized == "noop":
            channels["noop"] = NoopNotifier()
        elif normalized == "local" and settings.local_path:
            channels["local"] = LocalNotifier(settings.local_path)
        elif normalized == "webhook" and settings.webhook_url:
            channels["webhook"] = WebhookNotifier(
                settings.webhook_url,
                timeout_seconds=settings.webhook_timeout_seconds,
                sender=webhook_sender,
            )
        elif normalized == "email" and settings.email_host and settings.email_from and settings.email_to:
            channels["email"] = EmailNotifier(
                settings.email_host,
                port=settings.email_port,
                sender_address=settings.email_from,
                recipient=settings.email_to,
                username=settings.email_username,
                password=settings.email_password,
                starttls=settings.email_starttls,
                timeout_seconds=settings.webhook_timeout_seconds,
                smtp_factory=smtp_factory,
            )
    return NotificationDispatcher(channels, metrics=metrics)


def build_notification_worker(
    settings: NotificationSettings,
    *,
    webhook_sender: WebhookSender | None = None,
    smtp_factory: SMTPFactory | None = None,
    metrics: MetricsHooks | None = None,
) -> NotificationDeliveryWorker:
    return NotificationDeliveryWorker(
        build_notification_dispatcher(
            settings,
            webhook_sender=webhook_sender,
            smtp_factory=smtp_factory,
            metrics=metrics,
        )
    )


NotificationWorker = NotificationDeliveryWorker


def configured_notification_channels(settings: NotificationSettings) -> tuple[str, ...]:
    """Return explicitly enabled delivery channels, excluding the safe no-op."""

    selected = tuple(settings.channels)
    if not selected and settings.mode != "multi":
        selected = (settings.mode,)
    return tuple(channel.strip().lower() for channel in selected if channel.strip() and channel.strip().lower() != "noop")


def notification_payloads(
    alerts: list[Mapping[str, Any]],
    *,
    video_id: str,
    channels: tuple[str, ...],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Build secret-free, deduplicable outbox payloads for persisted alerts."""

    payloads: list[tuple[str, dict[str, Any]]] = []
    for alert in alerts:
        alert_id = str(alert.get("alert_id") or "")
        if not alert_id:
            continue
        for channel in channels:
            payloads.append(
                (
                    channel,
                    {
                        "notification_id": f"{alert_id}:{channel}",
                        "channel": channel,
                        "subject": "Video intelligence alert",
                        "body": str(alert.get("message") or "Video intelligence alert"),
                        "payload": {
                            "video_id": video_id,
                            "alert_id": alert_id,
                            "event_id": alert.get("event_id"),
                            "status": alert.get("status", "open"),
                        },
                    },
                )
            )
    return tuple(payloads)
