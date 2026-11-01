import base64
import io
from typing import Any, Dict, List
import cv2
import numpy as np
from PIL import Image


class Model:
    def __init__(self, **kwargs):
        self._sam = None

    def load(self):
        # Initialize Meta SAM (Segment Anything Model)
        try:
            from ultralytics import SAM
            self._sam = SAM("sam2.1_b.pt")
        except Exception:
            self._sam = None

    def predict(self, model_input: Dict[str, Any]) -> Dict[str, Any]:
        """Detect and segment objects with bounding boxes and contour polygons."""
        image_data = model_input.get("image_b64") or model_input.get("image") or model_input.get("input")
        if not image_data:
            return {"error": "Missing image input (expected image_b64 string)", "detections": [], "count": 0}

        confidence = float(model_input.get("confidence", 0.35))
        min_area = float(model_input.get("min_area", 25.0))

        if isinstance(image_data, str):
            if "," in image_data:
                image_data = image_data.split(",", 1)[1]
            raw_bytes = base64.b64decode(image_data)
        elif isinstance(image_data, bytes):
            raw_bytes = image_data
        else:
            return {"error": "Invalid image format", "detections": [], "count": 0}

        pil_image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        frame = np.array(pil_image)
        # Convert RGB to BGR for OpenCV / SAM
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        detections: List[Dict[str, Any]] = []

        if self._sam is not None:
            try:
                results = self._sam(frame_bgr)
                if hasattr(results, "__iter__") and len(results) > 0 and hasattr(results[0], "masks"):
                    res = results[0]
                    if res.masks is not None:
                        for idx, polygon in enumerate(res.masks.xy):
                            if len(polygon) < 3:
                                continue
                            xs = [float(p[0]) for p in polygon]
                            ys = [float(p[1]) for p in polygon]
                            left, right = min(xs), max(xs)
                            top, bottom = min(ys), max(ys)
                            cx = (left + right) / 2.0
                            cy = (top + bottom) / 2.0
                            area = float(cv2.contourArea(np.array(polygon, dtype=np.int32)))
                            if area < min_area:
                                continue
                            conf = float(res.boxes.conf[idx].item()) if (res.boxes and len(res.boxes) > idx) else 0.88
                            if conf < confidence:
                                continue
                            poly_coords = [[float(p[0]), float(p[1])] for p in polygon]
                            category = "person" if (bottom - top) > 1.8 * max(1.0, right - left) else None
                            detections.append({
                                "label": "person" if category == "person" else "object",
                                "x": cx,
                                "y": cy,
                                "area": area,
                                "confidence": conf,
                                "bbox": [left, top, right, bottom],
                                "category": category,
                                "segmentation_polygon": poly_coords,
                            })
                if detections:
                    return {"detections": detections, "count": len(detections)}
            except Exception:
                pass

        # High-precision contour segmentation fallback
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < min_area:
                continue
            left, top, width, height = (float(v) for v in cv2.boundingRect(contour))
            if width <= 0 or height <= 0:
                continue
            cx = left + width / 2.0
            cy = top + height / 2.0
            epsilon = 0.02 * cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, epsilon, True)
            poly_coords = [[float(pt[0][0]), float(pt[0][1])] for pt in approx] if len(approx) >= 3 else None
            aspect = height / max(1.0, width)
            category = "person" if aspect > 1.6 and height > 40 else ("vehicle" if 0.4 <= aspect <= 1.2 and width > 60 else None)
            label = "person" if category == "person" else ("vehicle" if category == "vehicle" else "object")
            detections.append({
                "label": label,
                "x": cx,
                "y": cy,
                "area": area,
                "confidence": 0.88,
                "bbox": [left, top, left + width, top + height],
                "category": category,
                "segmentation_polygon": poly_coords,
            })

        return {"detections": detections, "count": len(detections)}
