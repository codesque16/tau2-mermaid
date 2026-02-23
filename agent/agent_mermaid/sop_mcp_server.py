"""
SOP MCP server: streamable HTTP transport with load_graph, goto_node, and todo tools.
Exposes GET /api/connections and an Agent Monitor–style viewer (Overview + Session detail).
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route

# Optional: use mcp only when available
try:
    from mcp.server.fastmcp import FastMCP

    HAS_MCP = True
except ImportError:
    HAS_MCP = False


# --- Connection tracking (shared, so we can view all connections) ---
@dataclass
class ConnectionEvent:
    """A single tool-call event from a connection."""

    id: str
    ts: float
    tool: str
    params: dict[str, Any]
    session_id: str
    result_summary: str = ""


# In-memory store: session_id -> list of ConnectionEvent (append-only)
_connection_events: list[ConnectionEvent] = []
_session_to_events: dict[str, list[ConnectionEvent]] = defaultdict(list)
# Allow at most N events per session and globally to avoid unbounded growth
_MAX_EVENTS_PER_SESSION = 500
_MAX_TOTAL_EVENTS = 10_000


def _record_connection(session_id: str, tool: str, params: dict[str, Any], result_summary: str = "") -> None:
    event = ConnectionEvent(
        id=str(uuid.uuid4()),
        ts=time.time(),
        tool=tool,
        params=params,
        session_id=session_id,
        result_summary=result_summary,
    )
    _connection_events.append(event)
    _session_to_events[session_id].append(event)
    # Trim per-session
    if len(_session_to_events[session_id]) > _MAX_EVENTS_PER_SESSION:
        _session_to_events[session_id] = _session_to_events[session_id][-_MAX_EVENTS_PER_SESSION:]
    # Trim global
    while len(_connection_events) > _MAX_TOTAL_EVENTS:
        _connection_events.pop(0)


def _get_sessions() -> list[dict[str, Any]]:
    """List all known sessions (connection IDs) with last activity and event count."""
    out = []
    for sid, events in _session_to_events.items():
        if not events:
            continue
        first = events[0]
        last = events[-1]
        duration_sec = last.ts - first.ts if last.ts and first.ts else 0
        last_message = (last.result_summary or last.tool or "")[:80]
        if len((last.result_summary or last.tool or "")) > 80:
            last_message += "..."
        out.append({
            "session_id": sid,
            "first_ts": first.ts,
            "last_ts": last.ts,
            "last_tool": last.tool,
            "event_count": len(events),
            "duration_sec": duration_sec,
            "last_message": last_message,
        })
    out.sort(key=lambda x: -(x["last_ts"] or 0))
    return out


def _get_session_detail(session_id: str) -> dict[str, Any] | None:
    """Get full tool-call log and graph state (path, current node) for a session."""
    events = _session_to_events.get(session_id)
    if not events:
        return None
    state = _sessions.get(session_id)
    graph_state = {}
    if state and state.graphs:
        for gid, g in state.graphs.items():
            path = state.path.get(gid, [])
            m = g.get("skeleton") or g.get("mermaid_source", "")
            graph_state[gid] = {
                "mermaid_source": g.get("mermaid_source", m),
                "skeleton": g.get("skeleton", m),
                "path": path,
                "current_node": path[-1] if path else None,
                "entry_node": g.get("entry_node"),
            }
    return {
        "session_id": session_id,
        "events": [
            {
                "id": e.id,
                "ts": e.ts,
                "tool": e.tool,
                "params": e.params,
                "result_summary": e.result_summary,
            }
            for e in events
        ],
        "graph_state": graph_state,
    }


# --- Mermaid skeleton parsing (minimal, for load_graph output shape) ---
def _parse_mermaid_flowchart(mermaid_source: str) -> dict[str, Any]:
    """
    Parse flowchart TD source: extract node IDs, edges, and classify nodes.
    Returns dict with nodes, edges, entry_node, decision_nodes, terminal_nodes, skeleton (simplified).
    """
    lines = [s.strip() for s in mermaid_source.strip().splitlines() if s.strip() and not s.strip().startswith("%%")]
    node_ids: set[str] = set()
    edges: list[tuple[str, str, str | None]] = []  # (from, to, label)
    node_id_to_shape: dict[str, str] = {}  # rectangle, stadium, rhombus, parallelogram

    # Match node definitions: ID["..."] or ID(["..."]) or ID{...} or ID[/.../]
    node_def_re = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"(?:\[(?:[\"`].*?[\"`]|[^\]])*\]|\(\[.*?\]\)|\{.*?\}|\[/.*?/\])\s*$"
    )
    # Match edge: A --> B or A -->|label| B or A -.->|label| B
    edge_re = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"(?:-->|-.->)\s*"
        r"(?:\|[^|]*\|\s*)?"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*$"
    )
    edge_with_label_re = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:-->|-.->)\s*\|([^|]*)\|\s*([A-Za-z_][A-Za-z0-9_]*)\s*$"
    )

    for line in lines:
        if "-->" in line or "-.->" in line:
            m = edge_with_label_re.match(line)
            if m:
                from_id, label, to_id = m.groups()
                node_ids.add(from_id)
                node_ids.add(to_id)
                edges.append((from_id, to_id, label))
                continue
            m = edge_re.match(line)
            if m:
                from_id, to_id = m.groups()
                node_ids.add(from_id)
                node_ids.add(to_id)
                edges.append((from_id, to_id, None))
                continue
        # Node shape from first occurrence in source (simplified)
        for part in re.split(r"\s*-->\s*|-\.->\s*", line):
            part = part.strip()
            if "|" in part:
                part = re.sub(r"\|[^|]*\|", "", part).strip()
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", part)
            if m:
                nid = m.group(1)
                node_ids.add(nid)
                if "([" in part or "])" in part:
                    node_id_to_shape[nid] = "stadium"
                elif "{" in part and "}" in part:
                    node_id_to_shape[nid] = "rhombus"
                elif "[/" in part or "/]" in part:
                    node_id_to_shape[nid] = "parallelogram"
                else:
                    node_id_to_shape.setdefault(nid, "rectangle")

    # Entry: node that is target of no edge (or START)
    targets = {e[1] for e in edges}
    sources = {e[0] for e in edges}
    entry_candidates = sources - targets
    entry_node = "START" if "START" in node_ids else (entry_candidates.pop() if entry_candidates else "")

    decision_nodes = [n for n in node_ids if node_id_to_shape.get(n) == "rhombus"]
    terminal_nodes = [n for n in node_ids if node_id_to_shape.get(n) == "stadium" and n != "START"]
    # Parallelogram are annotations: drop from skeleton and from node count for "action" nodes
    action_node_ids = node_ids - {n for n, s in node_id_to_shape.items() if s == "parallelogram"}

    # Build skeleton: topology only (strip labels from rectangles, drop parallelograms)
    skeleton_lines = ["flowchart TD"]
    for (a, b, label) in edges:
        if a not in action_node_ids or b not in action_node_ids:
            continue
        if label:
            skeleton_lines.append(f"  {a} -->|{label}| {b}")
        else:
            skeleton_lines.append(f"  {a} --> {b}")
    skeleton = "\n".join(skeleton_lines) if len(skeleton_lines) > 1 else "flowchart TD\n  " + entry_node

    return {
        "nodes": list(node_ids),
        "edges": edges,
        "entry_node": entry_node,
        "decision_nodes": decision_nodes,
        "terminal_nodes": terminal_nodes,
        "node_id_to_shape": node_id_to_shape,
        "skeleton": skeleton,
        "node_count": len(action_node_ids),
    }


# --- Per-session state (graph_id -> graph data, path, todos) ---
@dataclass
class SessionState:
    graphs: dict[str, dict] = field(default_factory=dict)  # graph_id -> parsed graph + full source
    path: dict[str, list[str]] = field(default_factory=dict)  # graph_id -> path of node ids
    todos: list[dict] = field(default_factory=list)


_sessions: dict[str, SessionState] = defaultdict(SessionState)


def _get_or_create_session(session_id: str) -> str:
    if not session_id or not session_id.strip():
        session_id = str(uuid.uuid4())
    return session_id


# --- MCP app and tools ---
if HAS_MCP:
    mcp = FastMCP(
        "SOP Graph Navigation",
        json_response=True,
    )

    @mcp.tool()
    def load_graph(
        graph_id: str,
        mermaid_source: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        """
        Parse a Mermaid SOP flowchart. Returns a skeleton graph for the system prompt
        and stores the full graph for goto_node lookups.
        """
        session_id = _get_or_create_session(session_id)
        try:
            parsed = _parse_mermaid_flowchart(mermaid_source)
        except Exception as e:
            _record_connection(session_id, "load_graph", {"graph_id": graph_id, "mermaid_source": "(truncated)"}, f"error: {e}")
            return {
                "error": str(e),
                "graph_id": graph_id,
            }
        state = _sessions[session_id]
        state.graphs[graph_id] = {
            **parsed,
            "mermaid_source": mermaid_source,
        }
        state.path[graph_id] = []

        result = {
            "graph_id": graph_id,
            "skeleton": parsed["skeleton"],
            "entry_node": parsed["entry_node"],
            "node_count": parsed["node_count"],
            "decision_nodes": parsed["decision_nodes"],
            "terminal_nodes": parsed["terminal_nodes"],
        }
        _record_connection(
            session_id,
            "load_graph",
            {"graph_id": graph_id, "mermaid_source": mermaid_source[:200] + "..." if len(mermaid_source) > 200 else mermaid_source},
            f"node_count={parsed['node_count']}",
        )
        return result

    @mcp.tool()
    def goto_node(
        graph_id: str,
        node_id: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        """
        Move to a node in the SOP graph. Returns the full node instructions plus all
        annotation nodes between the current node and the next decision or action node.
        Validates that the transition is legal from the current position.
        """
        session_id = _get_or_create_session(session_id)
        state = _sessions[session_id]
        if graph_id not in state.graphs:
            _record_connection(session_id, "goto_node", {"graph_id": graph_id, "node_id": node_id}, "error: graph not loaded")
            return {"valid": False, "error": "Graph not loaded. Call load_graph first.", "graph_id": graph_id}

        g = state.graphs[graph_id]
        path = state.path[graph_id]
        nodes = set(g["nodes"])
        if node_id not in nodes:
            _record_connection(session_id, "goto_node", {"graph_id": graph_id, "node_id": node_id}, "error: node not found")
            return {"valid": False, "error": f"Node not found. Valid nodes: {list(nodes)[:20]}...", "node_id": node_id}

        # Valid if START or node_id is a direct next from current
        current = path[-1] if path else None
        edges_from_current = [(to_id, lab) for (a, to_id, lab) in g["edges"] if a == current] if current else []
        valid = node_id == g["entry_node"] and not path or any(to_id == node_id for to_id, _ in edges_from_current)

        if not valid and path:
            _record_connection(
                session_id, "goto_node", {"graph_id": graph_id, "node_id": node_id},
                f"invalid transition from {current}",
            )
            return {
                "valid": False,
                "error": f"Cannot reach {node_id} from {current}",
                "current_node": current,
                "valid_next": [t for t, _ in edges_from_current],
            }

        if node_id == g["entry_node"]:
            state.path[graph_id] = [node_id]
        else:
            state.path[graph_id] = path + [node_id] if path else [node_id]

        node_type = g["node_id_to_shape"].get(node_id, "rectangle")
        result = {
            "node": {"id": node_id, "type": node_type, "text": node_id},
            "annotations": [],
            "edges": [{"to": to_id, "condition": lab} for (a, to_id, lab) in g["edges"] if a == node_id],
            "path": state.path[graph_id],
            "valid": True,
        }
        if node_id in g["terminal_nodes"]:
            result["todo_reminder"] = f"Reached completion node {node_id}. Update todos and proceed to next task."

        _record_connection(session_id, "goto_node", {"graph_id": graph_id, "node_id": node_id}, f"path_len={len(state.path[graph_id])}")
        return result

    @mcp.tool()
    def todo(
        todos: list[dict[str, Any]],
        session_id: str = "",
    ) -> dict[str, Any]:
        """
        Create and manage a structured task list for the current conversation.
        Write the full updated list on each call.
        """
        session_id = _get_or_create_session(session_id)
        state = _sessions[session_id]
        state.todos = todos
        pending = sum(1 for t in todos if t.get("status") == "pending")
        in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
        completed = sum(1 for t in todos if t.get("status") == "completed")
        result = {
            "todos": todos,
            "summary": {"pending": pending, "in_progress": in_progress, "completed": completed},
        }
        _record_connection(
            session_id, "todo",
            {"todos": [{"content": t.get("content", "")[:80], "status": t.get("status")} for t in todos]},
            f"pending={pending} in_progress={in_progress} completed={completed}",
        )
        return result


# --- HTTP routes for viewing connections ---
async def list_connections(_request: Request) -> JSONResponse:
    """GET /api/connections — list all sessions with recent activity."""
    return JSONResponse({"sessions": _get_sessions(), "total_events": len(_connection_events)})


async def get_connection_detail(request: Request) -> JSONResponse:
    """GET /api/connections/{session_id} — tool-call log and state for one session."""
    session_id = request.path_params.get("session_id", "")
    detail = _get_session_detail(session_id)
    if detail is None:
        return JSONResponse({"error": "Session not found", "session_id": session_id}, status_code=404)
    return JSONResponse(detail)


def _mermaid_with_traversal_styles(mermaid_source: str, path: list[str], current_node: str | None) -> str:
    out = mermaid_source.rstrip()
    for n in path:
        if n and n.strip():
            out += "\n    style " + n + " fill:" + ("#ffa500" if n == current_node else "#90EE90")
    return out


# --- Viewer routes: /app/viewer (overview), /app/viewer/session/{session_id} (session detail) ---
VIEWER_BASE = "/app/viewer"


def _format_duration(sec: float) -> str:
    """Format duration in seconds as HH:MM:SS or MM:SS."""
    if sec <= 0:
        return "00:00"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


async def _viewer_overview_page(_request: Request) -> HTMLResponse:
    """GET /app/viewer — Sessions Overview (Agent Monitor style)."""
    from .viewer import render_overview, VIEWER_BASE

    sessions = _get_sessions()
    total = len(sessions)
    now = time.time()
    active = sum(1 for s in sessions if (s.get("last_ts") or 0) > now - 300)
    avg_sec = sum(s.get("duration_sec") or 0 for s in sessions) / total if total else 0
    html = render_overview(
        sessions=sessions[:50],
        total_sessions=total,
        active_agents=active,
        avg_duration=_format_duration(avg_sec),
        error_rate="0%",
        viewer_base=VIEWER_BASE,
    )
    return HTMLResponse(html)


async def _viewer_session_page(request: Request) -> HTMLResponse:
    """GET /app/viewer/session/{session_id} — Session Detail (Execution Logs + Process Graph + Traversal Path)."""
    from .viewer import render_session_detail, VIEWER_BASE

    session_id = request.path_params.get("session_id", "")
    detail = _get_session_detail(session_id)
    if detail is None:
        from starlette.responses import RedirectResponse
        return RedirectResponse(url=VIEWER_BASE + "/", status_code=302)
    events = detail.get("events") or []
    graph_state = detail.get("graph_state") or {}
    now = time.time()
    last_ts = events[-1]["ts"] if events else 0
    is_live = last_ts > now - 300
    first_ts = events[0]["ts"] if events else now
    total_uptime = _format_duration((last_ts or now) - first_ts)
    html = render_session_detail(
        session_id=session_id,
        events=events,
        graph_state=graph_state,
        is_live=is_live,
        total_uptime=total_uptime,
        viewer_base=VIEWER_BASE,
        mermaid_with_traversal_styles=_mermaid_with_traversal_styles,
    )
    return HTMLResponse(html)


async def _redirect_to_viewer(_request: Request) -> HTMLResponse:
    """Redirect / to /app/viewer."""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url=VIEWER_BASE + "/", status_code=302)


async def _redirect_view_to_app_viewer(request: Request) -> HTMLResponse:
    """Redirect /view/{session_id} to /app/viewer/session/{session_id}."""
    from starlette.responses import RedirectResponse
    session_id = request.path_params.get("session_id", "")
    return RedirectResponse(url=f"{VIEWER_BASE}/session/{session_id}", status_code=302)


def build_sop_mcp_app() -> Starlette:
    """Build the ASGI app: MCP at /mcp, viewer at /app/viewer, API at /api/connections."""

    if not HAS_MCP:
        return Starlette(
            routes=[
                Route("/", _redirect_to_viewer, methods=["GET"]),
                Route("/app/viewer", _redirect_to_viewer, methods=["GET"]),
                Route("/app/viewer/", _viewer_overview_page, methods=["GET"]),
                Route("/app/viewer/session/{session_id}", _viewer_session_page, methods=["GET"]),
                Route("/view/{session_id}", _redirect_view_to_app_viewer, methods=["GET"]),
                Route("/api/connections", list_connections, methods=["GET"]),
                Route("/api/connections/{session_id}", get_connection_detail, methods=["GET"]),
            ],
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/", _redirect_to_viewer, methods=["GET"]),
            Route("/app/viewer", _redirect_to_viewer, methods=["GET"]),
            Route("/app/viewer/", _viewer_overview_page, methods=["GET"]),
            Route("/app/viewer/session/{session_id}", _viewer_session_page, methods=["GET"]),
            Route("/view/{session_id}", _redirect_view_to_app_viewer, methods=["GET"]),
            Route("/api/connections", list_connections, methods=["GET"]),
            Route("/api/connections/{session_id}", get_connection_detail, methods=["GET"]),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )


app = build_sop_mcp_app()


if __name__ == "__main__":
    import uvicorn

    # Run with streamable HTTP so all connections can be viewed at GET /api/connections
    # MCP endpoint: http://localhost:8000/mcp
    uvicorn.run(
        "agent.agent_mermaid.sop_mcp_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
