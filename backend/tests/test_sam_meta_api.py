"""Tests for Meta Direct API SAM 3.1 integration."""

from __future__ import annotations

import json
import numpy as np
import pytest

from video_intelligence.model_adapters import SAMDetector


def test_sam_detector_meta_api_mock(monkeypatch):
    """Test that SAMDetector correctly parses responses from Meta API /v1/responses."""
    mock_meta_response = {
        "model": "sam-3.1",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [
                            {
                                "label": "person",
                                "bbox": [0.2, 0.3, 0.8, 0.7],  # ymin, xmin, ymax, xmax
                                "confidence": 0.96,
                                "category": "person",
                                "polygon": [[30.0, 20.0], [70.0, 20.0], [70.0, 80.0], [30.0, 80.0]],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    class MockHTTPResponse:
        def read(self):
            return json.dumps(mock_meta_response).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    import urllib.request
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=12.0: MockHTTPResponse(),
    )
    monkeypatch.setenv("SAM_MODEL_MODE", "meta")

    detector = SAMDetector(
        meta_api_key="LLM_test_key",
        meta_api_url="https://api.meta.ai/v1",
    )
    dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    detections = detector.detect(dummy_frame)

    assert len(detections) == 1
    det = detections[0]
    assert det.label == "person"
    assert det.confidence == pytest.approx(0.96)
    assert det.bbox == (30.0, 20.0, 70.0, 80.0)
    assert det.category == "person"
    assert len(det.segmentation_polygon) == 4

