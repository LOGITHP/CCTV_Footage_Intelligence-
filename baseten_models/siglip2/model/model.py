import base64
import io
from typing import Any, Dict
from PIL import Image
import torch
from transformers import AutoModel, AutoProcessor


class Model:
    def __init__(self, **kwargs):
        self._model = None
        self._processor = None
        self._device = None

    def load(self):
        # Load google/siglip2-base-patch16-224
        model_id = "google/siglip2-base-patch16-224"
        self._processor = AutoProcessor.from_pretrained(model_id)
        self._model = AutoModel.from_pretrained(model_id)
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model.to(self._device)
        self._model.eval()

    def predict(self, model_input: Dict[str, Any]) -> Dict[str, Any]:
        """Generate visual embeddings from image base64 input."""
        image_data = model_input.get("image_b64") or model_input.get("image") or model_input.get("input")
        if not image_data:
            return {"error": "Missing image input (expected image_b64 string)", "embedding": []}

        if isinstance(image_data, str):
            if "," in image_data:
                image_data = image_data.split(",", 1)[1]
            raw_bytes = base64.b64decode(image_data)
        elif isinstance(image_data, bytes):
            raw_bytes = image_data
        else:
            return {"error": "Invalid image format", "embedding": []}

        image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        inputs = self._processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.no_grad():
            features = self._model.get_image_features(**inputs)
            features = features / features.norm(dim=-1, keepdim=True)

        embedding = [float(x) for x in features[0].tolist()]
        return {
            "embedding": embedding,
            "dimension": len(embedding),
        }
