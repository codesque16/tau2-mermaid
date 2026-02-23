"""
Agent Monitor viewer UI: templates and render helpers.
Served at /app/viewer (overview) and /app/viewer/session/{session_id} (session detail).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

VIEWER_BASE = "/app/viewer"


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


def _format_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (TypeError, OSError):
        return "—"


def _format_ts_short(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (TypeError, OSError):
        return "—"


def _format_duration(sec: float) -> str:
    if sec <= 0:
        return "00:00"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_overview(
    sessions: list[dict],
    total_sessions: int,
    active_agents: int,
    avg_duration: str,
    error_rate: str,
    viewer_base: str = VIEWER_BASE,
) -> str:
    """Render the Sessions Overview page HTML."""
    now = time.time()
    rows = []
    for s in sessions:
        sid = s.get("session_id", "")
        last_ts = s.get("last_ts") or 0
        is_active = last_ts > now - 300
        status_class = (
            "bg-primary/10 text-primary ring-1 ring-inset ring-primary/20"
            if is_active
            else "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 ring-1 ring-inset ring-slate-200 dark:ring-slate-600"
        )
        status_label = "Active" if is_active else "Completed"
        start_str = _format_ts(s.get("first_ts") or 0)
        dur_str = _format_duration(s.get("duration_sec") or 0)
        last_msg = _escape_html((s.get("last_message") or "")[:200])
        short_id = sid[:12] + "…" if len(sid) > 12 else sid
        session_url = f"{viewer_base}/session/{sid}"
        rows.append(
            f'<tr class="hover:bg-slate-50 dark:hover:bg-slate-800/40 transition-colors">'
            f'<td class="px-6 py-4 whitespace-nowrap"><a href="{session_url}" class="font-mono text-sm text-primary hover:underline">{_escape_html(short_id)}</a></td>'
            f'<td class="px-6 py-4 whitespace-nowrap"><span class="text-sm font-medium">SOP Agent</span></td>'
            f'<td class="px-6 py-4 whitespace-nowrap"><span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium {status_class}">{status_label}</span></td>'
            f'<td class="px-6 py-4 whitespace-nowrap text-sm text-slate-500 dark:text-slate-400">{start_str}</td>'
            f'<td class="px-6 py-4 whitespace-nowrap text-sm text-slate-500 dark:text-slate-400">{dur_str}</td>'
            f'<td class="px-6 py-4 text-sm max-w-xs truncate text-slate-600 dark:text-slate-300" title="{last_msg}">{last_msg or "—"}</td>'
            f'<td class="px-6 py-4 text-right"><a href="{session_url}" class="text-slate-400 hover:text-primary transition-colors"><span class="material-symbols-outlined text-[20px]">more_vert</span></a></td>'
            f"</tr>"
        )
    showing_to = min(50, total_sessions)
    template = _load_template("overview.html")
    return template.replace("{{ viewer_base }}", viewer_base).replace(
        "{{ total_sessions }}", str(total_sessions)
    ).replace(
        "{{ active_agents }}", str(active_agents)
    ).replace(
        "{{ avg_duration }}", avg_duration
    ).replace(
        "{{ error_rate }}", error_rate
    ).replace(
        "{{ sessions_rows }}", "\n            ".join(rows)
    ).replace(
        "{{ showing_to }}", str(showing_to)
    ).replace(
        "{{ total_results }}", str(total_sessions)
    )


def render_session_detail(
    session_id: str,
    events: list[dict],
    graph_state: dict,
    is_live: bool,
    total_uptime: str,
    viewer_base: str = VIEWER_BASE,
    mermaid_with_traversal_styles=None,
) -> str:
    """Render the Session Detail page HTML (Execution Logs + Process Graph + Traversal Path)."""
    short_id = (session_id[:16] + "…") if len(session_id) > 16 else session_id
    live_indicator = ""
    if is_live:
        live_indicator = (
            '<span class="flex h-2 w-2 rounded-full bg-primary animate-pulse"></span>'
            '<span class="text-xs font-medium text-primary">LIVE</span>'
        )

    # Execution logs: each event as TOOL_CALL card
    log_entries = []
    for e in events:
        ts = e.get("ts") or 0
        tool = e.get("tool") or ""
        params = e.get("params") or {}
        result = e.get("result_summary") or ""
        ts_str = _format_ts_short(ts)
        params_str = json.dumps(params, indent=2)[:500]
        params_esc = _escape_html(params_str)
        result_esc = _escape_html(result[:300])
        log_entries.append(
            f'<div class="group">'
            f'<div class="flex items-center gap-3 mb-1">'
            f'<span class="text-slate-400 dark:text-[#5c7a7a] text-[11px]">{ts_str}</span>'
            f'<span class="bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded text-[10px] font-bold">TOOL_CALL</span>'
            f'<span class="text-slate-800 dark:text-slate-200 font-bold">{_escape_html(tool)}</span>'
            f'</div>'
            f'<div class="bg-white dark:bg-[#1a2e2e] rounded border border-slate-200 dark:border-[#283939] p-3 ml-4 shadow-sm">'
            f'<pre class="text-blue-600 dark:text-primary/80 leading-relaxed whitespace-pre-wrap break-words">{params_esc}</pre>'
            + (f'<div class="mt-2 pt-2 border-t border-slate-100 dark:border-[#283939]"><span class="text-[10px] text-slate-400 block mb-1">RESULT:</span><pre class="text-slate-600 dark:text-slate-400 leading-tight text-xs">{result_esc}</pre></div>' if result_esc else "")
            + f'</div></div>'
        )
    execution_logs_html = "\n        ".join(log_entries) if log_entries else '<p class="text-slate-500 dark:text-slate-400 text-sm">No tool calls yet.</p>'

    # Process graph: use full mermaid_source (from retail-agent-sop.md) so the graph matches the md file; fallback to skeleton
    process_graph_parts = []
    for gid, g in graph_state.items():
        mermaid_source = g.get("mermaid_source") or g.get("skeleton") or ""
        path = g.get("path") or []
        current = g.get("current_node")
        if mermaid_with_traversal_styles and mermaid_source:
            styled = mermaid_with_traversal_styles(mermaid_source, path, current)
            escaped = _escape_html(styled)
            process_graph_parts.append(
                f'<div class="w-full flex justify-center mb-6">'
                f'<div class="w-full max-w-2xl"><pre class="mermaid text-sm font-mono">{escaped}</pre></div>'
                f'</div>'
            )
        else:
            process_graph_parts.append(
                f'<div class="text-slate-500 dark:text-slate-400 text-sm">Graph: {_escape_html(gid)} (no mermaid)</div>'
            )
    process_graph_html = "\n      ".join(process_graph_parts) if process_graph_parts else '<p class="text-slate-500 dark:text-slate-400">No graph loaded.</p>'

    # Traversal path breadcrumb (from first graph)
    path_parts = []
    for _gid, _g in graph_state.items():
        path = _g.get("path") or []
        current = _g.get("current_node")
        for n in path:
            is_current = n == current
            if is_current:
                path_parts.append(
                    f'<div class="flex items-center px-3 py-1.5 rounded-lg bg-primary/10 border-2 border-primary shadow-[0_0_10px_rgba(13,242,242,0.2)]">'
                    f'<span class="text-xs font-bold text-primary">{_escape_html(n)}</span>'
                    f'<span class="material-symbols-outlined text-primary text-[14px] ml-1.5 animate-pulse">sync</span>'
                    f'</div>'
                )
            else:
                path_parts.append(
                    f'<div class="flex items-center px-3 py-1.5 rounded bg-slate-100 dark:bg-[#1a2e2e] border border-slate-200 dark:border-[#283939] traversal-arrow">'
                    f'<span class="text-xs font-semibold text-slate-600 dark:text-slate-300">{_escape_html(n)}</span>'
                    f'</div>'
                )
        break
    traversal_path_html = "\n        ".join(path_parts) if path_parts else '<span class="text-slate-500 dark:text-slate-400 text-xs">—</span>'

    template = _load_template("session_detail.html")
    return (
        template.replace("{{ viewer_base }}", viewer_base)
        .replace("{{ session_id_short }}", _escape_html(short_id))
        .replace("{{ live_indicator }}", live_indicator)
        .replace("{{ execution_logs }}", execution_logs_html)
        .replace("{{ process_graph }}", process_graph_html)
        .replace("{{ traversal_path }}", traversal_path_html)
        .replace("{{ total_uptime }}", total_uptime)
    )
