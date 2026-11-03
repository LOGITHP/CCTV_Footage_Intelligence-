from pathlib import Path
from fastapi.testclient import TestClient

import video_intelligence.api as api_module
from video_intelligence.storage import Store


def test_separated_copilots_and_storage_endpoints(tmp_path, monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "media_root", tmp_path / "media")

    (tmp_path / "media" / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "live_streams").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "evidence").mkdir(parents=True, exist_ok=True)

    # Create dummy files
    upload_file = tmp_path / "media" / "uploads" / "operator_test.mp4"
    upload_file.write_bytes(b"dummy-video-data")
    live_file = tmp_path / "media" / "live_streams" / "stream_chunk_01.mp4"
    live_file.write_bytes(b"live-stream-data")
    evidence_file = tmp_path / "media" / "evidence" / "clip-123.mp4"
    evidence_file.write_bytes(b"evidence-clip-data")

    client = TestClient(api_module.app)

    # 1. Test Storage Summary
    storage_resp = client.get("/api/v1/storage/summary")
    assert storage_resp.status_code == 200
    summary = storage_resp.json()
    assert "uploaded_footage" in summary
    assert "live_streams" in summary
    assert "forensic_evidence" in summary
    assert summary["uploaded_footage"]["total_files"] >= 1
    assert summary["live_streams"]["total_files"] >= 1
    assert summary["forensic_evidence"]["total_files"] >= 1

    # 2. Test Storage Deletion
    del_resp = client.delete("/api/v1/storage/uploads/operator_test.mp4")
    assert del_resp.status_code == 200
    assert not upload_file.exists()

    # 3. Test Live Chat Endpoint
    live_chat_resp = client.post(
        "/api/v1/chat/live",
        json={"message": "Are any cameras online or breaches active?"},
    )
    assert live_chat_resp.status_code == 200
    live_body = live_chat_resp.json()
    assert "Live Surveillance Fleet Overview" in live_body["answer"]
    assert "citations" in live_body

    # 4. Test Forensic Chat Endpoint
    forensic_chat_resp = client.post(
        "/api/v1/chat/forensic",
        json={"message": "Tell me about what happened in the video"},
    )
    assert forensic_chat_resp.status_code == 200
    forensic_body = forensic_chat_resp.json()
    assert "citations" in forensic_body

    # 5. Verify YOLO and ByteTrack are absent from system capabilities
    cap_resp = client.get("/api/v1/system/capabilities")
    assert cap_resp.status_code == 200
    caps = cap_resp.json()["models"]
    assert "yolo" not in caps.get("detectors", {})
    assert "bytetrack" not in caps.get("trackers", {})
