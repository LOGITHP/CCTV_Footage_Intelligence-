# Multi-Stream Video Intelligence

An on-premises, evidence-oriented video processing platform. 


## Meta AI Hackathon Architecture Stack

| Layer | Model / Technology | Description |
|---|---|---|
| **Video decoding** | FFmpeg | Authoritative PTS preservation & evidence clip extraction |
| **Frame preprocessing** | OpenCV | Source-frame decoding and color normalization |
| **Object detection** | Meta SAM 3.1 | Segment Anything Model 3.1 object localization |
| **Object segmentation** | Meta SAM 3.1 | Sub-pixel polygon boundary & mask contour generation |
| **Object tracking** | Meta SAM 3.1 | Persistent video mask and trajectory tracking across frames |
| **Visual embeddings** | SigLIP2 | Frame-level visual semantic feature representation |
| **Text embeddings** | BGE-M3 | Query and event text multi-lingual embedding |
| **Vector search** | Qdrant Cloud | Cloud-managed vector cluster (`sa-east-1-0.aws.cloud.qdrant.io`) |
| **LLM reasoning** | Meta Muse Spark Open | Grounded multimodal reasoning via Baseten Inference API (`meta-models/muse-spark-open-1026`) |

### Meta AI, Baseten Hosting & Qdrant Cloud Integration
- **LLM Reasoning**: Connected via `https://inference.baseten.co/v1/chat/completions` using Meta's official hackathon model `meta-models/muse-spark-open-1026` with Baseten bearer key `b10_nVtchD9y.qWy7Pmz8Glauaka2Z1A4Tlrwr4dQy5gL` (allocated with $100 credits & 2M TPM).
- **Model Hosting & Inference on Baseten**:
  - **Meta SAM 3.1** (Detection & Segmentation): Packaged as Truss in `baseten_models/sam3_1` -> `BASETEN_SAM_ENDPOINT`.
  - **SigLIP2** (Visual Frame Embeddings): Packaged as Truss in `baseten_models/siglip2` -> `BASETEN_SIGLIP_ENDPOINT`.
  - **BGE-M3** (Text & Query Embeddings): Packaged as Truss in `baseten_models/bge_m3` -> `BASETEN_BGE_ENDPOINT`.
  - **Baseten Authentication**: Securely authorized via `Api-Key b10_nVtchD9y.qWy7Pmz8Glauaka2Z1A4Tlrwr4dQy5gL`.
  - **Deployment Manager**: Run `python scripts/deploy_baseten.py` to validate packages and synchronize active predict endpoints.
- **Vector DB Storage**: Connected to Qdrant Cloud cluster `https://6b0d33bb-ed40-4932-b576-d73fb0d8bce0.sa-east-1-0.aws.cloud.qdrant.io` for event and observation semantic indexing.
- **Authoritative Database**: Connected to **Supabase PostgreSQL** (`db.iprbivrwcicogcjxzylr.supabase.co:5432`) housing 13 core relational tables across CCTV cameras, events, tracks, anomalies, outbox, and audit logs.
- **Strict Evidence Grounding**: The LLM reasoning agent is strictly constrained to authoritative event citations—eliminating hallucination and preserving forensic timestamps.

### Multi-Tier Data Storage Architecture

To manage non-video derivatives, metadata, and high-frequency analytical output efficiently, the system implements a strict 4-tier storage topology:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        DATA STORAGE TOPOLOGY                           │
├───────────────────┬────────────────────────────┬───────────────────────┤
│ Data Tier         │ Storage Medium             │ Contents Stored       │
├───────────────────┼────────────────────────────┼───────────────────────┤
│ 1. Raw & Evidence │ Docker Volume / S3         │ Source videos (.mp4), │
│    Media Clips    │ (/app/data/media)          │ trimmed evidence cuts │
├───────────────────┼────────────────────────────┼───────────────────────┤
│ 2. Relational &   │ Supabase PostgreSQL        │ Videos, Jobs, Events, │
│    Forensic Store │ (db.*.supabase.co:5432)    │ Track Observations,   │
│                   │                            │ Cameras, Alerts, Logs │
├───────────────────┼────────────────────────────┼───────────────────────┤
│ 3. Multimodal     │ Qdrant Cloud               │ SigLIP2 & BGE-M3      │
│    Vector Store   │ (sa-east-1-0.aws.cloud...) │ 384/768d embeddings   │
├───────────────────┼────────────────────────────┼───────────────────────┤
│ 4. Identity &     │ PBKDF2 + JWT               │ Bearer tokens, users, │
│    Audit Trail    │ (Supabase + Append-Only)   │ immutable audit trail │
└───────────────────┴────────────────────────────┴───────────────────────┘
```

#### Supabase Database Setup & Schema Migrations
The PostgreSQL schema is structured under `backend/migrations/`:
1. `001_initial.sql`: Core tables (`videos`, `jobs`, `events`, `track_observations`, `alert_rules`, `alerts`, `chat_messages`, `outbox_messages`, `security_users`, `audit_logs`).
2. `002_camera_monitoring.sql`: Multi-camera topology, worker health metrics, scoped observations, and anomaly records.

To apply all migrations directly to your Supabase PostgreSQL cluster:
```bash
python scripts/migrate_database.py
```

To run end-to-end integration tests verifying read/write/cascades on Supabase:
```bash
python scripts/test_supabase_integration.py
```

## 1. Repository layout

```text
.
├── frontend/                 # Browser dashboard: HTML, CSS, JavaScript
├── backend/
│   ├── src/video_intelligence/
│   │   ├── api.py            # FastAPI routes and static frontend serving
│   │   ├── domain.py         # Timestamp, observation, zone, and event logic
│   │   ├── evidence.py       # FFmpeg evidence clip creation
│   │   ├── media.py          # FFprobe metadata and frame timestamp tools
│   │   ├── pipeline.py       # OpenCV decode, baseline detect, centroid track
│   │   ├── model_adapters.py # Optional YOLO/ByteTrack/SigLIP2/BGE/Qwen adapters
│   │   ├── model_manifest.py  # Local-only checkpoint/version/dimension validation
│   │   ├── embedding_index.py # SigLIP2/BGE-M3 to versioned Qdrant orchestration
│   │   ├── benchmark.py       # Real CUDA full-stack benchmark CLI
│   │   ├── evaluate.py        # Real-recording annotation evaluation CLI
│   │   ├── evaluation.py      # Dataset readers and offline evaluation metrics
│   │   ├── chat.py           # Grounded structured query and citations
│   │   ├── infrastructure/   # PostgreSQL, MinIO/S3, Qdrant, Celery adapters
│   │   ├── operations/        # Celery tasks, outbox, notifications, retention, backups
│   │   ├── schemas.py        # Pydantic request contracts
│   │   └── storage.py        # SQLite authoritative local store
│   ├── tests/                # Backend unit and API/integration tests
│   ├── migrations/           # PostgreSQL reference schema
│   ├── pyproject.toml        # Python package/test/lint configuration
│   ├── requirements.lock     # Pinned runtime and test dependencies
│   ├── .python-version       # Python 3.12 pin
│   └── Dockerfile            # Reproducible backend/API image
├── deployment/              # Compose, env templates, model manifest, preflight
├── scripts/                 # Explicit reproducible model provisioning
├── docs/                    # Audit and architecture notes
├── Makefile                 # Root developer commands
├── .env.example             # Non-secret local configuration template
└── README.md
```

The attached architecture image and PDF are requirements references, not
runtime configuration or model weights.

## 2. Implemented end-to-end pipeline

The verified recorded-video path is:

```text
Browser upload
  → chunked upload validation and local media storage
  → FFprobe metadata inspection
  → OpenCV frame decoding
  → decoder timestamp validation
  → configurable bright-blob baseline detector
  → per-session centroid tracker
  → polygon zone enter/exit event engine
  → SQLite video/job/event persistence
  → frame and track-observation persistence
  → optional PostgreSQL/MinIO/Qdrant/Redis adapters
  → FFmpeg source-relative evidence clip
   → structured search or grounded chat
   → citations, track history, and rule-based alerts
```

The multi-camera path extends this without replacing the recorded-video flow:

```text
Camera registry (file / RTSP / local device)
  → camera/session-scoped worker
  → bounded frame sampling and reconnect handling
  → persistent reports, observations, tracks, events, anomalies, and alerts
  → transactional notification outbox
  → camera/session-scoped grounded queries and Monitoring dashboard
```

Finite file cameras run to completion. RTSP and local-device cameras use the
live worker when explicitly started; a broker-backed deployment queues the live
task, while local development can run the bounded worker inline. The worker
reports `starting`, `running`, `degraded`, `completed`, `stopped`, or `failed`
from persisted state. It never invents UTC capture time when the source does
not provide a media timestamp.

The source timestamp is authoritative. The implementation rejects invalid or
discontinuous decoder timestamps and does not replace source timing with
`frame_index / nominal_fps`.

The contract-test path is also retained:

```text
POST /api/v1/videos/{video_id}/process
```

It accepts explicitly timestamped structured observations so event and evidence
correctness can be tested without an AI model. The real media path is:

```text
POST /api/v1/videos/{video_id}/process-media
```

## 3. What is implemented

### Frontend

- Same-origin dashboard served at `/` by FastAPI.
- Upload form with file selection.
- API base override for a separately hosted trusted frontend.
- Runtime health and capability display.
- Device selector: CPU or explicitly requested GPU.
- Sampling interval control.
- Brightness threshold control.
- Minimum blob area control.
- Evidence clip padding control.
- Editable polygon-zone JSON configuration.
- Job lookup.
- Event search by object and zone.
- Evidence status/path display.
- Grounded chat with event citations.
- Persisted object-track history.
- Evidence workbench track table with camera/video-session scope, person/vehicle
  category, and explicitly estimated non-biometric appearance cues.
- Login UI that separates local-development access from authenticated deployments,
  with visible backend/API-origin readiness and local setup guidance.
- Alert-rule creation, alert listing, and acknowledgement.
- YOLO/ByteTrack configuration controls that fail honestly when the backend runtime is unavailable.
- No credentials, tokens, RTSP passwords, or model keys are placed in browser code.

### Backend

- FastAPI/Uvicorn service.
- Typed Pydantic request contracts with unknown fields rejected.
- Chunked upload handling and configurable upload-size limit.
- Video content-type validation and partial-file cleanup.
- FFprobe metadata probing.
- Frame-level timestamp reading.
- OpenCV media decoding.
- Deterministic baseline detector for high-contrast fixture objects.
- Local centroid tracking with session-scoped IDs.
- Optional YOLO detections normalize person/vehicle labels and attach coarse crop
  observations such as dominant color, upper/lower garment color, or vehicle type;
  these are marked estimated and non-biometric and are never treated as identity.
- Track IDs are scoped to the video/camera session and are persisted with category,
  bounding box, and appearance metadata.
- Polygon boundary-aware zone containment.
- Stable, deterministic enter/exit event IDs.
- Idempotent event insertion.
- SQLite video, job, event, and evidence metadata.
- FFmpeg evidence clip generation.
- Liveness/readiness endpoints.
- Runtime status/capability endpoints.
- Explicit GPU-unavailable error instead of silent CPU fallback when GPU is requested.
- Configurable CORS allowlist for separate frontend origins.
- Persisted frame/track observations.
- Grounded structured chat with optional Qwen responder mode.
- Rule-based alert creation, event matching, persistence, and acknowledgement.
- Environment-driven PostgreSQL repository adapter.
- Local/S3-compatible object-storage adapter with checksum/object-key metadata.
- Qdrant adapter with explicit collection/vector configuration.
- Inline/Celery queue adapter boundary.
- Optional YOLO, ByteTrack, SigLIP2, BGE-M3, and Qwen adapter boundaries.
- Local-only model manifest loading with checkpoint existence/checksum checks;
  remote model identifiers are rejected rather than downloaded.
- Versioned SigLIP2/BGE-M3 Qdrant collections with explicit dimensions and
  dimension checks on collection creation and upsert.
- Real GPU benchmark command reporting per-stage latency, throughput, and peak
  VRAM for detector, tracker, embeddings, and the chat model when local
  checkpoints are available.
- JSON/JSONL/CSV evaluation readers with timestamp, IoU detection, tracking
  (ID switches/MOTA/IDF1), and ranked search metrics.
- Uploaded-video Celery task definitions with injectable processing runners.
- SQLite/PostgreSQL-compatible transactional outbox and bounded reconciliation.
- Prometheus hooks with an offline no-op/in-memory metrics fallback.
- No-op, local JSONL, webhook, and SMTP notification channel abstractions.
- Dry-run retention planning with explicit deletion gates and propagation hooks.
- Consistent SQLite backup/restore utilities that never overwrite by default.
- Persistent multi-camera registry for file, RTSP, and local-device sources.
- Camera/session-scoped monitoring reports with frame counts, media progress,
  reconnect counts, dropped-frame counts, and worker mode.
- Bounded live monitoring worker with explicit stop events, reconnect attempts,
  source-timestamp validation, and Celery dispatch support.
- Deterministic anomaly rules for count spikes, dwell, unexpected labels,
  camera silence, and processing failures, persisted and routed through the
  notification outbox.

### Deployment

- Python 3.12 pin.
- Locked dependency file.
- CPU Dockerfile with FFmpeg installed inside the image.
- CPU Compose service with persistent application volume.
- NVIDIA Compose override requesting a GPU device.
- NVIDIA host/container preflight scripts.
- Model manifest declaring baseline and opt-in model adapters without bundling unverified weights.
- Optional PostgreSQL, Redis, MinIO, and Qdrant services under the `data` Compose profile.
- Environment templates with no secrets.
- Default same-origin frontend/API deployment.

## 4. API reference

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | Local store readiness |
| `GET` | `/api/v1/system/status` | Runtime and detector status |
| `GET` | `/api/v1/system/capabilities` | CPU/GPU capability report |
| `POST` | `/api/v1/auth/register` | Bootstrap/admin user registration |
| `POST` | `/api/v1/auth/login` | Issue a local JWT bearer token |
| `GET` | `/api/v1/auth/me` | Read the authenticated principal |
| `POST` | `/api/v1/videos` | Upload a video file |
| `GET` | `/api/v1/videos/{video_id}` | Read video metadata |
| `POST` | `/api/v1/videos/{video_id}/probe` | Probe media metadata |
| `POST` | `/api/v1/videos/{video_id}/process` | Process supplied observations |
| `POST` | `/api/v1/videos/{video_id}/process-media` | Run the recorded-media baseline |
| `GET` | `/api/v1/jobs/{job_id}` | Read job status |
| `POST` | `/api/v1/search` | Structured event search |
| `POST` | `/api/v1/semantic-search` | BGE-M3 search with authoritative-store verification |
| `POST` | `/api/v1/videos/{video_id}/semantic-index` | Index verified event text or uploaded frames |
| `GET` | `/api/v1/events` | Filter event timeline |
| `GET` | `/api/v1/events/{event_id}` | Read one event |
| `GET` | `/api/v1/evidence/{event_id}` | Read evidence status/path |
| `GET` | `/api/v1/evidence/{event_id}/url` | Issue a short-lived signed derivative URL |
| `GET` | `/api/v1/evidence/{event_id}/download` | Download through a signed grant |
| `GET` | `/api/v1/videos/{video_id}/tracks` | Read persisted track history |
| `POST` | `/api/v1/chat` | Grounded chat over verified events |
| `POST` | `/api/v1/alerts/rules` | Create an event alert rule |
| `GET` | `/api/v1/alerts/rules` | List alert rules |
| `GET` | `/api/v1/alerts` | List generated alerts |
| `POST` | `/api/v1/alerts/{alert_id}/acknowledge` | Acknowledge an alert |
| `GET` | `/api/v1/cameras` | List registered camera sources and latest reports |
| `POST` | `/api/v1/cameras` | Register a file, RTSP, or local-device camera |
| `PATCH` | `/api/v1/cameras/{camera_id}` | Update a camera definition |
| `POST` | `/api/v1/cameras/{camera_id}/run` | Run a finite camera source or start bounded live monitoring |
| `POST` | `/api/v1/cameras/{camera_id}/stop` | Request stop for an active inline live worker |
| `GET` | `/api/v1/cameras/{camera_id}/reports` | Read persisted monitoring reports |
| `GET` | `/api/v1/monitoring/anomalies` | Read persisted anomaly records by camera/session |

OpenAPI documentation is available at `/docs` and `/redoc` when the service is
running.

### Example API flow

```bash
# 1. Upload a supported recording.
curl -F "file=@fixtures/camera.mp4" http://localhost:8000/api/v1/videos

# 2. Probe the returned video_id.
curl -X POST http://localhost:8000/api/v1/videos/<video_id>/probe

# 3. Process with a configured zone.
curl -X POST http://localhost:8000/api/v1/videos/<video_id>/process-media \
  -H 'Content-Type: application/json' \
  -d '{
    "device": "cpu",
    "model_profile": "baseline",
    "tracker_profile": "centroid",
    "every": 1,
    "threshold": 200,
    "min_area": 25,
    "clip_padding": 5,
    "zones": [{
      "zone_id": "main-gate",
      "polygon": [[0,0],[640,0],[640,480],[0,480]]
    }]
  }'

# 4. Search the authoritative event table.
curl -X POST http://localhost:8000/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"zone_id":"main-gate"}'

# 5. Ask a grounded question. The response contains only persisted citations.
curl -X POST http://localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Which person entered zone main-gate?","zone_id":"main-gate"}'

# 6. Configure an alert rule for future processed events.
curl -X POST http://localhost:8000/api/v1/alerts/rules \
  -H 'Content-Type: application/json' \
  -d '{"name":"Gate entry","event_type":"enter","zone_id":"main-gate","object_label":"person"}'
```

## 5. Local development

### Prerequisites

- Python 3.12.
- FFmpeg and FFprobe on `PATH` for local media probing/clip generation.
- Node.js only for the `node --check` frontend syntax check; the frontend has
  no npm dependency or build step.
- Docker Desktop or Docker Engine for container verification.

### Install and run

From the repository root:

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux/macOS
# source .venv/bin/activate

python -m pip install -r backend/requirements.lock
python -m pytest -q backend/tests
python -m uvicorn --app-dir backend/src video_intelligence.api:app --host 127.0.0.1 --port 8000
```

For a host that will run the optional real CPU ML stack, install the separately
locked runtime inside a project-local virtual environment:

```bash
python -m venv .venv-ml
# Linux/macOS: . .venv-ml/bin/activate
# Windows PowerShell: .\.venv-ml\Scripts\Activate.ps1
python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple \
  -r backend/requirements-ml.cpu.lock \
  -r backend/requirements-ml.lock
```

For a CUDA 12.4 host, replace `requirements-ml.cpu.lock` with
`requirements-ml.cuda.lock` and use the NVIDIA deployment path. The GPU path is
not verified on this machine.

The root `Makefile` provides equivalent commands:

```bash
make test
make run
make lint
make evaluate VIDEO=data/media/uploaded-recording.mp4 ANNOTATIONS=data/evaluation/labels.json
```

On Windows, `make` may not be installed; run the Python commands directly.

## 6. Docker deployment

### Quick Start with Docker Compose
The Docker environment provides end-to-end persistence for all uploaded video files, evidence clips, and databases, with enforced JWT authentication.

```bash
# Validate compose configuration
docker compose config

# Build and start the service
docker compose up --build -d
```

### Actual Video File Storage & Volumes
All actual video files, FFmpeg derivative clips, and SQLite databases are stored in the persistent volume mounted at `/app/data`:
- `/app/data/media/`: Uploaded raw video recordings.
- `/app/data/media/evidence/`: Source-cut forensic evidence MP4 clips.
- `/app/data/video_intelligence.sqlite3`: Authoritative event, track, and camera store.
- `/app/data/auth.sqlite3`: User credentials and audit trails.

The named volume `video_intelligence_actual_media` preserves all footage and databases across container lifecycles, upgrades, and restarts.

### JWT Authentication & Security
- **Enforced Authentication**: Set `AUTH_REQUIRED=true` (enabled by default in `docker-compose.yml`).
- **Initial Bootstrap Admin**: Pre-configured via `ADMIN_USERNAME=admin` and `ADMIN_PASSWORD=adminpassword123`.
- **Token Format**: Standard HS256-signed Bearer tokens with configurable TTL (`JWT_TOKEN_TTL_SECONDS=3600`).
- **Web Console & API**: The frontend UI stores tokens in `localStorage` upon login, transparently attaching `Authorization: Bearer <token>` to all protected endpoints.
- **REST Endpoints**:
  - `POST /api/v1/auth/register`: Create user account.
  - `POST /api/v1/auth/login`: Authenticate and obtain JWT token.
  - `GET /api/v1/auth/me`: Inspect current user and permissions.

Stop it with:

```bash
docker compose --env-file deployment/.env -f deployment/compose.yaml down
```

The named `app-data` volume contains the SQLite database, uploaded media, and
generated evidence. Models are embedded in the image rather than mounted from
the host. Do not delete the volume unless data deletion is intentional.

### PostgreSQL, Redis, MinIO, and Qdrant data profile

The optional data profile starts the services represented in the architecture
diagram. The API still defaults to local storage unless the corresponding
environment variables are enabled. To use the profile:

```bash
# Copy the template, replace every change-me password, and uncomment the
# DATABASE_URL, REDIS_URL, Celery, S3, and QDRANT settings.
cp deployment/.env.example deployment/.env
docker compose --env-file deployment/.env --profile data \
  -f deployment/compose.yaml -f deployment/compose.data.yaml config
docker compose --env-file deployment/.env --profile data \
  -f deployment/compose.yaml -f deployment/compose.data.yaml up --build
```

For a production data deployment set at minimum:

```env
# Supabase PostgreSQL Database
DATABASE_URL=postgresql+psycopg://postgres:<PASSWORD>@db.<PROJECT_REF>.supabase.co:5432/postgres?sslmode=require

# Qdrant Cloud Cluster
QDRANT_URL=https://6b0d33bb-ed40-4932-b576-d73fb0d8bce0.sa-east-1-0.aws.cloud.qdrant.io
QDRANT_API_KEY=<your-qdrant-api-key>
QDRANT_COLLECTION=video-events

# Optional Redis / Celery Background Processing
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
OBJECT_STORAGE_BACKEND=local
MEDIA_ROOT=data/media
```

The adapters are optional at runtime so offline tests do not need these
services. Run migrations with `python scripts/migrate_database.py` (which runs
`backend/migrations/001_initial.sql` and `backend/migrations/002_camera_monitoring.sql`)
before pointing a production API at PostgreSQL. The data profile includes a Celery worker and beat scheduler;
the worker handles queued jobs and beat reconciles the durable notification
outbox.

### NVIDIA deployment

The GPU override is a deployment contract and preflight path. It does not turn
the baseline detector into a CUDA-accelerated production model.

Prerequisites:

- NVIDIA driver compatible with the host OS.
- NVIDIA Container Toolkit.
- Docker Compose support for GPU device reservations.
- A validated GPU model adapter and weights before enabling real inference.

Validate and start:

```bash
sh deployment/preflight.sh gpu
docker compose -f deployment/compose.yaml -f deployment/compose.nvidia.yaml config
docker compose -f deployment/compose.yaml -f deployment/compose.nvidia.yaml up --build
```

The preflight must fail loudly if the NVIDIA runtime is not available. Never
silently treat a failed CUDA request as successful CPU inference.

### Local model manifest, indexing, and benchmark

The shipped `deployment/model-manifest.json` declares model IDs, revisions,
embedding dimensions, and environment names for local checkpoints. Set those
environment variables to filesystem paths; a model ID or URL is not accepted:

```bash
export YOLO_MODEL=/models/yolo.pt
export SIGLIP_MODEL=/models/siglip2
export BGE_MODEL=/models/bge-m3
export QWEN_MODEL=/models/qwen3-8b
```

The checked-in model manifest pins public Hugging Face revisions for SigLIP2,
BGE-M3, and Qwen3-8B, plus the Ultralytics YOLO-World artifact. The Qwen3
checkpoint requires Transformers 4.51 or newer; the ML lockfile pins a
compatible version. Downloaded model files are intentionally ignored by Git.

Run the real full-stack CUDA benchmark from the repository root:

```bash
PYTHONPATH=backend/src python -m video_intelligence.benchmark \
  --video data/media/uploaded-recording.mp4 \
  --manifest deployment/model-manifest.json \
  --detector yolo --tracker bytetrack \
  --output benchmark-report.json
```

The command fails clearly when CUDA, an optional runtime package, or a selected
local checkpoint is unavailable. For a detector/tracker-only capability report
that records missing optional embedding/chat stages instead of failing, pass
`--detector baseline --tracker centroid --allow-missing-optional`. The report
contains stage sample counts, mean/p50/p95 latency, throughput, and peak VRAM.

Embedding indexing is available to backend workers through
`EmbeddingIndexOrchestrator.from_manifest(...)`; it creates separate Qdrant
collections for each model revision (for example, `video-events__bge-m3-v1`)
and rejects vectors whose length differs from the manifest dimension.

Evaluation datasets can be read with `read_evaluation_dataset(...)`. JSON
objects may contain `timestamps`, `detections`, `tracking`, and `search`
arrays; JSONL/CSV rows may instead use a `type`/`kind` field. The metric
functions are offline and deterministic, so metric-only tests do not need
Qdrant, CUDA, or model weights. To evaluate real detector/tracker output from
an uploaded recording, use the new runner:

```bash
PYTHONPATH=backend/src python -m video_intelligence.evaluate \
  --video data/media/uploaded-recording.mp4 \
  --annotations data/evaluation/labels.json \
  --profile baseline --tracker centroid --device cpu \
  --output evaluation-report.json
```

When the annotation file contains timestamp/event labels, provide the trusted
zone polygons separately with `--zones zones.json`; the runner refuses to
score event timestamps without zones. The command reports detection IoU
precision/recall/F1, tracking ID switches/MOTA/IDF1, and timestamp matching
from the real decoded frames. It does not create synthetic labels or perfect
search results when semantic retrieval is unavailable.

## 7. Configuration and environment security

### Safe configuration rule

The browser is an untrusted client. It may contain UI preferences and a public
API origin, but it must never contain:

- database passwords;
- JWT signing keys;
- cloud/API provider keys;
- RTSP usernames or passwords;
- MinIO access keys;
- Qdrant credentials;
- model download tokens.

Local JWT authentication, RBAC, audit logging, rate limiting, and signed
evidence grants are implemented. In development, the process uses an ephemeral
secret and development principal for the existing offline tests. In staging or
production, set `APP_ENV=production`, `JWT_SECRET`, and `AUTH_REQUIRED=true`;
the API will reject missing or invalid bearer tokens and the first registration
is limited to the bootstrap path.

### Environment files

- `.env.example` documents local non-secret defaults.
- `deployment/.env.example` documents Compose defaults.
- Copy templates to `.env` or `deployment/.env` locally.
- Those files are ignored by Git; never commit them.
- Keep real secrets in a host secret manager or Docker/Kubernetes secrets, not
  in the frontend, image, Compose file, or README.
- Keep `CORS_ORIGINS` empty for the integrated same-origin dashboard.
- If the frontend is hosted separately, list exact trusted origins, for example
  `https://console.example.internal`; never use `*` with a production API.
- Rotate credentials when a deployment host, volume, or log archive is exposed.

### Frontend configuration

The dashboard exposes all settings supported by the current backend baseline:

- API base URL;
- CPU/GPU device request;
- frame sampling interval;
- detector threshold;
- minimum detection area;
- clip padding;
- zone polygons;
- baseline vs YOLO model profile;
- centroid vs ByteTrack tracker profile;
- grounded chat questions;
- alert rule name, event type, zone, and object label.
- local JWT login/registration when authentication is required;

These controls configure a request; they do not grant access to secrets or
enable features that the backend does not advertise through capabilities.

## 8. Real CCTV deployment guidance

The current pipeline accepts recorded files. It does **not** yet implement RTSP
camera registration, reconnect supervision, rolling buffers, camera topology, or
live stream clock reconciliation. For real CCTV footage, implement these in the
following order:

1. Add a camera registry with stable IDs, timezone, location, nominal FPS, and a
   secret reference instead of a returned password.
2. Add a stream-session worker using FFmpeg/PyAV with bounded queues,
   reconnect-backoff, last-decoded-frame health, and per-camera session IDs.
3. Preserve PTS/time base and mark timestamp quality when wall-clock mapping is
   unavailable or discontinuous.
4. Add a rolling segment buffer before enabling pre-event clips.
5. Replace the bright-blob detector with a pinned, licensed detector adapter
   such as a validated YOLO/ONNX/PyTorch model. Record model revision,
   threshold, device, and preprocessing version with every observation.
6. Replace centroid tracking with a validated ByteTrack adapter and keep track
   IDs scoped to a camera and processing session.
7. Add privacy processing before public derivatives. Keep originals restricted
   and audit every access.
8. Benchmark sampling rate, object size, motion speed, queue wait, CPU/RAM,
   GPU/VRAM, and timestamp error on annotated footage.
9. Test variable-frame-rate recordings, dropped frames, reconnects, camera clock
   drift, occlusion, corrupted segments, and duplicate retries.

Do not use this baseline to make identity, intent, criminality, or arbitrary
action claims. Appearance similarity is not proof of identity.

## 9. Production upgrade plan

### Persistence and jobs

The repository now contains a PostgreSQL adapter, reference migration, and
Redis/Celery adapter. The offline API still runs inline on SQLite by default;
when a broker URL is configured, uploaded media is dispatched to Celery and the
worker writes authoritative results back to the configured store. The durable
outbox is used for processing and configured alert notifications. Run worker
restart/retry tests before production rollout. Redis must not be the only
permanent event record.

### Media and evidence

The repository now records checksums/object keys and contains a MinIO/S3 adapter.
The local evidence status endpoint does not expose a raw playable path. The
signed evidence endpoint issues short-lived derivative URLs only after RBAC
checks. Original evidence remains disabled by default; public derivatives are
redacted with fail-closed policy settings. Apply the same privacy policy
consistently to frames, thumbnails, and clips.

### Search and intelligence

Structured retrieval and grounded chat are implemented for persisted local
events. The semantic routes can index verified event text or uploaded frames
into separate versioned Qdrant collections when `MODEL_MANIFEST`, local
checkpoints, and `QDRANT_URL` are configured. Every returned candidate is
verified against the authoritative store; missing events/videos and timestamp
mismatches are discarded. Qwen must summarize verified citations and never
create evidence or timestamps.

### Security and operations

Authentication, RBAC, audit logs, upload limits, rate limits, and signed
evidence grants are configured through the backend security modules. Uploaded
video operations now include a durable outbox, bounded reconciliation worker,
notification delivery abstractions, retention/deletion propagation, SQLite
backup/restore helpers, and operation-level Prometheus hooks. Notification
delivery is a no-op unless a channel is selected in environment configuration;
retention is dry-run and deletion-disabled by default. Put the API behind TLS
and a reverse proxy for a networked CCTV deployment.

### GPU and models

Keep model weights in a persistent, integrity-checked cache. Pin model IDs and
revisions in `deployment/model-manifest.json`. Provide offline initialization
for air-gapped sites. Validate CUDA availability inside the actual inference
container and benchmark peak VRAM; do not assume all detector, embedding,
Re-ID, and language models fit simultaneously.

## 10. Feature status matrix

| Feature | Status | Evidence |
|---|---|---|
| Recorded upload | Implemented/tested | FastAPI upload route and API tests |
| FFprobe metadata | Implemented/tested | `backend/src/video_intelligence/media.py` |
| Source-time frame sampling | Implemented/tested | FFprobe/OpenCV timestamp tests |
| Baseline detection | Implemented/tested | Bright-blob fixture detector |
| Local tracking | Implemented/tested | Centroid tracker tests |
| Zone events | Implemented/tested | Deterministic domain tests |
| Evidence clips | Implemented/tested | FFmpeg adapter and readback contract |
| Structured event search | Implemented/tested | SQLite filters and API tests |
| Track observation persistence | Implemented/tested | `/api/v1/videos/{video_id}/tracks` and API tests |
| Grounded chat | Implemented/tested | Structured citations; optional Qwen responder |
| Alert rules and acknowledgements | Implemented/tested | SQLite/PostgreSQL adapters and API tests |
| Frontend dashboard | Implemented/tested | Same-origin static dashboard and JS syntax check |
| Upload validation | Implemented/tested | MIME/size/cleanup tests |
| CPU Compose | Implemented/config validated | `deployment/compose.yaml` |
| NVIDIA preflight | Implemented/config validated | `compose.nvidia.yaml` and scripts |
| YOLO adapter | Implemented/configuration-only | Requires reviewed weights and optional ML runtime |
| ByteTrack adapter | Implemented/configuration-only | Requires `supervision` and bounding boxes |
| SigLIP2/BGE-M3 adapters | Implemented/configuration-only | Requires model packages, weights, and Qdrant indexing |
| Qwen grounded responder | Implemented/configuration-only | Disabled by default; citations remain authoritative |
| RTSP/live cameras | Not implemented | Requires stream worker and secret handling |
| PostgreSQL/MinIO/Redis/Qdrant | Adapters implemented/configurable | External services are optional and not required for offline tests |
| Celery uploaded-video processing | Implemented/configurable | `backend/src/video_intelligence/operations/tasks.py` |
| Transactional outbox/reconciliation | Implemented/tested | SQLite/PostgreSQL SQLAlchemy repository and fake-handler tests |
| Prometheus metrics hooks | Implemented/configurable | No-op/in-memory fallback; opt-in Prometheus registry |
| Notifications | Implemented/configurable | No-op/local/webhook/SMTP channels; no network calls in offline mode |
| Retention/deletion propagation | Implemented/safe-by-default | Dry-run plus explicit policy and confirmation gates |
| Backup/restore | Implemented/tested | SQLite online backup API; no overwrite by default |
| Authentication/RBAC | Implemented/configurable | Local JWT/RBAC boundary; credentials remain environment configured |
| Privacy redaction | Implemented/configurable | Public derivatives redact by default and fail closed |
| Semantic search/vector indexing | Implemented/configuration-only | `/api/v1/semantic-search` and `/api/v1/videos/{video_id}/semantic-index`; requires local checkpoints and Qdrant |
| Model evaluation | Implemented/offline/runner-ready | JSON/JSONL/CSV readers, real-recording runner, and timestamp/detection/tracking/search metrics |
| GPU full-stack benchmark | Implemented/host-dependent | `video_intelligence.benchmark`; requires CUDA and selected local checkpoints |
| Alerts | Implemented/tested | Rule matching, acknowledgement, and configured notification outbox messages |
| Notifications/WebSockets | Notifications implemented; WebSockets pending | Notification worker is durable; live event transport remains deferred |

## 11. Verification commands

```bash
python -m pytest -q backend/tests
node --check frontend/app.js
docker compose -f deployment/compose.yaml config
docker compose -f deployment/compose.yaml -f deployment/compose.nvidia.yaml config
docker compose --profile data -f deployment/compose.yaml -f deployment/compose.data.yaml config
git diff --check
```

The Docker image must be built and run on a host with a working Docker daemon:

```bash
docker build --tag multi-stream-video-intelligence:cpu \
  --file backend/Dockerfile .
docker compose -f deployment/compose.yaml up --build
```

GPU checks are only meaningful on compatible NVIDIA hardware. Report them as
unrun when that hardware or the Docker runtime is unavailable.

## 12. Design principle

The value of this system is trustworthy evidence, not the number of AI models.
Every result should be traceable to a source media object, source timestamp,
camera/session, observation, event rule, permission decision, and playable
artifact. When evidence is missing or uncertain, the API should return an
explicit pending, uncertain, or no-supported-match result instead of inventing
an answer.
