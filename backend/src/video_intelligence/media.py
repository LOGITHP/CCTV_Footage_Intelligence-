"""Timestamp-preserving media probing and frame sampling.

The module intentionally has no import-time process or filesystem effects.  The
external FFmpeg tools are started only by :func:`probe_media` and
:func:`sample_frames`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Iterator, Sequence


class MediaToolError(RuntimeError):
    """Base error for unavailable or unsuccessful FFmpeg commands."""


class MediaToolNotFoundError(MediaToolError):
    """Raised when ffprobe or ffmpeg is not installed or not on PATH."""


class MediaProbeError(MediaToolError):
    """Raised when a media tool returns unusable output."""


class MediaTimestampError(MediaToolError):
    """Raised when a frame has no trustworthy media timestamp."""


@dataclass(frozen=True)
class VideoStreamInfo:
    """The useful, typed subset of one probed video stream."""

    index: int
    codec_name: str | None
    width: int | None
    height: int | None
    time_base: str | None
    duration_seconds: float | None
    frame_count: int | None


@dataclass(frozen=True)
class MediaProbeResult:
    """Metadata returned by ffprobe for a media resource."""

    format_name: str | None
    duration_seconds: float | None
    streams: tuple[VideoStreamInfo, ...]


@dataclass(frozen=True)
class FrameSample:
    """A decoded frame's ordinal and its presentation timestamp in seconds."""

    index: int
    timestamp_seconds: float


def _run(command: Sequence[str], tool: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MediaToolNotFoundError(
            f"{tool} is required for media processing but was not found on PATH"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise MediaProbeError(f"{tool} failed: {detail}")
    return result


def _json_result(result: subprocess.CompletedProcess[str], tool: str) -> dict:
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MediaProbeError(f"{tool} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise MediaProbeError(f"{tool} returned an unexpected JSON value")
    return value


def _number(value: object) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def probe_media(path: str | Path, *, ffprobe: str = "ffprobe") -> MediaProbeResult:
    """Probe video metadata without making any frame-rate-based assumptions."""
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    data = _json_result(_run(command, ffprobe), ffprobe)
    streams: list[VideoStreamInfo] = []
    for raw in data.get("streams", []):
        if not isinstance(raw, dict) or raw.get("codec_type") != "video":
            continue
        count = raw.get("nb_frames")
        try:
            frame_count = int(count) if count not in (None, "", "N/A") else None
        except (TypeError, ValueError):
            frame_count = None
        streams.append(
            VideoStreamInfo(
                index=int(raw.get("index", len(streams))),
                codec_name=raw.get("codec_name"),
                width=int(raw["width"]) if raw.get("width") is not None else None,
                height=int(raw["height"]) if raw.get("height") is not None else None,
                time_base=raw.get("time_base"),
                duration_seconds=_number(raw.get("duration")),
                frame_count=frame_count,
            )
        )
    format_data = data.get("format") or {}
    return MediaProbeResult(
        format_name=format_data.get("format_name"),
        duration_seconds=_number(format_data.get("duration")),
        streams=tuple(streams),
    )


def sample_frames(
    path: str | Path,
    *,
    every: int = 1,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> Iterator[FrameSample]:
    """Decode a video and yield selected frame ordinals with media timestamps.

    ``every`` selects every Nth frame in decode order.  Timestamps come from
    ffprobe's frame-level presentation timestamps; nominal FPS is never used.
    """
    if every < 1:
        raise ValueError("every must be at least 1")
    probe_command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_pts_time",
        "-print_format",
        "json",
        str(path),
    ]
    data = _json_result(_run(probe_command, ffprobe), ffprobe)
    frames = data.get("frames")
    if not isinstance(frames, list):
        raise MediaTimestampError("ffprobe returned no frame timestamps")
    timestamps: list[float] = []
    for frame in frames:
        if not isinstance(frame, dict):
            raise MediaTimestampError("ffprobe returned an invalid frame record")
        timestamp = _number(frame.get("best_effort_timestamp_time"))
        if timestamp is None:
            timestamp = _number(frame.get("pkt_pts_time"))
        if timestamp is None:
            raise MediaTimestampError("a video frame has no presentation timestamp")
        timestamps.append(timestamp)
    if not timestamps:
        raise MediaTimestampError("ffprobe returned no frame timestamps")

    # Force decoding so the yielded samples represent frames ffmpeg can read.
    decode_command = [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"]
    _run(decode_command, ffmpeg)
    return iter(
        FrameSample(index=index, timestamp_seconds=timestamp)
        for index, timestamp in enumerate(timestamps)
        if index % every == 0
    )
