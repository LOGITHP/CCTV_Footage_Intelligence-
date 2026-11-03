"""Configure the Telegram bot webhook from environment settings.

Required environment variables: ``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_WEBHOOK_URL`` and ``TELEGRAM_WEBHOOK_SECRET``.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from video_intelligence.infrastructure.config import load_settings
from video_intelligence.operations.telegram import TelegramBotClient


def main() -> int:
    settings = load_settings().notifications
    url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not settings.telegram_bot_token or not url or not settings.telegram_webhook_secret:
        print(
            "TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_URL, and TELEGRAM_WEBHOOK_SECRET are required",
            file=sys.stderr,
        )
        return 2
    response = TelegramBotClient(
        settings.telegram_bot_token,
        api_base_url=settings.telegram_api_base_url,
        timeout_seconds=settings.webhook_timeout_seconds,
    ).set_webhook(url, secret_token=settings.telegram_webhook_secret)
    print({"status": "configured", "telegram_ok": response.get("ok", False), "webhook_url": url})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
