"""Privacy controls for public derivatives of uploaded recordings.

The redactor is deliberately fail-closed: when public redaction is enabled and
no detector regions are supplied, a public frame is fully masked rather than
silently publishing the original.  Production deployments can pass reviewed
face/license-plate regions from their detector and retain useful blurred
derivatives while keeping originals restricted.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class PrivacyError(ValueError):
    """Raised when a public derivative cannot be made safe."""


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    redact_public_derivatives: bool = True
    allow_originals: bool = False
    fail_closed: bool = True
    redaction_method: str = "pixelate"
    retention_days: int = 30

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PrivacyPolicy":
        env = os.environ if environ is None else environ
        redaction = True
        originals = False
        fail_closed = True
        method = "pixelate"
        retention = 30
        raw = (env.get("PRIVACY_POLICY") or "").strip()
        if raw:
            if raw.lower() in {"strict", "redact", "redacted"}:
                redaction = True
            elif raw.lower() in {"development", "dev"}:
                redaction = True
            else:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as error:
                    raise PrivacyError("PRIVACY_POLICY must be strict, development, or JSON") from error
                if not isinstance(parsed, dict):
                    raise PrivacyError("PRIVACY_POLICY JSON must be an object")
                redaction = bool(parsed.get("redact_public_derivatives", parsed.get("enabled", redaction)))
                originals = bool(parsed.get("allow_originals", originals))
                fail_closed = bool(parsed.get("fail_closed", fail_closed))
                method = str(parsed.get("method", method))
                retention = int(parsed.get("retention_days", retention))
        redaction = _env_bool(env, "PRIVACY_REDACTION_ENABLED", redaction)
        originals = _env_bool(env, "PRIVACY_ALLOW_ORIGINALS", originals)
        fail_closed = _env_bool(env, "PRIVACY_FAIL_CLOSED", fail_closed)
        method = env.get("PRIVACY_REDACTION_METHOD", method).strip().lower()
        if method not in {"pixelate", "solid", "blur"}:
            raise PrivacyError("PRIVACY_REDACTION_METHOD must be pixelate, solid, or blur")
        try:
            retention = int(env.get("PRIVACY_RETENTION_DAYS", env.get("EVIDENCE_RETENTION_DAYS", retention)))
        except ValueError as error:
            raise PrivacyError("privacy retention days must be an integer") from error
        if retention < 1:
            raise PrivacyError("privacy retention days must be at least 1")
        return cls(redaction, originals, fail_closed, method, retention)


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise PrivacyError(f"{name} must be a boolean value")


@dataclass(frozen=True, slots=True)
class RedactionRegion:
    """A rectangle in pixel coordinates (right/bottom are exclusive)."""

    x: int
    y: int
    width: int
    height: int
    label: str = "sensitive"

    @classmethod
    def from_value(cls, value: "RedactionRegion | Mapping[str, Any] | Sequence[int]") -> "RedactionRegion":
        if isinstance(value, cls):
            region = value
        elif isinstance(value, Mapping):
            if {"x", "y", "width", "height"}.issubset(value):
                region = cls(int(value["x"]), int(value["y"]), int(value["width"]), int(value["height"]), str(value.get("label", "sensitive")))
            elif {"x1", "y1", "x2", "y2"}.issubset(value):
                region = cls(
                    int(value["x1"]),
                    int(value["y1"]),
                    int(value["x2"]) - int(value["x1"]),
                    int(value["y2"]) - int(value["y1"]),
                    str(value.get("label", "sensitive")),
                )
            else:
                raise PrivacyError("redaction region requires x/y/width/height or x1/y1/x2/y2")
        else:
            if len(value) != 4:
                raise PrivacyError("redaction region sequence must contain four values")
            region = cls(int(value[0]), int(value[1]), int(value[2]), int(value[3]))
        if region.width <= 0 or region.height <= 0:
            raise PrivacyError("redaction region dimensions must be positive")
        return region


_PUBLIC_OMISSIONS = {
    "media_path",
    "media_object_key",
    "evidence_path",
    "original_path",
    "source_uri",
    "signed_url",
    "download_url",
    "storage_key",
    "checksum_sha256",
    "track_id",
    "object_id",
}


def redact_public_metadata(
    payload: Mapping[str, Any],
    *,
    policy: PrivacyPolicy | None = None,
) -> dict[str, Any]:
    """Return a safe public metadata view without mutating the source record."""

    selected = policy or PrivacyPolicy()
    if not selected.redact_public_derivatives:
        result = dict(payload)
        result.setdefault("privacy_redacted", False)
        return result
    result = {
        key: value
        for key, value in payload.items()
        if key.lower() not in _PUBLIC_OMISSIONS and not key.lower().endswith("_token")
    }
    result["privacy_redacted"] = True
    result["visibility"] = "public-derivative"
    result["original_available"] = bool(selected.allow_originals)
    return result


def _region_bounds(region: RedactionRegion, width: int, height: int) -> tuple[int, int, int, int] | None:
    x1 = max(0, min(width, region.x))
    y1 = max(0, min(height, region.y))
    x2 = max(0, min(width, region.x + region.width))
    y2 = max(0, min(height, region.y + region.height))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _solid_fill(target: Any, bounds: tuple[int, int, int, int], value: int = 0) -> None:
    x1, y1, x2, y2 = bounds
    target[y1:y2, x1:x2] = value


def _pixelate(target: Any, bounds: tuple[int, int, int, int], block_size: int = 12) -> None:
    x1, y1, x2, y2 = bounds
    region = target[y1:y2, x1:x2]
    if getattr(region, "size", 0) == 0:
        return
    # A deterministic block mosaic uses the mean color of each coarse cell.
    height = y2 - y1
    width = x2 - x1
    for top in range(0, height, block_size):
        for left in range(0, width, block_size):
            bottom = min(height, top + block_size)
            right = min(width, left + block_size)
            cell = region[top:bottom, left:right]
            mean = cell.mean(axis=(0, 1), keepdims=True).astype(target.dtype)
            cell[...] = mean


def _blur(target: Any, bounds: tuple[int, int, int, int], radius: int = 7) -> None:
    # Use OpenCV when available (it is part of the runtime lockfile), while
    # retaining a dependency-light mean-cell fallback for unit tests.
    x1, y1, x2, y2 = bounds
    try:
        import cv2

        target[y1:y2, x1:x2] = cv2.GaussianBlur(target[y1:y2, x1:x2], (radius * 2 + 1, radius * 2 + 1), 0)
    except ImportError:
        _pixelate(target, bounds, block_size=max(4, radius * 2))


def redact_frame(
    frame: Any,
    regions: Iterable[RedactionRegion | Mapping[str, Any] | Sequence[int]] | None = None,
    *,
    policy: PrivacyPolicy | None = None,
) -> Any:
    """Redact sensitive regions in a NumPy/OpenCV frame.

    If no regions are supplied while fail-closed redaction is enabled, the
    entire frame is masked.  This avoids treating a detector miss as proof
    that a public derivative is safe.
    """

    selected = policy or PrivacyPolicy()
    try:
        result = frame.copy()
        height, width = result.shape[:2]
    except AttributeError as error:
        raise PrivacyError("frame must provide copy() and a two-dimensional shape") from error
    if not selected.redact_public_derivatives:
        return result
    normalized = [RedactionRegion.from_value(region) for region in (regions or ())]
    if not normalized:
        if selected.fail_closed:
            result[...] = 0
            return result
        raise PrivacyError("no redaction regions were supplied")
    for region in normalized:
        bounds = _region_bounds(region, width, height)
        if bounds is None:
            continue
        if selected.redaction_method == "solid":
            _solid_fill(result, bounds)
        elif selected.redaction_method == "blur":
            _blur(result, bounds)
        else:
            _pixelate(result, bounds)
    return result


def public_derivative_allowed(*, original: bool = False, policy: PrivacyPolicy | None = None) -> bool:
    selected = policy or PrivacyPolicy()
    if original:
        return selected.allow_originals
    return selected.redact_public_derivatives


class PrivacyRedactor:
    """Object-oriented wrapper convenient for processing worker dependencies."""

    def __init__(self, policy: PrivacyPolicy | None = None) -> None:
        self.policy = policy or PrivacyPolicy.from_env()

    def metadata(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return redact_public_metadata(payload, policy=self.policy)

    def frame(
        self,
        frame: Any,
        regions: Iterable[RedactionRegion | Mapping[str, Any] | Sequence[int]] | None = None,
    ) -> Any:
        return redact_frame(frame, regions, policy=self.policy)


def detect_face_regions(frame: Any) -> tuple[RedactionRegion, ...]:
    """Detect conservative face regions with OpenCV's bundled Haar cascade.

    This is a privacy helper, not an identity detector. If the cascade or
    OpenCV is unavailable, the caller receives no regions and the fail-closed
    policy masks the frame rather than publishing an unredacted derivative.
    """

    try:
        import cv2
    except ImportError:
        return ()
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    classifier = cv2.CascadeClassifier(str(cascade_path))
    if classifier.empty():
        return ()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
    return tuple(RedactionRegion(int(x), int(y), int(width), int(height), "face") for x, y, width, height in faces)


def redact_public_derivative(
    value: Any,
    regions: Iterable[RedactionRegion | Mapping[str, Any] | Sequence[int]] | None = None,
    *,
    policy: PrivacyPolicy | None = None,
) -> Any:
    """Redact either a metadata mapping or an image/frame value.

    The small dispatcher keeps callers from accidentally using a public-view
    metadata helper on raw media bytes, while preserving simple worker code.
    """

    if isinstance(value, Mapping):
        return redact_public_metadata(value, policy=policy)
    return redact_frame(value, regions, policy=policy)


sanitize_public_payload = redact_public_metadata


def redact_video(
    source: str | Path,
    destination: str | Path,
    *,
    regions_by_frame: Mapping[int, Iterable[RedactionRegion | Mapping[str, Any] | Sequence[int]]] | None = None,
    region_provider: Any | None = None,
    policy: PrivacyPolicy | None = None,
) -> Path:
    """Write a redacted MP4 derivative using the existing OpenCV runtime.

    Audio is intentionally omitted from public derivatives.  The default
    fail-closed frame behavior prevents an unredacted frame when a region
    provider misses a frame.
    """

    selected = policy or PrivacyPolicy()
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        if source_path.resolve() == destination_path.resolve():
            raise PrivacyError("public derivative destination must differ from the original")
    except FileNotFoundError:
        # ``resolve`` can fail for a not-yet-created destination on older
        # Python/platform combinations; the source path is still validated by
        # VideoCapture below.
        pass
    try:
        import cv2
    except ImportError as error:  # pragma: no cover - OpenCV is locked in runtime
        raise PrivacyError("OpenCV is required to write public video derivatives") from error
    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        capture.release()
        raise PrivacyError("source video could not be opened")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if width <= 0 or height <= 0:
        capture.release()
        raise PrivacyError("source video has no usable frame dimensions")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(destination_path), cv2.VideoWriter_fourcc(*"mp4v"), fps if fps > 0 else 30.0, (width, height))
    if not writer.isOpened():
        capture.release()
        writer.release()
        raise PrivacyError("public derivative writer could not be opened")
    frame_index = 0
    written = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if region_provider is not None:
                regions = region_provider(frame_index)
            elif regions_by_frame is not None:
                regions = regions_by_frame.get(frame_index)
            else:
                regions = None
            writer.write(redact_frame(frame, regions, policy=selected))
            frame_index += 1
            written += 1
    finally:
        capture.release()
        writer.release()
    if written == 0 or not destination_path.exists() or destination_path.stat().st_size == 0:
        destination_path.unlink(missing_ok=True)
        raise PrivacyError("source video contained no writable frames")
    return destination_path
