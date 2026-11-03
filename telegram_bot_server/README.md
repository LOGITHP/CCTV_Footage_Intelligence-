# VI Sentry Telegram Alert & Bot Server

Standalone, production-ready edge microservice for **VI Sentry Autonomous Surveillance**.

## Capabilities

1. **Instant Security Alert Dispatch (`POST /api/alerts`)**:
   - Dispatches formatted incident reports with priority icons (`🚨`, `⚠️`, `ℹ️`), rule name, camera ID, zone, timecode, telemetry, and clickable evidence clips.
   - Supports targeted single-chat dispatch and fleet-wide broadcast (`POST /api/alerts/broadcast`).
   - Authenticated via `X-Telegram-Alert-Secret` or Bearer token.

2. **Inbound Telegram Webhook (`POST /telegram/webhook`)**:
   - Secure verification of incoming Telegram Bot API updates via `X-Telegram-Bot-Api-Secret-Token`.
   - **Tactical Commands**:
     - `/start` or `/help` — Command menu and operator guide.
     - `/status` — Live health check of VMS core daemon, store, and AI stack.
     - `/cameras` — Monitored camera fleet registry and online states.
     - `/alerts` — Recent perimeter intrusions and security infractions.
   - **Natural Language Sentry Copilot**: Free-form surveillance queries are routed directly to the backend LangGraph / Muse Spark 1.3 Copilot for zero-hallucination grounded responses.

3. **Health & Readiness Probes (`GET /health`, `GET /health/ready`)**:
   - Reports bot token configuration, allowed chat counts, and backend connectivity.

---

## Quickstart

### 1. Local Run
```bash
# Install dependencies
pip install -r telegram_bot_server/requirements.txt

# Run the server
set PYTHONPATH=backend/src
uvicorn telegram_bot_server.server:app --host 0.0.0.0 --port 8090
```

### 2. Docker Run
```bash
# Build Docker image
docker build -f telegram_bot_server/Dockerfile -t vi-sentry-telegram .

# Run container
docker run -d --name vi-sentry-telegram \
  --env-file telegram_bot_server/.env \
  -p 8090:8090 \
  vi-sentry-telegram
```

### 3. Docker Compose
Add to `docker-compose.yml` or launch alongside the main stack:
```bash
docker compose up -d telegram-bot-server
```

---

## API Endpoints

### 1. Dispatch Security Alert
```http
POST /api/alerts
X-Telegram-Alert-Secret: <TELEGRAM_ALERT_SHARED_SECRET>
Content-Type: application/json

{
  "rule_name": "Perimeter Breach Detected",
  "severity": "CRITICAL",
  "camera_name": "East Gate Sentry",
  "zone_id": "zone-perimeter-north",
  "timestamp": "2026-11-03 13:00:00 UTC",
  "details": "Person crossed armed boundary into restricted quadrant",
  "evidence_url": "https://vms.corp.local/api/v1/evidence/clip-8812.mp4"
}
```

### 2. Broadcast to All Officers
```http
POST /api/alerts/broadcast
X-Telegram-Alert-Secret: <TELEGRAM_ALERT_SHARED_SECRET>
Content-Type: application/json

{
  "rule_name": "Facility Lockdown Triggered",
  "severity": "CRITICAL",
  "details": "Manual lockdown protocol initiated by Command Sentry."
}
```
