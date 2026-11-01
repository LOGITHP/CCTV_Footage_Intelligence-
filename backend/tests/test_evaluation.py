import json

from video_intelligence.evaluation import (
    detection_metrics,
    evaluate_dataset,
    read_evaluation_dataset,
    search_metrics,
    timestamp_metrics,
    tracking_metrics,
)


def test_timestamp_detection_and_tracking_metrics_are_deterministic():
    timestamps = timestamp_metrics(
        [{"sample_id": "cam-a", "timestamp_seconds": 10.0, "event_type": "enter"}],
        [{"sample_id": "cam-a", "timestamp_seconds": 10.2, "event_type": "enter"}],
        tolerance_seconds=0.25,
    )
    assert timestamps["true_positives"] == 1
    assert timestamps["f1"] == 1.0

    detections = detection_metrics(
        [{"sample_id": "cam-a", "frame_index": 4, "label": "person", "bbox": [0, 0, 10, 10]}],
        [{"sample_id": "cam-a", "frame_index": 4, "label": "person", "bbox": [1, 1, 9, 9], "confidence": 0.9}],
        iou_threshold=0.5,
    )
    assert detections["true_positives"] == 1
    assert 0.0 < detections["mean_iou"] < 1.0

    tracks = tracking_metrics(
        [
            {"sample_id": "cam-a", "frame_index": 0, "track_id": "gt-1", "bbox": [0, 0, 10, 10]},
            {"sample_id": "cam-a", "frame_index": 1, "track_id": "gt-1", "bbox": [0, 0, 10, 10]},
        ],
        [
            {"sample_id": "cam-a", "frame_index": 0, "track_id": "pred-1", "bbox": [0, 0, 10, 10]},
            {"sample_id": "cam-a", "frame_index": 1, "track_id": "pred-2", "bbox": [0, 0, 10, 10]},
        ],
    )
    assert tracks["true_positives"] == 2
    assert tracks["id_switches"] == 1
    assert tracks["mota"] == 0.5


def test_search_metrics_reader_and_dataset_evaluation(tmp_path):
    path = tmp_path / "annotations.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {"name": "fixture"},
                "timestamps": [{"sample_id": "cam-a", "timestamp_seconds": 3.0}],
                "detections": [{"sample_id": "cam-a", "frame_index": 0, "bbox": [0, 0, 5, 5]}],
                "tracking": [{"sample_id": "cam-a", "frame_index": 0, "track_id": "t-1"}],
                "search": [{"query_id": "q-1", "query": "person", "relevant_ids": ["e-1"]}],
            }
        ),
        encoding="utf-8",
    )
    dataset = read_evaluation_dataset(path)
    assert dataset.metadata["name"] == "fixture"
    assert len(dataset.timestamps) == len(dataset.detections) == len(dataset.tracking) == len(dataset.search) == 1

    search = search_metrics(
        dataset.search,
        [{"query_id": "q-1", "retrieved_ids": ["e-2", "e-1"]}],
        k=2,
    )
    assert search["mrr"] == 0.5
    assert search["recall_at_k"] == 1.0

    result = evaluate_dataset(
        dataset,
        {
            "timestamps": [{"sample_id": "cam-a", "timestamp_seconds": 3.0}],
            "detections": [{"sample_id": "cam-a", "frame_index": 0, "bbox": [0, 0, 5, 5]}],
            "tracking": [{"sample_id": "cam-a", "frame_index": 0, "track_id": "t-1"}],
            "search": [{"query_id": "q-1", "retrieved_ids": ["e-1"]}],
        },
    )
    assert result["timestamps"]["f1"] == 1.0
    assert result["search"]["map"] == 1.0
