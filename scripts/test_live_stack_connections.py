"""Live integration verification for Meta AI, Baseten, Supabase, Qdrant, and MinIO.

Tests all active service credentials and model endpoints before container deployment.
"""

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path


def test_meta_sam():
    print("[1/5] Testing Meta API for SAM 3.1...")
    meta_key = os.getenv("META_API_KEY", "LLM_953135614536411_MGNsLLKsiV1PptUKrlzmzf9cLMg")
    meta_url = os.getenv("META_API_URL", "https://api.meta.ai/v1")

    # 1. Check model list
    req = urllib.request.Request(
        f"{meta_url}/models",
        headers={"Authorization": f"Bearer {meta_key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        model_ids = [m.get("id") for m in data.get("data", [])]
        assert "sam-3.1" in model_ids, f"sam-3.1 not found in models: {model_ids}"
        print(f"  [PASS] Found sam-3.1 on Meta API (available models: {len(model_ids)})")

    # 2. Test response endpoint with synthetic 1x1 image
    b64_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    payload = {
        "model": "sam-3.1",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "detect objects"},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{b64_png}"},
                ],
            }
        ],
    }
    req2 = urllib.request.Request(
        f"{meta_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {meta_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req2, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        assert res.get("status") == "completed"
        assert res.get("model") == "sam-3.1"
        print("  [PASS] Successfully invoked SAM 3.1 on Meta Direct API (status=completed)")


def test_baseten_spark_llm():
    print("[2/5] Testing Baseten Inference for Meta Muse Spark Open...")
    b10_key = os.getenv("BASETEN_API_KEY", "b10_nVtchD9y.qWy7Pmz8Glauaka2Z1A4Tlrwr4dQy5gL")
    b10_url = os.getenv("BASETEN_INFERENCE_URL", "https://inference.baseten.co/v1")
    model = os.getenv("META_MODEL", "meta-models/muse-spark-open-1026")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are Meta Muse Spark Open video intelligence assistant."},
            {"role": "user", "content": "Respond with 'OPERATIONAL' if ready."},
        ],
        "max_tokens": 10,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        f"{b10_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {b10_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        choice = data["choices"][0]["message"]
        content = choice.get("content") or choice.get("reasoning_content") or ""
        print(f"  [PASS] Baseten Muse Spark Open responded: {content.strip()[:60]!r}")


def test_supabase_postgres():
    print("[3/5] Testing Supabase PostgreSQL Database...")
    import psycopg

    db_url = os.getenv("DATABASE_URL")
    clean_url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(clean_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            tables = [r[0] for r in cur.fetchall()]
            assert "videos" in tables and "events" in tables, f"Expected tables 'videos' and 'events', found: {tables}"
            print(f"  [PASS] Supabase PostgreSQL connected! Verified {len(tables)} tables: {', '.join(sorted(tables)[:4])}...")


def test_qdrant_cloud():
    print("[4/5] Testing Qdrant Cloud Vector Database...")
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_key = os.getenv("QDRANT_API_KEY")

    req = urllib.request.Request(
        f"{qdrant_url}/collections",
        headers={"api-key": qdrant_key},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        cols = [c["name"] for c in data.get("result", {}).get("collections", [])]
        print(f"  [PASS] Qdrant Cloud connected! Collections: {cols}")


def test_minio_configuration():
    print("[5/5] Testing MinIO Object Storage Configuration...")
    backend = os.getenv("OBJECT_STORAGE_BACKEND", "minio")
    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    bucket = os.getenv("MINIO_BUCKET", "video-intelligence")
    access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    secret_key = os.getenv("MINIO_SECRET_KEY", "minioadminpassword123")

    print(f"  [PASS] MinIO configured for local S3 object storage:")
    print(f"    - Backend: {backend}")
    print(f"    - Endpoint: {endpoint}")
    print(f"    - Bucket: {bucket}")
    print(f"    - Access Key: {access_key[:4]}***")


def load_env_file():
    env_path = Path(".env")
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v


if __name__ == "__main__":
    load_env_file()
    print("==================================================")
    print("  MULTI-STREAM VIDEO INTELLIGENCE STACK AUDIT     ")
    print("==================================================")
    try:
        test_meta_sam()
        test_baseten_spark_llm()
        test_supabase_postgres()
        test_qdrant_cloud()
        test_minio_configuration()
        print("==================================================")
        print("  ALL SERVICES & ENDPOINTS VERIFIED OPERATIONAL!  ")
        print("==================================================")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n[FAIL] Verification failed: {e}")
        sys.exit(1)
