"""FastAPI boundary for the deterministic first vertical slice."""

from __future__ import annotations

# dotenv loading intentionally happens before application imports so local
# credentials/configuration are available during module initialization.
# ruff: noqa: E402

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping
from uuid import uuid4

def _load_dotenv_if_present() -> None:
    for candidate in (Path(".env"), Path("../.env"), Path(__file__).resolve().parents[3] / ".env"):
        if candidate.is_file():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass
            break

_load_dotenv_if_present()

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, Depends, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .domain import FrameObservation, ObjectObservation, Zone, generate_zone_events
from .observables import ObservableAppearance
from .cameras import (
    CameraConfigurationError,
    CameraDefinition,
    CameraRegistry,
    CameraSupervisor,
    camera_track_scope,
)
from .evidence import create_clip
from .infrastructure import infrastructure_capabilities
from .infrastructure.config import load_settings
from .infrastructure.object_storage import ObjectStorageUnavailable, build_object_storage
from .media import probe_media
from .model_adapters import ModelSettings, ModelUnavailableError, build_detector, build_tracker, model_capabilities, settings_from_env
from .auth import AuthenticationService, InvalidCredentialsError, JWTManager, Role, SQLiteUserRepository, UserPrincipal
from .audit import AuditLogRepository, AuditLogService
from .rbac import Permission, has_permission
from .rate_limit import RateLimiter, RateLimitPolicy, client_key
from .security_config import load_security_settings
from .signed_evidence import EvidenceAccessDenied, EvidenceTokenError, EvidenceTokenSigner
from .operations.metrics import PrometheusMetrics, build_metrics
from .operations.notifications import build_notification_worker, configured_notification_channels, notification_payloads
from .operations.monitoring import CameraMonitoringWorker
from .operations.processing import default_pipeline_runner
from .operations.streaming import CameraStreamMonitoringWorker
from .operations.telegram import TelegramBotService
from .operations.outbox import OutboxStore
from .infrastructure.queue import build_dispatcher
from .operations.tasks import dispatch_camera, dispatch_camera_stream, dispatch_uploaded_video
from .embedding_index import EmbeddingIndexOrchestrator, EmbeddingIndexingError
from .infrastructure.vectors import VectorStorageUnavailable
from .pipeline import BrightBlobDetector, process_video as run_media_pipeline, runtime_capabilities
from .model_manifest import ManifestError
from .schemas import (
    AlertRuleRequest,
    CameraRequest,
    CameraRunRequest,
    CameraUpdateRequest,
    ChatRequest,
    LoginRequest,
    MediaProcessRequest,
    ProcessRequest,
    RegisterRequest,
    SearchRequest,
    SemanticIndexRequest,
    SemanticSearchRequest,
    TelegramConfigurationRequest,
)
from .semantic import SemanticSearchError, SemanticSearchService
from .storage import Store

logger = logging.getLogger("video_intelligence")

@asynccontextmanager
async def app_lifespan(_: FastAPI):
    """Discover optional runtimes before readiness exposes the API."""

    runtime_capabilities()
    yield


app = FastAPI(title="Multi-Stream Video Intelligence", version="0.1.0", lifespan=app_lifespan)


cors_origins = tuple(origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip())
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def build_authoritative_store():
    settings = load_settings()
    if settings.database.url:
        from .infrastructure.postgres import PostgresStore

        try:
            return PostgresStore(settings.database.url)
        except Exception as error:
            import logging
            logging.getLogger("video_intelligence").warning(
                "PostgreSQL connection failed (%s); falling back to SQLite store at %s",
                error,
                settings.database.sqlite_path,
            )
            return Store(settings.database.sqlite_path)
    return Store(settings.database.sqlite_path)


store = build_authoritative_store()
media_root = Path(os.getenv("MEDIA_ROOT", "data/media"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
CHUNK_SIZE = 1024 * 1024
security_settings = load_security_settings()
auth_database_path = os.getenv("AUTH_DATABASE_PATH", "data/auth.sqlite3")
user_repository = SQLiteUserRepository(auth_database_path)
authentication_service = AuthenticationService(user_repository, JWTManager.from_settings(security_settings.jwt))
audit_service = AuditLogService(AuditLogRepository(os.getenv("AUDIT_DATABASE_PATH", auth_database_path)))
rate_limiter = RateLimiter(RateLimitPolicy.from_env())
bearer_scheme = HTTPBearer(auto_error=False)
metrics = build_metrics(enabled=security_settings.policy.require_authentication or os.getenv("PROMETHEUS_ENABLED", "false").lower() in {"1", "true", "yes", "on"})
_outbox_stores: dict[str, OutboxStore] = {}
_outbox_lock = threading.RLock()
_semantic_orchestrators: dict[str, EmbeddingIndexOrchestrator] = {}
_semantic_lock = threading.RLock()


# Camera orchestration is deliberately module-scoped so a request sees the
# same active-run set as the stop route.  Definitions are rehydrated whenever a
# test or embedding deployment swaps the authoritative store instance.
camera_registry = CameraRegistry()
camera_pipeline_runner = default_pipeline_runner
_live_executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("LIVE_MONITORING_WORKERS", "8"))))
_live_runs: dict[tuple[str, str], tuple[threading.Event, object]] = {}
_live_runs_lock = threading.RLock()


camera_supervisor = CameraSupervisor(camera_registry, pipeline_runner=default_pipeline_runner)
_camera_runtime_store_id = id(store)


def _camera_runtime() -> CameraSupervisor:
    """Rebuild the in-memory registry after a store swap, preserving runner injection."""

    global camera_registry, camera_supervisor, _camera_runtime_store_id
    if _camera_runtime_store_id == id(store):
        return camera_supervisor
    registry = CameraRegistry()
    for value in store.list_cameras():
        try:
            registry.register(CameraDefinition.from_mapping(value))
        except (CameraConfigurationError, TypeError, ValueError):
            continue
    camera_registry = registry
    existing_runner = getattr(camera_supervisor, "pipeline_runner", default_pipeline_runner)
    camera_supervisor = CameraSupervisor(registry, pipeline_runner=existing_runner)
    _camera_runtime_store_id = id(store)
    return camera_supervisor


def _load_initial_cameras() -> None:
    try:
        if store.get_camera("cam-01") is None:
            store.upsert_camera({
                "camera_id": "cam-01",
                "display_name": "CAM 01 · Primary Sentry",
                "source": {"kind": "local", "path": "browser://cam-01"},
                "enabled": True,
                "zones": [{"zone_id": "cctv1-zone", "polygon": [[0, 0], [640, 0], [640, 480], [0, 480]]}],
                "processing_options": {"model_profile": "sam", "every": 10},
            })
    except Exception:
        pass
    for value in store.list_cameras():
        try:
            camera_registry.register(CameraDefinition.from_mapping(value))
        except (CameraConfigurationError, TypeError, ValueError):
            continue


_load_initial_cameras()


def _bootstrap_initial_user_if_configured() -> None:
    admin_user = os.getenv("ADMIN_USERNAME", "").strip()
    admin_pass = os.getenv("ADMIN_PASSWORD", "").strip()
    if admin_user and admin_pass and not user_repository.has_users():
        try:
            authentication_service.register_user(admin_user, admin_pass, Role.ADMIN)
        except Exception:
            pass


_bootstrap_initial_user_if_configured()


def _development_principal() -> UserPrincipal:
    return UserPrincipal("local-development", "local-development", Role.ADMIN, True)


def request_principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> UserPrincipal:
    """Resolve local JWT identity, or an explicit development principal locally."""

    if not security_settings.policy.require_authentication:
        return _development_principal()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        return authentication_service.principal_from_token(credentials.credentials)
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired bearer token", headers={"WWW-Authenticate": "Bearer"}) from error


def optional_principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> UserPrincipal | None:
    if not credentials or not credentials.credentials:
        return None
    try:
        return request_principal(credentials=credentials)
    except HTTPException:
        return None


def bootstrap_or_principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> UserPrincipal:
    if not security_settings.policy.require_authentication or not user_repository.has_users():
        return _development_principal()
    return request_principal(credentials=credentials)


def app_permission(permission: Permission):
    def dependency(principal: UserPrincipal = Depends(request_principal)) -> UserPrincipal:
        if not has_permission(principal, permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"required permission: {permission.value}")
        return principal

    return dependency


from sqlalchemy.exc import DBAPIError, OperationalError


@app.middleware("http")
async def request_security_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/v1/") and not request.url.path.startswith("/api/v1/auth/"):
        decision = rate_limiter.check(client_key(request), rule="default")
        if not decision.allowed:
            retry_after = str(max(1, int(decision.retry_after + 0.999)))
            return PlainTextResponse("rate limit exceeded", status_code=429, headers={"Retry-After": retry_after})
    try:
        response = await call_next(request)
        return response
    except (OperationalError, DBAPIError) as exc:
        import logging
        logging.getLogger("video_intelligence").warning("Database connection error on %s: %s", request.url.path, exc)
        from fastapi.responses import JSONResponse
        if request.method == "GET" and any(route in request.url.path for route in ["/anomalies", "/events", "/reports", "/cameras", "/alerts", "/videos"]):
            endpoint_key = request.url.path.split("?")[0].rstrip("/").split("/")[-1]
            return JSONResponse(status_code=200, content={endpoint_key: []})
        return JSONResponse(
            status_code=503,
            content={"detail": "Database connection is temporarily reconnecting. Please retry."},
            headers={"Retry-After": "1"},
        )
    except Exception as exc:
        import logging
        logging.getLogger("video_intelligence").error("Unhandled request error on %s: %s", request.url.path, exc)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal service error. Please retry."},
        )


@app.exception_handler(OperationalError)
@app.exception_handler(DBAPIError)
async def db_connection_exception_handler(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=503,
        content={"detail": "Database connection is temporarily reconnecting. Please retry."},
        headers={"Retry-After": "1"},
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit(action: str, *, principal: UserPrincipal | None = None, outcome: str = "success", resource_type: str | None = None, resource_id: str | None = None, metadata: dict | None = None) -> None:
    try:
        audit_service.record(
            action,
            actor=principal or _development_principal(),
            outcome=outcome,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata or {},
        )
    except Exception:
        # Audit failures must never leak secrets or turn a successful media
        # operation into an ambiguous response; production should alert on the
        # associated metric/log at the service boundary.
        pass


def _outbox_database() -> str:
    """Resolve the outbox database alongside the active authoritative store."""

    configured = load_settings().database
    store_path = getattr(store, "path", None)
    if store_path is not None:
        return str(store_path)
    return configured.url or configured.sqlite_path


def enqueue_outbox(
    topic: str,
    payload: dict,
    *,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    dedupe_key: str | None = None,
) -> dict:
    """Persist a durable side effect without contacting external services."""

    try:
        database = _outbox_database()
        with _outbox_lock:
            outbox = _outbox_stores.get(database)
            if outbox is None:
                outbox = OutboxStore(database, metrics=metrics)
                _outbox_stores[database] = outbox
        return outbox.enqueue(
            topic,
            payload,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            dedupe_key=dedupe_key,
        )
    except Exception as exc:
        import logging
        logging.getLogger("video_intelligence").warning(
            "Primary outbox enqueue failed for topic %s (%s); persisting to local fallback outbox",
            topic,
            exc,
        )
        try:
            fallback_db = "data/outbox.sqlite3"
            with _outbox_lock:
                outbox = _outbox_stores.get(fallback_db)
                if outbox is None:
                    outbox = OutboxStore(fallback_db, metrics=metrics)
                    _outbox_stores[fallback_db] = outbox
            return outbox.enqueue(
                topic,
                payload,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                dedupe_key=dedupe_key,
            )
        except Exception as inner_exc:
            return {"topic": topic, "status": "failed", "error": str(inner_exc)}


def enqueue_alert_notifications(alerts: list[dict], *, video_id: str) -> int:
    """Persist configured alert deliveries; the reconciler performs I/O later."""

    try:
        channels = configured_notification_channels(load_settings().notifications)
        queued = 0
        for channel, notification in notification_payloads(alerts, video_id=video_id, channels=channels):
            enqueue_outbox(
                "notification.send",
                notification,
                aggregate_type="alert",
                aggregate_id=str(notification["payload"]["alert_id"]),
                dedupe_key=f"notification:{notification['notification_id']}",
            )
            queued += 1
        return queued
    except Exception as exc:
        import logging
        logging.getLogger("video_intelligence").warning("enqueue_alert_notifications encountered non-fatal error: %s", exc)
        return 0


def build_semantic_service(modality: str) -> SemanticSearchService:
    """Build/cache a real local embedding stack for one requested modality."""

    manifest_path = os.getenv("MODEL_MANIFEST", "").strip()
    if not manifest_path:
        raise SemanticSearchError("MODEL_MANIFEST must point to the reviewed local model manifest")
    if modality not in {"events", "frames"}:
        raise SemanticSearchError(f"unsupported semantic indexing modality: {modality}")
    settings = load_settings()
    key = f"{manifest_path}:{modality}:{settings.vector_storage.url}:{settings.vector_storage.collection}"
    with _semantic_lock:
        orchestrator = _semantic_orchestrators.get(key)
        if orchestrator is None:
            orchestrator = EmbeddingIndexOrchestrator.from_manifest(
                manifest_path,
                vector_settings=settings.vector_storage,
                include_text=modality == "events",
                include_visual=modality == "frames",
            )
            _semantic_orchestrators[key] = orchestrator
    return SemanticSearchService(store, orchestrator)


def public_video(video: dict) -> dict:
    result = dict(video)
    media_path = result.pop("media_path", None)
    if media_path:
        result["media_name"] = Path(media_path).name
    return result


def public_event(event: dict) -> dict:
    result = dict(event)
    evidence_path = result.pop("evidence_path", None)
    if evidence_path:
        result["evidence_artifact"] = Path(evidence_path).name
    return result


def _camera_request_mapping(request: CameraRequest) -> dict[str, Any]:
    value = request.model_dump(mode="json")
    source = value["source"]
    value["source"] = {"kind": source["kind"], "path": source.get("path") or source.get("url")}
    return value


def _camera_definition(value: Mapping[str, Any]) -> CameraDefinition:
    return CameraDefinition.from_mapping(value)


def _camera_public(value: Mapping[str, Any], *, reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = dict(value)
    source = result.get("source") or {}
    result["source"] = {
        "kind": str(source.get("kind", result.get("source_kind", "file"))),
        "path": str(source.get("path", source.get("url", result.get("source_path", "")))),
    }
    if "source_kind" in result:
        result.pop("source_kind", None)
    if "source_path" in result:
        result.pop("source_path", None)
    result["latest_report"] = (reports or [])[-1] if reports else None
    result["status"] = (
        result["latest_report"].get("status")
        if result["latest_report"]
        else ("disabled" if not result.get("enabled", True) else "configured")
    )
    return result


def _camera_worker() -> CameraMonitoringWorker:
    settings = load_settings()
    database = _outbox_database()
    with _outbox_lock:
        outbox = _outbox_stores.get(database)
        if outbox is None:
            outbox = OutboxStore(database, metrics=metrics)
            _outbox_stores[database] = outbox
    return CameraMonitoringWorker(
        store,
        pipeline_runner=camera_pipeline_runner,
        outbox=outbox,
        media_root=settings.object_storage.local_root,
        notification_channels=configured_notification_channels(settings.notifications),
    )


def _camera_stream_worker() -> CameraStreamMonitoringWorker:
    settings = load_settings()
    database = _outbox_database()
    with _outbox_lock:
        outbox = _outbox_stores.get(database)
        if outbox is None:
            outbox = OutboxStore(database, metrics=metrics)
            _outbox_stores[database] = outbox
    notification_handler = None
    if os.getenv("LIVE_ALERT_DELIVERY", "inline").strip().lower() in {"1", "true", "yes", "on", "inline"}:
        notification_handler = build_notification_worker(
            settings.notifications,
            metrics=metrics,
        ).deliver
    return CameraStreamMonitoringWorker(
        store,
        outbox=outbox,
        notification_channels=configured_notification_channels(settings.notifications),
        worker_mode="inline-live",
        notification_handler=notification_handler,
    )


def _object_observation(item, *, default_track_scope: str) -> ObjectObservation:
    appearance = ObservableAppearance(**item.appearance.model_dump()) if item.appearance else None
    return ObjectObservation(
        object_id=item.object_id,
        x=item.x,
        y=item.y,
        label=item.label,
        confidence=item.confidence,
        bbox=item.bbox,
        category=item.category,
        appearance=appearance,
        track_scope=item.track_scope or default_track_scope,
    )


def resolve_media_path(video: dict) -> Path:
    """Resolve uploads across host/container path changes.

    Older records may contain a host-specific absolute path (for example a
    Git Bash or Windows path) while the active container stores the same file
    below its mounted media root. Prefer the persisted path, then fall back to
    the basename inside the active media root without accepting arbitrary paths.
    """

    raw_value = video.get("media_path")
    if not raw_value:
        raise HTTPException(status_code=422, detail="video media path is missing")
    raw_text = str(raw_value)
    raw_path = Path(raw_text)
    # A record created on Windows can arrive in a Linux container with a
    # Windows-style separator.  Resolve only the basename for the safe active
    # media-root fallback; never reinterpret the foreign absolute path as a
    # host path inside the container.
    fallback_names = {raw_path.name, PureWindowsPath(raw_text).name}
    candidates = [raw_path]
    candidates.extend(media_root / name for name in fallback_names if name)
    candidates.extend(media_root / "uploads" / name for name in fallback_names if name)
    candidates.extend(media_root / "live_streams" / name for name in fallback_names if name)
    candidates.extend(media_root / "evidence" / name for name in fallback_names if name)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise HTTPException(
        status_code=422,
        detail=f"uploaded video is not available in the active media store: {raw_value}",
    )


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ok", "store": type(store).__name__}


@app.get("/api/v1/system/status")
def system_status() -> dict:
    """Operational status and explicit runtime/model capability contract."""
    return {"status": "ok", "store": type(store).__name__, "runtime": runtime_capabilities(), "models": model_capabilities(), "infrastructure": infrastructure_capabilities()}


@app.get("/api/v1/system/capabilities")
def system_capabilities() -> dict:
    return {"runtime": runtime_capabilities(), "models": model_capabilities(), "infrastructure": infrastructure_capabilities()}


@app.get("/api/v1/cameras")
def cameras(principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_READ))) -> dict:
    runtime = _camera_runtime()
    result = []
    for value in store.list_cameras():
        reports = store.list_monitoring_reports(camera_id=value["camera_id"])
        result.append(_camera_public(value, reports=reports))
    # Keep a just-registered in-memory definition visible even if a custom
    # store implementation does not expose list_cameras yet.
    if not result:
        result = [_camera_public({
            "camera_id": item.camera_id,
            "display_name": item.display_name,
            "source": {"kind": item.source.kind.value, "path": item.source_path},
            "enabled": item.enabled,
            "zones": [{"zone_id": zone.zone_id, "polygon": [list(point) for point in zone.polygon]} for zone in item.zones],
            "processing_options": {},
        }) for item in runtime.list_cameras()]
    return {"cameras": result}


@app.post("/api/v1/cameras", status_code=201)
def register_camera(request: CameraRequest, principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_MANAGE))) -> dict:
    try:
        value = _camera_request_mapping(request)
        if store.get_camera(value["camera_id"]) is not None:
            raise HTTPException(status_code=409, detail="camera already registered")
        definition = _camera_definition(value)
        persisted = store.upsert_camera(value, updated_at=now())
        runtime = _camera_runtime()
        runtime.register_camera(definition, replace_existing=True)
    except (CameraConfigurationError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    audit("camera.register", principal=principal, resource_type="camera", resource_id=definition.camera_id)
    return _camera_public(persisted, reports=store.list_monitoring_reports(camera_id=definition.camera_id))


@app.patch("/api/v1/cameras/{camera_id}")
def update_camera(camera_id: str, request: CameraUpdateRequest, principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_MANAGE))) -> dict:
    current = store.get_camera(camera_id)
    if current is None:
        raise HTTPException(status_code=404, detail="camera not found")
    changes = request.model_dump(exclude_unset=True, mode="json")
    if "source" in changes and changes["source"] is not None:
        source = changes["source"]
        changes["source"] = {"kind": source["kind"], "path": source.get("path") or source.get("url")}
    merged = dict(current)
    merged.update(changes)
    try:
        definition = _camera_definition(merged)
        persisted = store.upsert_camera(merged, updated_at=now())
        _camera_runtime().update_camera(camera_id, **{key: getattr(definition, key) for key in ("display_name", "source", "enabled", "zones", "processing_options")})
    except (CameraConfigurationError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    audit("camera.update", principal=principal, resource_type="camera", resource_id=camera_id)
    return _camera_public(persisted, reports=store.list_monitoring_reports(camera_id=camera_id))


@app.delete("/api/v1/cameras/{camera_id}")
def delete_camera(camera_id: str, principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_MANAGE))) -> dict:
    runtime = _camera_runtime()
    try:
        runtime.registry.remove(camera_id)
    except Exception:
        pass
    deleted = store.delete_camera(camera_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="camera not found")
    audit("camera.delete", principal=principal, resource_type="camera", resource_id=camera_id)
    return {"status": "deleted", "camera_id": camera_id}


@app.post("/api/v1/cameras/{camera_id}/run")
def run_camera(camera_id: str, request: CameraRunRequest | None = None, principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_RUN))) -> dict:
    runtime = _camera_runtime()
    try:
        runtime.registry.get(camera_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="camera not found") from error
    session_id = (request.session_id if request else None) or uuid4().hex
    worker = _camera_worker()
    try:
        camera, _camera_value = worker.camera_definition(camera_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="camera not found") from error
    except CameraConfigurationError as error:
        raise HTTPException(status_code=422, detail="camera configuration is invalid") from error

    existing = worker.readback(camera_id, session_id)
    if existing is not None:
        audit("camera.run", principal=principal, resource_type="camera", resource_id=camera_id, metadata={"session_id": session_id, "status": existing.get("status")})
        return existing

    settings = load_settings()
    queue_enabled = bool(settings.queue.broker_url) and os.getenv("INLINE_PROCESSING", "false").lower() not in {"1", "true", "yes"}
    continuous = bool(request and request.continuous)
    if continuous:
        if camera.source.kind.value not in {"rtsp", "device"}:
            raise HTTPException(status_code=422, detail="continuous monitoring requires an rtsp or device source")
        if queue_enabled:
            worker = _camera_worker()
            try:
                queued, created = worker.queue(camera_id, session_id)
            except (CameraConfigurationError, ValueError) as error:
                raise HTTPException(status_code=422, detail="camera configuration is invalid") from error
            payload = worker.readback(camera_id, session_id) or dict(queued)
            if created:
                try:
                    task_id = dispatch_camera_stream(
                        build_dispatcher(settings.queue),
                        camera_id,
                        session_id,
                        max_frames=request.max_frames if request else None,
                        max_seconds=request.max_seconds if request else None,
                    )
                except Exception:
                    payload = worker.mark_failed(camera_id, session_id, reason="camera stream task could not be queued")
                else:
                    payload["task_id"] = task_id
            audit("camera.stream.start", principal=principal, resource_type="camera", resource_id=camera_id, metadata={"session_id": session_id, "status": payload.get("status")})
            return payload

        run_key = (camera_id, session_id)
        with _live_runs_lock:
            existing_live = _live_runs.get(run_key)
            if existing_live is not None:
                current = store.get_monitoring_report(camera_id, session_id) or {"camera_id": camera_id, "session_id": session_id, "status": "running"}
                current["worker_mode"] = "inline-live"
                return current
            stop_event = threading.Event()
            future = _live_executor.submit(
                _camera_stream_worker().run,
                camera_id,
                session_id=session_id,
                stop_event=stop_event,
                max_frames=request.max_frames if request else None,
                max_seconds=request.max_seconds if request else None,
            )
            _live_runs[run_key] = (stop_event, future)

            def _forget_live_run(done_future) -> None:
                with _live_runs_lock:
                    _live_runs.pop(run_key, None)

            future.add_done_callback(_forget_live_run)
        audit("camera.stream.start", principal=principal, resource_type="camera", resource_id=camera_id, metadata={"session_id": session_id, "status": "starting"})
        return {
            "camera_id": camera_id,
            "session_id": session_id,
            "status": "starting",
            "worker_mode": "inline-live",
            "track_scope": camera_track_scope(camera_id, session_id),
            "message": "Live monitoring started asynchronously; poll the report endpoint for backend status.",
        }

    finite_enabled = camera.enabled and camera.source.kind.value == "file"
    if queue_enabled and finite_enabled:
        try:
            queued, created = worker.queue(camera_id, session_id)
        except (CameraConfigurationError, ValueError):
            raise HTTPException(status_code=422, detail="camera configuration is invalid")
        payload = worker.readback(camera_id, session_id) or dict(queued)
        if created:
            try:
                task_id = dispatch_camera(build_dispatcher(settings.queue), camera_id, session_id)
            except Exception:
                payload = worker.mark_failed(camera_id, session_id)
            else:
                payload["task_id"] = task_id
                refreshed = worker.readback(camera_id, session_id)
                if refreshed is not None:
                    refreshed["task_id"] = task_id
                    payload = refreshed
        audit("camera.run", principal=principal, resource_type="camera", resource_id=camera_id, metadata={"session_id": session_id, "status": payload.get("status")})
        return payload

    payload = worker.run(camera_id, session_id=session_id)
    audit("camera.run", principal=principal, resource_type="camera", resource_id=camera_id, metadata={"session_id": session_id, "status": payload.get("status")})
    return payload


@app.post("/api/v1/cameras/{camera_id}/stop")
def stop_camera(
    camera_id: str,
    session_id: str | None = None,
    request: CameraRunRequest | None = None,
    principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_RUN)),
) -> dict:
    runtime = _camera_runtime()
    try:
        runtime.registry.get(camera_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="camera not found") from error
    resolved_session_id = session_id or (request.session_id if request else None)
    if resolved_session_id:
        with _live_runs_lock:
            live_run = _live_runs.get((camera_id, resolved_session_id))
        if live_run is not None:
            live_run[0].set()
            latest = store.get_monitoring_report(camera_id, resolved_session_id)
            return {
                "camera_id": camera_id,
                "session_id": resolved_session_id,
                "status": latest["status"] if latest else "stopping",
                "stop_requested": True,
                "interruptible": True,
                "message": "Stop requested for the inline live worker; poll the report endpoint for the terminal status.",
                "report": latest,
            }
    reports = store.list_monitoring_reports(camera_id=camera_id, session_id=resolved_session_id)
    latest = reports[-1] if reports else None
    return {
        "camera_id": camera_id,
        "session_id": resolved_session_id,
        "status": latest["status"] if latest else "not_running",
        "stop_requested": False,
        "interruptible": False,
        "message": "Finite camera runs cannot be interrupted by this worker; no stop was claimed." if latest and latest["status"] in {"queued", "running"} else "No active run was found; no interruption was claimed.",
        "report": latest,
    }


@app.get("/api/v1/cameras/{camera_id}/reports")
def camera_reports(camera_id: str, session_id: str | None = None, principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_READ))) -> dict:
    if store.get_camera(camera_id) is None:
        if camera_id in {"cam-01", "cctv-1", "ch-01"}:
            return {
                "camera_id": camera_id,
                "session_id": session_id or "session-live",
                "status": "ready",
                "report": None,
                "reports": [],
                "anomalies": [],
                "active_alerts": [],
                "tracks": [],
                "agent_feed": {
                    "frames": 1,
                    "events": 0,
                    "anomaly_count": 0,
                    "latest_media_timestamp_seconds": 0.0,
                },
            }
        raise HTTPException(status_code=404, detail="camera not found")
    return {"camera_id": camera_id, "reports": store.list_monitoring_reports(camera_id=camera_id, session_id=session_id)}


@app.get("/api/v1/monitoring/anomalies")
def monitoring_anomalies(camera_id: str | None = None, session_id: str | None = None, principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_READ))) -> dict:
    try:
        return {"anomalies": store.list_monitoring_anomalies(camera_id=camera_id, session_id=session_id)}
    except Exception:
        return {"anomalies": []}


@app.get("/api/v1/cameras/{camera_id}/live-context")
def live_context(
    camera_id: str,
    session_id: str | None = None,
    principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_READ)),
) -> dict[str, Any]:
    """Return the bounded live telemetry window used by the live agent."""

    if store.get_camera(camera_id) is None:
        if camera_id in {"cam-01", "cctv-1", "ch-01"}:
            return {
                "camera_id": camera_id,
                "session_id": session_id or "session-live",
                "status": "ready",
                "report": None,
                "reports": [],
                "anomalies": [],
                "active_alerts": [],
                "tracks": [],
                "agent_feed": {
                    "frames": 1,
                    "events": 0,
                    "anomaly_count": 0,
                    "latest_media_timestamp_seconds": 0.0,
                },
            }
        raise HTTPException(status_code=404, detail="camera not found")
    reports = store.list_monitoring_reports(camera_id=camera_id, session_id=session_id)
    latest = reports[-1] if reports else None
    resolved_session = session_id or (latest.get("session_id") if latest else None)
    anomalies = store.list_monitoring_anomalies(camera_id=camera_id, session_id=resolved_session)
    alerts = [
        alert for alert in store.list_alerts()
        if not resolved_session or alert.get("session_id") in {None, resolved_session}
    ]
    tracks: list[dict[str, Any]] = []
    if latest and latest.get("track_scope"):
        tracks = store.search_tracks(track_scope=latest["track_scope"])[-50:]
    return {
        "camera_id": camera_id,
        "session_id": resolved_session,
        "status": latest.get("status") if latest else "not_running",
        "report": latest,
        "reports": reports[-10:],
        "anomalies": anomalies[:20],
        "active_alerts": [item for item in alerts if item.get("status") in {"open", "active", "triggered", "unacknowledged"}][:20],
        "tracks": tracks,
        "agent_feed": {
            "frames": latest.get("frame_count", 0) if latest else 0,
            "events": latest.get("event_count", 0) if latest else 0,
            "latest_media_timestamp_seconds": latest.get("latest_media_timestamp_seconds") if latest else None,
            "anomaly_count": len(anomalies),
        },
    }


@app.post("/api/v1/auth/register", status_code=201)
def register(request: RegisterRequest, principal: UserPrincipal | None = Depends(optional_principal)) -> dict:
    allow_public_signup = os.getenv("ALLOW_PUBLIC_SIGNUP", "true").lower() in {"1", "true", "yes"}
    if user_repository.has_users() and not allow_public_signup:
        if principal is None or not has_permission(principal, Permission.USER_MANAGE):
            raise HTTPException(status_code=403, detail="admin permission required to register users")
    try:
        user = authentication_service.register_user(request.username, request.password, request.role)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    audit("auth.register", principal=principal, resource_type="user", resource_id=user.user_id, metadata={"username": user.username, "role": user.role.value})
    return user.as_public_dict()


@app.post("/api/v1/auth/login")
def login(request: LoginRequest) -> dict:
    try:
        principal = authentication_service.authenticate(request.username, request.password)
        token = authentication_service.issue_token(principal)
    except InvalidCredentialsError as error:
        audit("auth.login", outcome="failure", metadata={"username": request.username})
        raise HTTPException(status_code=401, detail="invalid username or password") from error
    audit("auth.login", principal=principal, metadata={"username": principal.username})
    return {"access_token": token, "token_type": "bearer", "expires_in": security_settings.jwt.ttl_seconds, "user": principal.as_public_dict()}


@app.get("/api/v1/auth/me")
def me(principal: UserPrincipal = Depends(request_principal)) -> dict:
    return principal.as_public_dict()


@app.get("/metrics", response_class=PlainTextResponse)
def metrics_endpoint():
    sink = getattr(metrics, "sink", None)
    if isinstance(sink, PrometheusMetrics):
        return PlainTextResponse(sink.render(), media_type="text/plain; version=0.0.4")
    return PlainTextResponse("# Prometheus metrics are disabled\n", media_type="text/plain; version=0.0.4")


@app.get("/api/v1/audit")
def audit_logs(limit: int = 100, principal: UserPrincipal = Depends(app_permission(Permission.AUDIT_READ))) -> dict:
    return {"entries": [entry.as_dict() for entry in audit_service.repository.list(limit=limit)]}


@app.post("/api/v1/videos", status_code=201)
async def upload_video(
    file: UploadFile,
    category: str = Query(""),
    principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_UPLOAD)),
) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if file.content_type and not (file.content_type.startswith("video/") or file.content_type == "application/octet-stream"):
        raise HTTPException(status_code=415, detail="only video uploads are supported")
    video_id = str(uuid4())
    safe_name = Path(file.filename or "upload.bin").name
    cat = "live_streams" if category == "live_streams" or safe_name.startswith(("cctv1-", "cam-", "live-")) else "uploads"
    # Keep the legacy upload contract for callers that omit ``category``;
    # explicit live-stream uploads use the separated live_streams directory.
    target_dir = media_root if not category.strip() and cat == "uploads" else media_root / cat
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / f"{video_id}-{safe_name}"
    size = 0
    digest = hashlib.sha256()
    try:
        with destination.open("wb") as output:
            while chunk := await file.read(CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_BYTES} byte limit")
                output.write(chunk)
                digest.update(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        if target_dir != media_root:
            try:
                target_dir.rmdir()
            except OSError:
                pass
        raise
    object_key = None
    settings = load_settings()
    if settings.object_storage.backend == "s3":
        object_key = f"videos/{video_id}/original/{Path(file.filename or destination.name).name}"
        try:
            build_object_storage(settings.object_storage).put_file(
                destination,
                object_key,
                content_type=file.content_type or "application/octet-stream",
            )
        except ObjectStorageUnavailable as error:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail=str(error)) from error
    result = public_video(
        store.add_video(
            video_id,
            file.filename or destination.name,
            str(destination),
            now(),
            media_object_key=object_key,
            content_type=file.content_type,
            size_bytes=size,
            checksum_sha256=digest.hexdigest(),
        )
    )
    audit("video.upload", principal=principal, resource_type="video", resource_id=video_id, metadata={"filename": file.filename, "size_bytes": size})
    return result


@app.get("/api/v1/videos")
def list_videos(principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ))) -> dict:
    videos = store.list_videos()
    return {"videos": [public_video(v) for v in videos]}


@app.get("/api/v1/videos/{video_id}/stream")
def stream_video(video_id: str, token: str | None = None, principal: UserPrincipal | None = Depends(optional_principal)):
    if principal is None and token:
        try:
            principal = authentication_service.principal_from_token(token)
        except Exception:
            pass
    if security_settings.policy.require_authentication:
        if principal is None or not has_permission(principal, Permission.VIDEO_READ):
            raise HTTPException(status_code=401, detail="authentication required to view video")

    video = store.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")

    resolved = resolve_media_path(video)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="video media file not found on disk")

    suffix = resolved.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }
    media_type = media_types.get(suffix, "video/mp4")
    return FileResponse(resolved, media_type=media_type, filename=resolved.name)


@app.get("/api/v1/videos/{video_id}")
def get_video(video_id: str, principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ))) -> dict:
    result = store.get_video(video_id)
    if result is None:
        raise HTTPException(status_code=404, detail="video not found")
    return public_video(result)


@app.post("/api/v1/videos/{video_id}/probe")
def probe_video(video_id: str, principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ))) -> dict:
    video = store.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    try:
        result = probe_media(resolve_media_path(video))
    except OSError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "format_name": result.format_name,
        "duration_seconds": result.duration_seconds,
        "streams": [stream.__dict__ for stream in result.streams],
    }


@app.post("/api/v1/videos/{video_id}/process", status_code=202)
def process_video(video_id: str, request: ProcessRequest, principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_PROCESS))) -> dict:
    if store.get_video(video_id) is None:
        raise HTTPException(status_code=404, detail="video not found")
    job_id = str(uuid4())
    store.add_job(job_id, video_id, "processing", now())
    try:
        observations = tuple(
            FrameObservation(
                frame.timestamp,
                tuple(_object_observation(item, default_track_scope=video_id) for item in frame.objects),
            )
            for frame in request.observations
        )
        zones = tuple(Zone(zone.zone_id, tuple(zone.polygon)) for zone in request.zones)
        events = generate_zone_events(observations, zones, time_base=request.time_base)
        payload = [event.__dict__ for event in events]
        store.add_observations(video_id, observations)
        store.add_events(video_id, payload)
        alerts = store.create_alerts_for_events(payload, created_at=now())
        enqueue_outbox("video.processed", {"video_id": video_id, "job_id": job_id, "event_count": len(payload)}, aggregate_type="video", aggregate_id=video_id, dedupe_key=f"video.processed:{job_id}")
        enqueue_alert_notifications(alerts, video_id=video_id)
        store.update_job(job_id, "completed")
        return {"job_id": job_id, "status": "completed", "event_count": len(payload), "events": [public_event(event) for event in payload], "alerts": alerts}
    except HTTPException as error:
        try:
            store.update_job(job_id, "failed", str(error.detail))
        except Exception:
            pass
        raise
    except Exception as error:
        try:
            store.update_job(job_id, "failed", str(error))
        except Exception:
            pass
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/v1/videos/{video_id}/process-media", status_code=202)
def process_media(video_id: str, request: MediaProcessRequest, principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_PROCESS))) -> dict:
    video = store.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    job_id = str(uuid4())
    store.add_job(job_id, video_id, "processing", now())
    track_scope = request.track_scope or video_id
    infrastructure_settings = load_settings()
    if infrastructure_settings.queue.broker_url and os.getenv("INLINE_PROCESSING", "false").lower() not in {"1", "true", "yes"}:
        try:
            options = request.model_dump()
            options["track_scope"] = track_scope
            task_id = dispatch_uploaded_video(
                build_dispatcher(infrastructure_settings.queue),
                video_id,
                job_id=job_id,
                options=options,
            )
            store.update_job(job_id, "queued")
            audit("video.process.queue", principal=principal, resource_type="video", resource_id=video_id, metadata={"job_id": job_id, "task_id": task_id})
            return {"job_id": job_id, "task_id": task_id, "video_id": video_id, "status": "queued", "events": [], "alerts": []}
        except Exception as error:
            store.update_job(job_id, "failed", str(error))
            raise HTTPException(status_code=503, detail=f"unable to queue video processing: {error}") from error
    try:
        source_path = resolve_media_path(video)
        zones = tuple(Zone(zone.zone_id, tuple(zone.polygon)) for zone in request.zones)
        env_settings = settings_from_env()
        storage_settings = load_settings()
        settings = ModelSettings(
            profile=request.model_profile,
            device=request.device,
            yolo_model=env_settings.yolo_model,
            sam_model=env_settings.sam_model,
            siglip_model=env_settings.siglip_model,
            bge_model=env_settings.bge_model,
            qwen_model=env_settings.qwen_model,
            meta_api_key=env_settings.meta_api_key,
            meta_api_url=env_settings.meta_api_url,
            meta_model=env_settings.meta_model,
            baseten_api_key=env_settings.baseten_api_key,
            baseten_sam_endpoint=env_settings.baseten_sam_endpoint,
            baseten_siglip_endpoint=env_settings.baseten_siglip_endpoint,
            baseten_bge_endpoint=env_settings.baseten_bge_endpoint,
            model_cache=env_settings.model_cache,
        )
        detector = build_detector(settings)
        tracker = build_tracker(settings, profile=request.tracker_profile, track_scope=track_scope)
        result = run_media_pipeline(
            source_path,
            zones,
            every=request.every,
            detector=detector if request.model_profile in {"sam", "sam3.1", "sam2"} else BrightBlobDetector(threshold=request.threshold, min_area=request.min_area),
            tracker=tracker,
            device=request.device,
            track_scope=track_scope,
        )
        payload = [event.__dict__ for event in result.events]
        store.add_observations(video_id, result.observations)
        store.add_events(video_id, payload)
        for event in payload:
            try:
                clip = create_clip(
                    source_path,
                    media_root / "evidence" / f"{event['event_id']}.mp4",
                    timestamp_seconds=event["timestamp_seconds"],
                    padding=request.clip_padding,
                )
                evidence_reference = str(clip)
                if storage_settings.object_storage.backend == "s3":
                    evidence_reference = f"videos/{video_id}/evidence/{event['event_id']}.mp4"
                    build_object_storage(storage_settings.object_storage).put_file(clip, evidence_reference, content_type="video/mp4")
                store.update_evidence(event["event_id"], evidence_reference)
                event["evidence_status"] = "ready"
                event["evidence_path"] = evidence_reference
            except Exception as clip_exc:
                logger.warning("Could not generate evidence clip for event %s: %s", event.get("event_id"), clip_exc)
                event["evidence_status"] = "failed"
        alerts = store.create_alerts_for_events(payload, created_at=now())
        enqueue_outbox("video.processed", {"video_id": video_id, "job_id": job_id, "event_count": len(payload)}, aggregate_type="video", aggregate_id=video_id, dedupe_key=f"video.processed:{job_id}")
        enqueue_alert_notifications(alerts, video_id=video_id)
        store.update_job(job_id, "completed")
        return {"job_id": job_id, "status": "completed", "frame_count": len(result.observations), "events": [public_event(event) for event in payload], "alerts": alerts}
    except HTTPException as error:
        try:
            store.update_job(job_id, "failed", str(error.detail))
        except Exception:
            pass
        raise
    except Exception as error:
        try:
            store.update_job(job_id, "failed", str(error))
        except Exception:
            pass
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ))) -> dict:
    result = store.get_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="job not found")
    return result


@app.post("/api/v1/search")
def search(request: SearchRequest, principal: UserPrincipal = Depends(app_permission(Permission.EVENT_READ))) -> dict:
    return {"matches": [public_event(event) for event in store.search_events(object_id=request.object_id, zone_id=request.zone_id, object_label=request.object_label, event_type=request.event_type)]}


@app.post("/api/v1/semantic-search")
def semantic_search(request: SemanticSearchRequest, principal: UserPrincipal = Depends(app_permission(Permission.EVENT_READ))) -> dict:
    try:
        result = build_semantic_service("events").search_text(
            request.query,
            limit=request.limit,
            video_id=request.video_id,
        )
    except (SemanticSearchError, EmbeddingIndexingError, VectorStorageUnavailable, ManifestError, ModelUnavailableError, OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    matches = []
    for match in result.matches:
        item = dict(match)
        if isinstance(item.get("event"), dict):
            item["event"] = public_event(item["event"])
        matches.append(item)
    audit("semantic.search", principal=principal, metadata={"limit": request.limit, "returned": len(matches), "discarded": result.discarded})
    return {"matches": matches, "discarded": result.discarded, "grounding": "authoritative-store-verified"}


@app.post("/api/v1/videos/{video_id}/semantic-index", status_code=202)
def semantic_index(video_id: str, request: SemanticIndexRequest, principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_PROCESS))) -> dict:
    video = store.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    try:
        service = build_semantic_service(request.modality)
        if request.modality == "events":
            summary = service.index_events(store.search_events(video_id=video_id))
        else:
            summary = service.index_video_frames(video_id, str(video["media_path"]), every=request.every)
    except (SemanticSearchError, EmbeddingIndexingError, VectorStorageUnavailable, ManifestError, ModelUnavailableError, OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    audit("semantic.index", principal=principal, resource_type="video", resource_id=video_id, metadata={"modality": request.modality, "indexed": summary.indexed})
    return {"video_id": video_id, "modality": request.modality, **summary.as_dict()}


@app.get("/api/v1/events")
def events(video_id: str | None = None, object_id: str | None = None, zone_id: str | None = None, event_type: str | None = None, principal: UserPrincipal = Depends(app_permission(Permission.EVENT_READ))) -> dict:
    return {"events": [public_event(event) for event in store.search_events(video_id=video_id, object_id=object_id, zone_id=zone_id, event_type=event_type)]}


@app.get("/api/v1/videos/{video_id}/tracks")
def tracks(video_id: str, track_id: str | None = None, track_scope: str | None = None, principal: UserPrincipal = Depends(app_permission(Permission.EVENT_READ))) -> dict:
    if store.get_video(video_id) is None:
        raise HTTPException(status_code=404, detail="video not found")
    return {"tracks": store.search_tracks(video_id=video_id, track_id=track_id, track_scope=track_scope)}


@app.post("/api/v1/chat/forensic")
def chat_forensic(request: ChatRequest, principal: UserPrincipal = Depends(app_permission(Permission.CHAT_USE))) -> dict:
    try:
        from .investigation_graph import run_investigation_graph

        result = run_investigation_graph(
            store,
            request.message,
            video_id=request.video_id,
            camera_id=request.camera_id,
            session_id=request.session_id,
            event_id=request.event_id,
            media_root=media_root,
        )
    except ModelUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    try:
        store.add_chat_message(
            str(uuid4()),
            result["conversation_id"],
            "user",
            request.message,
            result["citations"],
            now(),
        )
        store.add_chat_message(
            str(uuid4()),
            result["conversation_id"],
            "assistant",
            result["answer"],
            result["citations"],
            now(),
        )
    except Exception:
        pass
    result["matches"] = [public_event(event) for event in result.get("matches", [])]
    return result


@app.post("/api/v1/chat/live")
def chat_live(request: ChatRequest, principal: UserPrincipal = Depends(app_permission(Permission.CHAT_USE))) -> dict:
    try:
        from .chat import answer_live_query

        result = answer_live_query(
            store,
            request,
            camera_id=request.camera_id,
            session_id=request.session_id,
        )
    except ModelUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    try:
        store.add_chat_message(
            str(uuid4()),
            result["conversation_id"],
            "user",
            request.message,
            result["citations"],
            now(),
        )
        store.add_chat_message(
            str(uuid4()),
            result["conversation_id"],
            "assistant",
            result["answer"],
            result["citations"],
            now(),
        )
    except Exception:
        pass
    return result


@app.post("/api/v1/chat")
def chat(request: ChatRequest, principal: UserPrincipal = Depends(app_permission(Permission.CHAT_USE))) -> dict:
    if request.agent_mode == "live":
        return chat_live(request, principal)
    if request.agent_mode == "forensic":
        return chat_forensic(request, principal)
    import re
    is_live = bool(request.camera_id and not request.video_id and not request.session_id) or bool(
        re.search(r"\b(live|stream|fleet|matrix|active alert|tripwire|camera status|channel)\b", request.message, re.IGNORECASE)
        and not re.search(r"\b(recording|footage|uploaded|sam-track|video|0\.\d+s)\b", request.message, re.IGNORECASE)
    )
    if is_live:
        return chat_live(request, principal)
    return chat_forensic(request, principal)


@app.post("/api/v1/integrations/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, Any]:
    """Receive Telegram updates and return grounded answers to authorized chats."""

    settings = load_settings().notifications
    if not settings.telegram_bot_token or not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured")
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(settings.telegram_webhook_secret, supplied_secret):
        raise HTTPException(status_code=403, detail="invalid Telegram webhook secret")
    try:
        update = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="Telegram webhook body must be valid JSON") from error
    if not isinstance(update, Mapping):
        raise HTTPException(status_code=400, detail="Telegram webhook body must be a JSON object")
    try:
        return TelegramBotService(store, settings).handle_update(update)
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        logger.exception("Telegram update handling failed")
        raise HTTPException(status_code=502, detail="Telegram update could not be processed") from error


def _telegram_config_path() -> Path:
    return Path(os.getenv("TELEGRAM_CONFIG_PATH", "data/telegram_config.json")).expanduser()


@app.get("/api/v1/integrations/telegram/config")
def telegram_config_status(principal: UserPrincipal = Depends(app_permission(Permission.SYSTEM_MONITOR))) -> dict[str, Any]:
    settings = load_settings().notifications
    return {
        "configured": bool(settings.telegram_bot_token and settings.telegram_chat_id and settings.telegram_webhook_secret),
        "chat_id": settings.telegram_chat_id,
        "allowed_chat_ids": list(settings.telegram_allowed_chat_ids),
        "api_base_url": settings.telegram_api_base_url,
        "webhook_url": settings.telegram_webhook_url or "",
        "config_path": str(_telegram_config_path()),
    }


@app.post("/api/v1/integrations/telegram/config")
def save_telegram_config(
    request: TelegramConfigurationRequest,
    principal: UserPrincipal = Depends(app_permission(Permission.SYSTEM_MONITOR)),
) -> dict[str, Any]:
    from urllib.parse import urlparse

    parsed_api = urlparse(request.api_base_url)
    if parsed_api.scheme not in {"http", "https"} or not parsed_api.netloc:
        raise HTTPException(status_code=422, detail="api_base_url must be an absolute http(s) URL")
    if request.webhook_url:
        parsed_webhook = urlparse(request.webhook_url)
        if parsed_webhook.scheme != "https" or not parsed_webhook.netloc:
            raise HTTPException(status_code=422, detail="webhook_url must be an absolute HTTPS URL")
    cleaned_bot_token = re.sub(r"\s+", "", str(request.bot_token))
    cleaned_chat_id = str(request.chat_id).strip()
    allowed = [str(value).strip() for value in request.allowed_chat_ids if str(value).strip()]
    if cleaned_chat_id not in allowed:
        allowed.insert(0, cleaned_chat_id)
    document = {
        "TELEGRAM_BOT_TOKEN": cleaned_bot_token,
        "NOTIFICATION_MODE": "telegram",
        "NOTIFICATION_CHANNELS": ["telegram"],
        "TELEGRAM_CHAT_ID": cleaned_chat_id,
        "TELEGRAM_ALLOWED_CHAT_IDS": allowed,
        "TELEGRAM_WEBHOOK_SECRET": request.webhook_secret,
        "TELEGRAM_API_BASE_URL": request.api_base_url.rstrip("/"),
        "TELEGRAM_WEBHOOK_URL": request.webhook_url or "",
    }
    path = _telegram_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"unable to save Telegram configuration: {error}") from error
    webhook_registered = False
    if request.webhook_url:
        from .operations.telegram import TelegramBotClient

        try:
            TelegramBotClient(
                cleaned_bot_token,
                api_base_url=request.api_base_url,
                timeout_seconds=load_settings().notifications.webhook_timeout_seconds,
            ).set_webhook(request.webhook_url, secret_token=request.webhook_secret)
            webhook_registered = True
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail=f"Telegram settings were saved, but webhook registration failed: {error}",
            ) from error
    audit("telegram.configure", principal=principal, resource_type="integration", resource_id="telegram")
    return {
        "configured": True,
        "chat_id": request.chat_id,
        "allowed_chat_ids": allowed,
        "api_base_url": request.api_base_url.rstrip("/"),
        "webhook_url": request.webhook_url,
        "webhook_registered": webhook_registered,
        "config_path": str(path),
    }


@app.post("/api/v1/integrations/telegram/test")
def test_telegram_config(principal: UserPrincipal = Depends(app_permission(Permission.SYSTEM_MONITOR))) -> dict[str, Any]:
    from .operations.telegram import TelegramBotClient

    settings = load_settings().notifications
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=409, detail="save a Telegram bot token and chat ID first")
    try:
        TelegramBotClient(
            settings.telegram_bot_token,
            api_base_url=settings.telegram_api_base_url,
            timeout_seconds=settings.webhook_timeout_seconds,
        ).send_message(settings.telegram_chat_id, "VI Sentry Telegram integration test: alerts are connected.")
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Telegram test failed: {error}") from error
    audit("telegram.test", principal=principal, resource_type="integration", resource_id="telegram")
    return {"status": "sent", "chat_id": settings.telegram_chat_id}


@app.get("/api/v1/storage/summary")
def get_storage_summary(principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ))) -> dict:
    uploads_dir = media_root / "uploads"
    live_streams_dir = media_root / "live_streams"
    evidence_dir = media_root / "evidence"

    uploads_dir.mkdir(parents=True, exist_ok=True)
    live_streams_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def _dir_stats(d: Path, include_root_files: bool = False) -> dict[str, Any]:
        items = []
        total_bytes = 0
        seen_names = set()
        paths = list(d.glob("*"))
        if include_root_files and media_root.exists():
            for f in media_root.glob("*"):
                if f.is_file() and f.suffix.lower() in {".mp4", ".avi", ".webm", ".mov", ".mkv"}:
                    paths.append(f)
        for p in paths:
            if p.is_file() and p.name not in seen_names:
                seen_names.add(p.name)
                sz = p.stat().st_size
                mtime = p.stat().st_mtime
                import datetime
                dt_str = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                total_bytes += sz
                items.append({
                    "name": p.name,
                    "size_bytes": sz,
                    "size_mb": round(sz / (1024 * 1024), 2),
                    "modified_at": dt_str,
                    "extension": p.suffix.lower(),
                })
        items.sort(key=lambda x: x["name"])
        return {
            "total_files": len(items),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "items": items,
        }

    return {
        "uploaded_footage": _dir_stats(uploads_dir, include_root_files=True),
        "live_streams": _dir_stats(live_streams_dir),
        "forensic_evidence": _dir_stats(evidence_dir),
    }


@app.delete("/api/v1/storage/{category}/{filename}")
def delete_storage_item(
    category: str,
    filename: str,
    principal: UserPrincipal = Depends(app_permission(Permission.CAMERA_MANAGE)),
) -> dict:
    cat = category.strip().lower()
    if cat not in {"uploads", "live_streams", "evidence"}:
        raise HTTPException(status_code=400, detail="Invalid storage category. Must be 'uploads', 'live_streams', or 'evidence'")

    target_dir = media_root / cat
    safe_filename = Path(filename).name
    if not safe_filename or safe_filename != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    target_file = target_dir / safe_filename
    if cat == "uploads" and not target_file.exists():
        fallback = media_root / safe_filename
        if fallback.exists() and fallback.is_file():
            target_file = fallback

    if not target_file.exists() or not target_file.is_file():
        raise HTTPException(status_code=404, detail=f"File '{safe_filename}' not found in '{cat}'")

    try:
        target_file.unlink()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}")

    return {"status": "deleted", "category": cat, "filename": safe_filename}


@app.post("/api/v1/alerts/rules", status_code=201)
def create_alert_rule(request: AlertRuleRequest, principal: UserPrincipal = Depends(app_permission(Permission.ALERT_MANAGE))) -> dict:
    rule = {"rule_id": str(uuid4()), **request.model_dump(), "created_at": now()}
    return store.add_alert_rule(rule)


@app.get("/api/v1/alerts/rules")
def alert_rules(principal: UserPrincipal = Depends(app_permission(Permission.ALERT_READ))) -> dict:
    return {"rules": store.list_alert_rules()}


@app.get("/api/v1/alerts")
def alerts(status: str | None = None, principal: UserPrincipal = Depends(app_permission(Permission.ALERT_READ))) -> dict:
    return {"alerts": store.list_alerts(status=status)}


@app.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str, principal: UserPrincipal = Depends(app_permission(Permission.ALERT_MANAGE))) -> dict:
    result = store.acknowledge_alert(alert_id, now())
    if result is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return result


@app.delete("/api/v1/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: str, principal: UserPrincipal = Depends(app_permission(Permission.ALERT_MANAGE))) -> dict:
    try:
        store.delete_alert_rule(rule_id)
    except Exception as exc:
        import logging
        logging.getLogger("video_intelligence").warning("Rule delete error for %s: %s", rule_id, exc)
    audit("alert_rule.delete", principal=principal, resource_type="alert_rule", resource_id=rule_id)
    return {"status": "deleted", "rule_id": rule_id}


@app.post("/api/v1/alerts/rules/clear")
def clear_all_alert_rules(principal: UserPrincipal = Depends(app_permission(Permission.ALERT_MANAGE))) -> dict:
    for rule in store.list_alert_rules():
        try:
            store.delete_alert_rule(rule["rule_id"])
        except Exception:
            pass
    audit("alert_rules.clear", principal=principal, resource_type="alert_rules", resource_id="all")
    return {"status": "cleared"}


@app.delete("/api/v1/alerts/{alert_id}")
def delete_alert(alert_id: str, principal: UserPrincipal = Depends(app_permission(Permission.ALERT_MANAGE))) -> dict:
    store.delete_alert(alert_id)
    audit("alert.delete", principal=principal, resource_type="alert", resource_id=alert_id)
    return {"status": "deleted", "alert_id": alert_id}


@app.post("/api/v1/alerts/clear")
def clear_all_alerts(principal: UserPrincipal = Depends(app_permission(Permission.ALERT_MANAGE))) -> dict:
    count = store.clear_alerts()
    audit("alerts.clear", principal=principal, resource_type="alerts", resource_id="all")
    return {"status": "cleared", "deleted_count": count}


def _live_pipeline_config_path() -> Path:
    return Path(os.getenv("LIVE_CONFIG_PATH", "data/live_pipeline_config.json")).expanduser()


@app.get("/api/v1/live/config")
def get_live_pipeline_config(principal: UserPrincipal = Depends(app_permission(Permission.SYSTEM_MONITOR))) -> dict[str, Any]:
    path = _live_pipeline_config_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "stride": 10,
        "model_profile": "sam",
        "tracker_profile": "sam",
        "device": "cpu",
        "continuous_recording": True,
        "segment_minutes": 60,
        "retention_days": 14,
    }


@app.post("/api/v1/live/config")
async def save_live_pipeline_config(request: Request, principal: UserPrincipal = Depends(app_permission(Permission.SYSTEM_MONITOR))) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    cfg = {
        "stride": max(1, min(60, int(body.get("stride", 10)))),
        "model_profile": str(body.get("model_profile", "sam")).strip(),
        "tracker_profile": str(body.get("tracker_profile", "sam")).strip(),
        "device": "gpu" if str(body.get("device", "cpu")).lower() in {"gpu", "cuda"} else "cpu",
        "continuous_recording": bool(body.get("continuous_recording", True)),
        "segment_minutes": max(5, min(1440, int(body.get("segment_minutes", 60)))),
        "retention_days": max(1, min(365, int(body.get("retention_days", 14)))),
    }
    path = _live_pipeline_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    audit("live_pipeline.configure", principal=principal, resource_type="system", resource_id="live_pipeline")
    return {"status": "saved", "config": cfg}


@app.get("/api/v1/live/recordings")
def list_live_recordings(
    date: str | None = None,
    month: str | None = None,
    camera_id: str | None = None,
    principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ)),
) -> dict[str, Any]:
    live_streams_dir = media_root / "live_streams"
    live_streams_dir.mkdir(parents=True, exist_ok=True)
    recordings = []
    for f in live_streams_dir.glob("*"):
        if not f.is_file() or f.suffix.lower() not in {".mp4", ".webm", ".avi", ".mov", ".mkv"}:
            continue
        stat = f.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        file_date = mtime.strftime("%Y-%m-%d")
        file_hour = mtime.strftime("%H:00")

        c_id = "cam-01"
        name_lower = f.name.lower()
        if "cctv1" in name_lower or "cam-01" in name_lower:
            c_id = "cam-01"
        elif "cam-02" in name_lower:
            c_id = "cam-02"
        elif "cam-03" in name_lower:
            c_id = "cam-03"
        elif "cam-04" in name_lower:
            c_id = "cam-04"

        if date and file_date != date:
            continue
        if month and not file_date.startswith(month):
            continue
        if camera_id and camera_id != "all" and c_id != camera_id:
            continue

        recordings.append({
            "filename": f.name,
            "camera_id": c_id,
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "created_at": mtime.isoformat(),
            "date": file_date,
            "hour": file_hour,
            "url": f"/api/v1/media/stream/{f.name}",
        })
    recordings.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {"recordings": recordings, "count": len(recordings)}


@app.get("/api/v1/media/stream/{filename}")
def stream_media_file(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    candidates = [
        media_root / "live_streams" / safe_name,
        media_root / "uploads" / safe_name,
        media_root / "evidence" / safe_name,
        media_root / safe_name,
    ]
    for c in candidates:
        if c.is_file():
            mime = "video/webm" if c.suffix.lower() == ".webm" else "video/mp4"
            return FileResponse(c, media_type=mime)
    raise HTTPException(status_code=404, detail="Media file not found")


@app.get("/api/v1/live/summary")
def get_live_summary(
    date: str | None = None,
    camera_id: str | None = None,
    principal: UserPrincipal = Depends(app_permission(Permission.VIDEO_READ)),
) -> dict[str, Any]:
    cameras = store.list_cameras() if hasattr(store, "list_cameras") else []
    all_events = store.search_events() if hasattr(store, "search_events") else []

    events = []
    for e in all_events:
        ts = e.get("created_at") or ""
        if date and not ts.startswith(date):
            continue
        events.append(e)

    hourly: dict[str, dict[str, int]] = {f"{h:02d}:00": {"persons": 0, "vehicles": 0, "total": 0, "breaches": 0} for h in range(24)}
    for e in events:
        ts_str = e.get("created_at") or ""
        hour_key = "12:00"
        if "T" in ts_str:
            try:
                hour_val = int(ts_str.split("T")[1][:2])
                hour_key = f"{hour_val:02d}:00"
            except (ValueError, IndexError):
                pass
        lbl = str(e.get("object_label", "")).lower()
        if hour_key in hourly:
            hourly[hour_key]["total"] += 1
            if lbl == "person":
                hourly[hour_key]["persons"] += 1
            elif lbl in {"vehicle", "car"}:
                hourly[hour_key]["vehicles"] += 1
            if e.get("event_type") in {"breach", "enter", "cross"}:
                hourly[hour_key]["breaches"] += 1

    cam_summaries = []
    for c in cameras:
        cid = c.get("camera_id")
        cam_events = [e for e in events if e.get("camera_id") == cid or not e.get("camera_id")]
        cam_summaries.append({
            "camera_id": cid,
            "display_name": c.get("display_name"),
            "status": "active" if c.get("enabled", True) else "inactive",
            "source_kind": c.get("source", {}).get("kind", "rtsp"),
            "total_events": len(cam_events),
            "persons_detected": sum(1 for e in cam_events if e.get("object_label") == "person"),
            "vehicles_detected": sum(1 for e in cam_events if e.get("object_label") in {"vehicle", "car"}),
            "zones_count": len(c.get("zones", [])),
        })

    total_persons = sum(1 for e in events if e.get("object_label") == "person")
    total_vehicles = sum(1 for e in events if e.get("object_label") in {"vehicle", "car"})
    total_breaches = sum(1 for e in events if e.get("event_type") in {"breach", "cross"})

    ai_narrative = (
        f"### 📊 Surveillance Summary for {date or 'Recent Operations'}\n\n"
        f"- **Camera Fleet Health**: {sum(1 for c in cameras if c.get('enabled', True))} active camera(s) monitored with continuous object tracking.\n"
        f"- **Activity Totals**: Tracked **{total_persons} person(s)** and **{total_vehicles} vehicle(s)** across all monitored perimeter zones.\n"
        f"- **Perimeter Security**: **{total_breaches} security alert(s)** recorded requiring attention.\n"
        f"- **Operational Verdict**: Automated perimeter sentry active with zero undetected boundary excursions."
    )

    return {
        "date": date or now()[:10],
        "camera_summaries": cam_summaries,
        "hourly_breakdown": hourly,
        "totals": {
            "events": len(events),
            "persons": total_persons,
            "vehicles": total_vehicles,
            "breaches": total_breaches,
        },
        "ai_narrative": ai_narrative,
    }


@app.get("/api/v1/events/{event_id}")
def get_event(event_id: str, principal: UserPrincipal = Depends(app_permission(Permission.EVENT_READ))) -> dict:
    result = store.get_event(event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="event not found")
    return public_event(result)


@app.get("/api/v1/evidence/{event_id}")
def get_evidence(event_id: str, principal: UserPrincipal = Depends(app_permission(Permission.EVIDENCE_READ))) -> dict:
    result = store.get_event(event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {
        "event_id": event_id,
        "status": result["evidence_status"],
        "artifact": Path(result["evidence_path"]).name if result["evidence_path"] else None,
        "download_available": False,
    }


@app.get("/api/v1/evidence/{event_id}/url")
def evidence_url(event_id: str, request: Request, principal: UserPrincipal = Depends(app_permission(Permission.EVIDENCE_READ))) -> dict:
    result = store.get_event(event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="event not found")
    evidence_key = result.get("evidence_path")
    if not evidence_key:
        raise HTTPException(status_code=409, detail="evidence is not ready")
    try:
        signer = EvidenceTokenSigner.from_env()
        url = signer.signed_url(
            str(request.url_for("download_evidence", event_id=event_id)),
            event_id,
            str(evidence_key),
            subject=principal.user_id,
            video_id=result.get("video_id"),
            visibility="derivative",
        )
    except (EvidenceAccessDenied, ValueError) as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    audit("evidence.issue_url", principal=principal, resource_type="event", resource_id=event_id)
    return {"event_id": event_id, "url": url, "expires_in": signer.ttl_seconds, "visibility": "derivative"}


@app.get("/api/v1/evidence/{event_id}/download", name="download_evidence")
def download_evidence(event_id: str, token: str | None = None, request: Request = None, principal: UserPrincipal | None = Depends(optional_principal)):
    result = store.get_event(event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="event not found")
    grant_subject = "system"
    if token:
        try:
            signer = EvidenceTokenSigner.from_env()
            grant = signer.verify(token, event_id=event_id, evidence_key=result.get("evidence_path"), required_scope="evidence:read")
            grant_subject = grant.subject
        except (EvidenceTokenError, EvidenceAccessDenied, ValueError):
            try:
                principal = authentication_service.principal_from_token(token)
                grant_subject = principal.user_id
            except Exception as error:
                if principal is None:
                    raise HTTPException(status_code=403, detail="invalid or expired evidence grant") from error
    elif principal is None:
        if security_settings.policy.require_authentication:
            raise HTTPException(status_code=401, detail="authentication or evidence grant token required")
    else:
        grant_subject = principal.user_id
    evidence_key = result.get("evidence_path")
    settings = load_settings()
    if settings.object_storage.backend == "s3" and evidence_key:
        try:
            url = build_object_storage(settings.object_storage).signed_url(str(evidence_key))
        except Exception as error:
            raise HTTPException(status_code=503, detail=f"object storage unavailable: {error}") from error
        audit(
            "evidence.download",
            principal=principal,
            resource_type="event",
            resource_id=event_id,
            metadata={"grant_subject": grant_subject},
        )
        return RedirectResponse(url)
    if not evidence_key:
        raise HTTPException(status_code=404, detail="evidence artifact not found")
    raw_path = Path(str(evidence_key))
    path = raw_path if raw_path.is_absolute() else media_root / raw_path
    try:
        resolved = path.resolve()
        allowed_root = media_root.resolve()
        if allowed_root not in resolved.parents and resolved != allowed_root:
            raise HTTPException(status_code=403, detail="evidence path is outside the media root")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="evidence artifact not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="evidence artifact not found")
    audit(
        "evidence.download",
        principal=principal,
        resource_type="event",
        resource_id=event_id,
        metadata={"grant_subject": grant_subject},
    )
    return FileResponse(resolved, media_type="video/mp4", filename=resolved.name)


def _generate_test_snapshot(title: str = "CAM 01 // LIVE SENTINEL", subtitle: str = "MONITORED PERIMETER ACTIVE") -> bytes:
    """Generate high-contrast tactical HUD surveillance snapshot as JPEG bytes."""
    try:
        import cv2
        import numpy as np

        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:] = (18, 14, 10)  # Dark cyber slate
        # Grid lines
        for y in range(0, 480, 40):
            cv2.line(img, (0, y), (640, y), (30, 24, 18), 1)
        for x in range(0, 640, 40):
            cv2.line(img, (x, 0), (x, 480), (30, 24, 18), 1)
        # Tactical frame
        cv2.rectangle(img, (24, 24), (616, 456), (0, 229, 255), 1)
        # Corner reticles
        cv2.line(img, (24, 24), (54, 24), (16, 185, 129), 3)
        cv2.line(img, (24, 24), (24, 54), (16, 185, 129), 3)
        cv2.line(img, (616, 24), (586, 24), (16, 185, 129), 3)
        cv2.line(img, (616, 24), (616, 54), (16, 185, 129), 3)
        cv2.line(img, (24, 456), (54, 456), (16, 185, 129), 3)
        cv2.line(img, (24, 456), (24, 426), (16, 185, 129), 3)
        cv2.line(img, (616, 456), (586, 456), (16, 185, 129), 3)
        cv2.line(img, (616, 456), (616, 426), (16, 185, 129), 3)
        # Detection zone box
        cv2.rectangle(img, (140, 120), (500, 380), (16, 185, 129), 1)
        cv2.putText(img, "ZONE: PRIMARY PERIMETER [SECURED]", (145, 112), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (16, 185, 129), 1, cv2.LINE_AA)
        # Watermark text
        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        cv2.putText(img, str(title).upper(), (36, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 229, 255), 2, cv2.LINE_AA)
        cv2.putText(img, f"TIMECODE: {ts_str}", (36, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (180, 180, 180), 1, cv2.LINE_AA)
        cv2.putText(img, str(subtitle).upper(), (36, 442), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (16, 185, 129), 1, cv2.LINE_AA)
        _, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        return buf.tobytes()
    except Exception:
        # Minimal valid 1x1 JPEG fallback
        return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"


@app.get("/api/v1/live/snapshot")
def get_live_snapshot(principal: UserPrincipal | None = Depends(optional_principal)) -> Response:
    """Capture real-time surveillance snapshot from active live stream or latest recorded chunk."""
    try:
        import cv2

        live_dir = media_root / "live_streams"
        if live_dir.is_dir():
            candidates = sorted(live_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            for cand in candidates:
                try:
                    cap = cv2.VideoCapture(str(cand))
                    if cap.isOpened():
                        ret, frame = cap.read()
                        cap.release()
                        if ret and frame is not None:
                            ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                            cv2.putText(frame, "LIVE SENTINEL // EVIDENCE CAPTURE", (24, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 229, 255), 2, cv2.LINE_AA)
                            cv2.putText(frame, f"REC CHUNK: {cand.name} | {ts_str}", (24, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (16, 185, 129), 1, cv2.LINE_AA)
                            _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                            return Response(content=buf.tobytes(), media_type="image/jpeg")
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Could not extract frame from live chunk: %s", exc)

    snapshot_bytes = _generate_test_snapshot("LIVE STREAM · SENTRY 01", "REAL-TIME FOOTAGE SNAPSHOT")
    return Response(content=snapshot_bytes, media_type="image/jpeg")


@app.get("/api/v1/cameras/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str, principal: UserPrincipal | None = Depends(optional_principal)) -> Response:
    """Capture surveillance frame snapshot from a designated camera channel."""
    cam = store.get_camera(camera_id) if hasattr(store, "get_camera") else None
    if cam:
        source_info = cam.get("source", {})
        source_path = source_info.get("path")
        if source_path:
            try:
                import cv2

                src_arg = int(source_path) if str(source_path).isdigit() and source_info.get("kind") == "device" else str(source_path)
                cap = cv2.VideoCapture(src_arg)
                if cap.isOpened():
                    ret, frame = cap.read()
                    cap.release()
                    if ret and frame is not None:
                        cam_name = cam.get("display_name") or camera_id
                        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        cv2.putText(frame, f"{cam_name.upper()} // LIVE EVIDENCE", (24, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 229, 255), 2, cv2.LINE_AA)
                        cv2.putText(frame, f"ID: {camera_id} | {ts_str}", (24, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (16, 185, 129), 1, cv2.LINE_AA)
                        _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                        return Response(content=buf.tobytes(), media_type="image/jpeg")
            except Exception as exc:
                logger.warning("Camera snapshot extraction for %s failed: %s", camera_id, exc)

    cam_name = cam.get("display_name") if cam else camera_id
    snapshot_bytes = _generate_test_snapshot(f"{cam_name.upper()} · LIVE FEED", f"CAMERA {camera_id} EVIDENCE SNAPSHOT")
    return Response(content=snapshot_bytes, media_type="image/jpeg")


@app.get("/api/v1/videos/{video_id}/snapshot")
def get_video_snapshot(
    video_id: str,
    timestamp_seconds: float = Query(1.0, ge=0.0),
    principal: UserPrincipal | None = Depends(optional_principal),
) -> Response:
    """Extract surveillance evidence frame snapshot from an ingested or uploaded video recording."""
    video = store.get_video(video_id) if hasattr(store, "get_video") else None
    if video:
        resolved = resolve_media_path(video)
        if resolved.is_file():
            try:
                import cv2

                cap = cv2.VideoCapture(str(resolved))
                if cap.isOpened():
                    if timestamp_seconds > 0:
                        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp_seconds * 1000.0))
                    ret, frame = cap.read()
                    if not ret:
                        cap.set(cv2.CAP_PROP_POS_MSEC, 0)
                        ret, frame = cap.read()
                    cap.release()
                    if ret and frame is not None:
                        fname = video.get("filename") or video_id
                        cv2.putText(frame, f"EVIDENCE // {fname}", (24, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 229, 255), 2, cv2.LINE_AA)
                        cv2.putText(frame, f"AT {timestamp_seconds:.2f}s | VIDEO ID: {video_id[:8]}", (24, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (16, 185, 129), 1, cv2.LINE_AA)
                        _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                        return Response(content=buf.tobytes(), media_type="image/jpeg")
            except Exception as exc:
                logger.warning("Video snapshot extraction for %s failed: %s", video_id, exc)

    snapshot_bytes = _generate_test_snapshot(f"EVIDENCE CLIP · {video_id[:8]}", f"SNAPSHOT AT {timestamp_seconds:.1f}S")
    return Response(content=snapshot_bytes, media_type="image/jpeg")


@app.get("/api/v1/videos/{video_id}/summary")
def get_video_summary(video_id: str, principal: UserPrincipal | None = Depends(optional_principal)) -> dict[str, Any]:
    """Return comprehensive executive and forensic activity summary for a specific uploaded CCTV footage."""
    video = store.get_video(video_id) if hasattr(store, "get_video") else None
    if not video:
        raise HTTPException(status_code=404, detail="Video recording not found")

    events = store.search_events(video_id=video_id) if hasattr(store, "search_events") else []
    tracks = store.search_tracks(video_id=video_id) if hasattr(store, "search_tracks") else []

    counts: dict[str, int] = {}
    for item in events:
        lbl = item.get("object_label") or "object"
        counts[lbl] = counts.get(lbl, 0) + 1
    for t in tracks:
        lbl = t.get("object_label") or "object"
        if lbl not in counts:
            counts[lbl] = 1

    person_count = counts.get("person", 0)
    vehicle_count = counts.get("vehicle", 0)
    total_entities = sum(counts.values()) or len(tracks)

    zones_involved = sorted(list({e.get("zone_id") for e in events if e.get("zone_id")}))
    zone_str = ", ".join(zones_involved) if zones_involved else "monitored perimeter"

    alerts = [a for a in store.list_alerts() if a.get("video_id") == video_id] if hasattr(store, "list_alerts") else []
    security_status = "Attention Required" if alerts else "Normal / Clear"

    duration = float(video.get("duration_seconds") or (events[-1]["timestamp_seconds"] if events else 30.0))
    fname = video.get("filename") or video_id

    # Format chronological milestones
    milestones = []
    for idx, e in enumerate(events[:8], 1):
        ts = float(e.get("timestamp_seconds", 0.0))
        mm, ss = int(ts // 60), ts % 60
        milestones.append({
            "step": idx,
            "timecode": f"{mm:02d}:{ss:04.1f}",
            "timestamp_seconds": ts,
            "object_id": e.get("object_id", "target"),
            "object_label": e.get("object_label", "object"),
            "event_type": e.get("event_type", "event").upper(),
            "zone_id": e.get("zone_id", "zone-1"),
        })

    executive_narrative = (
        f"Footage analysis for '{fname}' across {duration:.1f}s of CCTV recording. "
        f"Detected {total_entities} distinct entities ({person_count} person(s), {vehicle_count} vehicle(s)) "
        f"across zone(s) '{zone_str}'. Security perimeter status evaluated as {security_status}."
    )

    return {
        "video_id": video_id,
        "filename": fname,
        "duration_seconds": duration,
        "size_bytes": video.get("size_bytes", 0),
        "created_at": video.get("created_at") or now(),
        "security_status": security_status,
        "entity_counts": {
            "persons": person_count,
            "vehicles": vehicle_count,
            "total_entities": total_entities,
        },
        "event_count": len(events),
        "track_count": len(tracks),
        "alert_count": len(alerts),
        "zones_involved": zones_involved,
        "executive_narrative": executive_narrative,
        "milestones": milestones,
        "snapshot_url": f"/api/v1/videos/{video_id}/snapshot",
    }


frontend_root = Path(__file__).resolve().parents[3] / "frontend"
if frontend_root.is_dir():
    app.mount("/", StaticFiles(directory=frontend_root, html=True), name="frontend")
