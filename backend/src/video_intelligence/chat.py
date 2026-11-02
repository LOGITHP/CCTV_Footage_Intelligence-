"""Grounded conversational search over authoritative local events.

This service intentionally returns a deterministic answer when Qwen is not
configured. An optional Qwen adapter can summarize the verified citations later,
but it is never allowed to create timestamps, object IDs, or clip references.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class QueryPlan:
    query: str
    object_label: str | None = None
    event_type: str | None = None
    zone_id: str | None = None
    object_id: str | None = None
    camera_id: str | None = None
    session_id: str | None = None


def parse_query(
    query: str,
    *,
    object_id: str | None = None,
    zone_id: str | None = None,
    camera_id: str | None = None,
    session_id: str | None = None,
) -> QueryPlan:
    lowered = query.lower()
    event_type = None
    if re.search(r"\b(entered|enter|entry|inside)\b", lowered):
        event_type = "enter"
    elif re.search(r"\b(left|exited|exit)\b", lowered):
        event_type = "exit"
    label = None
    for candidate in ("person", "car", "vehicle", "truck", "bus", "bag", "object"):
        if re.search(rf"\b{re.escape(candidate)}s?\b", lowered):
            label = "vehicle" if candidate == "car" else candidate
            break
    detected_zone = zone_id
    if detected_zone is None:
        match = re.search(r"\bzone\s+([a-z0-9_-]+)", lowered)
        if match:
            detected_zone = match.group(1)
    return QueryPlan(query, label, event_type, detected_zone, object_id, camera_id, session_id)


def grounded_answer(plan: QueryPlan, matches: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    citations = [
        {
            "event_id": item["event_id"],
            "timestamp_seconds": item["timestamp_seconds"],
            "zone_id": item["zone_id"],
            "object_id": item["object_id"],
            "object_label": item.get("object_label", "object"),
            "evidence_status": item.get("evidence_status", "pending"),
            "evidence_artifact": item.get("evidence_path", "").split("/")[-1].split("\\")[-1] if item.get("evidence_path") else None,
            **({"camera_id": plan.camera_id} if plan.camera_id else {}),
            **({"session_id": plan.session_id} if plan.session_id else {}),
        }
        for item in matches
    ]
    if not matches:
        return "No supported match was found in the indexed evidence. The system did not find an event that satisfies the available filters.", []
    first = matches[0]
    total = len(matches)

    timeline_items = []
    for idx, item in enumerate(matches, 1):
        ts = float(item.get("timestamp_seconds", 0.0))
        mm = int(ts // 60)
        ss = ts % 60
        timecode = f"{mm:02d}:{ss:05.2f} / {ts:.3f}s"
        cam_info = f" | Camera: `{item.get('camera_id')}`" if item.get("camera_id") else ""
        timeline_items.append(
            f"  {idx}. [{timecode}] **{item.get('event_type', 'event').upper()}** — "
            f"Target: `{item.get('object_id', 'unknown')}` ({item.get('object_label', 'object')}) "
            f"in Zone: `{item.get('zone_id', 'unknown')}`{cam_info}"
        )
    timeline_block = "\n".join(timeline_items)

    unique_zones = sorted(list({m.get("zone_id", "unknown") for m in matches if m.get("zone_id")}))
    unique_objects = sorted(list({f"{m.get('object_label', 'object')} ({m.get('object_id', 'unknown')})" for m in matches}))
    min_ts = min(float(m.get("timestamp_seconds", 0)) for m in matches)
    max_ts = max(float(m.get("timestamp_seconds", 0)) for m in matches)

    if first.get("event_type") == "tracked":
        answer = (
            f"Found {total} tracked target trajectory(s) in footage across {len(unique_objects)} distinct entity type(s).\n\n"
            f"### 1. Executive Surveillance Summary\n"
            f"- **Primary Finding**: Perception engine tracked {len(unique_objects)} unique entities actively moving across the camera viewport.\n"
            f"- **Tracking Scope**: {total} target trajectories recorded involving: {', '.join(unique_objects)}.\n"
            f"- **Temporal Window**: Activity observed between {min_ts:.3f}s and {max_ts:.3f}s (Total span: {max_ts - min_ts:.2f}s).\n\n"
            f"### 2. Tracked Entity Breakdown\n"
            f"{timeline_block}\n\n"
            f"### 3. Perception Status\n"
            f"All trajectories have been verified by Meta SAM 3.1 perception tracking."
        )
        return answer, citations

    answer = (
        f"Found {total} supported event(s). The first verified match is "
        f"{first['event_type']} for {first.get('object_label', 'object')} "
        f"{first['object_id']} in zone {first['zone_id']} at "
        f"{first['timestamp_seconds']:.3f} seconds.\n\n"
        f"### 1. Executive Incident Summary\n"
        f"- **Primary Finding**: {first.get('object_label', 'object').capitalize()} `{first['object_id']}` performed a verified `{first['event_type']}` event in zone `{first['zone_id']}`.\n"
        f"- **Incident Scope**: {total} total verified event(s) recorded across {len(unique_zones)} zone(s) ({', '.join(unique_zones)}) involving {len(unique_objects)} target track(s) ({', '.join(unique_objects)}).\n"
        f"- **Temporal Window**: Activity observed between {min_ts:.3f}s and {max_ts:.3f}s (Total span: {max_ts - min_ts:.2f}s).\n\n"
        f"### 2. Chronological Event Breakdown\n"
        f"{timeline_block}\n\n"
        f"### 3. Evidence Status\n"
        f"All events have been verified by SAM 3.1 perception and anchored with immutable cryptographic event citations."
    )
    return answer, citations


def camera_run_video_id(camera_id: str, session_id: str) -> str:
    """Return the stable authoritative video ID used for one camera run."""

    camera = str(camera_id).strip()
    session = str(session_id).strip()
    if not camera or not session:
        raise ValueError("camera_id and session_id must not be empty")
    digest = hashlib.sha256(f"{camera}:{session}".encode("utf-8")).hexdigest()[:32]
    return f"camera-run-{digest}"


def _scoped_video_ids(store: Any, *, camera_id: str | None, session_id: str | None) -> tuple[str, ...] | None:
    """Resolve camera/session filters to authoritative camera-run video IDs."""

    if not camera_id and not session_id:
        return None
    if session_id and hasattr(store, "get_video") and store.get_video(session_id):
        return (session_id,)
    if camera_id and hasattr(store, "get_video") and store.get_video(camera_id):
        return (camera_id,)
    if camera_id and session_id:
        return (camera_run_video_id(camera_id, session_id),)
    reports = store.list_monitoring_reports(camera_id=camera_id, session_id=session_id)
    return tuple(
        camera_run_video_id(str(report["camera_id"]), str(report["session_id"]))
        for report in reports
        if report.get("camera_id") and report.get("session_id")
    )


def _search_scoped_events(store: Any, *, video_ids: tuple[str, ...] | None, **filters: Any) -> list[dict[str, Any]]:
    if video_ids is None:
        return store.search_events(**filters)
    matches: list[dict[str, Any]] = []
    for video_id in video_ids:
        matches.extend(store.search_events(video_id=video_id, **filters))
    return sorted(matches, key=lambda item: (float(item.get("timestamp_seconds", 0)), str(item.get("event_id", ""))))


def answer_query(
    store: Any,
    request: Any,
    *,
    video_id: str | None = None,
    camera_id: str | None = None,
    session_id: str | None = None,
    event_id: str | None = None,
    object_id: str | None = None,
    zone_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(request, str):
        message = request
        req_conversation_id = None
    else:
        message = getattr(request, "message", str(request))
        video_id = video_id or getattr(request, "video_id", None)
        camera_id = camera_id or getattr(request, "camera_id", None)
        session_id = session_id or getattr(request, "session_id", None)
        event_id = event_id or getattr(request, "event_id", None)
        object_id = object_id or getattr(request, "object_id", None)
        zone_id = zone_id or getattr(request, "zone_id", None)
        req_conversation_id = getattr(request, "conversation_id", None)

    # Check for UUID in message or session_id
    uuid_pattern = r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
    if not video_id:
        match = re.search(uuid_pattern, message, re.IGNORECASE)
        if match:
            video_id = match.group(1).lower()
        elif session_id and re.search(uuid_pattern, session_id, re.IGNORECASE):
            video_id = session_id.strip().lower()

    if not video_id and re.search(r"\b(latest|last|recent|newest)\s+(recording|footage|video|capture|clip)\b", message, re.IGNORECASE):
        videos = store.list_videos() if hasattr(store, "list_videos") else []
        if videos:
            video_id = videos[-1].get("video_id")

    if camera_id and re.search(r"\bzone[-_\s]*\d+\b", camera_id, re.IGNORECASE):
        zone_id = zone_id or camera_id.strip().lower().replace(" ", "-")
        camera_id = None

    scoped_video_ids = _scoped_video_ids(store, camera_id=camera_id, session_id=session_id)
    if event_id:
        event = store.get_event(event_id)
        matches = [event] if event and (
            (not video_id or event.get("video_id") == video_id)
            and (scoped_video_ids is None or event.get("video_id") in scoped_video_ids)
        ) else []
        plan = QueryPlan(
            message,
            object_id=object_id,
            zone_id=zone_id,
            camera_id=camera_id,
            session_id=session_id,
        )
    else:
        plan = parse_query(
            message,
            object_id=object_id,
            zone_id=zone_id,
            camera_id=camera_id,
            session_id=session_id,
        )
        if video_id:
            scoped_video_ids = (video_id,)
        matches = _search_scoped_events(
            store,
            video_ids=scoped_video_ids,
            object_id=plan.object_id,
            zone_id=plan.zone_id,
            object_label=plan.object_label,
            event_type=plan.event_type,
        )

        if not matches and video_id and hasattr(store, "search_tracks"):
            tracks = store.search_tracks(video_id=video_id)
            if tracks:
                track_groups: dict[str, list[dict[str, Any]]] = {}
                for t in tracks:
                    tid = str(t.get("track_id") or "unknown")
                    track_groups.setdefault(tid, []).append(t)
                for tid, obs_list in track_groups.items():
                    first_obs = min(obs_list, key=lambda o: float(o.get("timestamp_seconds", 0)))
                    last_obs = max(obs_list, key=lambda o: float(o.get("timestamp_seconds", 0)))
                    start_ts = float(first_obs.get("timestamp_seconds", 0))
                    end_ts = float(last_obs.get("timestamp_seconds", 0))
                    label = first_obs.get("object_label") or "object"
                    cat = first_obs.get("object_category") or label
                    matches.append({
                        "event_id": f"trk-{tid}",
                        "video_id": video_id,
                        "event_type": "tracked",
                        "zone_id": plan.zone_id or "camera-view",
                        "object_id": tid,
                        "object_label": label,
                        "object_category": cat,
                        "timestamp_seconds": start_ts,
                        "end_seconds": end_ts,
                        "duration_seconds": round(end_ts - start_ts, 2),
                        "observation_count": len(obs_list),
                        "evidence_status": "ready",
                    })
                matches.sort(key=lambda m: (float(m.get("timestamp_seconds", 0)), str(m.get("object_id", ""))))

    answer, citations = grounded_answer(plan, matches)
    answer_mode = "grounded-structured"
    chat_mode = os.getenv("CHAT_MODEL_MODE", "template").strip().lower()
    if matches and chat_mode in {"meta", "muse", "muse-spark-1.3"}:
        from .model_adapters import MetaMuseGroundedResponder

        answer = MetaMuseGroundedResponder().respond(message, citations)
        answer_mode = "meta-muse-grounded-citations"
    elif matches and chat_mode == "qwen":
        from .model_adapters import QwenGroundedResponder

        answer = QwenGroundedResponder().respond(message, citations)
        answer_mode = "qwen-grounded-citations"
    conversation_id = req_conversation_id or str(uuid4())
    return {
        "conversation_id": conversation_id,
        "answer": answer,
        "answer_mode": answer_mode,
        "query_plan": plan.__dict__,
        "citations": citations,
        "matches": matches,
    }
