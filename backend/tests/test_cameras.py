from __future__ import annotations

from datetime import datetime, timezone

from video_intelligence.cameras import (
    CameraDefinition,
    CameraRegistry,
    CameraSource,
    CameraSourceKind,
    CameraSupervisor,
    MonitoringStatus,
)
from video_intelligence.domain import FrameObservation, ObjectObservation
from video_intelligence.operations.processing import ProcessingOptions
from video_intelligence.pipeline import PipelineResult


def _clock():
    values = iter(
        (
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc),
        )
    )
    return lambda: next(values)


def test_run_all_processes_two_file_cameras_with_isolated_track_scopes(tmp_path):
    first = tmp_path / "first.fixture"
    second = tmp_path / "second.fixture"
    first.write_bytes(b"fixture-1")
    second.write_bytes(b"fixture-2")
    calls = []

    def runner(path, options):
        calls.append((path, options))
        observation = FrameObservation(
            1,
            (ObjectObservation("track-1", 10, 10, track_scope=options.track_scope or "missing"),),
        )
        return PipelineResult((observation,), ({"event_id": path, "timestamp_seconds": 1.25},))

    supervisor = CameraSupervisor(
        CameraRegistry(
            [
                CameraDefinition("cam-a", "Front", CameraSource.file(first)),
                CameraDefinition(
                    "cam-b",
                    "Back",
                    CameraSource.file(second),
                    processing_options=ProcessingOptions(every=2),
                ),
            ]
        ),
        pipeline_runner=runner,
        clock=_clock(),
    )

    reports = supervisor.run_all(session_id="session-1")

    assert set(reports) == {"cam-a", "cam-b"}
    assert all(report.status is MonitoringStatus.COMPLETED for report in reports.values())
    assert all(report.frame_count == 1 and report.event_count == 1 for report in reports.values())
    assert reports["cam-a"].track_scope != reports["cam-b"].track_scope
    assert reports["cam-a"].track_scope == "camera:cam-a:session:session-1"
    assert [options.track_scope for _, options in calls] == [
        "camera:cam-a:session:session-1",
        "camera:cam-b:session:session-1",
    ]
    assert reports["cam-a"].started_at == "2026-01-01T00:00:00+00:00"


def test_unavailable_and_rtsp_sources_fail_without_claiming_to_run(tmp_path):
    calls = []

    def runner(path, options):
        calls.append(path)
        return PipelineResult((), ())

    supervisor = CameraSupervisor(
        CameraRegistry(
            [
                CameraDefinition("missing", "Missing", CameraSource.file(tmp_path / "no.mp4")),
                CameraDefinition("live", "Live", CameraSource.rtsp("rtsp://camera.invalid/live")),
            ]
        ),
        pipeline_runner=runner,
        clock=lambda: None,
    )

    reports = supervisor.run_all(session_id="session-2")

    assert reports["missing"].status is MonitoringStatus.FAILED
    assert "unavailable" in (reports["missing"].last_error or "")
    assert reports["live"].status is MonitoringStatus.FAILED
    assert "not supported" in (reports["live"].last_error or "")
    assert calls == []
    assert reports["live"].started_at is None


def test_disable_and_stop_behaviors_are_explicit(tmp_path):
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"fixture")
    supervisor_ref = {}

    def runner(path, options):
        supervisor_ref["supervisor"].stop("cam", "session-stop")
        return PipelineResult((), ())

    supervisor = CameraSupervisor(
        CameraRegistry([CameraDefinition("cam", "Camera", source)]),
        pipeline_runner=runner,
        clock=lambda: None,
    )
    supervisor_ref["supervisor"] = supervisor

    stopped = supervisor.run("cam", session_id="session-stop")
    assert stopped.status is MonitoringStatus.STOPPED
    assert stopped.last_error is None

    supervisor.disable("cam")
    disabled = supervisor.run("cam", session_id="session-disabled")
    assert disabled.status is MonitoringStatus.DISABLED


def test_registry_register_list_and_update_are_in_memory(tmp_path):
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"fixture")
    registry = CameraRegistry()
    registered = registry.register(
        {
            "camera_id": "cam",
            "display_name": "Camera",
            "source": {"kind": "file", "path": str(source)},
        }
    )
    updated = registry.update("cam", enabled=False, display_name="Disabled Camera")

    assert registered.source.kind is CameraSourceKind.FILE
    assert [camera.display_name for camera in registry.list()] == ["Disabled Camera"]
    assert updated.enabled is False
