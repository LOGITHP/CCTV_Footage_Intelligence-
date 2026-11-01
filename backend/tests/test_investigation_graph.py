"""Tests for the LangGraph-powered investigative agent & dynamic evidence trimming."""

from __future__ import annotations

from pathlib import Path
import pytest

from video_intelligence.investigation_graph import (
    build_investigation_graph,
    run_investigation_graph,
)
from video_intelligence.storage import Store


def test_investigation_graph_structure():
    """Verify the compiled LangGraph has all 5 nodes and correct entry point."""
    graph = build_investigation_graph()
    assert graph is not None


def test_investigation_graph_no_match():
    """Test investigation graph when no match is found in evidence."""
    store = Store(":memory:")
    result = run_investigation_graph(store, "find a red truck in warehouse zone")
    assert result["answer_mode"] == "grounded-structured"
    assert "No supported match" in result["answer"]
    assert len(result["matches"]) == 0
    assert len(result["trimmed_clips"]) == 0


def test_investigation_graph_with_match_and_clip_trimming(tmp_path, monkeypatch):
    """Test that matched events trigger automatic dynamic FFmpeg clip trimming."""
    store = Store(":memory:")
    video_id = "test-vid-123"
    dummy_video_path = tmp_path / "source_cctv.mp4"
    dummy_video_path.write_bytes(b"dummy-cctv-video-stream-content")

    store.add_video(
        video_id,
        "source_cctv.mp4",
        str(dummy_video_path),
        "2026-11-01T12:00:00Z",
    )

    store.add_events(
        video_id,
        [
            {
                "event_id": "evt-001",
                "event_type": "enter",
                "zone_id": "secure_gate",
                "object_id": "person-42",
                "object_label": "person",
                "timestamp_seconds": 12.5,
                "evidence_status": "pending",
                "evidence_path": None,
                "source_frame_index": 375,
            }
        ],
    )

    # Mock create_clip so unit test doesn't depend on actual video codecs
    def mock_create_clip(source, dest, *, timestamp_seconds, padding=4.0):
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"trimmed-clip-mp4-data")
        return target

    monkeypatch.setattr(
        "video_intelligence.investigation_graph.create_clip",
        mock_create_clip,
    )

    result = run_investigation_graph(
        store,
        "Who entered the secure_gate?",
        media_root=tmp_path / "media",
    )

    assert len(result["matches"]) == 1
    assert result["matches"][0]["event_id"] == "evt-001"
    assert len(result["trimmed_clips"]) == 1

    clip = result["trimmed_clips"][0]
    assert clip["event_id"] == "evt-001"
    assert clip["timestamp_seconds"] == 12.5
    assert clip["start_seconds"] == 8.5
    assert clip["end_seconds"] == 16.5
    assert clip["interval_formatted"] == "00:08 - 00:16"
    assert "clip-evt-001.mp4" in clip["clip_filename"]
    assert Path(clip["clip_path"]).is_file()

