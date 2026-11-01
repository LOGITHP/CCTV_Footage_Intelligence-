"""Offline evaluation readers and metrics for recorded-video annotations.

The reader accepts a small, explicit JSON/JSONL/CSV interchange format so
annotations can be produced by labeling tools without pulling another dataset
framework into the service.  Metric functions also accept plain mappings,
which makes them useful for evaluating persisted API payloads directly.

No metric fabricates labels or timestamps.  Missing fields are rejected with a
line/record-specific error, while matching remains deterministic and
one-to-one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class EvaluationDataError(ValueError):
    """Raised when an evaluation dataset or annotation is malformed."""


def _mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    try:
        value = asdict(item)
    except TypeError as exc:
        raise EvaluationDataError("annotation must be a mapping or dataclass") from exc
    if not isinstance(value, Mapping):
        raise EvaluationDataError("annotation must be a mapping or dataclass")
    return value


def _string(item: Mapping[str, Any], names: tuple[str, ...], *, default: str = "") -> str:
    for name in names:
        value = item.get(name)
        if value is not None:
            if not isinstance(value, str):
                value = str(value)
            value = value.strip()
            if value:
                return value
    return default


def _number(value: Any, field_name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise EvaluationDataError(f"{field_name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationDataError(f"{field_name} must be numeric") from exc
    if not math.isfinite(result):
        raise EvaluationDataError(f"{field_name} must be finite")
    if minimum is not None and result < minimum:
        raise EvaluationDataError(f"{field_name} must be at least {minimum}")
    return result


def _integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    number = _number(value, field_name, minimum=minimum)
    if number != int(number):
        raise EvaluationDataError(f"{field_name} must be an integer")
    return int(number)


def _optional_timestamp(item: Mapping[str, Any]) -> float | None:
    value = item.get("timestamp_seconds", item.get("timestamp", item.get("time")))
    return None if value is None or value == "" else _number(value, "timestamp_seconds", minimum=0)


def _bbox_value(value: Any, field_name: str = "bbox") -> tuple[float, float, float, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvaluationDataError(f"{field_name} must be an array of four numbers") from exc
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise EvaluationDataError(f"{field_name} must be an array of four numbers")
    box = tuple(_number(item, field_name) for item in value)
    left, top, right, bottom = box
    if right < left or bottom < top:
        raise EvaluationDataError(f"{field_name} must have right >= left and bottom >= top")
    return box


@dataclass(frozen=True, slots=True)
class TimestampAnnotation:
    sample_id: str
    timestamp_seconds: float
    event_type: str | None = None
    object_id: str | None = None
    object_label: str | None = None
    zone_id: str | None = None


@dataclass(frozen=True, slots=True)
class DetectionAnnotation:
    sample_id: str
    frame_index: int | None
    bbox: tuple[float, float, float, float]
    label: str = "object"
    timestamp_seconds: float | None = None
    confidence: float | None = None
    object_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrackingAnnotation:
    sample_id: str
    frame_index: int
    track_id: str
    bbox: tuple[float, float, float, float] | None = None
    label: str = "object"
    timestamp_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SearchAnnotation:
    query_id: str
    query: str
    relevant_ids: tuple[str, ...]
    retrieved_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """All annotation families associated with one evaluation run."""

    timestamps: tuple[TimestampAnnotation, ...] = ()
    detections: tuple[DetectionAnnotation, ...] = ()
    tracking: tuple[TrackingAnnotation, ...] = ()
    search: tuple[SearchAnnotation, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def tracks(self) -> tuple[TrackingAnnotation, ...]:
        return self.tracking


def _timestamp_annotation(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> TimestampAnnotation:
    merged = {**defaults, **raw}
    value = merged.get("timestamp_seconds", merged.get("timestamp", merged.get("time")))
    if value is None:
        raise EvaluationDataError("timestamp annotation requires timestamp_seconds")
    return TimestampAnnotation(
        sample_id=_string(merged, ("sample_id", "video_id", "camera_id"), default="default"),
        timestamp_seconds=_number(value, "timestamp_seconds", minimum=0),
        event_type=_string(merged, ("event_type", "type"), default="") or None,
        object_id=_string(merged, ("object_id", "track_id"), default="") or None,
        object_label=_string(merged, ("object_label", "label"), default="") or None,
        zone_id=_string(merged, ("zone_id", "zone"), default="") or None,
    )


def _detection_annotation(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> DetectionAnnotation:
    merged = {**defaults, **raw}
    frame_value = merged.get("frame_index", merged.get("frame"))
    frame = None if frame_value is None or frame_value == "" else _integer(frame_value, "frame_index")
    bbox = merged.get("bbox", merged.get("box", merged.get("xyxy")))
    if bbox is None:
        components = [merged.get(name) for name in ("left", "top", "right", "bottom")]
        if any(value is None for value in components):
            raise EvaluationDataError("detection annotation requires bbox or left/top/right/bottom")
        bbox = components
    confidence_value = merged.get("confidence")
    confidence = None if confidence_value in (None, "") else _number(confidence_value, "confidence")
    if confidence is not None and not 0 <= confidence <= 1:
        raise EvaluationDataError("confidence must be between 0 and 1")
    return DetectionAnnotation(
        sample_id=_string(merged, ("sample_id", "video_id", "camera_id"), default="default"),
        frame_index=frame,
        bbox=_bbox_value(bbox),
        label=_string(merged, ("label", "object_label"), default="object"),
        timestamp_seconds=_optional_timestamp(merged),
        confidence=confidence,
        object_id=_string(merged, ("object_id", "track_id"), default="") or None,
    )


def _tracking_annotation(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> TrackingAnnotation:
    merged = {**defaults, **raw}
    frame_value = merged.get("frame_index", merged.get("frame"))
    if frame_value is None:
        raise EvaluationDataError("tracking annotation requires frame_index")
    track_id = _string(merged, ("track_id", "object_id", "id"))
    if not track_id:
        raise EvaluationDataError("tracking annotation requires track_id")
    bbox_value = merged.get("bbox", merged.get("box", merged.get("xyxy")))
    bbox = None if bbox_value in (None, "") else _bbox_value(bbox_value)
    return TrackingAnnotation(
        sample_id=_string(merged, ("sample_id", "video_id", "camera_id"), default="default"),
        frame_index=_integer(frame_value, "frame_index"),
        track_id=track_id,
        bbox=bbox,
        label=_string(merged, ("label", "object_label"), default="object"),
        timestamp_seconds=_optional_timestamp(merged),
    )


def _search_annotation(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> SearchAnnotation:
    merged = {**defaults, **raw}
    query_id = _string(merged, ("query_id", "id"))
    if not query_id:
        raise EvaluationDataError("search annotation requires query_id")
    query = _string(merged, ("query", "text"), default="")
    relevant = merged.get("relevant_ids", merged.get("relevant", merged.get("ground_truth", ())))
    retrieved = merged.get("retrieved_ids", merged.get("results", merged.get("predicted_ids", ())))
    def ids(value: Any, field_name: str) -> tuple[str, ...]:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple)):
            raise EvaluationDataError(f"{field_name} must be an array of IDs")
        result = []
        for item in value:
            if isinstance(item, Mapping):
                item = item.get("id", item.get("event_id", item.get("document_id")))
            if item is None:
                continue
            text = str(item).strip()
            if text:
                result.append(text)
        return tuple(result)
    return SearchAnnotation(query_id, query, ids(relevant, "relevant_ids"), ids(retrieved, "retrieved_ids"))


def _records_from_section(value: Any, defaults: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvaluationDataError("annotation sections must be arrays")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise EvaluationDataError("each annotation must be an object")
        result.append({**defaults, **item})
    return result


def _load_document(path: Path) -> Any:
    try:
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            lines = []
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise EvaluationDataError(f"invalid JSON on line {line_number}: {exc}") from exc
            return lines
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8", newline="") as stream:
                return list(csv.DictReader(stream))
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvaluationDataError(f"unable to read evaluation dataset {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationDataError(f"invalid evaluation JSON: {path}: {exc}") from exc


def read_evaluation_dataset(path: str | Path) -> EvaluationDataset:
    """Read JSON, JSONL, or CSV timestamp/detection/tracking/search annotations."""

    source = Path(path).expanduser()
    if not source.is_file():
        raise EvaluationDataError(f"evaluation dataset does not exist: {source}")
    document = _load_document(source)
    if isinstance(document, list):
        # JSONL and flat JSON arrays use a per-row ``type``/``kind`` field.
        root: Mapping[str, Any] = {"annotations": document}
    elif isinstance(document, Mapping):
        root = document
    else:
        raise EvaluationDataError("evaluation dataset root must be an object or array")

    metadata = root.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise EvaluationDataError("metadata must be an object")
    timestamps: list[TimestampAnnotation] = []
    detections: list[DetectionAnnotation] = []
    tracking: list[TrackingAnnotation] = []
    search: list[SearchAnnotation] = []

    sections = {
        "timestamps": root.get("timestamps", root.get("timestamp_annotations")),
        "detections": root.get("detections", root.get("detection_annotations")),
        "tracking": root.get("tracking", root.get("tracks", root.get("tracking_annotations"))),
        "search": root.get("search", root.get("search_annotations")),
    }
    for item in _records_from_section(sections["timestamps"], {}):
        timestamps.append(_timestamp_annotation(item, {}))
    for item in _records_from_section(sections["detections"], {}):
        detections.append(_detection_annotation(item, {}))
    for item in _records_from_section(sections["tracking"], {}):
        tracking.append(_tracking_annotation(item, {}))
    for item in _records_from_section(sections["search"], {}):
        search.append(_search_annotation(item, {}))

    annotations = root.get("annotations")
    if annotations is not None:
        if not isinstance(annotations, list):
            raise EvaluationDataError("annotations must be an array")
        for index, item in enumerate(annotations):
            if not isinstance(item, Mapping):
                raise EvaluationDataError(f"annotations[{index}] must be an object")
            kind = _string(item, ("kind", "type", "annotation_type"), default="")
            if kind in {"timestamp", "timestamps", "event"}:
                timestamps.append(_timestamp_annotation(item, {}))
            elif kind in {"detection", "detections"}:
                detections.append(_detection_annotation(item, {}))
            elif kind in {"tracking", "track", "tracks"}:
                tracking.append(_tracking_annotation(item, {}))
            elif kind in {"search", "query"}:
                search.append(_search_annotation(item, {}))
            else:
                raise EvaluationDataError(f"annotations[{index}] has unknown kind/type: {kind or '<missing>'}")

    # A convenient dataset format nests all annotation sections per recording
    # under ``samples``.  Flattening preserves the source IDs in each record.
    samples = root.get("samples")
    if samples is not None:
        if not isinstance(samples, list):
            raise EvaluationDataError("samples must be an array")
        for sample_index, sample in enumerate(samples):
            if not isinstance(sample, Mapping):
                raise EvaluationDataError(f"samples[{sample_index}] must be an object")
            sample_defaults = {
                key: sample[key]
                for key in ("sample_id", "video_id", "camera_id")
                if key in sample
            }
            for item in _records_from_section(sample.get("timestamps"), sample_defaults):
                timestamps.append(_timestamp_annotation(item, sample_defaults))
            for item in _records_from_section(sample.get("detections"), sample_defaults):
                detections.append(_detection_annotation(item, sample_defaults))
            for item in _records_from_section(sample.get("tracking", sample.get("tracks")), sample_defaults):
                tracking.append(_tracking_annotation(item, sample_defaults))
            for item in _records_from_section(sample.get("search"), sample_defaults):
                search.append(_search_annotation(item, sample_defaults))

    return EvaluationDataset(tuple(timestamps), tuple(detections), tuple(tracking), tuple(search), dict(metadata))


load_evaluation_dataset = read_evaluation_dataset
read_annotations = read_evaluation_dataset
load_dataset = read_evaluation_dataset


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _group_key(item: Mapping[str, Any], *, include_frame: bool = False) -> tuple[Any, ...]:
    key: list[Any] = [_string(item, ("sample_id", "video_id", "camera_id"), default="default")]
    if include_frame:
        frame = item.get("frame_index", item.get("frame"))
        key.append(None if frame is None or frame == "" else int(float(frame)))
    return tuple(key)


def _label(item: Mapping[str, Any]) -> str:
    return _string(item, ("label", "object_label", "class_name"), default="object")


def _timestamp(item: Mapping[str, Any]) -> float:
    value = item.get("timestamp_seconds", item.get("timestamp", item.get("time")))
    return _number(value, "timestamp_seconds", minimum=0)


def timestamp_metrics(
    expected: Iterable[TimestampAnnotation | Mapping[str, Any]],
    predicted: Iterable[TimestampAnnotation | Mapping[str, Any]],
    *,
    tolerance_seconds: float = 0.5,
    key_fields: Sequence[str] = ("sample_id", "event_type", "object_id", "zone_id"),
) -> dict[str, float | int]:
    """Match event timestamps one-to-one within a source-time tolerance."""

    tolerance = _number(tolerance_seconds, "tolerance_seconds", minimum=0)
    expected_items = [_mapping(item) for item in expected]
    predicted_items = [_mapping(item) for item in predicted]
    candidates: list[tuple[float, int, int]] = []
    for expected_index, reference in enumerate(expected_items):
        for predicted_index, candidate in enumerate(predicted_items):
            compatible = True
            for field_name in key_fields:
                if field_name not in reference or reference.get(field_name) in (None, ""):
                    continue
                if field_name not in candidate or candidate.get(field_name) in (None, ""):
                    continue
                if str(reference[field_name]) != str(candidate[field_name]):
                    compatible = False
                    break
            if not compatible:
                continue
            delta = abs(_timestamp(reference) - _timestamp(candidate))
            if delta <= tolerance:
                candidates.append((delta, expected_index, predicted_index))
    candidates.sort(key=lambda value: (value[0], value[1], value[2]))
    matched_expected: set[int] = set()
    matched_predicted: set[int] = set()
    errors: list[float] = []
    for delta, expected_index, predicted_index in candidates:
        if expected_index in matched_expected or predicted_index in matched_predicted:
            continue
        matched_expected.add(expected_index)
        matched_predicted.add(predicted_index)
        errors.append(delta)
    result = _prf(len(errors), len(predicted_items) - len(errors), len(expected_items) - len(errors))
    result.update(
        {
            "tolerance_seconds": tolerance,
            "matched": len(errors),
            "timestamp_accuracy": len(errors) / len(expected_items) if expected_items else 1.0,
            "mean_absolute_error_seconds": sum(errors) / len(errors) if errors else None,
        }
    )
    return result


def _box(item: Mapping[str, Any]) -> tuple[float, float, float, float]:
    value = item.get("bbox", item.get("box", item.get("xyxy")))
    if value is None:
        components = [item.get(name) for name in ("left", "top", "right", "bottom")]
        if any(component is None for component in components):
            raise EvaluationDataError("detection/tracking item requires bbox")
        value = components
    return _bbox_value(value)


def intersection_over_union(left: Sequence[float], right: Sequence[float]) -> float:
    """Return IoU for ``(left, top, right, bottom)`` boxes."""

    first = _bbox_value(left)
    second = _bbox_value(right)
    overlap_left = max(first[0], second[0])
    overlap_top = max(first[1], second[1])
    overlap_right = min(first[2], second[2])
    overlap_bottom = min(first[3], second[3])
    overlap = max(0.0, overlap_right - overlap_left) * max(0.0, overlap_bottom - overlap_top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - overlap
    return overlap / union if union else 0.0


def detection_metrics(
    expected: Iterable[DetectionAnnotation | Mapping[str, Any]],
    predicted: Iterable[DetectionAnnotation | Mapping[str, Any]],
    *,
    iou_threshold: float = 0.5,
    confidence_threshold: float = 0.0,
) -> dict[str, float | int]:
    """Compute one-to-one frame/label detection precision, recall, and F1."""

    threshold = _number(iou_threshold, "iou_threshold", minimum=0)
    if threshold > 1:
        raise EvaluationDataError("iou_threshold must be at most 1")
    confidence = _number(confidence_threshold, "confidence_threshold", minimum=0)
    if confidence > 1:
        raise EvaluationDataError("confidence_threshold must be at most 1")
    references = [_mapping(item) for item in expected]
    candidates = [item for item in (_mapping(value) for value in predicted) if item.get("confidence") in (None, "") or _number(item["confidence"], "confidence") >= confidence]
    pairs: list[tuple[float, int, int]] = []
    for reference_index, reference in enumerate(references):
        for candidate_index, candidate in enumerate(candidates):
            if _group_key(reference, include_frame=True) != _group_key(candidate, include_frame=True):
                continue
            if _label(reference) != _label(candidate):
                continue
            overlap = intersection_over_union(_box(reference), _box(candidate))
            if overlap >= threshold:
                pairs.append((overlap, reference_index, candidate_index))
    pairs.sort(key=lambda value: (-value[0], value[1], value[2]))
    used_reference: set[int] = set()
    used_candidate: set[int] = set()
    overlaps: list[float] = []
    for overlap, reference_index, candidate_index in pairs:
        if reference_index in used_reference or candidate_index in used_candidate:
            continue
        used_reference.add(reference_index)
        used_candidate.add(candidate_index)
        overlaps.append(overlap)
    result = _prf(len(overlaps), len(candidates) - len(overlaps), len(references) - len(overlaps))
    result.update({"iou_threshold": threshold, "mean_iou": sum(overlaps) / len(overlaps) if overlaps else 0.0})
    return result


def _track_id(item: Mapping[str, Any]) -> str:
    value = _string(item, ("track_id", "object_id", "id"))
    if not value:
        raise EvaluationDataError("tracking item requires track_id")
    return value


def tracking_metrics(
    expected: Iterable[TrackingAnnotation | Mapping[str, Any]],
    predicted: Iterable[TrackingAnnotation | Mapping[str, Any]],
    *,
    iou_threshold: float = 0.5,
) -> dict[str, float | int]:
    """Compute detection-style tracking scores, ID switches, MOTA, and IDF1."""

    threshold = _number(iou_threshold, "iou_threshold", minimum=0)
    if threshold > 1:
        raise EvaluationDataError("iou_threshold must be at most 1")
    references = [_mapping(item) for item in expected]
    candidates = [_mapping(item) for item in predicted]
    by_frame_expected: dict[tuple[Any, ...], list[tuple[int, Mapping[str, Any]]]] = {}
    by_frame_predicted: dict[tuple[Any, ...], list[tuple[int, Mapping[str, Any]]]] = {}
    for index, item in enumerate(references):
        by_frame_expected.setdefault(_group_key(item, include_frame=True), []).append((index, item))
    for index, item in enumerate(candidates):
        by_frame_predicted.setdefault(_group_key(item, include_frame=True), []).append((index, item))

    matched = 0
    id_switches = 0
    previous_assignments: dict[tuple[Any, ...] | str, str] = {}
    matched_reference: set[int] = set()
    matched_candidate: set[int] = set()
    for frame_key in sorted(set(by_frame_expected) | set(by_frame_predicted), key=str):
        frame_references = by_frame_expected.get(frame_key, [])
        frame_candidates = by_frame_predicted.get(frame_key, [])
        pairs: list[tuple[float, int, int]] = []
        for reference_index, reference in frame_references:
            for candidate_index, candidate in frame_candidates:
                same_label = _label(reference) == _label(candidate)
                reference_has_box = any(reference.get(name) not in (None, "") for name in ("bbox", "box", "xyxy"))
                candidate_has_box = any(candidate.get(name) not in (None, "") for name in ("bbox", "box", "xyxy"))
                overlap = (
                    intersection_over_union(_box(reference), _box(candidate))
                    if reference_has_box and candidate_has_box
                    else 1.0 if _track_id(reference) == _track_id(candidate) else 0.0
                )
                if same_label and overlap >= threshold:
                    pairs.append((overlap, reference_index, candidate_index))
        pairs.sort(key=lambda value: (-value[0], value[1], value[2]))
        for overlap, reference_index, candidate_index in pairs:
            if reference_index in matched_reference or candidate_index in matched_candidate:
                continue
            matched_reference.add(reference_index)
            matched_candidate.add(candidate_index)
            matched += 1
            reference = references[reference_index]
            candidate = candidates[candidate_index]
            reference_key = (frame_key[0], _track_id(reference))
            candidate_id = _track_id(candidate)
            prior = previous_assignments.get(reference_key)
            if prior is not None and prior != candidate_id:
                id_switches += 1
            previous_assignments[reference_key] = candidate_id

    true_positives = matched
    false_positives = len(candidates) - matched
    false_negatives = len(references) - matched
    result = _prf(true_positives, false_positives, false_negatives)
    id_precision = true_positives / (true_positives + false_positives) if true_positives + false_positives else 1.0
    id_recall = true_positives / (true_positives + false_negatives) if true_positives + false_negatives else 1.0
    idf1 = 2 * true_positives / (2 * true_positives + false_positives + false_negatives) if true_positives else 0.0
    mota_denominator = len(references)
    mota = 1.0 - (false_negatives + false_positives + id_switches) / mota_denominator if mota_denominator else 1.0
    result.update(
        {
            "iou_threshold": threshold,
            "id_switches": id_switches,
            "id_switch_count": id_switches,
            "id_precision": id_precision,
            "id_recall": id_recall,
            "idf1": idf1,
            "IDF1": idf1,
            "mota": mota,
            "MOTA": mota,
        }
    )
    return result


def _retrieved(item: Mapping[str, Any]) -> tuple[str, ...]:
    value = item.get("retrieved_ids", item.get("results", item.get("predicted_ids", ())))
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise EvaluationDataError("retrieved_ids must be an array")
    ids: list[str] = []
    for result in value:
        if isinstance(result, Mapping):
            result = result.get("id", result.get("event_id", result.get("document_id")))
        if result is not None:
            ids.append(str(result))
    return tuple(ids)


def search_metrics(
    expected: Iterable[SearchAnnotation | Mapping[str, Any]],
    predicted: Iterable[SearchAnnotation | Mapping[str, Any]] | None = None,
    *,
    k: int = 10,
) -> dict[str, Any]:
    """Compute P@k, R@k, average precision, MRR, and nDCG for search queries."""

    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise EvaluationDataError("k must be a positive integer")
    references = [_mapping(item) for item in expected]
    if predicted is None:
        predicted_items = references
    else:
        predicted_items = [_mapping(item) for item in predicted]
    predicted_by_query = {_string(item, ("query_id", "id")): item for item in predicted_items}
    rows: list[dict[str, float | int]] = []
    for reference in references:
        query_id = _string(reference, ("query_id", "id"))
        relevant_value = reference.get("relevant_ids", reference.get("relevant", reference.get("ground_truth", ())))
        if isinstance(relevant_value, str):
            relevant = {part.strip() for part in relevant_value.split(",") if part.strip()}
        else:
            relevant = {str(value) for value in (relevant_value or ())}
        source = predicted_by_query.get(query_id, reference)
        retrieved = _retrieved(source)[:k]
        hits = [index for index, identifier in enumerate(retrieved, 1) if identifier in relevant]
        precision = len(hits) / k
        recall = len(hits) / len(relevant) if relevant else 1.0
        average_precision = sum(index / rank for index, rank in enumerate(hits, 1)) / len(relevant) if relevant else 1.0
        reciprocal_rank = 1 / hits[0] if hits else 0.0
        dcg = sum(1 / math.log2(rank + 1) for rank in hits)
        ideal_hits = min(len(relevant), k)
        idcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
        rows.append(
            {
                "precision_at_k": precision,
                "recall_at_k": recall,
                "average_precision": average_precision,
                "reciprocal_rank": reciprocal_rank,
                "ndcg_at_k": dcg / idcg if idcg else 1.0,
                "relevant": len(relevant),
                "retrieved": len(retrieved),
            }
        )
    if not rows:
        return {
            "queries": 0,
            "precision_at_k": 0.0,
            "precision@k": 0.0,
            "recall_at_k": 0.0,
            "recall@k": 0.0,
            "map": 0.0,
            "mAP": 0.0,
            "mrr": 0.0,
            "MRR": 0.0,
            "ndcg_at_k": 0.0,
            "k": k,
            "per_query": [],
        }
    def mean(key: str) -> float:
        return sum(float(row[key]) for row in rows) / len(rows)
    return {
        "queries": len(rows),
        "precision_at_k": mean("precision_at_k"),
        "precision@k": mean("precision_at_k"),
        "recall_at_k": mean("recall_at_k"),
        "recall@k": mean("recall_at_k"),
        "map": mean("average_precision"),
        "mean_average_precision": mean("average_precision"),
        "mAP": mean("average_precision"),
        "mrr": mean("reciprocal_rank"),
        "MRR": mean("reciprocal_rank"),
        "ndcg_at_k": mean("ndcg_at_k"),
        "k": k,
        "per_query": rows,
    }


def evaluate_dataset(
    dataset: EvaluationDataset,
    predictions: Mapping[str, Iterable[Any]],
    *,
    timestamp_tolerance_seconds: float = 0.5,
    iou_threshold: float = 0.5,
    search_k: int = 10,
) -> dict[str, Any]:
    """Evaluate all annotation families present in a dataset."""

    result: dict[str, Any] = {}
    if dataset.timestamps:
        result["timestamps"] = timestamp_metrics(
            dataset.timestamps,
            predictions.get("timestamps", ()),
            tolerance_seconds=timestamp_tolerance_seconds,
        )
    if dataset.detections:
        result["detections"] = detection_metrics(
            dataset.detections,
            predictions.get("detections", ()),
            iou_threshold=iou_threshold,
        )
    if dataset.tracking:
        result["tracking"] = tracking_metrics(
            dataset.tracking,
            predictions.get("tracking", predictions.get("tracks", ())),
            iou_threshold=iou_threshold,
        )
    if dataset.search:
        result["search"] = search_metrics(
            dataset.search,
            predictions.get("search"),
            k=search_k,
        )
    return result


# Explicit ``calculate_*`` names are convenient for notebooks and preserve a
# discoverable API for callers that prefer verb-first metric functions.
calculate_timestamp_metrics = timestamp_metrics
calculate_detection_metrics = detection_metrics
calculate_tracking_metrics = tracking_metrics
calculate_search_metrics = search_metrics
compute_timestamp_metrics = timestamp_metrics
compute_detection_metrics = detection_metrics
compute_tracking_metrics = tracking_metrics
compute_search_metrics = search_metrics


__all__ = [
    "DetectionAnnotation",
    "EvaluationDataError",
    "EvaluationDataset",
    "SearchAnnotation",
    "TimestampAnnotation",
    "TrackingAnnotation",
    "calculate_detection_metrics",
    "calculate_search_metrics",
    "calculate_timestamp_metrics",
    "calculate_tracking_metrics",
    "compute_detection_metrics",
    "compute_search_metrics",
    "compute_timestamp_metrics",
    "compute_tracking_metrics",
    "detection_metrics",
    "evaluate_dataset",
    "intersection_over_union",
    "load_evaluation_dataset",
    "load_dataset",
    "read_annotations",
    "read_evaluation_dataset",
    "search_metrics",
    "timestamp_metrics",
    "tracking_metrics",
]
