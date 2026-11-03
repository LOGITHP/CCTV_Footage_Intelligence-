"""Baseten Model Deployment & Synchronization Manager.

Validates Truss configurations for:
  1. Meta SAM 3.1 (Detection & Segmentation) -> baseten_models/sam3_1
  2. SigLIP2 (Visual Frame Embeddings)       -> baseten_models/siglip2
  3. BGE-M3 (Text & Query Embeddings)        -> baseten_models/bge_m3

Queries active models on Baseten and links their predict endpoints to .env.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request

BASE_URL = "https://api.baseten.co/v1"
MODELS_DIR = Path(__file__).resolve().parent.parent / "baseten_models"


def load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_deployed_models(api_key: str) -> list[dict]:
    req = urllib.request.Request(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Api-Key {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("models", data.get("data", []))
    except Exception as exc:
        print(f"Error fetching models from Baseten: {exc}")
        return []


def validate_trusses():
    import truss
    print("Validating local Truss model definitions...")
    truss_dirs = {
        "SAM 3.1": MODELS_DIR / "sam3_1",
        "SigLIP2": MODELS_DIR / "siglip2",
        "BGE-M3": MODELS_DIR / "bge_m3",
    }
    for name, path in truss_dirs.items():
        if not path.exists():
            print(f"  [ERROR] Missing {path}")
            continue
        handle = truss.load(path)
        print(f"  [OK] {name}: '{handle.spec.config.model_name}' (Python {handle.spec.config.python_version})")


def update_env_endpoints(endpoints: dict[str, str]):
    env_path = Path(__file__).resolve().parent.parent / ".env"
    lines = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        matched = False
        for k, v in endpoints.items():
            if line.startswith(f"{k}="):
                new_lines.append(f"{k}={v}")
                updated_keys.add(k)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    for k, v in endpoints.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"\n[OK] Updated {env_path.name} with Baseten model endpoints:")
    for k, v in endpoints.items():
        print(f"  {k}={v}")


def main():
    load_env()
    api_key = os.getenv("BASETEN_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("BASETEN_API_KEY must be set in the environment or .env")
    print("=== Baseten Model Services Manager ===\n")
    validate_trusses()

    print("\nQuerying Baseten workspace with API key...")
    models = get_deployed_models(api_key)
    print(f"Found {len(models)} active model(s) in Baseten workspace.")

    endpoints = {
        "BASETEN_API_KEY": api_key,
    }

    model_map = {m.get("name", "").lower(): m for m in models}
    
    for service, default_name in [("SAM", "sam-3-1"), ("SIGLIP", "siglip2"), ("BGE", "bge-m3")]:
        match = model_map.get(default_name)
        if match:
            model_id = match.get("id")
            endpoint = f"https://model-{model_id}.api.baseten.co/production/predict"
            endpoints[f"BASETEN_{service}_ENDPOINT"] = endpoint
            endpoints[f"BASETEN_{service}_MODEL_ID"] = model_id
            print(f"  Found deployed {service}: ID={model_id} -> {endpoint}")
        else:
            # Set placeholder or development fallback endpoint
            endpoints.setdefault(f"BASETEN_{service}_ENDPOINT", f"https://model-{default_name}.api.baseten.co/production/predict")

    update_env_endpoints(endpoints)

    print("\nTruss deployment commands (from terminal or CI/CD):")
    print("  truss push baseten_models/sam3_1 --publish")
    print("  truss push baseten_models/siglip2 --publish")
    print("  truss push baseten_models/bge_m3 --publish")


if __name__ == "__main__":
    main()
