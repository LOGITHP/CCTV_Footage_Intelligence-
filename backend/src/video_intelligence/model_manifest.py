"""Local model manifest loading and checkpoint validation.

Model execution in this project is deliberately *local only*.  A manifest is
the boundary between deployment configuration and an optional model runtime:
it describes the model version and embedding dimension, while this module
resolves and validates the checkpoint before an adapter is allowed to load it.

The validator never downloads weights and never treats a model identifier (for
example ``google/siglip2-base-patch16-224``) as a local checkpoint.  This keeps
an offline deployment honest: a missing or remote checkpoint is an actionable
configuration error rather than a late network request from Transformers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


class ManifestError(ValueError):
    """Raised when a model manifest is malformed or violates local-only policy."""


class CheckpointMissingError(ManifestError):
    """Raised when a requested model has no usable local checkpoint."""


class CheckpointIntegrityError(ManifestError):
    """Raised when a checkpoint does not match its manifest checksum."""


# Compatibility aliases make the failure type discoverable without requiring
# callers to know the internal distinction between schema and path errors.
ManifestValidationError = ManifestError
MissingCheckpointError = CheckpointMissingError


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_REMOTE_SCHEMES = {"http", "https", "hf", "s3", "gs", "ftp"}


def _text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{field_name} must be a string")
    result = value.strip()
    if not result and not allow_empty:
        raise ManifestError(f"{field_name} must not be empty")
    return result


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(f"{field_name} must be a positive integer")
    return value


def _is_remote_reference(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in _REMOTE_SCHEMES


def _looks_like_local_path(value: str) -> bool:
    """Return whether a legacy ``weights`` value looks like a filesystem path.

    The existing deployment manifest uses human-readable values such as
    ``YOLO_MODEL environment variable``.  Those must remain descriptions, not
    accidental paths.  Explicit ``checkpoint`` is preferred; this helper only
    preserves useful compatibility for manifests that used a path in
    ``weights``.
    """

    if not value or _is_remote_reference(value):
        return False
    candidate = Path(value)
    return (
        value.startswith((".", "/", "\\"))
        or candidate.drive != ""
        or "/" in value
        or "\\" in value
        or value.endswith((".bin", ".pt", ".pth", ".safetensors", ".onnx", ".gguf"))
    )


def _hash_checkpoint(path: Path) -> tuple[str, int]:
    """Hash a file or directory deterministically without following symlinks."""

    digest = hashlib.sha256()
    total_bytes = 0
    if path.is_file():
        files = (path,)
        relative_names = (None,)
    elif path.is_dir():
        entries = sorted(item for item in path.rglob("*") if item.is_file() and not item.is_symlink())
        files = tuple(entries)
        relative_names = tuple(item.relative_to(path).as_posix() for item in entries)
        if not files:
            raise CheckpointMissingError(f"local checkpoint directory is empty: {path}")
    else:
        raise CheckpointMissingError(f"local checkpoint does not exist: {path}")

    for file_path, relative_name in zip(files, relative_names):
        if relative_name is not None:
            encoded_name = relative_name.encode("utf-8")
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
        with file_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total_bytes += len(chunk)
                digest.update(chunk)
    return digest.hexdigest(), total_bytes


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One executable model entry from a manifest."""

    id: str
    kind: str
    source: str
    version: str
    checkpoint: Path | None = None
    dimension: int | None = None
    devices: tuple[str, ...] = ("cpu",)
    sha256: str | None = None
    required: bool = False
    weight_reference: str | None = None
    checkpoint_env: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_checkpoint(self) -> bool:
        return self.checkpoint is not None

    @property
    def embedding_dimension(self) -> int | None:
        """Alias used by indexing code and evaluation reports."""

        return self.dimension


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """Validated, path-resolved model manifest."""

    schema_version: int
    models: tuple[ModelSpec, ...]
    path: Path
    download_weights: bool = False
    runtime: Mapping[str, Any] = field(default_factory=dict)
    policy: Mapping[str, Any] = field(default_factory=dict)

    def model(self, model_id: str) -> ModelSpec:
        for item in self.models:
            if item.id == model_id:
                return item
        raise ManifestError(f"model is not declared in manifest: {model_id}")

    def by_kind(self, kind: str) -> tuple[ModelSpec, ...]:
        return tuple(item for item in self.models if item.kind == kind)

    def required(self) -> tuple[ModelSpec, ...]:
        return tuple(item for item in self.models if item.required)


@dataclass(frozen=True, slots=True)
class CheckpointValidation:
    """Evidence that one local checkpoint is present and, when requested, intact."""

    model_id: str
    path: Path
    sha256: str
    bytes: int


def _resolve_checkpoint(
    value: str,
    *,
    manifest_dir: Path,
    checkpoint_root: Path | None,
) -> Path:
    if _is_remote_reference(value):
        raise CheckpointMissingError(
            f"model checkpoint must be a local path, not a remote reference: {value}"
        )
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw
    base = checkpoint_root if checkpoint_root is not None else manifest_dir
    return (base / raw).resolve()


def _checkpoint_value(raw: Mapping[str, Any]) -> tuple[str | None, str | None]:
    explicit = raw.get("checkpoint", raw.get("checkpoint_path", raw.get("local_path", raw.get("path"))))
    reference = raw.get("weights")
    if explicit is not None:
        return _text(explicit, "model.checkpoint"), _text(reference, "model.weights", allow_empty=True) if reference else None
    if isinstance(reference, str) and _looks_like_local_path(reference):
        return _text(reference, "model.weights"), None
    if reference is None:
        return None, None
    if not isinstance(reference, str):
        raise ManifestError("model.weights must be a string or null")
    return None, reference.strip() or None


def load_model_manifest(
    path: str | Path,
    *,
    checkpoint_root: str | Path | None = None,
    checkpoint_overrides: Mapping[str, str | Path] | None = None,
    validate_checkpoints: bool = False,
    required_models: Iterable[str] | None = None,
) -> ModelManifest:
    """Load and structurally validate a JSON model manifest.

    ``validate_checkpoints`` performs the optional filesystem check in the same
    call.  Callers that only need capability discovery can leave it disabled;
    model construction and benchmark entry points should enable it for the
    selected models.
    """

    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ManifestError(f"model manifest does not exist: {manifest_path}")
    try:
        raw_document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid model manifest JSON: {manifest_path}: {exc}") from exc
    if not isinstance(raw_document, Mapping):
        raise ManifestError("model manifest root must be an object")

    schema_version = raw_document.get("schema_version", raw_document.get("manifest_version"))
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise ManifestError("schema_version must be a positive integer")
    raw_models = raw_document.get("models")
    if isinstance(raw_models, Mapping):
        raw_models = [
            {"id": model_id, **model_value}
            if isinstance(model_value, Mapping)
            else {"id": model_id, "checkpoint": model_value}
            for model_id, model_value in raw_models.items()
        ]
    if not isinstance(raw_models, list) or not raw_models:
        raise ManifestError("models must be a non-empty array")

    runtime = raw_document.get("runtime", {})
    policy = raw_document.get("policy", {})
    if not isinstance(runtime, Mapping) or not isinstance(policy, Mapping):
        raise ManifestError("runtime and policy must be objects")
    download_weights = policy.get("download_weights", False)
    if not isinstance(download_weights, bool):
        raise ManifestError("policy.download_weights must be boolean")
    if download_weights:
        raise ManifestError("model downloads are disabled; policy.download_weights must be false")

    root = Path(checkpoint_root).expanduser().resolve() if checkpoint_root is not None else None
    overrides = checkpoint_overrides or {}
    models: list[ModelSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_models):
        if not isinstance(item, Mapping):
            raise ManifestError(f"models[{index}] must be an object")
        model_id = _text(item.get("id", item.get("model_id")), f"models[{index}].id")
        if model_id in seen:
            raise ManifestError(f"duplicate model id: {model_id}")
        seen.add(model_id)
        kind = _text(item.get("kind"), f"models[{index}].kind")
        source = _text(item.get("source", "local"), f"models[{index}].source")
        version = _text(item.get("version", item.get("model_version", item.get("revision", "unversioned"))), f"models[{index}].version")

        checkpoint_value, weight_reference = _checkpoint_value(item)
        if model_id in overrides:
            override = overrides[model_id]
            if not isinstance(override, (str, Path)):
                raise ManifestError(f"checkpoint override for {model_id} must be a path")
            checkpoint_value = str(override)
        checkpoint = (
            _resolve_checkpoint(checkpoint_value, manifest_dir=manifest_path.parent, checkpoint_root=root)
            if checkpoint_value
            else None
        )

        dimension_raw = item.get("dimension", item.get("vector_dimension", item.get("embedding_dimension")))
        dimension = None if dimension_raw is None else _positive_int(dimension_raw, f"models[{index}].dimension")
        devices_raw = item.get("devices", item.get("device", ("cpu",)))
        if isinstance(devices_raw, str):
            devices_raw = (devices_raw,)
        if not isinstance(devices_raw, (list, tuple)) or not devices_raw:
            raise ManifestError(f"models[{index}].devices must be a non-empty array")
        devices = tuple(_text(value, f"models[{index}].devices[]").lower() for value in devices_raw)
        checksum = item.get("sha256", item.get("checksum_sha256"))
        if checksum is not None:
            checksum = _text(checksum, f"models[{index}].sha256").lower()
            if not _SHA256.fullmatch(checksum):
                raise ManifestError(f"models[{index}].sha256 must be a 64-character hexadecimal digest")
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise ManifestError(f"models[{index}].required must be boolean")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ManifestError(f"models[{index}].metadata must be an object")
        checkpoint_env = item.get("checkpoint_env")
        if checkpoint_env is not None:
            checkpoint_env = _text(checkpoint_env, f"models[{index}].checkpoint_env")
            if checkpoint_value is None:
                environment_value = os.getenv(checkpoint_env, "").strip()
                if environment_value:
                    checkpoint_value = environment_value
                    checkpoint = _resolve_checkpoint(
                        checkpoint_value,
                        manifest_dir=manifest_path.parent,
                        checkpoint_root=root,
                    )
                elif weight_reference is None:
                    weight_reference = f"${checkpoint_env} environment variable"
        models.append(
            ModelSpec(
                id=model_id,
                kind=kind,
                source=source,
                version=version,
                checkpoint=checkpoint,
                dimension=dimension,
                devices=devices,
                sha256=checksum,
                required=required,
                weight_reference=weight_reference,
                checkpoint_env=checkpoint_env,
                metadata=dict(metadata),
            )
        )

    manifest = ModelManifest(
        schema_version=schema_version,
        models=tuple(models),
        path=manifest_path,
        download_weights=download_weights,
        runtime=dict(runtime),
        policy=dict(policy),
    )
    if validate_checkpoints:
        validate_local_checkpoints(manifest, required_models=required_models)
    return manifest


def validate_local_checkpoints(
    manifest: ModelManifest,
    *,
    model_ids: Iterable[str] | None = None,
    required_models: Iterable[str] | None = None,
) -> dict[str, CheckpointValidation]:
    """Validate selected local checkpoints and return checksum evidence.

    With no selector, entries that declare ``required: true`` or an explicit
    local checkpoint are checked.  ``required_models`` is a strict selector:
    each named model must have a checkpoint, even when its manifest entry only
    contains a remote ``weights`` description.
    """

    explicit_ids = set(model_ids or ())
    required_ids = set(required_models or ())
    unknown = (explicit_ids | required_ids) - {item.id for item in manifest.models}
    if unknown:
        raise ManifestError(f"models are not declared in manifest: {', '.join(sorted(unknown))}")

    selected: list[ModelSpec] = []
    for item in manifest.models:
        if explicit_ids or required_ids:
            if item.id in explicit_ids or item.id in required_ids:
                selected.append(item)
        elif item.required or item.checkpoint is not None:
            selected.append(item)

    validations: dict[str, CheckpointValidation] = {}
    for item in selected:
        if item.checkpoint is None:
            detail = item.weight_reference or "no checkpoint path was provided"
            raise CheckpointMissingError(
                f"model '{item.id}' has no local checkpoint ({detail}); "
                "set models[].checkpoint or a local checkpoint override"
            )
        if _is_remote_reference(str(item.checkpoint)):
            raise CheckpointMissingError(
                f"model '{item.id}' checkpoint is remote; provide a local path: {item.checkpoint}"
            )
        validations[item.id] = validate_checkpoint(item)
    return validations


def validate_checkpoint(
    checkpoint: str | Path | ModelSpec,
    *,
    expected_sha256: str | None = None,
    model_id: str | None = None,
) -> CheckpointValidation:
    """Validate one checkpoint path or manifest model entry."""

    if isinstance(checkpoint, ModelSpec):
        model = checkpoint
        path = model.checkpoint
        expected = model.sha256 if expected_sha256 is None else expected_sha256
        resolved_model_id = model.id if model_id is None else model_id
    else:
        path = Path(checkpoint).expanduser()
        expected = expected_sha256
        resolved_model_id = model_id or path.name
    if path is None:
        raise CheckpointMissingError(
            f"model '{resolved_model_id}' has no local checkpoint; provide a local checkpoint path"
        )
    if _is_remote_reference(str(path)):
        raise CheckpointMissingError(f"checkpoint must be local, not remote: {path}")
    digest, byte_count = _hash_checkpoint(path)
    if expected is not None:
        normalized = _text(expected, "expected_sha256").lower()
        if not _SHA256.fullmatch(normalized):
            raise ManifestError("expected_sha256 must be a 64-character hexadecimal digest")
        if digest.lower() != normalized:
            raise CheckpointIntegrityError(
                f"checkpoint checksum mismatch for model '{resolved_model_id}': expected {normalized}, got {digest}"
            )
    return CheckpointValidation(str(resolved_model_id), path.resolve(), digest, byte_count)


def load_and_validate_model_manifest(
    path: str | Path,
    *,
    checkpoint_root: str | Path | None = None,
    checkpoint_overrides: Mapping[str, str | Path] | None = None,
    model_ids: Iterable[str] | None = None,
    required_models: Iterable[str] | None = None,
) -> ModelManifest:
    """Load a manifest and require local checkpoints for the selected models."""

    manifest = load_model_manifest(
        path,
        checkpoint_root=checkpoint_root,
        checkpoint_overrides=checkpoint_overrides,
    )
    validate_local_checkpoints(manifest, model_ids=model_ids, required_models=required_models)
    return manifest


# Short aliases for command-line and integration callers.
load_manifest = load_model_manifest
validate_checkpoints = validate_local_checkpoints


__all__ = [
    "CheckpointIntegrityError",
    "CheckpointMissingError",
    "CheckpointValidation",
    "ManifestError",
    "ManifestValidationError",
    "MissingCheckpointError",
    "ModelManifest",
    "ModelSpec",
    "load_and_validate_model_manifest",
    "load_manifest",
    "load_model_manifest",
    "validate_checkpoint",
    "validate_checkpoints",
    "validate_local_checkpoints",
]
