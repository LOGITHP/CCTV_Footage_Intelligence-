"""Live integration verification for Meta AI, Baseten, Supabase, Qdrant, and MinIO.

Tests all active service credentials and model endpoints before container deployment.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _read_json(request: urllib.request.Request, *, attempts: int = 3) -> dict:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}") from last_error


def test_meta_sam():
    print("[1/5] Testing Meta API for SAM 3.1...")
    meta_key = _required("META_API_KEY")
    meta_url = os.getenv("META_API_URL", "https://api.meta.ai/v1")

    # 1. Check model list
    req = urllib.request.Request(
        f"{meta_url}/models",
        headers={"Authorization": f"Bearer {meta_key}"},
    )
    data = _read_json(req)
    model_ids = [m.get("id") for m in data.get("data", data.get("models", []))]
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
    res = _read_json(req2)
    assert res.get("status") == "completed", f"SAM request did not complete: {res}"
    assert res.get("model") == "sam-3.1", f"SAM returned an unexpected model: {res.get('model')}"
    print("  [PASS] Successfully invoked SAM 3.1 on Meta Direct API (status=completed)")


def test_baseten_spark_llm():
    print("[2/5] Testing Baseten Inference for Meta Muse Spark Open...")
    b10_key = _required("BASETEN_API_KEY")
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
    data = _read_json(req)
    choices = data.get("choices") or []
    assert choices and isinstance(choices[0].get("message"), dict), "Baseten returned no chat choice"
    choice = choices[0]["message"]
    content = choice.get("content") or choice.get("reasoning_content") or ""
    assert content.strip(), "Baseten returned an empty chat response"
    print(f"  [PASS] Baseten Muse Spark Open responded: {content.strip()[:60]!r}")


def test_supabase_postgres():
    print("[3/5] Testing Supabase PostgreSQL Database...")
    import psycopg

    db_url = _required("DATABASE_URL")
    clean_url = db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(clean_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            tables = [r[0] for r in cur.fetchall()]
            assert "videos" in tables and "events" in tables, f"Expected tables 'videos' and 'events', found: {tables}"
            print(f"  [PASS] Supabase PostgreSQL connected! Verified {len(tables)} tables: {', '.join(sorted(tables)[:4])}...")


def test_qdrant_cloud():
    print("[4/5] Testing Qdrant Cloud Vector Database...")
    qdrant_url = _required("QDRANT_URL")
    qdrant_key = os.getenv("QDRANT_API_KEY")

    headers = {"api-key": qdrant_key} if qdrant_key else {}
    req = urllib.request.Request(f"{qdrant_url}/collections", headers=headers)
    data = _read_json(req)
    cols = [c["name"] for c in data.get("result", {}).get("collections", [])]
    print(f"  [PASS] Qdrant connected! Collections: {cols}")


def test_minio_configuration():
    print("[5/5] Testing MinIO Object Storage Configuration...")
    backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    bucket = os.getenv("MINIO_BUCKET", "video-intelligence")
    if backend == "local":
        print("  [PASS] Local filesystem object storage selected; MinIO connectivity is not required.")
        return
    access_key = _required("MINIO_ACCESS_KEY")
    secret_key = _required("MINIO_SECRET_KEY")
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.getenv("S3_REGION", "us-east-1"),
        )
        client.head_bucket(Bucket=bucket)
    except Exception as error:
        raise RuntimeError(f"object storage connectivity failed: {error}") from error
    print(f"  [PASS] S3-compatible object storage connected: endpoint={endpoint}, bucket={bucket}")


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
    checks = (test_meta_sam, test_baseten_spark_llm, test_supabase_postgres, test_qdrant_cloud, test_minio_configuration)
    failures = []
    for check in checks:
        try:
            check()
        except Exception as error:
            failures.append((check.__name__, error))
            print(f"  [FAIL] {check.__name__}: {error}")
    if not failures:
        print("==================================================")
        print("  ALL SERVICES & ENDPOINTS VERIFIED OPERATIONAL!  ")
        print("==================================================")
    else:
        print("==================================================")
        print(f"  STACK AUDIT FAILED ({len(failures)} check(s))                ")
        print("==================================================")
        sys.exit(1)
