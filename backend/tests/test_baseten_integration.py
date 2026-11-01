"""Tests for Baseten model hosting & inference integration (SAM 3.1, SigLIP2, BGE-M3)."""

from __future__ import annotations

import base64
import numpy as np
import pytest

from video_intelligence.model_adapters import (
    ModelSettings,
    SAMDetector,
    SigLIP2EmbeddingService,
    EmbeddingService,
    model_capabilities,
    _call_baseten,
)


def test_model_capabilities_reports_baseten_config():
    settings = ModelSettings(
        baseten_api_key="b10_test_key",
        baseten_sam_endpoint="https://model-sam.api.baseten.co/production/predict",
        baseten_siglip_endpoint="https://model-siglip2.api.baseten.co/production/predict",
        baseten_bge_endpoint="https://model-bge.api.baseten.co/production/predict",
    )
    caps = model_capabilities(settings)
    assert "baseten" in caps
    assert caps["baseten"]["api_configured"] is True
    assert caps["baseten"]["sam_endpoint"] == "https://model-sam.api.baseten.co/production/predict"
    assert caps["baseten"]["siglip_endpoint"] == "https://model-siglip2.api.baseten.co/production/predict"
    assert caps["baseten"]["bge_endpoint"] == "https://model-bge.api.baseten.co/production/predict"


def test_sam_detector_baseten_remote_mock(monkeypatch):
    mock_response = {
        "detections": [
            {
                "label": "person",
                "x": 150.0,
                "y": 250.0,
                "area": 400.0,
                "confidence": 0.95,
                "bbox": [100.0, 180.0, 200.0, 320.0],
                "category": "person",
                "segmentation_polygon": [[100.0, 180.0], [200.0, 180.0], [200.0, 320.0], [100.0, 320.0]],
            }
        ],
        "count": 1,
    }

    monkeypatch.setattr(
        "video_intelligence.model_adapters._call_baseten",
        lambda endpoint, payload, key, timeout=15.0: mock_response,
    )

    detector = SAMDetector(
        baseten_endpoint="https://model-sam-3-1.api.baseten.co/production/predict",
        baseten_api_key="b10_test_key",
    )

    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    detections = detector.detect(dummy_frame)

    assert len(detections) == 1
    det = detections[0]
    assert det.label == "person"
    assert det.x == 150.0
    assert det.confidence == 0.95
    assert det.segmentation_polygon is not None
    assert len(det.segmentation_polygon) == 4


def test_bge_embedding_service_baseten_mock(monkeypatch):
    mock_vector = [0.123] * 1024

    monkeypatch.setattr(
        "video_intelligence.model_adapters._call_baseten",
        lambda endpoint, payload, key, timeout=10.0: {"embedding": mock_vector},
    )

    settings = ModelSettings(
        baseten_api_key="b10_test_key",
        baseten_bge_endpoint="https://model-bge.api.baseten.co/production/predict",
    )
    svc = EmbeddingService(settings)
    vec = svc.text("person crossing restricted zone")
    assert len(vec) == 1024
    assert vec[0] == pytest.approx(0.123)


def test_siglip2_embedding_service_baseten_mock(monkeypatch):
    mock_vector = [0.045] * 768

    monkeypatch.setattr(
        "video_intelligence.model_adapters._call_baseten",
        lambda endpoint, payload, key, timeout=12.0: {"embedding": mock_vector},
    )

    settings = ModelSettings(
        baseten_api_key="b10_test_key",
        baseten_siglip_endpoint="https://model-siglip2.api.baseten.co/production/predict",
    )
    svc = SigLIP2EmbeddingService(settings)
    dummy_frame = np.zeros((224, 224, 3), dtype=np.uint8)
    vec = svc.image(dummy_frame)
    assert len(vec) == 768
    assert vec[0] == pytest.approx(0.045)


def test_baseten_offline_fallback():
    # If Baseten is offline/times out, SAMDetector falls back to local edge/contour detection
    detector = SAMDetector(
        baseten_endpoint="https://invalid-host-unreachable.baseten.co/predict",
        baseten_api_key="b10_dummy",
    )
    dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    # Bright rectangle in middle
    dummy_frame[30:70, 30:70] = 255
    detections = detector.detect(dummy_frame)
    assert len(detections) >= 1
    assert detections[0].area > 0
