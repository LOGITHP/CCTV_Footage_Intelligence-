"""Safe, coarse, non-biometric metadata derived from detection crops.

The helpers in this module deliberately describe only observable pixels and
the detector's supported object label.  They do not identify people or infer
face, ethnicity, gender, or any other sensitive characteristic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping


_LABEL_ALIASES = {
    "person": "person",
    "people": "person",
    "pedestrian": "person",
    "human": "person",
    "car": "car",
    "automobile": "car",
    "auto": "car",
    "sedan": "car",
    "truck": "truck",
    "lorry": "truck",
    "pickup truck": "truck",
    "pickup": "truck",
    "bus": "bus",
    "coach": "bus",
    "motorcycle": "motorcycle",
    "motorbike": "motorcycle",
    "motor cycle": "motorcycle",
    "bicycle": "bicycle",
    "bike": "bicycle",
    "cycle": "bicycle",
    "vehicle": "vehicle",
    "van": "vehicle",
    "suv": "vehicle",
}


def _clean_label(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("detection label must be a string")
    return " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split())


def normalize_yolo_label(label: str) -> str:
    """Return a stable canonical label for supported YOLO classes.

    Unknown labels are retained in a normalized form so optional YOLO mode
    does not silently discard detections outside the supported person/vehicle
    vocabulary.
    """

    cleaned = _clean_label(label)
    return _LABEL_ALIASES.get(cleaned, cleaned)


def object_category_for_label(label: str) -> str | None:
    """Classify a canonical or raw label as ``person`` or ``vehicle``."""

    canonical = normalize_yolo_label(label)
    if canonical == "person":
        return "person"
    if canonical in {"car", "truck", "bus", "motorcycle", "bicycle", "vehicle"}:
        return "vehicle"
    return None


@dataclass(frozen=True, slots=True)
class ObservableAppearance:
    """Estimated appearance cues from one detector crop.

    ``estimated=True`` and ``biometric=False`` are part of the serialized
    contract so consumers cannot mistake these values for identity attributes.
    """

    dominant_color: str | None = None
    upper_color: str | None = None
    lower_color: str | None = None
    vehicle_color: str | None = None
    vehicle_type: str | None = None
    source: str = "detection_crop"
    estimated: bool = True
    biometric: bool = False

    def __post_init__(self) -> None:
        if self.source != "detection_crop":
            raise ValueError("appearance source must be detection_crop")
        if not self.estimated:
            raise ValueError("appearance metadata must be marked estimated")
        if self.biometric:
            raise ValueError("appearance metadata cannot be biometric")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def appearance_to_dict(value: ObservableAppearance | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize appearance values before persistence or API serialization."""

    if value is None:
        return None
    if isinstance(value, ObservableAppearance):
        return value.as_dict()
    if isinstance(value, Mapping):
        result = dict(value)
        result.setdefault("source", "detection_crop")
        result.setdefault("estimated", True)
        result.setdefault("biometric", False)
        return ObservableAppearance(**result).as_dict()
    raise TypeError("appearance must be ObservableAppearance, mapping, or None")


def _crop(frame: Any, bbox: tuple[float, float, float, float] | None):
    if bbox is None:
        return None
    try:
        import numpy as np

        array = np.asarray(frame)
        if array.ndim not in {2, 3} or array.shape[0] < 1 or array.shape[1] < 1:
            return None
        left, top, right, bottom = (float(value) for value in bbox)
        if not all(math.isfinite(value) for value in (left, top, right, bottom)):
            return None
        width, height = array.shape[1], array.shape[0]
        x1 = max(0, min(width, int(math.floor(left))))
        y1 = max(0, min(height, int(math.floor(top))))
        x2 = max(0, min(width, int(math.ceil(right))))
        y2 = max(0, min(height, int(math.ceil(bottom))))
        if x2 <= x1 or y2 <= y1:
            return None
        result = array[y1:y2, x1:x2]
        if result.ndim == 2:
            result = np.repeat(result[:, :, None], 3, axis=2)
        if result.shape[2] < 3:
            return None
        return result[:, :, :3]
    except (ImportError, TypeError, ValueError, OverflowError):
        return None


def _representative_bgr(region: Any):
    """Choose a deterministic quantized-mode BGR color from a crop region."""

    import numpy as np

    pixels = np.asarray(region, dtype=np.float32).reshape(-1, 3)
    if len(pixels) == 0:
        return None
    # A fixed stride keeps large crops cheap while preserving deterministic
    # results for tests and repeat processing.
    if len(pixels) > 4096:
        indices = np.linspace(0, len(pixels) - 1, 4096, dtype=int)
        pixels = pixels[indices]
    quantized = np.clip(pixels, 0, 255).astype(np.uint8) // 32
    keys = quantized[:, 0].astype(np.int32) * 64 + quantized[:, 1] * 8 + quantized[:, 2]
    unique, counts = np.unique(keys, return_counts=True)
    mode_key = int(unique[int(np.argmax(counts))])
    selected = pixels[keys == mode_key]
    return np.median(selected, axis=0)


def _color_name(region: Any) -> str | None:
    representative = _representative_bgr(region)
    if representative is None:
        return None
    # OpenCV frames are BGR.  Convert the representative to HSV without an
    # OpenCV dependency so this remains deterministic in unit tests.
    b, g, r = (float(value) / 255.0 for value in representative)
    maximum = max(r, g, b)
    minimum = min(r, g, b)
    delta = maximum - minimum
    if maximum < 0.16:
        return "black"
    if minimum > 0.88 and delta < 0.18:
        return "white"
    if maximum <= 0.0:
        return "black"
    saturation = delta / maximum
    if saturation < 0.18:
        return "gray"
    if maximum == r:
        hue = 60.0 * (((g - b) / delta) % 6.0)
    elif maximum == g:
        hue = 60.0 * (((b - r) / delta) + 2.0)
    else:
        hue = 60.0 * (((r - g) / delta) + 4.0)
    if hue < 0:
        hue += 360.0
    if hue < 15.0 or hue >= 345.0:
        return "red"
    if hue < 45.0:
        return "brown" if maximum < 0.55 else "orange"
    if hue < 75.0:
        return "yellow"
    if hue < 170.0:
        return "green"
    if hue < 200.0:
        return "cyan"
    if hue < 260.0:
        return "blue"
    if hue < 290.0:
        return "purple"
    return "pink"


def extract_observable_appearance(
    frame: Any,
    bbox: tuple[float, float, float, float] | None,
    label: str,
) -> ObservableAppearance | None:
    """Estimate coarse colors and detector type from one detection crop."""

    crop = _crop(frame, bbox)
    if crop is None:
        return None
    canonical = normalize_yolo_label(label)
    category = object_category_for_label(canonical)
    height = crop.shape[0]
    upper = crop[: max(1, int(round(height * 0.5)))]
    lower = crop[min(height - 1, int(round(height * 0.5))) :]
    dominant = _color_name(crop)
    return ObservableAppearance(
        dominant_color=dominant,
        upper_color=_color_name(upper) if category == "person" else None,
        lower_color=_color_name(lower) if category == "person" else None,
        vehicle_color=dominant if category == "vehicle" else None,
        vehicle_type=canonical if category == "vehicle" else None,
    )
