from pathlib import Path

import pytest

from video_intelligence.infrastructure.config import InfrastructureSettings
from video_intelligence.infrastructure.object_storage import LocalObjectStorage
from video_intelligence.infrastructure.queue import InlineDispatcher
from video_intelligence.model_adapters import QwenGroundedResponder, ModelSettings, ModelUnavailableError, build_detector, model_capabilities


def test_environment_settings_are_explicit_and_non_secret_repr():
    settings = InfrastructureSettings.from_env({
        "DATABASE_URL": "postgresql://db/video",
        "OBJECT_STORAGE_BACKEND": "minio",
        "MINIO_ENDPOINT": "minio:9000",
        "MINIO_ACCESS_KEY": "secret-user",
        "MINIO_SECRET_KEY": "secret-value",
        "REDIS_URL": "redis://redis:6379/0",
    })
    assert settings.database.url.startswith("postgresql")
    assert settings.object_storage.backend == "s3"
    assert settings.queue.broker_url.startswith("redis")
    assert "secret-value" not in repr(settings)


def test_local_object_storage_and_inline_dispatcher_are_offline(tmp_path):
    source = Path(tmp_path) / "source.bin"
    source.write_bytes(b"evidence")
    settings = InfrastructureSettings.from_env({"OBJECT_STORAGE_ROOT": str(tmp_path / "objects")})
    storage = LocalObjectStorage(settings.object_storage)
    assert storage.put_file(source, "clips/event.mp4") == "clips/event.mp4"
    assert (Path(settings.object_storage.local_root) / "clips/event.mp4").read_bytes() == b"evidence"
    assert InlineDispatcher().dispatch("video.process") == "inline:video.process"


def test_yolo_profile_requires_explicit_model_and_baseline_is_named():
    with pytest.raises(ModelUnavailableError, match="YOLO_MODEL"):
        build_detector(ModelSettings(profile="yolo"))
    assert model_capabilities(ModelSettings(profile="baseline"))["detector"] == "bright-blob-baseline"


def test_qwen_responder_uses_chat_template_without_hidden_thinking():
    import torch

    class FakeTokenizer:
        chat_template = "template"

        def __init__(self):
            self.kwargs = None

        def apply_chat_template(self, messages, **kwargs):
            self.kwargs = kwargs
            assert messages[0]["role"] == "system"
            assert "Verified citations" in messages[1]["content"]
            return {
                "input_ids": torch.tensor([[1, 2]]),
                "attention_mask": torch.tensor([[1, 1]]),
            }

        def decode(self, tokens, skip_special_tokens=True):
            return "The verified event occurred at 1.000 seconds."

    class FakeModel:
        def generate(self, **inputs):
            return torch.tensor([[1, 2, 3]])

    responder = object.__new__(QwenGroundedResponder)
    responder.tokenizer = FakeTokenizer()
    responder.model = FakeModel()
    responder.device = torch.device("cpu")
    answer = responder.respond(
        "What happened?",
        [{"event_id": "event-1", "timestamp_seconds": 1.0}],
    )
    assert answer == "The verified event occurred at 1.000 seconds."
    assert responder.tokenizer.kwargs["enable_thinking"] is False
    assert responder.tokenizer.kwargs["add_generation_prompt"] is True
