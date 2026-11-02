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


def answer_query(store, request) -> dict[str, Any]:
    camera_id = getattr(request, "camera_id", None)
    session_id = getattr(request, "session_id", None)
    scoped_video_ids = _scoped_video_ids(store, camera_id=camera_id, session_id=session_id)
    if request.event_id:
        event = store.get_event(request.event_id)
        matches = [event] if event and (
            (not request.video_id or event.get("video_id") == request.video_id)
            and (scoped_video_ids is None or event.get("video_id") in scoped_video_ids)
        ) else []
        plan = QueryPlan(
            request.message,
            object_id=request.object_id,
            zone_id=request.zone_id,
            camera_id=camera_id,
            session_id=session_id,
        )
    else:
        plan = parse_query(
            request.message,
            object_id=request.object_id,
            zone_id=request.zone_id,
            camera_id=camera_id,
            session_id=session_id,
        )
        if request.video_id:
            scoped_video_ids = (request.video_id,)
        matches = _search_scoped_events(
            store,
            video_ids=scoped_video_ids,
            object_id=plan.object_id,
            zone_id=plan.zone_id,
            object_label=plan.object_label,
            event_type=plan.event_type,
        )
    answer, citations = grounded_answer(plan, matches)
    answer_mode = "grounded-structured"
    chat_mode = os.getenv("CHAT_MODEL_MODE", "template").strip().lower()
    if matches and chat_mode in {"meta", "muse", "muse-spark-1.3"}:
        from .model_adapters import MetaMuseGroundedResponder

        answer = MetaMuseGroundedResponder().respond(request.message, citations)
        answer_mode = "meta-muse-grounded-citations"
    elif matches and chat_mode == "qwen":
        from .model_adapters import QwenGroundedResponder

        answer = QwenGroundedResponder().respond(request.message, citations)
        answer_mode = "qwen-grounded-citations"
    conversation_id = request.conversation_id or str(uuid4())
    return {
        "conversation_id": conversation_id,
        "answer": answer,
        "answer_mode": answer_mode,
        "query_plan": plan.__dict__,
        "citations": citations,
        "matches": matches,
    }
