"""Run honest offline evaluation on one uploaded recording.

The CLI consumes real decoded frames and the selected detector/tracker. It
never turns missing labels, boxes, or zones into a passing score. Checkpoints
remain local-only through the model adapter boundary.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from itertools import islice
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

from .domain import FrameObservation, Zone, generate_zone_events
from .evaluation import EvaluationDataError, EvaluationDataset, evaluate_dataset, read_evaluation_dataset
from .model_adapters import ModelSettings, ModelUnavailableError, build_detector, build_tracker, settings_from_env
from .pipeline import BrightBlobDetector, FramePrediction, PipelineError, decode_predictions, require_device


class EvaluationRunError(RuntimeError):
    """Raised when a real recording evaluation cannot be completed."""


@dataclass(frozen=True, slots=True)
class RecordingEvaluationReport:
    video: str
    annotations: str
    sample_id: str
    profile: str
    tracker: str
    device: str
    frame_count: int
    metrics: Mapping[str, Any]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["metrics"] = dict(self.metrics)
        result["notes"] = list(self.notes)
        return result


def _zones_from_document(path: str | Path) -> tuple[Zone, ...]:
    source = Path(path).expanduser()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationRunError(f"unable to read zones JSON: {source}: {exc}") from exc
    values = document.get("zones") if isinstance(document, Mapping) else document
    if not isinstance(values, list):
        raise EvaluationRunError("zones JSON must be an array or an object containing a zones array")
    try:
        return tuple(Zone(str(item["zone_id"]), tuple(tuple(point) for point in item["polygon"])) for item in values)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationRunError(f"zones JSON contains an invalid zone: {exc}") from exc


def _prediction_mappings(
    predictions: Iterable[FramePrediction],
    *,
    sample_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[FrameObservation, ...]]:
    detections: list[dict[str, Any]] = []
    tracking: list[dict[str, Any]] = []
    observations: list[FrameObservation] = []
    for frame in predictions:
        observations.append(FrameObservation(frame.timestamp_seconds, frame.tracks))
        for detection in frame.detections:
            if detection.bbox is None:
                raise EvaluationRunError(
                    f"detector returned no bounding box for frame {frame.frame_index}; detection metrics cannot be computed"
                )
            detections.append(
                {
                    "sample_id": sample_id,
                    "frame_index": frame.frame_index,
                    "timestamp_seconds": frame.timestamp_seconds,
                    "bbox": list(detection.bbox),
                    "label": detection.label,
                    "confidence": detection.confidence,
                }
            )
        for track in frame.tracks:
            item: dict[str, Any] = {
                "sample_id": sample_id,
                "frame_index": frame.frame_index,
                "timestamp_seconds": frame.timestamp_seconds,
                "track_id": track.object_id,
                "label": track.label,
            }
            if track.bbox is not None:
                item["bbox"] = list(track.bbox)
            tracking.append(item)
    return detections, tracking, tuple(observations)


def evaluate_prediction_stream(
    dataset: EvaluationDataset,
    predictions: Iterable[FramePrediction],
    *,
    sample_id: str = "default",
    zones: Iterable[Zone] = (),
    timestamp_tolerance_seconds: float = 0.5,
    iou_threshold: float = 0.5,
    search_k: int = 10,
) -> dict[str, Any]:
    """Evaluate already-decoded real predictions against one annotation set."""

    prediction_list = tuple(predictions)
    detections, tracking, observations = _prediction_mappings(prediction_list, sample_id=sample_id)
    prediction_payload: dict[str, Iterable[Any]] = {
        "detections": detections,
        "tracking": tracking,
    }
    zone_values = tuple(zones)
    if dataset.timestamps:
        if not zone_values:
            raise EvaluationDataError("timestamp annotations require evaluation zones")
        events = generate_zone_events(observations, zone_values)
        prediction_payload["timestamps"] = [
            {
                "sample_id": sample_id,
                "timestamp_seconds": event.timestamp_seconds,
                "event_type": event.event_type,
                "object_id": event.object_id,
                "zone_id": event.zone_id,
                "object_label": event.object_label,
            }
            for event in events
        ]
    if dataset.search:
        # No semantic search result is silently treated as a perfect result.
        prediction_payload["search"] = [
            {"query_id": query.query_id, "retrieved_ids": []} for query in dataset.search
        ]
    return evaluate_dataset(
        dataset,
        prediction_payload,
        timestamp_tolerance_seconds=timestamp_tolerance_seconds,
        iou_threshold=iou_threshold,
        search_k=search_k,
    )


def run_recording_evaluation(
    video_path: str | Path,
    annotations_path: str | Path,
    *,
    profile: str = "baseline",
    tracker_profile: str = "centroid",
    device: str = "cpu",
    every: int = 1,
    sample_id: str = "default",
    zones: Iterable[Zone] = (),
    max_frames: int | None = None,
    timestamp_tolerance_seconds: float = 0.5,
    iou_threshold: float = 0.5,
    search_k: int = 10,
    model_settings: ModelSettings | None = None,
) -> RecordingEvaluationReport:
    """Decode a real uploaded recording and calculate requested metrics."""

    video = Path(video_path).expanduser().resolve()
    annotations = Path(annotations_path).expanduser().resolve()
    if not video.is_file():
        raise EvaluationRunError(f"uploaded recording does not exist: {video}")
    if not annotations.is_file():
        raise EvaluationRunError(f"evaluation annotations do not exist: {annotations}")
    if every < 1 or (max_frames is not None and max_frames < 1):
        raise EvaluationRunError("every and max_frames must be positive when provided")
    dataset = read_evaluation_dataset(annotations)
    require_device(device)
    base = model_settings or settings_from_env()
    settings = ModelSettings(
        profile=profile,
        device=device,
        yolo_model=base.yolo_model,
        siglip_model=base.siglip_model,
        bge_model=base.bge_model,
        qwen_model=base.qwen_model,
        meta_api_key=base.meta_api_key,
        meta_api_url=base.meta_api_url,
        meta_model=base.meta_model,
        baseten_api_key=base.baseten_api_key,
        baseten_sam_endpoint=base.baseten_sam_endpoint,
        baseten_siglip_endpoint=base.baseten_siglip_endpoint,
        baseten_bge_endpoint=base.baseten_bge_endpoint,
        model_cache=base.model_cache,
    )
    try:
        detector = build_detector(settings)
        tracker = build_tracker(settings, profile=tracker_profile)
        if profile == "baseline":
            detector = BrightBlobDetector()
        stream = decode_predictions(video, every=every, detector=detector, tracker=tracker)
        predictions = tuple(islice(stream, max_frames)) if max_frames is not None else tuple(stream)
        metrics = evaluate_prediction_stream(
            dataset,
            predictions,
            sample_id=sample_id,
            zones=zones,
            timestamp_tolerance_seconds=timestamp_tolerance_seconds,
            iou_threshold=iou_threshold,
            search_k=search_k,
        )
    except (ModelUnavailableError, PipelineError, EvaluationDataError, OSError, ValueError) as exc:
        raise EvaluationRunError(str(exc)) from exc
    return RecordingEvaluationReport(
        video=str(video),
        annotations=str(annotations),
        sample_id=sample_id,
        profile=profile,
        tracker=tracker_profile,
        device=device,
        frame_count=len(predictions),
        metrics=metrics,
        notes=("Metrics are computed from the supplied annotations and real model output; no synthetic labels are added.",),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate a real uploaded recording against local annotations")
    parser.add_argument("--video", required=True, help="uploaded recording path")
    parser.add_argument("--annotations", required=True, help="JSON/JSONL/CSV evaluation annotations")
    parser.add_argument("--profile", choices=("baseline", "yolo"), default="baseline")
    parser.add_argument("--tracker", choices=("centroid", "bytetrack"), default="centroid")
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--sample-id", default="default")
    parser.add_argument("--zones", type=Path, help="JSON array/object containing evaluation zones")
    parser.add_argument("--timestamp-tolerance", type=float, default=0.5)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--search-k", type=int, default=10)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        zones = _zones_from_document(args.zones) if args.zones else ()
        report = run_recording_evaluation(
            args.video,
            args.annotations,
            profile=args.profile,
            tracker_profile=args.tracker,
            device=args.device,
            every=args.every,
            sample_id=args.sample_id,
            zones=zones,
            max_frames=args.max_frames,
            timestamp_tolerance_seconds=args.timestamp_tolerance,
            iou_threshold=args.iou_threshold,
            search_k=args.search_k,
        )
    except (EvaluationRunError, EvaluationDataError, OSError, ValueError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 2
    document = json.dumps(report.as_dict(), indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(document + "\n", encoding="utf-8")
    print(document)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EvaluationRunError",
    "RecordingEvaluationReport",
    "evaluate_prediction_stream",
    "main",
    "run_recording_evaluation",
]
