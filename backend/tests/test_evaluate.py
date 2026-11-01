from __future__ import annotations

import pytest

from video_intelligence.domain import ObjectObservation, Zone
from video_intelligence.evaluate import evaluate_prediction_stream
from video_intelligence.evaluation import (
    DetectionAnnotation,
    EvaluationDataError,
    EvaluationDataset,
    SearchAnnotation,
    TimestampAnnotation,
    TrackingAnnotation,
)
from video_intelligence.pipeline import DetectedObject, FramePrediction


def test_prediction_stream_reports_real_detection_tracking_and_timestamp_scores():
    dataset = EvaluationDataset(
        timestamps=(
            TimestampAnnotation("default", 1.0, event_type="enter", object_id="track-1", zone_id="gate"),
        ),
        detections=(
            DetectionAnnotation("default", 0, (20, 20, 30, 30), label="object", timestamp_seconds=0.0),
            DetectionAnnotation("default", 1, (0, 0, 10, 10), label="object", timestamp_seconds=1.0),
        ),
        tracking=(
            TrackingAnnotation("default", 0, "track-1", (20, 20, 30, 30), timestamp_seconds=0.0),
            TrackingAnnotation("default", 1, "track-1", (0, 0, 10, 10), timestamp_seconds=1.0),
        ),
    )
    predictions = (
        FramePrediction(
            0,
            0.0,
            (DetectedObject("object", 25, 25, 100, 0.95, (20, 20, 30, 30)),),
            (ObjectObservation("track-1", 25, 25, "object", 0.95, (20, 20, 30, 30)),),
        ),
        FramePrediction(
            1,
            1.0,
            (DetectedObject("object", 5, 5, 100, 0.95, (0, 0, 10, 10)),),
            (ObjectObservation("track-1", 5, 5, "object", 0.95, (0, 0, 10, 10)),),
        ),
    )

    result = evaluate_prediction_stream(
        dataset,
        predictions,
        zones=(Zone("gate", ((0, 0), (10, 0), (10, 10), (0, 10))),),
    )

    assert result["detections"]["f1"] == 1.0
    assert result["tracking"]["IDF1"] == 1.0
    assert result["timestamps"]["matched"] == 1


def test_prediction_stream_requires_zones_for_timestamp_labels_and_does_not_fake_search_hits():
    dataset = EvaluationDataset(
        timestamps=(TimestampAnnotation("default", 1.0),),
        search=(SearchAnnotation("q1", "where", ("event-1",)),),
    )
    with pytest.raises(EvaluationDataError, match="evaluation zones"):
        evaluate_prediction_stream(dataset, ())

    search_only = EvaluationDataset(search=dataset.search)
    result = evaluate_prediction_stream(search_only, ())
    assert result["search"]["mrr"] == 0.0
