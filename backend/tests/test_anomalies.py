from __future__ import annotations

from video_intelligence.anomalies import (
    AnomalyConfig,
    AnomalyType,
    CameraHealthReport,
    anomalies_for_scope,
    anomaly_notification_payloads,
    detect_anomalies,
)
from video_intelligence.domain import FrameObservation, ObjectObservation, ZoneEvent


def _frame(timestamp, *objects: ObjectObservation) -> FrameObservation:
    return FrameObservation(timestamp, tuple(objects))


def _object(object_id: str, *, label: str = "person", category: str | None = "person", scope: str = "track-scope") -> ObjectObservation:
    return ObjectObservation(object_id, 10.0, 10.0, label=label, category=category, track_scope=scope)


def test_detects_count_dwell_and_unexpected_label_with_explanations():
    observations = (
        _frame(0.0, _object("track-1"), _object("track-2")),
        _frame(12.0, _object("track-1"), _object("track-2"), _object("track-3", label="dog", category=None)),
    )
    events = (ZoneEvent("event-1", "enter", "gate", "track-1", 0.0, "person", "track-scope"),)
    anomalies = detect_anomalies(
        observations,
        events,
        camera_id="cam-a",
        session_id="session-1",
        config=AnomalyConfig(
            baseline_count=1,
            dwell_threshold_seconds=10.0,
            expected_labels=frozenset({"person"}),
        ),
    )

    kinds = {item.anomaly_type for item in anomalies}
    assert AnomalyType.OBJECT_COUNT_SPIKE in kinds
    assert AnomalyType.DWELL_DURATION in kinds
    assert AnomalyType.UNEXPECTED_LABEL in kinds
    assert all(item.camera_id == "cam-a" and item.session_id == "session-1" for item in anomalies)
    dwell = next(
        item
        for item in anomalies
        if item.anomaly_type is AnomalyType.DWELL_DURATION and item.related_event_ids == ("event-1",)
    )
    assert dwell.timestamp_seconds == 12.0
    assert dwell.related_track_ids == ("track-1",)
    assert "threshold" in dwell.reason


def test_normal_input_produces_no_alerts():
    observations = (_frame(0.0, _object("track-1")), _frame(5.0, _object("track-1")))
    assert detect_anomalies(
        observations,
        camera_id="cam-a",
        session_id="session-1",
        config=AnomalyConfig(
            baseline_count=1,
            dwell_threshold_seconds=10.0,
            expected_labels=frozenset({"person"}),
        ),
    ) == ()


def test_health_rules_require_explicit_failure_or_silence_input():
    assert detect_anomalies(
        (),
        camera_id="cam-a",
        session_id="session-1",
        config=AnomalyConfig(silence_threshold_seconds=10.0),
    ) == ()

    anomalies = detect_anomalies(
        (),
        camera_id="cam-a",
        session_id="session-1",
        config=AnomalyConfig(silence_threshold_seconds=10.0),
        health_report=CameraHealthReport(
            "cam-a",
            "session-1",
            status="failed",
            silence_seconds=20.0,
            failure_reason="decoder unavailable",
            timestamp_seconds=41.5,
            orchestration_timestamp="2026-01-01T00:00:05+00:00",
        ),
    )
    assert {item.anomaly_type for item in anomalies} == {
        AnomalyType.PROCESSING_FAILURE,
        AnomalyType.CAMERA_SILENCE,
    }
    assert all(item.timestamp_seconds == 41.5 for item in anomalies)
    assert all(item.orchestration_timestamp == "2026-01-01T00:00:05+00:00" for item in anomalies)


def test_notification_payload_shape_is_secret_free_and_scoped():
    anomaly = detect_anomalies(
        (_frame(3.0, _object("track-1", label="dog", category=None)),),
        camera_id="cam-a",
        session_id="session-1",
        config=AnomalyConfig(expected_labels=frozenset({"person"})),
    )[0]
    payloads = anomaly_notification_payloads((anomaly,), channels=("local", "webhook"))

    assert [channel for channel, _ in payloads] == ["local", "webhook"]
    notification = payloads[0][1]
    assert notification["notification_id"].startswith("anomaly:")
    assert notification["channel"] == "local"
    assert notification["payload"]["camera_id"] == "cam-a"
    assert notification["payload"]["session_id"] == "session-1"
    assert notification["payload"]["alert_id"] == anomaly.anomaly_id
    assert "details" not in notification["payload"]
    assert "token=" not in str(notification).lower()


def test_scope_filter_isolates_same_track_id_across_cameras_and_sessions():
    first = detect_anomalies(
        (_frame(1.0, _object("track-1", label="dog", category=None)),),
        camera_id="cam-a",
        session_id="session-1",
        config=AnomalyConfig(expected_labels=frozenset({"person"})),
    )[0]
    second = detect_anomalies(
        (_frame(1.0, _object("track-1", label="dog", category=None)),),
        camera_id="cam-b",
        session_id="session-2",
        config=AnomalyConfig(expected_labels=frozenset({"person"})),
    )[0]

    assert first.anomaly_id != second.anomaly_id
    assert anomalies_for_scope((first, second), "cam-a", "session-1") == (first,)
    assert anomalies_for_scope((first, second), "cam-b", "session-1") == ()
