"""End-to-end Supabase PostgreSQL integration verification script."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import urllib.parse

# Add backend/src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from video_intelligence.infrastructure.postgres import PostgresStore
from video_intelligence.domain import FrameObservation, ObjectObservation
from video_intelligence.observables import ObservableAppearance


def _load_env_file():
    for p in (Path(".env"), Path(__file__).resolve().parent.parent / ".env"):
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass
            break


def run_verification():
    _load_env_file()
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        password = os.getenv("DB_PASSWORD", "")
        host = os.getenv("DB_HOST", "").strip()
        port = os.getenv("DB_PORT", "5432")
        user = os.getenv("DB_USER", "postgres")
        db_name = os.getenv("DB_NAME", "postgres")
        if not host or not password:
            raise RuntimeError("DATABASE_URL or DB_HOST plus DB_PASSWORD must be configured")
        pw = urllib.parse.quote(password)
        db_url = f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db_name}?sslmode=require"
    print("Connecting to Supabase PostgreSQL via PostgresStore...")
    store = PostgresStore(db_url, create_schema=False)
    print("PostgresStore connected successfully.")
    
    video_id = "test-video-supabase-1"
    filename = "gate_test_feed.mp4"
    media_path = "media/gate_test_feed.mp4"
    created_at = "2026-11-01T13:30:00Z"
    
    # Clean up any leftover test data
    try:
        store.delete_video(video_id)
    except Exception:
        pass
        
    print(f"Adding video {video_id}...")
    store.add_video(video_id, filename, media_path, created_at)
    stored_video = store.get_video(video_id)
    assert stored_video is not None, "Failed to retrieve stored video"
    assert stored_video["filename"] == filename
    print(f"[OK] Video stored and retrieved: {stored_video['filename']}")
    
    print("Adding observations with Meta SAM track coordinates & appearance...")
    observations = (
        FrameObservation(
            0.5,
            (
                ObjectObservation(
                    "sam-track-001",
                    120.0,
                    240.0,
                    "person",
                    confidence=0.96,
                    bbox=(100.0, 200.0, 140.0, 280.0),
                    appearance=ObservableAppearance(upper_color="dark jacket", lower_color="blue jeans"),
                    track_scope="gate-cam/session-1",
                ),
            ),
        ),
    )
    store.add_observations(video_id, observations)
    tracks = store.search_tracks(video_id=video_id)
    assert len(tracks) > 0, "Failed to retrieve tracks"
    assert tracks[0]["track_id"] == "sam-track-001"
    assert tracks[0]["appearance"]["upper_color"] == "dark jacket"
    print(f"[OK] Track observation verified: track_id={tracks[0]['track_id']}, appearance={tracks[0]['appearance']}")
    
    print("Adding security events...")
    events = [
        {
            "event_id": "evt-supabase-001",
            "event_type": "restricted_zone_entry",
            "zone_id": "secure_gate_alpha",
            "object_id": "sam-track-001",
            "object_label": "person",
            "timestamp_seconds": 0.5,
            "evidence_status": "captured",
            "evidence_path": "media/evidence/evt-supabase-001.mp4",
        }
    ]
    store.add_events(video_id, events)
    found_events = store.search_events(video_id=video_id, event_type="restricted_zone_entry")
    assert len(found_events) == 1, "Failed to retrieve events"
    assert found_events[0]["event_id"] == "evt-supabase-001"
    print(f"[OK] Event verified: event_id={found_events[0]['event_id']}, zone={found_events[0]['zone_id']}")
    
    print("Cleaning up test records...")
    store.delete_video(video_id)
    assert store.get_video(video_id) is None
    print("[OK] Video and cascading records cleaned up successfully.")
    
    print("\n*** ALL SUPABASE POSTGRESQL VERIFICATION CHECKS PASSED ***")


if __name__ == "__main__":
    run_verification()
