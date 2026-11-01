"""Evidence clip generation with bounded, source-relative intervals."""

from __future__ import annotations

from pathlib import Path
import subprocess

from .privacy import PrivacyError, PrivacyPolicy, detect_face_regions, redact_video


class EvidenceError(RuntimeError):
    pass


def create_clip(source: str | Path, destination: str | Path, *, timestamp_seconds: float, padding: float = 5.0, ffmpeg: str = "ffmpeg") -> Path:
    if timestamp_seconds < 0 or padding < 0:
        raise ValueError("timestamp and padding must be non-negative")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, timestamp_seconds - padding)
    duration = max(0.1, padding * 2)
    command = [ffmpeg, "-y", "-v", "error", "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{duration:.6f}", "-c", "copy", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise EvidenceError(f"{ffmpeg} is required for evidence clips") from exc
    if result.returncode != 0:
        raise EvidenceError(result.stderr.strip() or "evidence clip generation failed")
    if not target.exists() or target.stat().st_size == 0:
        raise EvidenceError("ffmpeg did not produce a playable evidence clip")
    policy = PrivacyPolicy.from_env()
    if policy.redact_public_derivatives:
        redacted = target.with_name(f"{target.stem}.redacted{target.suffix}")
        try:
            redact_video(target, redacted, region_provider=detect_face_regions, policy=policy)
            target.unlink(missing_ok=True)
            redacted.replace(target)
        except PrivacyError as exc:
            target.unlink(missing_ok=True)
            redacted.unlink(missing_ok=True)
            raise EvidenceError(f"privacy redaction failed closed: {exc}") from exc
    return target
