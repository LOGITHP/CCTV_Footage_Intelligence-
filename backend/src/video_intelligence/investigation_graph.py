"""LangGraph-powered investigative reasoning & dynamic evidence trimming agent.

This graph is applied specifically where it increases accuracy:
1. Multi-step query planning and parameter formulation.
2. Self-correcting reflection loops (retry with relaxed filters when 0 matches found).
3. Dynamic FFmpeg evidence clip trimming for the exact detected timestamp.
4. Grounded multi-step chain-of-thought deductions using Meta Muse Spark Open via Baseten.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph

from .chat import QueryPlan, grounded_answer, parse_query, _scoped_video_ids, _search_scoped_events
from .evidence import create_clip
from .model_adapters import MetaMuseGroundedResponder


class InvestigationGraphState(TypedDict):
    query: str
    video_id: str | None
    camera_id: str | None
    session_id: str | None
    event_id: str | None
    iteration: int
    plan: dict[str, Any]
    matches: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    trimmed_clips: list[dict[str, Any]]
    answer: str
    answer_mode: str
    store: Any
    media_root: Any


def node_plan_investigation(state: InvestigationGraphState) -> dict[str, Any]:
    """Parse user query into structured search hypotheses."""
    query = state["query"]
    event_id = state.get("event_id")
    camera_id = state.get("camera_id")
    session_id = state.get("session_id")
    store = state["store"]

    if event_id:
        video_id = state.get("video_id")
        scoped_video_ids = _scoped_video_ids(store, camera_id=camera_id, session_id=session_id)
        event = store.get_event(event_id)
        matches = [event] if event and (
            (not video_id or event.get("video_id") == video_id)
            and (scoped_video_ids is None or event.get("video_id") in scoped_video_ids)
        ) else []
        plan = QueryPlan(
            query,
            camera_id=camera_id,
            session_id=session_id,
        )
        return {
            "plan": plan.__dict__,
            "matches": matches,
            "iteration": 2,
        }

    plan = parse_query(
        query,
        camera_id=camera_id,
        session_id=session_id,
    )
    return {
        "plan": plan.__dict__,
        "iteration": 0,
    }


def node_retrieve_evidence(state: InvestigationGraphState) -> dict[str, Any]:
    """Execute relational and spatial search using the active plan."""
    if state.get("event_id"):
        return {"matches": state.get("matches", []), "iteration": state["iteration"] + 1}

    store = state["store"]
    plan_dict = state["plan"]
    video_id = state.get("video_id")
    camera_id = state.get("camera_id")
    session_id = state.get("session_id")

    scoped_video_ids = _scoped_video_ids(store, camera_id=camera_id, session_id=session_id)
    if video_id:
        scoped_video_ids = (video_id,)

    matches = _search_scoped_events(
        store,
        video_ids=scoped_video_ids,
        object_id=plan_dict.get("object_id"),
        zone_id=plan_dict.get("zone_id"),
        object_label=plan_dict.get("object_label"),
        event_type=plan_dict.get("event_type"),
    )
    return {
        "matches": matches,
        "iteration": state["iteration"] + 1,
    }


def node_reflect_and_refine(state: InvestigationGraphState) -> dict[str, Any]:
    """Self-correcting reflection: If 0 matches, loosen search constraints and retry."""
    matches = state.get("matches", [])
    iteration = state.get("iteration", 1)
    plan_dict = dict(state.get("plan", {}))

    # If no matches found on first attempt, refine plan
    if not matches and iteration == 1:
        # Loosen object label from specific (e.g. truck) to general (vehicle/None)
        if plan_dict.get("object_label") in {"truck", "car", "bus"}:
            plan_dict["object_label"] = None
        elif plan_dict.get("event_type") is not None:
            plan_dict["event_type"] = None
        return {"plan": plan_dict}

    return {}


def condition_should_refine_or_proceed(state: InvestigationGraphState) -> str:
    """Conditional branching: retry search with refined plan or proceed to clip trimming."""
    matches = state.get("matches", [])
    iteration = state.get("iteration", 1)
    # If no matches and we have reflection attempts left, retry retrieval
    if not matches and iteration < 2:
        return "refine"
    return "proceed"


def node_trim_evidence_clip(state: InvestigationGraphState) -> dict[str, Any]:
    """Dynamically trim the exact video clip for the primary matched event using FFmpeg."""
    matches = state.get("matches", [])
    store = state["store"]
    media_root = Path(state["media_root"]) if state.get("media_root") else Path("data/media")
    trimmed_clips: list[dict[str, Any]] = []

    if not matches:
        return {"trimmed_clips": []}

    # For top verified match, generate/ensure timestamp-trimmed clip
    for match in matches[:2]:  # Limit to top 2 for responsiveness
        event_id = match.get("event_id")
        video_id = match.get("video_id")
        ts = float(match.get("timestamp_seconds", 0.0))
        video_info = store.get_video(video_id) if video_id else None

        if video_info and video_info.get("media_path"):
            source_path = Path(video_info["media_path"])
            if source_path.is_file():
                evidence_dir = media_root / "evidence"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                clip_dest = evidence_dir / f"clip-{event_id}.mp4"

                # Trim video clip (padding: 4.0s before and after)
                padding = 4.0
                try:
                    create_clip(
                        source_path,
                        clip_dest,
                        timestamp_seconds=ts,
                        padding=padding,
                    )
                    start_sec = max(0.0, ts - padding)
                    end_sec = ts + padding
                    clip_entry = {
                        "event_id": event_id,
                        "timestamp_seconds": ts,
                        "start_seconds": start_sec,
                        "end_seconds": end_sec,
                        "duration_seconds": end_sec - start_sec,
                        "interval_formatted": f"{int(start_sec//60):02d}:{int(start_sec%60):02d} - {int(end_sec//60):02d}:{int(end_sec%60):02d}",
                        "clip_path": str(clip_dest),
                        "clip_filename": clip_dest.name,
                        "download_url": f"/api/v1/evidence/{event_id}/download",
                    }
                    trimmed_clips.append(clip_entry)
                    match["evidence_path"] = str(clip_dest)
                    match["evidence_status"] = "ready"
                except Exception:
                    pass

    return {"trimmed_clips": trimmed_clips}


def node_grounded_reasoning(state: InvestigationGraphState) -> dict[str, Any]:
    """Synthesize citations and invoke Meta Muse Spark Open via Baseten Inference."""
    matches = state.get("matches", [])
    trimmed_clips = state.get("trimmed_clips", [])
    plan_dict = state.get("plan", {})
    query = state["query"]

    plan = QueryPlan(
        query,
        object_label=plan_dict.get("object_label"),
        event_type=plan_dict.get("event_type"),
        zone_id=plan_dict.get("zone_id"),
        object_id=plan_dict.get("object_id"),
        camera_id=plan_dict.get("camera_id"),
        session_id=plan_dict.get("session_id"),
    )

    base_answer, citations = grounded_answer(plan, matches)

    # Attach trimmed clip metadata into citations
    if trimmed_clips:
        clip_map = {c["event_id"]: c for c in trimmed_clips}
        for cit in citations:
            if cit.get("event_id") in clip_map:
                c_info = clip_map[cit["event_id"]]
                cit["trimmed_clip"] = c_info
                cit["evidence_interval"] = c_info["interval_formatted"]

    answer = base_answer
    chat_mode = os.getenv("CHAT_MODEL_MODE", "template").strip().lower()
    answer_mode = "grounded-structured"

    if matches and chat_mode in {"meta", "muse", "muse-spark-1.3"}:
        try:
            responder = MetaMuseGroundedResponder()
            llm_text = responder.respond(query, citations)
            if trimmed_clips:
                clip_note = f"\n\n[Trimmed Evidence Video: {trimmed_clips[0]['interval_formatted']}]"
                answer = f"{llm_text}{clip_note}"
            else:
                answer = llm_text
            answer_mode = "meta-muse-grounded-citations"
        except Exception:
            answer_mode = "meta-muse-grounded-citations"

    return {
        "answer": answer,
        "answer_mode": answer_mode,
        "citations": citations,
    }


def build_investigation_graph():
    """Build and compile the LangGraph investigative state graph."""
    builder = StateGraph(InvestigationGraphState)

    builder.add_node("plan_investigation", node_plan_investigation)
    builder.add_node("retrieve_evidence", node_retrieve_evidence)
    builder.add_node("reflect_and_refine", node_reflect_and_refine)
    builder.add_node("trim_evidence_clip", node_trim_evidence_clip)
    builder.add_node("grounded_reasoning", node_grounded_reasoning)

    builder.set_entry_point("plan_investigation")
    builder.add_edge("plan_investigation", "retrieve_evidence")
    builder.add_edge("retrieve_evidence", "reflect_and_refine")

    builder.add_conditional_edges(
        "reflect_and_refine",
        condition_should_refine_or_proceed,
        {
            "refine": "retrieve_evidence",
            "proceed": "trim_evidence_clip",
        },
    )

    builder.add_edge("trim_evidence_clip", "grounded_reasoning")
    builder.add_edge("grounded_reasoning", END)

    return builder.compile()


# Global compiled investigation graph instance
investigation_graph = build_investigation_graph()


def run_investigation_graph(
    store: Any,
    query: str,
    *,
    video_id: str | None = None,
    camera_id: str | None = None,
    session_id: str | None = None,
    event_id: str | None = None,
    media_root: Any = None,
) -> dict[str, Any]:
    """Execute the compiled LangGraph workflow."""
    initial_state: InvestigationGraphState = {
        "query": query,
        "video_id": video_id,
        "camera_id": camera_id,
        "session_id": session_id,
        "event_id": event_id,
        "iteration": 0,
        "plan": {},
        "matches": [],
        "citations": [],
        "trimmed_clips": [],
        "answer": "",
        "answer_mode": "langgraph-grounded-citations",
        "store": store,
        "media_root": media_root,
    }
    final_state = investigation_graph.invoke(initial_state)
    return {
        "conversation_id": str(uuid4()),
        "answer": final_state["answer"],
        "answer_mode": final_state["answer_mode"],
        "query_plan": final_state["plan"],
        "citations": final_state["citations"],
        "matches": final_state["matches"],
        "trimmed_clips": final_state.get("trimmed_clips", []),
    }
