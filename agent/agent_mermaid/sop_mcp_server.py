"""
SOP MCP server: streamable HTTP transport with load_graph, goto_node, and todo tools.
Exposes GET /api/connections and an Agent Monitor–style viewer (Overview + Session detail).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# Optional: use mcp only when available
try:
    from mcp.server.fastmcp import FastMCP

    HAS_MCP = True
except ImportError:
    HAS_MCP = False

# Paths for viewer app (Vite build) and mermaid-agents list
from agent.agent_mermaid.mermaid_graph import mermaid_to_graph_json, graph_json_to_mermaid

_AGENT_MERMAID_DIR = Path(__file__).resolve().parent
VIEWER_APP_DIST = _AGENT_MERMAID_DIR / "viewer-app" / "dist"
MERMAID_AGENTS_DIR = _AGENT_MERMAID_DIR / "mermaid-agents"


def _safe_agent_name(name: str) -> bool:
    """Allow only dir-name-safe characters (no path traversal)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return False
    return all(c.isalnum() or c in "_-" for c in name)


def _parse_agents_md(content: str) -> dict[str, Any]:
    """Parse AGENTS.md content into frontmatter, mermaid, node_prompts, rest_md."""
    frontmatter = ""
    rest_md = ""
    mermaid = ""
    node_prompts: dict[str, str] = {}

    fm_start = content.find("---")
    if fm_start >= 0:
        fm_end = content.find("---", fm_start + 3)
        if fm_end >= 0:
            frontmatter = content[fm_start : fm_end + 3].strip()
            content = content[fm_end + 3 :].lstrip("\n")
    body = content

    sop_header = "## SOP Flowchart"
    idx_sop = body.find(sop_header)
    if idx_sop >= 0:
        rest_md = body[:idx_sop].strip()
        body = body[idx_sop:]
    else:
        rest_md = body.strip()
        body = ""

    if body:
        mm_start = body.find("```mermaid")
        if mm_start >= 0:
            mm_start = body.find("\n", mm_start) + 1
            mm_end = body.find("```", mm_start)
            if mm_end >= 0:
                mermaid = body[mm_start:mm_end].strip()
        np_header = "## Node Prompts"
        idx_np = body.find(np_header)
        if idx_np >= 0:
            prompts_section = body[idx_np + len(np_header) :].strip()
            parts = re.split(r"\n###\s+", prompts_section, flags=re.IGNORECASE)
            for i, block in enumerate(parts):
                block = block.strip()
                if not block:
                    continue
                first_line = block.split("\n")[0].strip()
                if not first_line:
                    continue
                if i == 0 and first_line.lower() == "node prompts":
                    continue
                node_id = first_line
                prompt_content = "\n".join(block.split("\n")[1:]).strip()
                node_prompts[node_id] = prompt_content

    return {
        "frontmatter": frontmatter,
        "rest_md": rest_md,
        "mermaid": mermaid,
        "node_prompts": node_prompts,
    }


def _compose_agents_md(
    frontmatter: str, rest_md: str, mermaid: str, node_prompts: dict[str, str]
) -> str:
    """Compose full AGENTS.md content from parts."""
    out = []
    if frontmatter:
        out.append(
            frontmatter if frontmatter.startswith("---") else f"---\n{frontmatter}\n---"
        )
        out.append("")
    if rest_md:
        out.append(rest_md.strip())
        out.append("")
    out.append("## SOP Flowchart")
    out.append("")
    out.append("```mermaid")
    out.append(mermaid.strip() if mermaid.strip() else "flowchart TD")
    out.append("```")
    out.append("")
    out.append("## Node Prompts")
    out.append("")
    for node_id, prompt in sorted(node_prompts.items()):
        out.append(f"### {node_id}")
        out.append("")
        out.append(prompt.strip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


class SpaStaticFiles(StaticFiles):
    """Serve static files and fallback to index.html for SPA client-side routes."""

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response
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
                "nodes": g.get("nodes"),
                "edges": g.get("edges"),
                "node_id_to_shape": g.get("node_id_to_shape"),
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


# --- Intermediate graph JSON: use mermaid_graph package (mermaid_to_graph_json, graph_json_to_mermaid) ---


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


async def list_agents(_request: Request) -> JSONResponse:
    """GET /api/agents — list agent names (subdirectories of mermaid-agents that contain AGENTS.md)."""
    agents: list[str] = []
    if MERMAID_AGENTS_DIR.is_dir():
        for p in sorted(MERMAID_AGENTS_DIR.iterdir()):
            if p.is_dir() and (p / "AGENTS.md").is_file():
                agents.append(p.name)
    return JSONResponse({"agents": agents})


async def get_agent_content(request: Request) -> JSONResponse:
    """GET /api/agents/{agent_name} — parsed AGENTS.md (frontmatter, mermaid, node_prompts, rest_md) + flow nodes/edges."""
    agent_name = request.path_params.get("agent_name", "").strip()
    if not _safe_agent_name(agent_name):
        return JSONResponse(
            {"error": "Invalid agent name", "agent_name": agent_name},
            status_code=400,
        )
    path = MERMAID_AGENTS_DIR / agent_name / "AGENTS.md"
    if not path.is_file():
        return JSONResponse(
            {"error": "Agent not found", "agent_name": agent_name},
            status_code=404,
        )
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        return JSONResponse(
            {"error": f"Failed to read file: {e}", "agent_name": agent_name},
            status_code=500,
        )
    parsed = _parse_agents_md(content)
    graph_json: dict[str, Any] = {"nodes": [], "edges": []}
    if parsed["mermaid"]:
        try:
            graph_json = mermaid_to_graph_json(parsed["mermaid"])
        except Exception:
            pass
    logging.info(
        "[get_agent_content] agent_name=%s graph_json nodes=%s edges=%s",
        agent_name,
        len(graph_json.get("nodes", [])),
        len(graph_json.get("edges", [])),
    )
    logging.info("[get_agent_content] edges: %s", graph_json.get("edges", []))
    logging.debug("[get_agent_content] graph_json full: %s", json.dumps(graph_json, indent=2))
    return JSONResponse({
        "agent_name": agent_name,
        "frontmatter": parsed["frontmatter"],
        "rest_md": parsed["rest_md"],
        "mermaid": parsed["mermaid"],
        "node_prompts": parsed["node_prompts"],
        "graph_json": graph_json,
    })


async def save_agent_content(request: Request) -> JSONResponse:
    """POST /api/agents/{agent_name}/save — overwrite AGENTS.md with composed content."""
    agent_name = request.path_params.get("agent_name", "").strip()
    if not _safe_agent_name(agent_name):
        return JSONResponse(
            {"error": "Invalid agent name", "agent_name": agent_name},
            status_code=400,
        )
    path = MERMAID_AGENTS_DIR / agent_name / "AGENTS.md"
    if not path.is_file():
        return JSONResponse(
            {"error": "Agent not found", "agent_name": agent_name},
            status_code=404,
        )
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"error": f"Invalid JSON: {e}", "agent_name": agent_name},
            status_code=400,
        )
    frontmatter = body.get("frontmatter", "")
    rest_md = body.get("rest_md", "")
    graph_json = body.get("graph_json")
    if graph_json is not None and isinstance(graph_json, dict):
        mermaid = graph_json_to_mermaid(graph_json)
    else:
        mermaid = body.get("mermaid", "")
    node_prompts = body.get("node_prompts", {})
    if not isinstance(node_prompts, dict):
        node_prompts = {}
    try:
        full_content = _compose_agents_md(
            frontmatter, rest_md, mermaid, node_prompts
        )
        path.write_text(full_content, encoding="utf-8")
    except OSError as e:
        return JSONResponse(
            {"error": f"Failed to write file: {e}", "agent_name": agent_name},
            status_code=500,
        )
    return JSONResponse({"ok": True, "agent_name": agent_name})


# --- Viewer routes: /app/viewer (Vite SPA when built, else "build required" page) ---
VIEWER_BASE = "/app/viewer"

_BUILD_REQUIRED_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Viewer — build required</title>
<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#1e293b;}
code{background:#f1f5f9;padding:.2em .4em;border-radius:4px;}</style></head>
<body>
<h1>Viewer not built</h1>
<p>Build the viewer app to use the UI at <code>/app/viewer</code>:</p>
<pre><code>cd agent/agent_mermaid/viewer-app
npm install
npm run build</code></pre>
<p>Then restart the server and open <a href="/app/viewer/">/app/viewer/</a>.</p>
</body>
</html>
"""


async def _viewer_build_required_page(_request: Request) -> HTMLResponse:
    """Served at /app/viewer when viewer-app/dist does not exist."""
    return HTMLResponse(_BUILD_REQUIRED_HTML)


def _format_duration(sec: float) -> str:
    """Format duration in seconds as HH:MM:SS or MM:SS."""
    if sec <= 0:
        return "00:00"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


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
    """Build the ASGI app: MCP at /mcp, viewer at /app/viewer, API at /api/connections.
    When viewer-app is built (viewer-app/dist exists), the Vite SPA is served at /app/viewer.
    Otherwise the template-based viewer is used.
    """
    api_routes = [
        Route("/api/connections", list_connections, methods=["GET"]),
        Route("/api/connections/{session_id}", get_connection_detail, methods=["GET"]),
        Route("/api/agents", list_agents, methods=["GET"]),
        Route("/api/agents/{agent_name}", get_agent_content, methods=["GET"]),
        Route("/api/agents/{agent_name}/save", save_agent_content, methods=["POST"]),
    ]
    if VIEWER_APP_DIST.is_dir():
        viewer_routes = [
            Mount(
                "/app/viewer",
                SpaStaticFiles(directory=str(VIEWER_APP_DIST), html=True),
                name="viewer_spa",
            ),
        ]
    else:
        viewer_routes = [
            Route("/app/viewer/", _viewer_build_required_page, methods=["GET"]),
            Route("/app/viewer/session/{session_id}", _viewer_build_required_page, methods=["GET"]),
        ]

    common_routes = [
        Route("/", _redirect_to_viewer, methods=["GET"]),
        Route("/app/viewer", _redirect_to_viewer, methods=["GET"]),
        Route("/view/{session_id}", _redirect_view_to_app_viewer, methods=["GET"]),
        *api_routes,
        *viewer_routes,
    ]

    if not HAS_MCP:
        return Starlette(routes=common_routes)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            *common_routes,
            Mount("/", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )


app = build_sop_mcp_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Run with streamable HTTP so all connections can be viewed at GET /api/connections
    # MCP endpoint: http://localhost:8000/mcp
    uvicorn.run(
        "agent.agent_mermaid.sop_mcp_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
