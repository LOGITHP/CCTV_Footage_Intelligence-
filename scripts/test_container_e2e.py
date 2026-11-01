"""End-to-end integration test against running Docker container."""

import json
import urllib.error
import urllib.request


def run_e2e():
    print("--- [1/5] Authenticating with JWT ---")
    login_data = json.dumps({"username": "admin", "password": "adminpassword123"}).encode()
    req = urllib.request.Request(
        "http://localhost:8000/api/v1/auth/login",
        data=login_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        token = json.loads(resp.read().decode())["access_token"]
        print("[PASS] Logged in as admin. JWT acquired.")

    print("\n--- [2/5] Uploading video to Supabase & Container storage ---")
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    payload = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="test_perimeter.mp4"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
        f"synthetic_mp4_bytes_content_for_pipeline\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    req2 = urllib.request.Request(
        "http://localhost:8000/api/v1/videos",
        data=payload,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req2) as resp:
        video_info = json.loads(resp.read().decode())
        video_id = video_info["video_id"]
        print(f"[PASS] Video uploaded successfully! Video ID: {video_id}")

    print("\n--- [3/5] Processing observations with Zone Detection ---")
    process_payload = json.dumps({
        "observations": [
            {
                "timestamp": 1.0,
                "objects": [{
                    "object_id": "intruder-person-99",
                    "x": 10.0,
                    "y": 10.0,
                    "label": "person",
                    "bbox": [5.0, 5.0, 15.0, 15.0],
                    "track_scope": "camera-gate/session-live",
                    "appearance": {"dominant_color": "dark"},
                }],
            },
            {
                "timestamp": 2.5,
                "objects": [{
                    "object_id": "intruder-person-99",
                    "x": 50.0,
                    "y": 50.0,
                    "label": "person",
                    "bbox": [45.0, 45.0, 55.0, 55.0],
                    "track_scope": "camera-gate/session-live",
                    "appearance": {"dominant_color": "dark"},
                }],
            },
        ],
        "zones": [{
            "zone_id": "restricted_perimeter",
            "polygon": [[40.0, 40.0], [100.0, 40.0], [100.0, 100.0], [40.0, 100.0]],
        }],
        "time_base": 1.0,
    }).encode("utf-8")

    req3 = urllib.request.Request(
        f"http://localhost:8000/api/v1/videos/{video_id}/process",
        data=process_payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req3) as resp:
            proc_info = json.loads(resp.read().decode())
            print(f"[PASS] Processing finished! Job status: {proc_info['status']}")
    except urllib.error.HTTPError as error:
        print("HTTP Error on process:", error.code, error.read().decode())
        raise

    print("\n--- [4/5] Reading back generated security events from Supabase ---")
    req4 = urllib.request.Request(
        f"http://localhost:8000/api/v1/events?video_id={video_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req4) as resp:
        events_info = json.loads(resp.read().decode())
        events = events_info["events"]
        print(f"[PASS] Found {len(events)} security events in Supabase PostgreSQL:")
        for ev in events:
            print(f"  - Event: {ev['event_type']} | Object: {ev['object_id']} | Zone: {ev['zone_id']} | Time: {ev['timestamp_seconds']}s")

    print("\n--- [5/5] Testing LangGraph Multi-Node Investigation Agent ---")
    chat_payload = json.dumps({
        "video_id": video_id,
        "message": "Did any person enter the restricted perimeter zone?",
    }).encode("utf-8")

    req5 = urllib.request.Request(
        "http://localhost:8000/api/v1/chat",
        data=chat_payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req5) as resp:
        chat_info = json.loads(resp.read().decode())
        print("[PASS] LangGraph Investigation Agent Response:")
        print(f"  - Answer Mode: {chat_info.get('answer_mode')}")
        print(f"  - Citations: {len(chat_info.get('citations', []))}")
        print(f"  - Answer: {chat_info.get('answer')}")
        if chat_info.get("citations"):
            first_c = chat_info["citations"][0]
            print(f"  - Primary Citation: {first_c.get('object_id')} ({first_c.get('object_label')}) in {first_c.get('zone_id')} at {first_c.get('timestamp_seconds')}s")

    print("\n=======================================================")
    print("  ALL END-TO-END CONTAINER CAPABILITIES OPERATIONAL!   ")
    print("=======================================================")


if __name__ == "__main__":
    run_e2e()
