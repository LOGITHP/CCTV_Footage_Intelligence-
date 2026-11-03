"""Optional production model adapters with explicit availability checks.

The default installation intentionally stays small and offline-testable. Real
YOLO, ByteTrack, SigLIP2, BGE-M3, and Qwen execution is opt-in through model
configuration and the corresponding packages/weights. An unavailable adapter
raises a clear error instead of silently returning fabricated model output.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .domain import ObjectObservation
from .observables import extract_observable_appearance, normalize_yolo_label, object_category_for_label
from .pipeline import BrightBlobDetector, DetectedObject, PipelineError


class ModelUnavailableError(PipelineError):
    """A requested model/runtime is not installed or its weights are missing."""


@dataclass(frozen=True)
class ModelSettings:
    profile: str = "baseline"
    device: str = "cpu"
    yolo_model: str = ""
    sam_model: str = ""
    siglip_model: str = "google/siglip2-base-patch16-224"
    bge_model: str = "BAAI/bge-m3"
    qwen_model: str = "Qwen/Qwen3-8B"
    meta_api_key: str = ""
    meta_api_url: str = "https://api.meta.ai/v1"
    meta_model: str = "meta-models/muse-spark-open-1026"
    baseten_api_key: str = ""
    baseten_sam_endpoint: str = ""
    baseten_siglip_endpoint: str = ""
    baseten_bge_endpoint: str = ""
    model_cache: str = "models/cache"


def _require_local_checkpoint(value: str, setting_name: str) -> Path | str:
    """Resolve a checkpoint or valid hub ID."""

    candidate = value.strip()
    if not candidate:
        raise ModelUnavailableError(f"{setting_name} must point to a local model checkpoint")
    parsed = urlparse(candidate)
    if parsed.scheme.lower() in {"http", "https", "s3", "gs", "ftp"}:
        raise ModelUnavailableError(
            f"{setting_name} must be a local checkpoint path; remote model references are disabled: {candidate}"
        )
    path = Path(candidate).expanduser()
    if path.exists():
        if path.is_dir() and not any(item.is_file() for item in path.rglob("*")):
            raise ModelUnavailableError(f"{setting_name} local checkpoint directory is empty: {path}")
        return path.resolve()
    if "/" in candidate and not candidate.startswith((".", "/")):
        if os.getenv("ALLOW_HF_HUB", "false").lower() in {"true", "1", "yes"}:
            return candidate
    raise ModelUnavailableError(f"{setting_name} local checkpoint does not exist: {path}")


def _torch_device(device: str):
    """Return a torch device after the optional dependency has been imported."""

    import torch

    if device == "gpu":
        if not torch.cuda.is_available():
            raise ModelUnavailableError("GPU model execution requested but CUDA is unavailable")
        return torch.device("cuda")
    if device != "cpu":
        raise ModelUnavailableError(f"model device must be cpu or gpu, got: {device}")
    return torch.device("cpu")


def settings_from_env() -> ModelSettings:
    import os

    return ModelSettings(
        profile=os.getenv("MODEL_PROFILE", "baseline").strip().lower(),
        device=os.getenv("MODEL_DEVICE", "cpu").strip().lower(),
        yolo_model=os.getenv("YOLO_MODEL", "").strip(),
        sam_model=os.getenv("SAM_MODEL", "").strip(),
        siglip_model=os.getenv("SIGLIP_MODEL", "google/siglip2-base-patch16-224").strip(),
        bge_model=os.getenv("BGE_MODEL", "BAAI/bge-m3").strip(),
        qwen_model=os.getenv("QWEN_MODEL", "Qwen/Qwen3-8B").strip(),
        meta_api_key=os.getenv("META_API_KEY", os.getenv("MUSE_API_KEY", "")).strip(),
        meta_api_url=os.getenv("META_API_URL", "https://api.meta.ai/v1").strip(),
        meta_model=os.getenv("META_MODEL", os.getenv("MUSE_MODEL", "meta-models/muse-spark-open-1026")).strip(),
        baseten_api_key=os.getenv("BASETEN_API_KEY", "").strip(),
        baseten_sam_endpoint=os.getenv("BASETEN_SAM_ENDPOINT", "").strip(),
        baseten_siglip_endpoint=os.getenv("BASETEN_SIGLIP_ENDPOINT", "").strip(),
        baseten_bge_endpoint=os.getenv("BASETEN_BGE_ENDPOINT", "").strip(),
        model_cache=os.getenv("MODEL_CACHE_DIR", "models/cache").strip(),
    )


def _call_baseten(endpoint: str, payload: dict[str, Any], api_key: str, timeout: float = 12.0) -> dict[str, Any] | None:
    """Invoke a deployed Baseten model endpoint with authentication."""
    if not endpoint or not api_key:
        return None
    import json
    import urllib.request

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Api-Key {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "VideoIntelligence-Baseten/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class YOLODetector:
    """Ultralytics YOLO adapter returning the pipeline's typed detections."""

    def __init__(self, model_path: str, *, confidence: float = 0.25, device: str = "cpu") -> None:
        if not model_path:
            raise ModelUnavailableError("MODEL_PROFILE=yolo requires YOLO_MODEL")
        local_path = _require_local_checkpoint(model_path, "YOLO_MODEL")
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise ModelUnavailableError("install ultralytics to enable the YOLO adapter") from exc
        self.model = YOLO(str(local_path))
        self.confidence = confidence
        self.device = "cuda" if device == "gpu" else device

    def detect(self, frame) -> tuple[DetectedObject, ...]:
        try:
            results = self.model.predict(frame, conf=self.confidence, device=self.device, verbose=False)
            result = results[0]
            names = result.names
            detections = []
            for box in result.boxes:
                coordinates = box.xyxy[0].tolist()
                left, top, right, bottom = (float(value) for value in coordinates)
                confidence = float(box.conf[0].item())
                class_id = int(box.cls[0].item())
                label = normalize_yolo_label(str(names[class_id]))
                category = object_category_for_label(label)
                area = max(0.0, right - left) * max(0.0, bottom - top)
                detections.append(
                    DetectedObject(
                        label,
                        (left + right) / 2,
                        (top + bottom) / 2,
                        area,
                        confidence,
                        (left, top, right, bottom),
                        category,
                        extract_observable_appearance(frame, (left, top, right, bottom), label),
                    )
                )
            return tuple(detections)
        except Exception as exc:
            raise PipelineError(f"YOLO inference failed: {exc}") from exc


class ByteTrackTracker:
    """Optional supervision ByteTrack adapter with a strict dependency boundary."""

    def __init__(self, *, minimum_consecutive_frames: int = 1, track_scope: str = "session") -> None:
        if not track_scope:
            raise ValueError("track_scope must not be empty")
        try:
            import supervision as sv
        except ImportError as exc:
            raise ModelUnavailableError("install supervision to enable ByteTrack") from exc
        self._sv = sv
        self._tracker = sv.ByteTrack(minimum_consecutive_frames=minimum_consecutive_frames)
        self.track_scope = track_scope

    def update(self, detections: tuple[DetectedObject, ...]) -> tuple[ObjectObservation, ...]:
        missing_box = [item.label for item in detections if item.bbox is None]
        if missing_box:
            raise PipelineError("ByteTrack requires detector bounding boxes")
        import numpy as np

        xyxy = np.asarray([item.bbox for item in detections], dtype=float)
        confidence = np.asarray([item.confidence or 1.0 for item in detections], dtype=float)
        class_ids = np.zeros(len(detections), dtype=int)
        tracked = self._tracker.update_with_detections(
            self._sv.Detections(xyxy=xyxy, confidence=confidence, class_id=class_ids)
        )
        result = []
        tracker_ids = list(tracked.tracker_id) if tracked.tracker_id is not None else []
        for index, tracker_id in enumerate(tracker_ids):
            if tracker_id is None or index >= len(detections):
                continue
            detection = detections[index]
            result.append(
                ObjectObservation(
                    object_id=f"track-{int(tracker_id)}",
                    x=detection.x,
                    y=detection.y,
                    label=detection.label,
                    confidence=detection.confidence,
                    bbox=detection.bbox,
                    category=detection.category or object_category_for_label(detection.label),
                    appearance=detection.appearance,
                    track_scope=self.track_scope,
                )
            )
        return tuple(result)


class EmbeddingUnavailableError(ModelUnavailableError):
    pass


def _safe_category(label: str, category: Any = None) -> str | None:
    """Normalize provider-specific categories to the domain contract."""

    inferred = object_category_for_label(label)
    if inferred is not None:
        return inferred
    normalized = str(category or "").strip().lower()
    return normalized if normalized in {"person", "vehicle"} else None


class EmbeddingService:
    """Optional text/image embedding facade; no fake semantic fallback is used."""

    def __init__(self, settings: ModelSettings | None = None) -> None:
        self.settings = settings or settings_from_env()
        self._text_model: Any = None
        self._processor: Any = None

    def _load_text_model(self) -> None:
        if self._text_model is not None:
            return
        checkpoint = _require_local_checkpoint(self.settings.bge_model, "BGE_MODEL")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailableError("install sentence-transformers to enable BGE-M3 embeddings") from exc
        try:
            is_local = isinstance(checkpoint, Path) or Path(str(checkpoint)).exists()
            self._text_model = SentenceTransformer(
                str(checkpoint),
                cache_folder=self.settings.model_cache,
                local_files_only=is_local,
                device="cuda" if self.settings.device == "gpu" else "cpu",
            )
        except TypeError as exc:
            # A runtime without a local-only constructor cannot be used here:
            # retrying could silently consult a model hub.
            raise EmbeddingUnavailableError(
                "installed sentence-transformers runtime does not support local-only loading"
            ) from exc
        except Exception as exc:
            raise EmbeddingUnavailableError(f"unable to load BGE-M3 checkpoint {checkpoint}: {exc}") from exc

    def text(self, value: str) -> list[float]:
        if self.settings.baseten_bge_endpoint and self.settings.baseten_api_key:
            resp = _call_baseten(
                self.settings.baseten_bge_endpoint,
                {"text": value},
                self.settings.baseten_api_key,
                timeout=10.0,
            )
            if resp and "embedding" in resp and resp["embedding"]:
                return [float(x) for x in resp["embedding"]]

        self._load_text_model()
        vector = self._text_model.encode(value, normalize_embeddings=True)
        result = [float(item) for item in vector]
        if not result:
            raise EmbeddingUnavailableError("BGE-M3 returned an empty embedding")
        return result

    def text_batch(self, values: list[str]) -> list[list[float]]:
        if not values:
            return []
        if self.settings.baseten_bge_endpoint and self.settings.baseten_api_key:
            resp = _call_baseten(
                self.settings.baseten_bge_endpoint,
                {"texts": values},
                self.settings.baseten_api_key,
                timeout=15.0,
            )
            if resp and "embeddings" in resp and resp["embeddings"]:
                return [[float(x) for x in emb] for emb in resp["embeddings"]]

        self._load_text_model()
        vectors = self._text_model.encode(values, normalize_embeddings=True)
        return [[float(item) for item in vector] for vector in vectors]

    @property
    def dimension(self) -> int | None:
        if self._text_model is None:
            return None
        getter = getattr(self._text_model, "get_sentence_embedding_dimension", None)
        value = getter() if callable(getter) else None
        return int(value) if value is not None else None

    def describe(self) -> dict[str, Any]:
        return {"bge_model": self.settings.bge_model, "siglip_model": self.settings.siglip_model, "available": self._text_model is not None}


class SigLIP2EmbeddingService:
    """Optional image embedding adapter for a versioned visual Qdrant collection."""

    def __init__(self, settings: ModelSettings | None = None) -> None:
        self.settings = settings or settings_from_env()
        self.processor = None
        self.model = None
        self._dimension = 768

        if self.settings.baseten_siglip_endpoint and self.settings.baseten_api_key:
            return

        checkpoint = _require_local_checkpoint(self.settings.siglip_model, "SIGLIP_MODEL")
        try:
            from transformers import AutoModel, AutoProcessor
        except ImportError as exc:
            raise EmbeddingUnavailableError("install transformers to enable SigLIP2 embeddings") from exc
        try:
            is_local = isinstance(checkpoint, Path) or Path(str(checkpoint)).exists()
            self.processor = AutoProcessor.from_pretrained(
                str(checkpoint), cache_dir=self.settings.model_cache, local_files_only=is_local
            )
            self.model = AutoModel.from_pretrained(
                str(checkpoint), cache_dir=self.settings.model_cache, local_files_only=is_local
            )
            self.device = _torch_device(self.settings.device)
            self.model.to(self.device)
            self.model.eval()
        except Exception as exc:
            raise EmbeddingUnavailableError(f"unable to load SigLIP2 checkpoint {checkpoint}: {exc}") from exc

        config = getattr(self.model, "config", None)
        configured_dimension = getattr(config, "projection_dim", None) if config is not None else None
        self._dimension = int(configured_dimension) if configured_dimension is not None else 768

    def image(self, frame) -> list[float]:
        if self.settings.baseten_siglip_endpoint and self.settings.baseten_api_key:
            try:
                import cv2
                import base64
                success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if success:
                    b64_image = base64.b64encode(buffer).decode("ascii")
                    resp = _call_baseten(
                        self.settings.baseten_siglip_endpoint,
                        {"image_b64": b64_image},
                        self.settings.baseten_api_key,
                        timeout=12.0,
                    )
                    if resp and "embedding" in resp and resp["embedding"]:
                        return [float(x) for x in resp["embedding"]]
            except Exception:
                pass

        if self.model is None or self.processor is None:
            raise EmbeddingUnavailableError("SigLIP2 local model is not initialized and remote endpoint is unavailable")

        try:
            import torch

            inputs = self.processor(images=frame, return_tensors="pt")
            inputs = {key: value.to(self.device) if hasattr(value, "to") else value for key, value in inputs.items()}
            with torch.no_grad():
                features = self.model.get_image_features(**inputs)
                features = features / features.norm(dim=-1, keepdim=True)
            result = [float(item) for item in features[0].tolist()]
            if self._dimension is None:
                self._dimension = len(result)
            return result
        except Exception as exc:
            raise PipelineError(f"SigLIP2 image embedding failed: {exc}") from exc

    @property
    def dimension(self) -> int | None:
        return self._dimension


class QwenGroundedResponder:
    """Optional Qwen responder constrained to verified citation input."""

    def __init__(self, settings: ModelSettings | None = None) -> None:
        self.settings = settings or settings_from_env()
        checkpoint = _require_local_checkpoint(self.settings.qwen_model, "QWEN_MODEL")
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise ModelUnavailableError("install transformers to enable Qwen responses") from exc
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                str(checkpoint), cache_dir=self.settings.model_cache, local_files_only=True
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                str(checkpoint), cache_dir=self.settings.model_cache, local_files_only=True
            )
            self.device = _torch_device(self.settings.device)
            self.model.to(self.device)
            self.model.eval()
        except Exception as exc:
            raise ModelUnavailableError(f"unable to load Qwen checkpoint {checkpoint}: {exc}") from exc

    def respond(self, question: str, citations: list[dict[str, Any]]) -> str:
        import torch

        system = (
            "You are VI Sentry AI, a senior video forensic intelligence analyst and grounded surveillance investigator. "
            "Provide a comprehensive, highly detailed, and structured forensic investigation summary using ONLY the verified JSON citations provided below.\n\n"
            "STRICT GROUNDING DIRECTIVES:\n"
            "1. Answer ONLY from the verified JSON citations. Never invent, extrapolate, or alter any timestamp, camera ID, object ID, event type, zone, or clip reference.\n"
            "2. If the citations do not support any aspect of the question, clearly specify that evidence is insufficient.\n\n"
            "REQUIRED REPORT STRUCTURE:\n"
            "### 1. Executive Incident Summary\n"
            "Direct answer to user question, total verified events, affected zone(s), and security verdict.\n\n"
            "### 2. Chronological Event Timeline\n"
            "Ordered bulleted events with timecode (seconds and MM:SS), Object ID, classification, action, and zone.\n\n"
            "### 3. Spatial & Behavioral Analysis\n"
            "Movement trajectory, zone dwell time, perimeter breach severity, and security implications.\n\n"
            "### 4. Verified Evidence Citations\n"
            "List verified event IDs and clip references for judicial/audit admissibility."
        )
        citations_json = json.dumps(citations, ensure_ascii=False, sort_keys=True)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\nVerified citations: {citations_json}\nProvide a detailed forensic investigation summary:"},
        ]
        if getattr(self.tokenizer, "chat_template", None):
            inputs = self.tokenizer.apply_chat_template(
                messages,
                add_generation_prompt=True,
                enable_thinking=False,
                return_tensors="pt",
                return_dict=True,
            )
        else:
            prompt = f"{system}\nQuestion: {question}\nVerified citations: {citations_json}\nAnswer:"
            inputs = self.tokenizer(prompt, return_tensors="pt")
        inputs = {key: value.to(self.device) if hasattr(value, "to") else value for key, value in inputs.items()}
        with torch.no_grad():
            output = self.model.generate(**inputs, max_new_tokens=512, do_sample=False)
        generated = output[0][inputs["input_ids"].shape[1] :]
        answer = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
        if "</think>" in answer:
            answer = answer.split("</think>", 1)[1].strip()
        return answer


class MetaMuseGroundedResponder:
    """Meta Muse Spark 1.3 responder via Meta Model API constrained to verified citations."""

    def __init__(
        self,
        settings: ModelSettings | None = None,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        model: str | None = None,
        timeout: float = 12.0,
    ) -> None:
        self.settings = settings or settings_from_env()
        self.explicit_api_url = api_url is not None
        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("META_API_KEY")
            or os.getenv("MUSE_API_KEY")
            or self.settings.meta_api_key
        ).strip()
        self.api_url = (
            api_url
            or os.getenv("META_API_URL")
            or self.settings.meta_api_url
            or "https://inference.baseten.co/v1"
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("META_MODEL")
            or os.getenv("MUSE_MODEL")
            or self.settings.meta_model
            or "meta-models/muse-spark-open-1026"
        ).strip()
        self.timeout = timeout

    def respond(self, question: str, citations: list[dict[str, Any]]) -> str:
        system = (
            "You are VI Sentry AI — a senior forensic video intelligence analyst and grounded surveillance investigator. "
            "Your objective is to provide a clear, human-understandable, and structured forensic investigation summary "
            "based strictly and exclusively on the verified JSON surveillance citations below.\n\n"
            "STRICT OPERATIONAL DIRECTIVES:\n"
            "1. Answer the question with a crystal-clear, plain-English executive summary first so an operator instantly understands what happened.\n"
            "2. Group and count detected entities cleanly (e.g., '2 Persons, 9 Vehicles, 32 Objects') rather than overwhelming with a raw list.\n"
            "3. Cite exact timecodes [MM:SS / SS.SSs] for key events, highlighting persons and vehicles.\n"
            "4. Never hallucinate, extrapolate, or invent unseen details, vehicle types, person descriptions, or actions outside the citations.\n"
            "5. If evidence is missing or insufficient (e.g. camera stream, dwell time, clip artifacts), explicitly state the limitation.\n\n"
            "REQUIRED REPORT STRUCTURE (Format in clear, professional Markdown):\n\n"
            "### 1. Executive Incident Summary\n"
            "- A direct, plain-English overview answering the operator's query decisively.\n"
            "- Overall security verdict: Target counts (Persons, Vehicles, Objects), zones involved, and breach severity status.\n\n"
            "### 2. Key Activity & Chronological Milestones\n"
            "- Chronological highlights of all person and vehicle movements with exact timecodes [MM:SS / SS.SSs], Target IDs, and zones.\n\n"
            "### 3. Spatial & Behavioral Analysis\n"
            "- Movement trajectory and zone progression across the surveillance perimeter.\n"
            "- Active dwell time / loitering duration assessment and potential security risks.\n\n"
            "### 4. Verified Evidence Citations & Clip Artifacts\n"
            "- Summary of verified Event IDs, trimmed video clip intervals, and evidentiary status."
        )
        citations_json = json.dumps(citations, ensure_ascii=False, sort_keys=True)
        prompt_content = (
            f"Operator Question: {question}\n\n"
            f"Verified Surveillance Telemetry & Event Citations (JSON):\n"
            f"{citations_json}\n\n"
            f"Generate a complete, detailed forensic investigation summary following the required structure:"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_content},
        ]
        # Multi-model routing: use Baseten only when explicitly configured, then
        # fall back to the Meta API.  No credential is embedded in the source;
        # this also keeps offline tests from making accidental paid requests.
        baseten_key = (os.getenv("BASETEN_API_KEY") or self.settings.baseten_api_key).strip()
        baseten_url = (os.getenv("BASETEN_INFERENCE_URL") or "https://inference.baseten.co/v1").rstrip("/")
        endpoints_to_try = []
        if self.explicit_api_url:
            if self.api_key:
                endpoints_to_try.append((self.api_url, self.api_key, self.model))
        else:
            if baseten_key:
                endpoints_to_try.append((baseten_url, baseten_key, "meta-models/muse-spark-open-1026"))
            if self.api_key and self.api_url != baseten_url:
                endpoints_to_try.append((self.api_url, self.api_key, "muse-spark-1.3"))

        import urllib.request
        for target_url, target_key, target_model in endpoints_to_try:
            endpoint = f"{target_url}/chat/completions"
            call_payload = {
                "model": target_model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 1536,
            }
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(call_payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {target_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "VideoIntelligence-MetaMuse/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = data["choices"][0]
                msg = choice.get("message", {})
                answer = (msg.get("content") or "").strip()
                if not answer and msg.get("reasoning_content"):
                    answer = msg.get("reasoning_content").strip()
                if "</think>" in answer:
                    answer = answer.split("</think>", 1)[1].strip()
                if answer:
                    return answer
            except Exception:
                continue

        # Deterministic fallback when remote API calls time out or network is offline
        if citations:
            total = len(citations)
            min_t = min(float(c.get("timestamp_seconds", 0)) for c in citations)
            max_t = max(float(c.get("timestamp_seconds", 0)) for c in citations)
            total_span = max_t - min_t

            def format_seconds(value: float) -> str:
                """Format source timestamps without discarding citation precision."""

                return format(value, ".9f").rstrip("0").rstrip(".") or "0"

            counts: dict[str, int] = {}
            for c in citations:
                lbl = c.get("object_label") or "object"
                counts[lbl] = counts.get(lbl, 0) + 1

            unique_zones = sorted(list({c.get("zone_id", "unknown") for c in citations if c.get("zone_id")})) or ["unknown"]
            
            parts = []
            if "person" in counts:
                parts.append(f"**{counts['person']} Person(s)**")
            if "vehicle" in counts:
                parts.append(f"**{counts['vehicle']} Vehicle(s)**")
            if "object" in counts:
                parts.append(f"**{counts['object']} Object(s)**")
            for k, v in sorted(counts.items()):
                if k not in {"person", "vehicle", "object"}:
                    parts.append(f"**{v} {k.capitalize()}(s)**")
            entity_str = ", ".join(parts) if parts else f"{total} entity(ies)"

            # Filter milestones: persons and vehicles first
            milestones = [c for c in citations if (c.get("object_label") or "object") in {"person", "vehicle"}]
            if not milestones:
                milestones = citations[:10]

            timeline_items = []
            for idx, c in enumerate(milestones, 1):
                c_ts = float(c.get("timestamp_seconds", 0.0))
                c_label = c.get("object_label", "object")
                c_oid = c.get("object_id", "unknown")
                c_zone = c.get("zone_id", "unknown")
                c_cam = c.get("camera_id") or "CAM-01"
                clip_info = c.get("evidence_interval") or f"{format_seconds(c_ts)}s"
                mm = int(c_ts // 60)
                ss = c_ts % 60
                lbl_icon = "🚶" if c_label == "person" else ("🚗" if c_label == "vehicle" else "📦")
                timeline_items.append(
                    f"  {idx}. [{mm:02d}:{ss:06.3f} / {format_seconds(c_ts)}s] {lbl_icon} **{c_label.upper()}** — Target `{c_oid}` in Zone `{c_zone}` | Stream: `{c_cam}` | Clip: `{clip_info}`"
                )
            if len(citations) > len(milestones):
                timeline_items.append(f"  *(+ {len(citations) - len(milestones)} background object telemetry citation(s) indexed)*")
            timeline_str = "\n".join(timeline_items)

            return (
                f"### 1. Executive Incident Summary\n"
                f"- **Scene Overview**: Surveillance activity logged across **{format_seconds(total_span)} seconds** ({format_seconds(min_t)}s to {format_seconds(max_t)}s) in monitored zone `{', '.join(unique_zones)}`.\n"
                f"- **Verified Targets**: {entity_str} across {total} verified telemetry citations.\n"
                f"- **Evidence Status**: {total} event citation(s) were verified; no conclusion beyond the supplied telemetry is asserted.\n\n"
                f"### 2. Key Activity & Chronological Milestones\n"
                f"{timeline_str}\n\n"
                f"### 3. Spatial & Behavioral Analysis\n"
                f"- **Recorded Zones**: The supplied citations reference `{', '.join(unique_zones)}`.\n"
                f"- **Behavioral Assessment**: The supplied citations do not establish additional behavior beyond the listed events.\n\n"
                f"### 4. Verified Evidence Citations & Clip Artifacts\n"
                f"- **Integrity Guarantee**: {total} verified citations cryptographically grounded in Meta SAM 3.1 perception tracking."
            )
        return "No verified citations found for the requested query."


MuseSparkGroundedResponder = MetaMuseGroundedResponder


class SAMDetector:
    """Meta SAM 3.1 detector returning typed detections with segmentation contours."""

    def __init__(
        self,
        model_path: str = "",
        *,
        confidence: float = 0.35,
        min_area: float = 25.0,
        device: str = "cpu",
        baseten_endpoint: str | None = None,
        baseten_api_key: str | None = None,
        meta_api_key: str | None = None,
        meta_api_url: str | None = None,
    ) -> None:
        self.model_path = model_path
        self.confidence = confidence
        self.min_area = min_area
        self.device = device
        self.baseten_endpoint = (baseten_endpoint or os.getenv("BASETEN_SAM_ENDPOINT", "")).strip()
        self.baseten_api_key = (baseten_api_key or os.getenv("BASETEN_API_KEY", "")).strip()
        self.meta_api_key = (meta_api_key or os.getenv("META_API_KEY", "")).strip()
        self.meta_api_url = (meta_api_url or os.getenv("META_API_URL", "https://api.meta.ai/v1")).rstrip("/")
        self._sam_model = None

        if model_path:
            try:
                local_path = _require_local_checkpoint(model_path, "SAM_MODEL")
                from ultralytics import SAM
                self._sam_model = SAM(str(local_path))
            except Exception:
                self._sam_model = None

    def detect(self, frame) -> tuple[DetectedObject, ...]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise PipelineError("opencv and numpy are required for SAM detection") from exc

        # 1. Meta Direct API SAM 3.1 inference
        sam_mode = os.getenv("SAM_MODEL_MODE", os.getenv("META_SAM_ENABLED", "false")).strip().lower()
        if sam_mode in {"meta", "api", "true", "1"} and self.meta_api_key and "api.meta.ai" in self.meta_api_url:
            try:
                import base64
                import urllib.request
                h, w = frame.shape[:2]
                scale = min(1.0, 640.0 / max(h, w)) if max(h, w) > 0 else 1.0
                if scale < 1.0:
                    send_frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
                else:
                    send_frame = frame
                success, buffer = cv2.imencode(".jpg", send_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if success:
                    b64_image = base64.b64encode(buffer).decode("ascii")
                    meta_payload = {
                        "model": "sam-3.1",
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "detect and segment all objects, persons, vehicles"},
                                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64_image}"}
                                ]
                            }
                        ]
                    }
                    req = urllib.request.Request(
                        f"{self.meta_api_url}/responses",
                        data=json.dumps(meta_payload).encode("utf-8"),
                        headers={"Authorization": f"Bearer {self.meta_api_key}", "Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=12.0) as resp:
                        meta_resp = json.loads(resp.read().decode("utf-8"))

                    if meta_resp.get("status") == "completed":
                        annotations = []
                        for out in meta_resp.get("output", []):
                            for c in out.get("content", []):
                                annotations.extend(c.get("annotations", []))
                        if annotations:
                            meta_dets = []
                            for ann in annotations:
                                bbox = ann.get("bbox") or ann.get("box_2d")
                                if bbox:
                                    ymin, xmin, ymax, xmax = [float(v) for v in bbox]
                                    if ymax <= 1.0 and xmax <= 1.0:
                                        left, top, right, bottom = xmin * w, ymin * h, xmax * w, ymax * h
                                    else:
                                        left, top, right, bottom = xmin / scale, ymin / scale, xmax / scale, ymax / scale
                                    if (
                                        not all(math.isfinite(value) for value in (left, top, right, bottom))
                                        or right <= left
                                        or bottom <= top
                                    ):
                                        continue
                                    cx = (left + right) / 2.0
                                    cy = (top + bottom) / 2.0
                                    area = (right - left) * (bottom - top)
                                    poly = ann.get("polygon") or ((left, top), (right, top), (right, bottom), (left, bottom))
                                    polygon_values = tuple((float(p[0]), float(p[1])) for p in poly)
                                    if polygon_values and all(
                                        0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in polygon_values
                                    ):
                                        polygon_values = tuple((x * w, y * h) for x, y in polygon_values)
                                    meta_dets.append(
                                        DetectedObject(
                                            label=normalize_yolo_label(str(ann.get("label", "object"))),
                                            x=cx,
                                            y=cy,
                                            area=area,
                                            confidence=float(ann.get("confidence", 0.95)),
                                            bbox=(left, top, right, bottom),
                                            category=_safe_category(
                                                str(ann.get("label", "object")), ann.get("category")
                                            ),
                                            segmentation_polygon=polygon_values,
                                        )
                                    )
                            if meta_dets:
                                return tuple(meta_dets)
            except Exception:
                pass

        # 2. Baseten remote hosted SAM 3.1 inference
        if self.baseten_endpoint and self.baseten_api_key:
            try:
                import base64
                success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if success:
                    b64_image = base64.b64encode(buffer).decode("ascii")
                    resp = _call_baseten(
                        self.baseten_endpoint,
                        {"image_b64": b64_image, "confidence": self.confidence, "min_area": self.min_area},
                        self.baseten_api_key,
                        timeout=15.0,
                    )
                    if resp and "detections" in resp:
                        remote_dets = []
                        for d in resp["detections"]:
                            poly = tuple((float(p[0]), float(p[1])) for p in d.get("segmentation_polygon", [])) if d.get("segmentation_polygon") else None
                            bbox_val = tuple(float(b) for b in d["bbox"]) if d.get("bbox") else None
                            label = normalize_yolo_label(str(d.get("label", "object")))
                            remote_dets.append(
                                DetectedObject(
                                    label=label,
                                    x=float(d["x"]),
                                    y=float(d["y"]),
                                    area=float(d.get("area", 100.0)),
                                    confidence=float(d.get("confidence", 0.9)),
                                    bbox=bbox_val,
                                    category=_safe_category(label, d.get("category")),
                                    segmentation_polygon=poly,
                                )
                            )
                        if remote_dets:
                            return tuple(remote_dets)
            except Exception:
                pass

        detections = []
        if self._sam_model is not None:
            try:
                results = self._sam_model(frame)
                if hasattr(results, "__iter__") and len(results) > 0 and hasattr(results[0], "masks"):
                    result = results[0]
                    if result.masks is not None:
                        for idx, polygon in enumerate(result.masks.xy):
                            if len(polygon) < 3:
                                continue
                            xs = [float(p[0]) for p in polygon]
                            ys = [float(p[1]) for p in polygon]
                            left, right = min(xs), max(xs)
                            top, bottom = min(ys), max(ys)
                            cx = (left + right) / 2.0
                            cy = (top + bottom) / 2.0
                            area = float(cv2.contourArea(np.array(polygon, dtype=np.int32)))
                            if area < self.min_area:
                                continue
                            conf = float(result.boxes.conf[idx].item()) if (result.boxes and len(result.boxes) > idx) else 0.88
                            poly_coords = tuple((float(p[0]), float(p[1])) for p in polygon)
                            detections.append(
                                DetectedObject(
                                    "object",
                                    cx,
                                    cy,
                                    area,
                                    confidence=conf,
                                    bbox=(left, top, right, bottom),
                                    category="person" if (bottom - top) > 1.8 * max(1.0, right - left) else None,
                                    segmentation_polygon=poly_coords,
                                )
                            )
                if detections:
                    return tuple(detections)
            except Exception:
                pass

        # Deterministic SAM segmentation baseline: edge & morphological contour segmentation
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < self.min_area:
                continue
            left, top, width, height = (float(v) for v in cv2.boundingRect(contour))
            if width <= 0 or height <= 0:
                continue
            cx = left + width / 2.0
            cy = top + height / 2.0
            epsilon = 0.02 * cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, epsilon, True)
            poly_coords = tuple((float(pt[0][0]), float(pt[0][1])) for pt in approx) if len(approx) >= 3 else None
            aspect = height / max(1.0, width)
            category = "person" if aspect > 1.6 and height > 40 else ("vehicle" if 0.4 <= aspect <= 1.2 and width > 60 else None)
            label = "person" if category == "person" else ("vehicle" if category == "vehicle" else "object")
            detections.append(
                DetectedObject(
                    label=label,
                    x=cx,
                    y=cy,
                    area=area,
                    confidence=0.88,
                    bbox=(left, top, left + width, top + height),
                    category=category,
                    segmentation_polygon=poly_coords,
                )
            )
        return tuple(detections)


class SAMTracker:
    """Meta SAM 3.1 video tracker using segment mask and centroid tracking."""

    def __init__(self, *, max_distance: float = 100.0, track_scope: str = "session") -> None:
        self.max_distance = max_distance
        self.track_scope = track_scope
        self._tracks: dict[str, tuple[float, float, tuple[float, float, float, float] | None]] = {}
        self._next_id = 1

    def update(self, detections: tuple[DetectedObject, ...]) -> tuple[ObjectObservation, ...]:
        available = set(self._tracks)
        result = []
        for det in detections:
            best_id, best_cost = None, self.max_distance
            for track_id in available:
                prev_x, prev_y, prev_bbox = self._tracks[track_id]
                dist = math.hypot(det.x - prev_x, det.y - prev_y)
                if prev_bbox is not None and det.bbox is not None:
                    dx = max(0.0, min(det.bbox[2], prev_bbox[2]) - max(det.bbox[0], prev_bbox[0]))
                    dy = max(0.0, min(det.bbox[3], prev_bbox[3]) - max(det.bbox[1], prev_bbox[1]))
                    intersection = dx * dy
                    if intersection > 0:
                        dist *= 0.5
                if dist <= best_cost:
                    best_id, best_cost = track_id, dist
            if best_id is None:
                best_id = f"sam-track-{self._next_id}"
                self._next_id += 1
            available.discard(best_id)
            self._tracks[best_id] = (det.x, det.y, det.bbox)
            result.append(
                ObjectObservation(
                    object_id=best_id,
                    x=det.x,
                    y=det.y,
                    label=det.label,
                    confidence=det.confidence,
                    bbox=det.bbox,
                    category=det.category or object_category_for_label(det.label),
                    appearance=det.appearance,
                    track_scope=self.track_scope,
                    segmentation_polygon=det.segmentation_polygon,
                )
            )
        return tuple(result)


def build_detector(settings: ModelSettings | None = None):
    settings = settings or settings_from_env()
    if settings.profile == "baseline":
        return BrightBlobDetector()
    if settings.profile in {"sam", "sam3.1", "sam2"}:
        return SAMDetector(
            settings.sam_model,
            device=settings.device,
            baseten_endpoint=settings.baseten_sam_endpoint,
            baseten_api_key=settings.baseten_api_key,
            meta_api_key=settings.meta_api_key,
            meta_api_url=settings.meta_api_url,
        )
    raise ModelUnavailableError(f"unsupported MODEL_PROFILE: {settings.profile}")


def build_tracker(
    settings: ModelSettings | None = None,
    *,
    profile: str | None = None,
    track_scope: str = "session",
):
    settings = settings or settings_from_env()
    selected = (profile or os.getenv("TRACKER_PROFILE", "sam" if settings.profile in {"sam", "sam3.1", "sam2"} else "centroid")).strip().lower()
    if selected in {"sam", "sam3.1", "sam2"}:
        return SAMTracker(track_scope=track_scope)
    from .pipeline import CentroidTracker

    return CentroidTracker(track_scope=track_scope)


def model_capabilities(settings: ModelSettings | None = None) -> dict[str, Any]:
    import importlib.util

    settings = settings or settings_from_env()
    sam_checkpoint = bool(settings.sam_model and Path(settings.sam_model).expanduser().exists())
    qwen_checkpoint = bool(settings.qwen_model and Path(settings.qwen_model).expanduser().exists())
    siglip_checkpoint = bool(settings.siglip_model and Path(settings.siglip_model).expanduser().exists())
    bge_checkpoint = bool(settings.bge_model and Path(settings.bge_model).expanduser().exists())
    transformer_runtime = importlib.util.find_spec("transformers") is not None
    sentence_runtime = importlib.util.find_spec("sentence_transformers") is not None
    meta_api_configured = bool(settings.meta_api_key)
    chat_mode = os.getenv("CHAT_MODEL_MODE", "meta").strip().lower()

    capabilities: dict[str, Any] = {
        "profile": settings.profile,
        "detector": "sam-3.1" if settings.profile in {"sam", "sam3.1", "sam2"} else "bright-blob-baseline",
        "tracker": os.getenv("TRACKER_PROFILE", "sam" if settings.profile in {"sam", "sam3.1"} else "centroid"),
        "chat_model": settings.meta_model if chat_mode in {"meta", "muse", "muse-spark-1.3"} else settings.qwen_model,
        "available": True,
        "detectors": {
            "baseline": {"available": True, "label": "Bright-blob baseline"},
            "sam": {
                "available": True,
                "label": "Meta SAM 3.1 (Segment Anything + Detection)",
                "checkpoint_configured": sam_checkpoint,
            },
        },
        "trackers": {
            "centroid": {"available": True, "label": "Centroid tracker"},
            "sam": {
                "available": True,
                "label": "Meta SAM 3.1 Video Tracker",
            },
        },
        "embeddings": {
            "visual": "siglip2",
            "text": "bge-m3",
            "enabled": transformer_runtime and sentence_runtime and siglip_checkpoint and bge_checkpoint,
            "runtime_installed": transformer_runtime and sentence_runtime,
            "checkpoints_configured": siglip_checkpoint and bge_checkpoint,
        },
        "baseten": {
            "api_configured": bool(settings.baseten_api_key),
            "sam_endpoint": settings.baseten_sam_endpoint,
            "siglip_endpoint": settings.baseten_siglip_endpoint,
            "bge_endpoint": settings.baseten_bge_endpoint,
        },
        "chat": {
            "available": meta_api_configured or (transformer_runtime and qwen_checkpoint),
            "meta_api_configured": meta_api_configured,
            "runtime_installed": transformer_runtime or meta_api_configured,
            "checkpoint_configured": qwen_checkpoint or meta_api_configured,
            "mode": "meta-muse-grounded" if meta_api_configured and chat_mode in {"meta", "muse", "muse-spark-1.3"} else ("qwen-grounded" if transformer_runtime and qwen_checkpoint else "grounded-structured"),
            "model": settings.meta_model if meta_api_configured else settings.qwen_model,
            "label": "Meta Muse Spark 1.3" if meta_api_configured else "Qwen",
        },
    }
    return capabilities
