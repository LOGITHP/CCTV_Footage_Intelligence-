import json
import subprocess

import pytest

from video_intelligence import (
    FrameSample,
    MediaProbeError,
    MediaTimestampError,
    MediaToolNotFoundError,
    probe_media,
    sample_frames,
)


def completed(stdout="", *, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_probe_media_returns_typed_video_metadata(monkeypatch):
    calls = []
    payload = {"format": {"format_name": "matroska", "duration": "4.5"}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640,
         "height": 360, "time_base": "1/90000", "nb_frames": "120"},
        {"codec_type": "audio"},
    ]}
    monkeypatch.setattr(
        "video_intelligence.media.subprocess.run",
        lambda command, **kwargs: (calls.append(command) or completed(json.dumps(payload))),
    )

    result = probe_media("clip.mkv")

    assert result.duration_seconds == 4.5
    assert result.streams[0].frame_count == 120
    assert calls[0][-1] == "clip.mkv"


def test_sample_frames_uses_frame_timestamps_not_fps(monkeypatch):
    responses = iter([
        completed(json.dumps({"frames": [
            {"best_effort_timestamp_time": "0.00"},
            {"best_effort_timestamp_time": "0.037"},
            {"best_effort_timestamp_time": "0.091"},
        ]})),
        completed(),
    ])
    monkeypatch.setattr("video_intelligence.media.subprocess.run", lambda *a, **k: next(responses))

    assert list(sample_frames("clip.mp4", every=2)) == [
        FrameSample(index=0, timestamp_seconds=0.0),
        FrameSample(index=2, timestamp_seconds=0.091),
    ]


def test_missing_tool_has_clear_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("video_intelligence.media.subprocess.run", missing)
    with pytest.raises(MediaToolNotFoundError, match="ffprobe.*not found"):
        probe_media("clip.mp4")


def test_frame_without_timestamp_is_rejected_without_decoding(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "video_intelligence.media.subprocess.run",
        lambda command, **kwargs: (calls.append(command) or completed(json.dumps({"frames": [{}]}))),
    )
    with pytest.raises(MediaTimestampError, match="no presentation timestamp"):
        list(sample_frames("clip.mp4"))
    assert len(calls) == 1


def test_tool_failure_has_stderr(monkeypatch):
    monkeypatch.setattr(
        "video_intelligence.media.subprocess.run",
        lambda *args, **kwargs: completed(returncode=1, stderr="bad input"),
    )
    with pytest.raises(MediaProbeError, match="ffprobe failed: bad input"):
        probe_media("clip.mp4")
