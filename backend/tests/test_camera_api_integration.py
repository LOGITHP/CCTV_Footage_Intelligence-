from __future__ import annotations

from fastapi.testclient import TestClient

import video_intelligence.api as api_module
from video_intelligence.domain import FrameObservation, ObjectObservation
from video_intelligence.infrastructure.postgres import PostgresStore
from video_intelligence.anomalies import AnomalyRecord, AnomalySeverity, AnomalyType
from video_intelligence.cameras import CameraMonitoringReport, MonitoringStatus
from video_intelligence.pipeline import PipelineResult
from video_intelligence.storage import Store


def _camera_payload(camera_id: str, source: str, *, expected_labels: list[str] | None = None) -> dict:
    return {
        "camera_id": camera_id,
        "display_name": camera_id.title(),
        "source": {"kind": "file", "path": source},
        "enabled": True,
        "zones": [],
        "processing_options": {"expected_labels": expected_labels or []},
    }


def _install_runtime(monkeypatch, tmp_path, runner):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "camera_pipeline_runner", runner)
    monkeypatch.setattr(api_module, "_camera_runtime_store_id", None)
    monkeypatch.setattr(api_module, "media_root", tmp_path)
    return TestClient(api_module.app), store


def test_camera_register_readback_and_strict_payload(monkeypatch, tmp_path):
    client, _store = _install_runtime(monkeypatch, tmp_path, lambda *_args: PipelineResult((), ()))
    source = tmp_path / "one.mp4"
    source.write_bytes(b"fixture")

    response = client.post("/api/v1/cameras", json=_camera_payload("cam-one", str(source)))
    assert response.status_code == 201
    assert response.json()["source"] == {"kind": "file", "path": str(source)}

    listed = client.get("/api/v1/cameras")
    assert listed.status_code == 200
    assert listed.json()["cameras"][0]["camera_id"] == "cam-one"
    assert client.post("/api/v1/cameras", json={**_camera_payload("bad", str(source)), "unexpected": True}).status_code == 422


def test_two_camera_runs_are_isolated_and_reports_are_persisted(monkeypatch, tmp_path):
    calls = []

    def runner(path, options):
        calls.append((path, options.track_scope))
        return PipelineResult(
            (FrameObservation(1.0, (ObjectObservation(f"track-{len(calls)}", 2, 3, label="person", track_scope=options.track_scope or "missing"),)),),
            (),
        )

    client, store = _install_runtime(monkeypatch, tmp_path, runner)
    for camera_id in ("cam-one", "cam-two"):
        source = tmp_path / f"{camera_id}.mp4"
        source.write_bytes(b"fixture")
        assert client.post("/api/v1/cameras", json=_camera_payload(camera_id, str(source))).status_code == 201

    first = client.post("/api/v1/cameras/cam-one/run", json={"session_id": "session-1"})
    second = client.post("/api/v1/cameras/cam-two/run", json={"session_id": "session-1"})
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "completed"
    assert first.json()["track_scope"] != second.json()["track_scope"]
    assert len(store.list_monitoring_reports(camera_id="cam-one")) == 1
    assert len(store.list_monitoring_reports(camera_id="cam-two")) == 1
    assert calls[0][1] == "camera:cam-one:session:session-1"
    assert calls[1][1] == "camera:cam-two:session:session-1"


def test_failed_source_and_anomaly_readback(monkeypatch, tmp_path):
    client, _store = _install_runtime(monkeypatch, tmp_path, lambda *_args: PipelineResult((), ()))
    missing = tmp_path / "missing.mp4"
    assert client.post("/api/v1/cameras", json=_camera_payload("missing", str(missing))).status_code == 201
    failed = client.post("/api/v1/cameras/missing/run")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert "unavailable" in failed.json()["last_error"]

    source = tmp_path / "anomaly.mp4"
    source.write_bytes(b"fixture")

    def anomalous_runner(_path, options):
        return PipelineResult(
            (FrameObservation(2.5, (ObjectObservation("track-1", 4, 5, label="car", track_scope=options.track_scope or "missing"),)),),
            (),
        )

    monkeypatch.setattr(api_module, "camera_pipeline_runner", anomalous_runner)
    assert client.post("/api/v1/cameras", json=_camera_payload("anomaly", str(source), expected_labels=["person"])).status_code == 201
    result = client.post("/api/v1/cameras/anomaly/run", json={"session_id": "session-anomaly"})
    assert result.status_code == 200
    assert result.json()["anomaly_count"] == 1
    anomalies = client.get("/api/v1/monitoring/anomalies?camera_id=anomaly&session_id=session-anomaly")
    assert anomalies.status_code == 200
    assert anomalies.json()["anomalies"][0]["camera_id"] == "anomaly"
    assert anomalies.json()["anomalies"][0]["timestamp_seconds"] == 2.5


def test_rtsp_is_explicit_and_stop_does_not_claim_interruption(monkeypatch, tmp_path):
    client, _store = _install_runtime(monkeypatch, tmp_path, lambda *_args: PipelineResult((), ()))
    response = client.post(
        "/api/v1/cameras",
        json={
            "camera_id": "live",
            "display_name": "Live",
            "source": {"kind": "rtsp", "url": "rtsp://camera.invalid/live"},
        },
    )
    assert response.status_code == 201
    run = client.post("/api/v1/cameras/live/run")
    assert run.status_code == 200
    assert run.json()["status"] == "failed"
    stop = client.post("/api/v1/cameras/live/stop")
    assert stop.status_code == 200
    assert stop.json()["stop_requested"] is False
    assert stop.json()["status"] == "failed"


def test_postgres_store_persists_camera_report_and_anomaly_readback():
    store = PostgresStore("sqlite+pysqlite:///:memory:")
    store.upsert_camera({"camera_id": "cam", "display_name": "Camera", "source": {"kind": "file", "path": "fixture.mp4"}})
    report = store.save_monitoring_report(CameraMonitoringReport("cam", "session", MonitoringStatus.COMPLETED))
    anomaly = store.save_monitoring_anomaly(
        AnomalyRecord(
            "anomaly-1",
            AnomalyType.PROCESSING_FAILURE,
            "cam",
            "session",
            "failed",
            AnomalySeverity.HIGH,
        )
    )
    assert report["status"] == "completed"
    assert store.list_monitoring_reports(camera_id="cam")[0]["session_id"] == "session"
    assert store.list_monitoring_anomalies(camera_id="cam", session_id="session") == [anomaly]
