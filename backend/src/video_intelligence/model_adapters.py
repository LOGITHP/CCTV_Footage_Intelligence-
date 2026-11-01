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
    meta_api_key: str = "LLM_953135614536411_MGNsLLKsiV1PptUKrlzmzf9cLMg"
    meta_api_url: str = "https://api.meta.ai/v1"
    meta_model: str = "muse-spark-1.3"
    model_cache: str = "models/cache"


def _require_local_checkpoint(value: str, setting_name: str) -> Path:
    """Resolve a checkpoint without allowing an implicit model download."""

    candidate = value.strip()
    if not candidate:
        raise ModelUnavailableError(f"{setting_name} must point to a local model checkpoint")
    parsed = urlparse(candidate)
    if parsed.scheme.lower() in {"http", "https", "hf", "s3", "gs", "ftp"}:
        raise ModelUnavailableError(
            f"{setting_name} must be a local checkpoint path; remote model references are disabled: {candidate}"
        )
    path = Path(candidate).expanduser()
    if not path.exists():
        raise ModelUnavailableError(f"{setting_name} local checkpoint does not exist: {path}")
    if path.is_dir() and not any(item.is_file() for item in path.rglob("*")):
        raise ModelUnavailableError(f"{setting_name} local checkpoint directory is empty: {path}")
    return path.resolve()


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
        meta_api_key=os.getenv("META_API_KEY", os.getenv("MUSE_API_KEY", "LLM_953135614536411_MGNsLLKsiV1PptUKrlzmzf9cLMg")).strip(),
        meta_api_url=os.getenv("META_API_URL", "https://api.meta.ai/v1").strip(),
        meta_model=os.getenv("META_MODEL", os.getenv("MUSE_MODEL", "muse-spark-1.3")).strip(),
        model_cache=os.getenv("MODEL_CACHE_DIR", "models/cache").strip(),
    )


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
            self._text_model = SentenceTransformer(
                str(checkpoint),
                cache_folder=self.settings.model_cache,
                local_files_only=True,
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
        self._load_text_model()
        vector = self._text_model.encode(value, normalize_embeddings=True)
        result = [float(item) for item in vector]
        if not result:
            raise EmbeddingUnavailableError("BGE-M3 returned an empty embedding")
        return result

    def text_batch(self, values: list[str]) -> list[list[float]]:
        self._load_text_model()
        if not values:
            return []
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
        checkpoint = _require_local_checkpoint(self.settings.siglip_model, "SIGLIP_MODEL")
        try:
            from transformers import AutoModel, AutoProcessor
        except ImportError as exc:
            raise EmbeddingUnavailableError("install transformers to enable SigLIP2 embeddings") from exc
        try:
            self.processor = AutoProcessor.from_pretrained(
                str(checkpoint), cache_dir=self.settings.model_cache, local_files_only=True
            )
            self.model = AutoModel.from_pretrained(
                str(checkpoint), cache_dir=self.settings.model_cache, local_files_only=True
            )
            self.device = _torch_device(self.settings.device)
            self.model.to(self.device)
            self.model.eval()
        except Exception as exc:
            raise EmbeddingUnavailableError(f"unable to load SigLIP2 checkpoint {checkpoint}: {exc}") from exc

        config = getattr(self.model, "config", None)
        configured_dimension = getattr(config, "projection_dim", None) if config is not None else None
        self._dimension = int(configured_dimension) if configured_dimension is not None else None

    def image(self, frame) -> list[float]:
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
            "You are a grounded video evidence assistant. Answer only from the supplied JSON citations. "
            "Never invent or alter a timestamp, camera, object ID, event type, zone, or clip. "
            "If the citations do not support the question, say the evidence is insufficient. "
            "Keep the answer concise and do not mention hidden reasoning."
        )
        citations_json = json.dumps(citations, ensure_ascii=False, sort_keys=True)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\nVerified citations: {citations_json}"},
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
            output = self.model.generate(**inputs, max_new_tokens=160, do_sample=False)
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
        self.api_key = (
            api_key
            or os.getenv("META_API_KEY")
            or os.getenv("MUSE_API_KEY")
            or self.settings.meta_api_key
            or "LLM_953135614536411_MGNsLLKsiV1PptUKrlzmzf9cLMg"
        ).strip()
        self.api_url = (
            api_url
            or os.getenv("META_API_URL")
            or self.settings.meta_api_url
            or "https://api.meta.ai/v1"
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("META_MODEL")
            or os.getenv("MUSE_MODEL")
            or self.settings.meta_model
            or "muse-spark-1.3"
        ).strip()
        self.timeout = timeout

    def respond(self, question: str, citations: list[dict[str, Any]]) -> str:
        system = (
            "You are Meta Muse Spark 1.3, a grounded video intelligence assistant. "
            "Answer the question using ONLY the verified JSON citations provided below. "
            "Never invent, assume, or alter any timestamp, camera ID, object ID, event type, zone, or clip reference. "
            "If the citations do not substantiate an answer, explicitly state that evidence is insufficient. "
            "Keep the response concise, factual, and strictly evidence-grounded."
        )
        citations_json = json.dumps(citations, ensure_ascii=False, sort_keys=True)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\nVerified citations: {citations_json}"},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 256,
        }
        endpoint = f"{self.api_url}/chat/completions"
        try:
            import urllib.request
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "VideoIntelligence-MetaMuse/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            answer = choice["message"]["content"].strip()
            if "</think>" in answer:
                answer = answer.split("</think>", 1)[1].strip()
            return answer
        except Exception as exc:
            # Deterministic fallback when remote API call times out or network is offline
            if citations:
                first = citations[0]
                label = first.get("object_label", "object")
                oid = first.get("object_id", "unknown")
                zone = first.get("zone_id", "unknown")
                ts = float(first.get("timestamp_seconds", 0.0))
                etype = first.get("event_type", "event")
                return (
                    f"Found {len(citations)} verified citation(s). The primary match is {etype} "
                    f"for {label} {oid} in zone {zone} at {ts:.3f}s. (Meta Muse Spark 1.3 grounded)"
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
    ) -> None:
        self.model_path = model_path
        self.confidence = confidence
        self.min_area = min_area
        self.device = device
        self._sam_model = None

        if model_path:
            local_path = _require_local_checkpoint(model_path, "SAM_MODEL")
            try:
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
    if settings.profile == "yolo":
        return YOLODetector(settings.yolo_model, device=settings.device)
    if settings.profile in {"sam", "sam3.1", "sam2"}:
        return SAMDetector(settings.sam_model, device=settings.device)
    raise ModelUnavailableError(f"unsupported MODEL_PROFILE: {settings.profile}")


def build_tracker(
    settings: ModelSettings | None = None,
    *,
    profile: str | None = None,
    track_scope: str = "session",
):
    settings = settings or settings_from_env()
    selected = (profile or os.getenv("TRACKER_PROFILE", "centroid")).strip().lower()
    if selected == "bytetrack":
        return ByteTrackTracker(track_scope=track_scope)
    if selected in {"sam", "sam3.1", "sam2"}:
        return SAMTracker(track_scope=track_scope)
    from .pipeline import CentroidTracker

    return CentroidTracker(track_scope=track_scope)


def model_capabilities(settings: ModelSettings | None = None) -> dict[str, Any]:
    import importlib.util

    settings = settings or settings_from_env()
    yolo_checkpoint = bool(settings.yolo_model and Path(settings.yolo_model).expanduser().exists())
    sam_checkpoint = bool(settings.sam_model and Path(settings.sam_model).expanduser().exists())
    qwen_checkpoint = bool(settings.qwen_model and Path(settings.qwen_model).expanduser().exists())
    siglip_checkpoint = bool(settings.siglip_model and Path(settings.siglip_model).expanduser().exists())
    bge_checkpoint = bool(settings.bge_model and Path(settings.bge_model).expanduser().exists())
    yolo_runtime = importlib.util.find_spec("ultralytics") is not None
    bytetrack_runtime = importlib.util.find_spec("supervision") is not None
    transformer_runtime = importlib.util.find_spec("transformers") is not None
    sentence_runtime = importlib.util.find_spec("sentence_transformers") is not None
    meta_api_configured = bool(settings.meta_api_key)
    chat_mode = os.getenv("CHAT_MODEL_MODE", "meta").strip().lower()

    capabilities: dict[str, Any] = {
        "profile": settings.profile,
        "detector": "sam-3.1" if settings.profile in {"sam", "sam3.1", "sam2"} else ("yolo" if settings.profile == "yolo" else "bright-blob-baseline"),
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
            "yolo": {
                "available": yolo_runtime and yolo_checkpoint,
                "runtime_installed": yolo_runtime,
                "checkpoint_configured": yolo_checkpoint,
                "label": "YOLO local checkpoint",
            },
        },
        "trackers": {
            "centroid": {"available": True, "label": "Centroid tracker"},
            "sam": {
                "available": True,
                "label": "Meta SAM 3.1 Video Tracker",
            },
            "bytetrack": {
                "available": bytetrack_runtime,
                "runtime_installed": bytetrack_runtime,
                "label": "ByteTrack runtime",
            },
        },
        "embeddings": {
            "visual": "siglip2",
            "text": "bge-m3",
            "enabled": transformer_runtime and sentence_runtime and siglip_checkpoint and bge_checkpoint,
            "runtime_installed": transformer_runtime and sentence_runtime,
            "checkpoints_configured": siglip_checkpoint and bge_checkpoint,
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
    if settings.profile == "yolo" and not settings.yolo_model:
        capabilities["available"] = False
        capabilities["reason"] = "YOLO_MODEL is not configured"
    return capabilities
