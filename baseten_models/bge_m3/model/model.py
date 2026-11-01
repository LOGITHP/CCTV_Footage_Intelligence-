from typing import Any, Dict, List, Union
from sentence_transformers import SentenceTransformer


class Model:
    def __init__(self, **kwargs):
        self._model = None

    def load(self):
        # Load BAAI/bge-m3 multilingual embedding model
        self._model = SentenceTransformer("BAAI/bge-m3")

    def predict(self, model_input: Dict[str, Any]) -> Dict[str, Any]:
        """Generate normalized text embeddings for single or batch texts."""
        if "texts" in model_input and isinstance(model_input["texts"], list):
            texts = model_input["texts"]
            embeddings = self._model.encode(texts, normalize_embeddings=True)
            return {
                "embeddings": [[float(x) for x in emb] for emb in embeddings],
                "dimension": len(embeddings[0]) if len(embeddings) > 0 else 1024,
            }
        
        text = model_input.get("text") or model_input.get("input") or ""
        if isinstance(text, list):
            embeddings = self._model.encode(text, normalize_embeddings=True)
            return {
                "embeddings": [[float(x) for x in emb] for emb in embeddings],
                "dimension": len(embeddings[0]) if len(embeddings) > 0 else 1024,
            }
            
        embedding = self._model.encode(str(text), normalize_embeddings=True)
        return {
            "embedding": [float(x) for x in embedding],
            "dimension": len(embedding),
        }
