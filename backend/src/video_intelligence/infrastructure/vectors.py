"""Qdrant vector adapter with no implicit local semantic substitute."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .config import VectorStorageSettings


class VectorStorageUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VectorCollectionSpec:
    """Explicit model/version contract for one Qdrant collection."""

    base_name: str
    dimension: int
    model_id: str = ""
    model_version: str = ""
    distance: str = "Cosine"

    def __post_init__(self) -> None:
        if not self.base_name.strip():
            raise ValueError("Qdrant collection name must not be empty")
        if self.dimension < 1:
            raise ValueError("Qdrant vector dimension must be positive")

    @property
    def collection_name(self) -> str:
        return versioned_collection_name(self.base_name, self.model_id, self.model_version)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
    return normalized.strip("-")


def versioned_collection_name(base_name: str, model_id: str, model_version: str) -> str:
    """Build a stable collection name that cannot mix model versions.

    A plain collection name is retained when no model/version is supplied for
    backwards compatibility with the structured-event adapter.  Embedding
    callers always supply both values and receive names such as
    ``video-events__bge-m3-v1``.
    """

    base = _slug(base_name)
    model = _slug(model_id)
    version = _slug(model_version)
    if not model and not version:
        return base
    if not model or not version:
        raise ValueError("model_id and model_version must be provided together for a versioned collection")
    return f"{base}__{model}-v{version}"


def _collection_dimension(info: Any) -> int | None:
    """Read a single-vector collection dimension across qdrant-client versions."""

    config = getattr(info, "config", None)
    params = getattr(config, "params", None) if config is not None else None
    vectors = getattr(params, "vectors", None) if params is not None else None
    if vectors is None and isinstance(config, dict):
        params = config.get("params", {})
        vectors = params.get("vectors") if isinstance(params, dict) else None
    if isinstance(vectors, dict):
        # Named-vector collections are not accepted by this single-vector
        # adapter unless there is exactly one unambiguous vector definition.
        if len(vectors) != 1:
            return None
        vectors = next(iter(vectors.values()))
    value = getattr(vectors, "size", None)
    if value is None and isinstance(vectors, dict):
        value = vectors.get("size")
    return int(value) if value is not None else None


def _qdrant_point_id(point_id: str) -> str:
    """Return a Qdrant-compatible UUID while keeping source IDs in payloads."""

    try:
        UUID(str(point_id))
    except (ValueError, AttributeError, TypeError):
        return str(uuid5(NAMESPACE_URL, str(point_id)))
    return str(point_id)


class QdrantIndex:
    def __init__(
        self,
        settings: VectorStorageSettings,
        *,
        dimension: int | None = None,
        model_id: str = "",
        model_version: str = "",
        collection_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not settings.url and client is None:
            raise VectorStorageUnavailable("QDRANT_URL is required to enable Qdrant")
        resolved_dimension = settings.vector_size if dimension is None else dimension
        if isinstance(resolved_dimension, bool) or not isinstance(resolved_dimension, int) or resolved_dimension < 1:
            raise VectorStorageUnavailable("Qdrant vector dimension must be a positive integer")
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as exc:
            raise VectorStorageUnavailable("install qdrant-client to enable Qdrant") from exc
        self.client = client or QdrantClient(url=settings.url, api_key=settings.api_key or None, timeout=settings.timeout_seconds)
        self.models = models
        self.dimension = resolved_dimension
        self.model_id = model_id
        self.model_version = model_version
        self.collection = collection_name or versioned_collection_name(settings.collection, model_id, model_version)
        if not self.collection:
            raise VectorStorageUnavailable("Qdrant collection name must not be empty")
        self.collection_spec = VectorCollectionSpec(
            base_name=settings.collection,
            dimension=self.dimension,
            model_id=model_id,
            model_version=model_version,
            distance=settings.distance,
        )
        self.distance = getattr(models.Distance, settings.distance.upper(), models.Distance.COSINE)
        collections = {item.name for item in self.client.get_collections().collections}
        if self.collection not in collections:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=self.dimension, distance=self.distance),
            )
        else:
            existing = _collection_dimension(self.client.get_collection(self.collection))
            if existing is not None and existing != self.dimension:
                raise VectorStorageUnavailable(
                    f"Qdrant collection '{self.collection}' has dimension {existing}, "
                    f"but model '{self.model_id or 'unspecified'}' requires {self.dimension}; "
                    "use a new model/versioned collection"
                )

    def upsert(self, point_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        if len(vector) != self.dimension:
            raise VectorStorageUnavailable(
                f"vector dimension mismatch for collection '{self.collection}': "
                f"expected {self.dimension}, got {len(vector)}"
            )
        try:
            finite = all(math.isfinite(float(value)) for value in vector)
        except (TypeError, ValueError) as exc:
            raise VectorStorageUnavailable("vector must contain numeric values") from exc
        if not finite:
            raise VectorStorageUnavailable("vector contains a non-finite value")
        enriched_payload = dict(payload)
        source_point_id = str(point_id)
        qdrant_id = _qdrant_point_id(source_point_id)
        if qdrant_id != source_point_id:
            enriched_payload.setdefault("source_point_id", source_point_id)
        enriched_payload.setdefault("embedding_dimension", self.dimension)
        if self.model_id:
            enriched_payload.setdefault("embedding_model", self.model_id)
        if self.model_version:
            enriched_payload.setdefault("embedding_version", self.model_version)
        self.client.upsert(
            collection_name=self.collection,
            points=[self.models.PointStruct(id=qdrant_id, vector=vector, payload=enriched_payload)],
        )

    def search(self, vector: list[float], *, limit: int = 10, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if len(vector) != self.dimension:
            raise VectorStorageUnavailable(
                f"query dimension mismatch for collection '{self.collection}': "
                f"expected {self.dimension}, got {len(vector)}"
            )
        query_filter = None
        if filters:
            query_filter = self.models.Filter(
                must=[self.models.FieldCondition(key=key, match=self.models.MatchValue(value=value)) for key, value in filters.items()]
            )
        hits = self.client.search(collection_name=self.collection, query_vector=vector, query_filter=query_filter, limit=limit)
        return [{"id": str(hit.id), "score": hit.score, "payload": hit.payload} for hit in hits]


__all__ = ["QdrantIndex", "VectorCollectionSpec", "VectorStorageUnavailable", "versioned_collection_name"]
