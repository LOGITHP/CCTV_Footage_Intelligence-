"""Model-backed embedding and Qdrant indexing orchestration.

This module intentionally keeps model construction separate from the upload
API.  A caller can build an orchestrator from a local manifest for production,
or inject small encoder/index doubles for offline tests.  There is no hashing,
zero-vector, or other semantic fallback: every indexed vector comes from the
selected SigLIP2 or BGE-M3 adapter and is checked against the manifest's
declared dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .infrastructure.config import VectorStorageSettings
from .infrastructure.vectors import QdrantIndex, VectorStorageUnavailable
from .model_adapters import EmbeddingService, ModelSettings, SigLIP2EmbeddingService, settings_from_env
from .model_manifest import ManifestError, ModelManifest, ModelSpec, load_model_manifest, validate_local_checkpoints


class EmbeddingIndexingError(RuntimeError):
    """Raised when an embedding cannot satisfy the manifest/index contract."""


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    """One text or image value and its evidence payload."""

    point_id: str
    value: Any
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.point_id.strip():
            raise ValueError("embedding point_id must not be empty")


@dataclass(frozen=True, slots=True)
class IndexingSummary:
    """Receipt for a batch written to one versioned Qdrant collection."""

    indexed: int
    collection: str
    dimension: int
    model_id: str
    model_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "indexed": self.indexed,
            "collection": self.collection,
            "dimension": self.dimension,
            "model_id": self.model_id,
            "model_version": self.model_version,
        }


def _find_spec(manifest: ModelManifest, *, ids: tuple[str, ...], kinds: tuple[str, ...]) -> ModelSpec:
    normalized_ids = {value.lower() for value in ids}
    normalized_kinds = {value.lower() for value in kinds}
    for spec in manifest.models:
        if spec.id.lower() in normalized_ids or spec.kind.lower() in normalized_kinds:
            return spec
    raise ManifestError(
        f"manifest does not declare a required model (ids={sorted(normalized_ids)}, kinds={sorted(normalized_kinds)})"
    )


def _vector_values(vector: Any) -> list[float]:
    try:
        result = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingIndexingError("embedding output must be a finite numeric vector") from exc
    if not result:
        raise EmbeddingIndexingError("embedding output is empty")
    return result


def _record(value: Any) -> EmbeddingRecord:
    if isinstance(value, EmbeddingRecord):
        return value
    if isinstance(value, Mapping):
        point_id = value.get("point_id", value.get("id"))
        if point_id is None:
            raise EmbeddingIndexingError("embedding record requires point_id or id")
        content = value.get("value", value.get("text", value.get("content", value.get("frame", value.get("image")))))
        if content is None:
            raise EmbeddingIndexingError(f"embedding record {point_id} has no value")
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise EmbeddingIndexingError(f"embedding record {point_id} payload must be an object")
        return EmbeddingRecord(str(point_id), content, dict(payload))
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        point_id, content = value[0], value[1]
        payload = value[2] if len(value) > 2 else {}
        if not isinstance(payload, Mapping):
            raise EmbeddingIndexingError("embedding tuple payload must be an object")
        return EmbeddingRecord(str(point_id), content, dict(payload))
    raise EmbeddingIndexingError("embedding records must be EmbeddingRecord, mapping, or tuple")


class EmbeddingIndexOrchestrator:
    """Encode evidence with local BGE-M3/SigLIP2 models and upsert to Qdrant."""

    def __init__(
        self,
        *,
        text_encoder: Any | None = None,
        visual_encoder: Any | None = None,
        text_index: Any | None = None,
        visual_index: Any | None = None,
        text_dimension: int | None = None,
        visual_dimension: int | None = None,
        text_model_id: str = "bge-m3",
        text_model_version: str = "local",
        visual_model_id: str = "siglip2",
        visual_model_version: str = "local",
    ) -> None:
        if (text_encoder is None) != (text_index is None):
            raise ValueError("text_encoder and text_index must be supplied together")
        if (visual_encoder is None) != (visual_index is None):
            raise ValueError("visual_encoder and visual_index must be supplied together")
        self.text_encoder = text_encoder
        self.visual_encoder = visual_encoder
        self.text_index = text_index
        self.visual_index = visual_index
        self.text_dimension = text_dimension
        self.visual_dimension = visual_dimension
        self.text_model_id = text_model_id
        self.text_model_version = text_model_version
        self.visual_model_id = visual_model_id
        self.visual_model_version = visual_model_version

    @classmethod
    def from_manifest(
        cls,
        manifest: ModelManifest | str | Path,
        *,
        vector_settings: VectorStorageSettings,
        model_settings: ModelSettings | None = None,
        text_model_id: str | None = None,
        visual_model_id: str | None = None,
        include_text: bool = True,
        include_visual: bool = True,
    ) -> "EmbeddingIndexOrchestrator":
        """Build real local embedding adapters and versioned Qdrant indexes."""

        if not include_text and not include_visual:
            raise ValueError("at least one embedding modality must be enabled")

        loaded = (
            load_model_manifest(manifest)
            if isinstance(manifest, (str, Path))
            else manifest
        )
        if not isinstance(loaded, ModelManifest):
            raise TypeError("manifest must be a ModelManifest or JSON path")
        text_spec = (
            _find_spec(
                loaded,
                ids=(text_model_id or "bge-m3", "bge-m3"),
                kinds=("text-embedding", "text_embedding"),
            )
            if include_text
            else None
        )
        visual_spec = (
            _find_spec(
                loaded,
                ids=(visual_model_id or "siglip2", "siglip2"),
                kinds=("visual-embedding", "visual_embedding", "image-embedding"),
            )
            if include_visual
            else None
        )
        validate_local_checkpoints(
            loaded,
            model_ids=tuple(spec.id for spec in (text_spec, visual_spec) if spec is not None),
        )
        for spec in (text_spec, visual_spec):
            if spec is not None and spec.dimension is None:
                raise EmbeddingIndexingError(f"manifest model '{spec.id}' must declare dimension")
        settings = model_settings or settings_from_env()
        settings = ModelSettings(
            profile=settings.profile,
            device=settings.device,
            yolo_model=settings.yolo_model,
            siglip_model=str(visual_spec.checkpoint) if visual_spec and visual_spec.checkpoint else settings.siglip_model,
            bge_model=str(text_spec.checkpoint) if text_spec and text_spec.checkpoint else settings.bge_model,
            qwen_model=settings.qwen_model,
            model_cache=settings.model_cache,
        )
        text_encoder = EmbeddingService(settings) if text_spec else None
        visual_encoder = SigLIP2EmbeddingService(settings) if visual_spec else None
        text_index = (
            QdrantIndex(
                vector_settings,
                dimension=text_spec.dimension,
                model_id=text_spec.id,
                model_version=text_spec.version,
            )
            if text_spec
            else None
        )
        visual_index = (
            QdrantIndex(
                vector_settings,
                dimension=visual_spec.dimension,
                model_id=visual_spec.id,
                model_version=visual_spec.version,
            )
            if visual_spec
            else None
        )
        return cls(
            text_encoder=text_encoder,
            visual_encoder=visual_encoder,
            text_index=text_index,
            visual_index=visual_index,
            text_dimension=text_spec.dimension if text_spec else None,
            visual_dimension=visual_spec.dimension if visual_spec else None,
            text_model_id=text_spec.id if text_spec else "bge-m3",
            text_model_version=text_spec.version if text_spec else "local",
            visual_model_id=visual_spec.id if visual_spec else "siglip2",
            visual_model_version=visual_spec.version if visual_spec else "local",
        )

    @staticmethod
    def _check_dimension(vector: list[float], expected: int | None, *, model_id: str, index: Any) -> None:
        declared = expected if expected is not None else getattr(index, "dimension", None)
        if declared is not None and len(vector) != declared:
            raise EmbeddingIndexingError(
                f"{model_id} produced dimension {len(vector)}, expected {declared}; "
                "check the local checkpoint and manifest version"
            )
        if any(value != value or value in (float("inf"), float("-inf")) for value in vector):
            raise EmbeddingIndexingError(f"{model_id} produced a non-finite embedding")

    def index_text(self, records: Iterable[EmbeddingRecord | Mapping[str, Any] | tuple[Any, ...]]) -> IndexingSummary:
        if self.text_encoder is None or self.text_index is None:
            raise EmbeddingIndexingError("BGE-M3 text indexing is not configured")
        normalized = [_record(item) for item in records]
        observed_dimension = self.text_dimension or getattr(self.text_index, "dimension", None) or getattr(self.text_encoder, "dimension", None)
        for item in normalized:
            vector = _vector_values(self.text_encoder.text(str(item.value)))
            if observed_dimension is None:
                observed_dimension = len(vector)
            self._check_dimension(vector, observed_dimension, model_id=self.text_model_id, index=self.text_index)
            try:
                self.text_index.upsert(item.point_id, vector, dict(item.payload))
            except (VectorStorageUnavailable, ValueError, TypeError) as exc:
                raise EmbeddingIndexingError(f"unable to index text point {item.point_id}: {exc}") from exc
        return IndexingSummary(
            indexed=len(normalized),
            collection=str(getattr(self.text_index, "collection", "")),
            dimension=int(observed_dimension or 0),
            model_id=self.text_model_id,
            model_version=self.text_model_version,
        )

    def search_text(
        self,
        query: str,
        *,
        limit: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search the text collection with a real BGE-M3 query embedding."""

        if self.text_encoder is None or self.text_index is None:
            raise EmbeddingIndexingError("BGE-M3 text search is not configured")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("semantic search query must not be empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("semantic search limit must be between 1 and 100")
        vector = _vector_values(self.text_encoder.text(query.strip()))
        expected = self.text_dimension or getattr(self.text_index, "dimension", None)
        self._check_dimension(vector, expected, model_id=self.text_model_id, index=self.text_index)
        try:
            return self.text_index.search(vector, limit=limit, filters=dict(filters or {}))
        except (VectorStorageUnavailable, ValueError, TypeError) as exc:
            raise EmbeddingIndexingError(f"unable to search text collection: {exc}") from exc

    def index_images(self, records: Iterable[EmbeddingRecord | Mapping[str, Any] | tuple[Any, ...]]) -> IndexingSummary:
        if self.visual_encoder is None or self.visual_index is None:
            raise EmbeddingIndexingError("SigLIP2 visual indexing is not configured")
        normalized = [_record(item) for item in records]
        observed_dimension = self.visual_dimension or getattr(self.visual_index, "dimension", None) or getattr(self.visual_encoder, "dimension", None)
        for item in normalized:
            vector = _vector_values(self.visual_encoder.image(item.value))
            if observed_dimension is None:
                observed_dimension = len(vector)
            self._check_dimension(vector, observed_dimension, model_id=self.visual_model_id, index=self.visual_index)
            try:
                self.visual_index.upsert(item.point_id, vector, dict(item.payload))
            except (VectorStorageUnavailable, ValueError, TypeError) as exc:
                raise EmbeddingIndexingError(f"unable to index visual point {item.point_id}: {exc}") from exc
        return IndexingSummary(
            indexed=len(normalized),
            collection=str(getattr(self.visual_index, "collection", "")),
            dimension=int(observed_dimension or 0),
            model_id=self.visual_model_id,
            model_version=self.visual_model_version,
        )

    def index_recording(
        self,
        video_id: str,
        frames: Iterable[Any],
        *,
        camera_id: str | None = None,
    ) -> IndexingSummary:
        """Index decoded uploaded-recording frames into the visual collection.

        Each item may be ``(timestamp_seconds, frame)``, a mapping containing
        ``timestamp_seconds`` and ``frame``/``image``, or an object with those
        attributes.  Source timestamps are persisted in the Qdrant payload and
        are never derived from nominal FPS.
        """

        records: list[EmbeddingRecord] = []
        for index, item in enumerate(frames):
            if isinstance(item, Mapping):
                timestamp = item.get("timestamp_seconds", item.get("timestamp"))
                frame = item.get("frame", item.get("image"))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                timestamp, frame = item[0], item[1]
            else:
                timestamp = getattr(item, "timestamp_seconds", getattr(item, "timestamp", None))
                frame = getattr(item, "frame", getattr(item, "image", None))
            if timestamp is None or frame is None:
                raise EmbeddingIndexingError("recording frame requires timestamp_seconds and frame")
            try:
                timestamp_value = float(timestamp)
            except (TypeError, ValueError) as exc:
                raise EmbeddingIndexingError("recording frame timestamp must be numeric") from exc
            if timestamp_value < 0:
                raise EmbeddingIndexingError("recording frame timestamp must be non-negative")
            payload = {"video_id": video_id, "timestamp_seconds": timestamp_value, "frame_index": index}
            if camera_id is not None:
                payload["camera_id"] = camera_id
            records.append(EmbeddingRecord(f"{video_id}:{index}:{timestamp_value:.6f}", frame, payload))
        return self.index_images(records)

    def index_video_path(
        self,
        video_id: str,
        recording_path: str | Path,
        *,
        every: int = 1,
        camera_id: str | None = None,
    ) -> IndexingSummary:
        """Decode an uploaded recording and index real frames by source time."""

        if every < 1:
            raise ValueError("every must be at least 1")
        try:
            import cv2
        except ImportError as exc:
            raise EmbeddingIndexingError("opencv-python-headless is required to index a recording") from exc
        path = Path(recording_path).expanduser()
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise EmbeddingIndexingError(f"unable to open uploaded recording: {path}")

        def frames():
            index = 0
            last_timestamp = -1.0
            try:
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    if index % every == 0:
                        timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
                        if not math.isfinite(timestamp) or timestamp < 0 or timestamp < last_timestamp:
                            raise EmbeddingIndexingError(
                                f"invalid or discontinuous decoder timestamp at frame {index}"
                            )
                        last_timestamp = timestamp
                        # OpenCV decodes BGR; processors conventionally expect
                        # RGB arrays, so convert without inventing image data.
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        yield timestamp, rgb
                    index += 1
            finally:
                capture.release()

        return self.index_recording(video_id, frames(), camera_id=camera_id)

    def index_uploaded_recording(
        self,
        video_id: str,
        recording: str | Path | Iterable[Any],
        *,
        every: int = 1,
        camera_id: str | None = None,
    ) -> IndexingSummary:
        """Index either a recording path or already decoded timestamped frames."""

        if isinstance(recording, (str, Path)):
            return self.index_video_path(video_id, recording, every=every, camera_id=camera_id)
        return self.index_recording(video_id, recording, camera_id=camera_id)

    def describe(self) -> dict[str, Any]:
        return {
            "text": {
                "model_id": self.text_model_id,
                "version": self.text_model_version,
                "dimension": self.text_dimension,
                "collection": getattr(self.text_index, "collection", None),
                "enabled": self.text_encoder is not None,
            },
            "visual": {
                "model_id": self.visual_model_id,
                "version": self.visual_model_version,
                "dimension": self.visual_dimension,
                "collection": getattr(self.visual_index, "collection", None),
                "enabled": self.visual_encoder is not None,
            },
        }


__all__ = [
    "EmbeddingIndexOrchestrator",
    "EmbeddingIndexingError",
    "EmbeddingRecord",
    "IndexingSummary",
]
