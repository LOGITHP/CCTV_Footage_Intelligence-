import hashlib
import json

import pytest

from video_intelligence.model_manifest import (
    CheckpointIntegrityError,
    CheckpointMissingError,
    ManifestError,
    load_model_manifest,
    validate_local_checkpoints,
)


def _write_manifest(path, checkpoint, *, digest=None, checkpoint_env=None):
    document = {
        "schema_version": 3,
        "policy": {"download_weights": False},
        "models": [
            {
                "id": "bge-m3-local",
                "kind": "text-embedding",
                "source": "sentence-transformers",
                "version": "1",
                "dimension": 4,
                "devices": ["cpu"],
                "required": True,
                **({"checkpoint": str(checkpoint)} if checkpoint is not None else {}),
                **({"sha256": digest} if digest else {}),
                **({"checkpoint_env": checkpoint_env} if checkpoint_env else {}),
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_manifest_resolves_and_hashes_local_checkpoint(tmp_path):
    checkpoint = tmp_path / "bge" / "weights.bin"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"offline-checkpoint")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, checkpoint, digest=digest)

    manifest = load_model_manifest(manifest_path, validate_checkpoints=True)
    validation = validate_local_checkpoints(manifest)["bge-m3-local"]
    assert validation.path == checkpoint.resolve()
    assert validation.bytes == len(b"offline-checkpoint")
    assert manifest.model("bge-m3-local").dimension == 4


def test_manifest_missing_checkpoint_fails_without_network_fallback(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, tmp_path / "missing.safetensors")
    manifest = load_model_manifest(manifest_path)
    with pytest.raises(CheckpointMissingError, match="does not exist"):
        validate_local_checkpoints(manifest)


def test_manifest_rejects_download_policy_and_checksum_mismatch(tmp_path):
    checkpoint = tmp_path / "weights.bin"
    checkpoint.write_bytes(b"weights")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, checkpoint, digest="0" * 64)
    manifest = load_model_manifest(manifest_path)
    with pytest.raises(CheckpointIntegrityError, match="checksum mismatch"):
        validate_local_checkpoints(manifest)

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["policy"]["download_weights"] = True
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ManifestError, match="downloads are disabled"):
        load_model_manifest(manifest_path)


def test_manifest_can_resolve_a_local_checkpoint_from_an_environment_path(tmp_path, monkeypatch):
    checkpoint = tmp_path / "weights.bin"
    checkpoint.write_bytes(b"env-checkpoint")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, None, checkpoint_env="LOCAL_BGE_CHECKPOINT")
    monkeypatch.setenv("LOCAL_BGE_CHECKPOINT", str(checkpoint))

    manifest = load_model_manifest(manifest_path, validate_checkpoints=True)
    assert manifest.model("bge-m3-local").checkpoint == checkpoint.resolve()
