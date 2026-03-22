"""Simple, self-contained evaluator for retail solo tasks.

For each task:
  - Replays the agent's actual tool calls over an in-memory RetailDB to get a
    predicted final DB hash.
  - Replays the golden action sequence from tasks_solo_comms.json over a fresh
    RetailDB to get the golden DB hash.
  - Compares the two hashes to determine pass/fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _json_schema_type_to_python(prop_schema: Any) -> str:
    if not isinstance(prop_schema, dict):
        return "Any"
    if "anyOf" in prop_schema or "oneOf" in prop_schema:
        return "Any"
    t = prop_schema.get("type")
    if t == "string":
        return "str"
    if t == "integer":
        return "int"
    if t == "number":
        return "float"
    if t == "boolean":
        return "bool"
    if t == "array":
        items = prop_schema.get("items")
        if isinstance(items, dict):
            inner = _json_schema_type_to_python(items)
        else:
            inner = "Any"
        return f"List[{inner}]"
    if t == "object":
        return "dict"
    return "Any"


def _json_literal_for_schema_default(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return repr(value)


def _format_tool_signature_from_json_schema(schema: Any) -> str:
    """``(order_id: str, reason: str)`` from MCP/OpenAI-style JSON Schema."""
    _HIDDEN = frozenset({"session_id", "ctx"})
    if not isinstance(schema, dict):
        return "()"
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "()"
    props = {k: v for k, v in props.items() if k not in _HIDDEN}
    if not props:
        return "()"
    required = set(schema.get("required") or []) - _HIDDEN
    keys = list(props.keys())
    ordered = [k for k in keys if k in required] + [k for k in keys if k not in required]
    parts: List[str] = []
    for key in ordered:
        psub = props[key]
        if not isinstance(psub, dict):
            psub = {}
        typ = _json_schema_type_to_python(psub)
        if key in required:
            parts.append(f"{key}: {typ}")
        elif "default" in psub:
            parts.append(f"{key}: {typ} = {_json_literal_for_schema_default(psub['default'])}")
        else:
            parts.append(f"{key}: {typ}")
    return "(" + ", ".join(parts) + ")"


def _tool_input_schema_dict(tool_obj: Any) -> dict[str, Any]:
    raw = getattr(tool_obj, "inputSchema", None) or getattr(tool_obj, "input_schema", None)
    if isinstance(raw, dict):
        return raw
    if raw is not None and hasattr(raw, "model_dump"):
        dumped = raw.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


async def list_mcp_tools_tau2_style(*, mcp_command: str) -> str:
    """Numbered tool list like tau2 GEPA: ``1) name(a: str, b: int = 0)`` from MCP ``list_tools``."""
    cmd_str = (mcp_command or "").strip()
    if not cmd_str:
        return "(MCP command not configured; cannot list tools.)"

    import shlex

    parts = shlex.split(cmd_str)
    if not parts:
        return f"(Invalid MCP command: {cmd_str!r})"

    cmd, *args = parts
    params = StdioServerParameters(command=cmd, args=args)

    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
    except Exception as e:
        return f"(Failed to list MCP tools: {type(e).__name__}: {e})"

    tools = list(getattr(tools_result, "tools", []) or [])
    tools.sort(key=lambda t: str(getattr(t, "name", "") or ""))

    lines: List[str] = ["Tool list available to the agent:"]
    for idx, t in enumerate(tools, start=1):
        name = getattr(t, "name", "") or ""
        if not name:
            continue
        sig = _format_tool_signature_from_json_schema(_tool_input_schema_dict(t))
        lines.append(f"{idx}) {name}{sig}")

    return "\n".join(lines) if len(lines) > 1 else "(MCP returned no tools.)"


def _norm_args_json(args: Any) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(args or {})


def format_solo_eval_for_gepa_diagnosis(
    *,
    task: dict[str, Any],
    eval_result: dict[str, Any],
    assistant_history: List[dict[str, Any]],
    score: float,
    evaluate_communication: bool,
) -> str:
    """Tau2-style ``<evaluation>`` block for qualitative diagnosis (solo retail / MCP)."""
    parts: List[str] = [
        f"Reward: {score:.4f}",
        "Termination: AGENT_STOP",
    ]
    db_ok = bool(eval_result.get("db_match", False))
    db_reward = 1.0 if db_ok else 0.0
    parts.append(f"DB check: {'match' if db_ok else 'MISMATCH'} (reward={db_reward})")

    golden = (task.get("evaluation_criteria") or {}).get("actions") or []
    predicted = _extract_predicted_actions(assistant_history)

    for i, g in enumerate(golden):
        name = g.get("name") or "?"
        exp_args = g.get("arguments") or {}
        exp_json = _norm_args_json(exp_args)
        if i >= len(predicted):
            parts.append(f"Action {name}: NOT CALLED (expected_args={exp_json})")
            continue
        p = predicted[i]
        if p.get("name") != name:
            parts.append(f"Action {name}: NOT CALLED (expected_args={exp_json})")
            continue
        act_args = p.get("arguments") or {}
        if _norm_args_json(act_args) != exp_json:
            act_json = _norm_args_json(act_args)
            parts.append(
                f"Action {name}: ARGUMENTS_MISMATCH "
                f"(expected_args={exp_json}, actual_args={act_json})"
            )

    if evaluate_communication and not eval_result.get("communicate_eval_skipped"):
        for chk in eval_result.get("communicate_checks") or []:
            info = str(chk.get("info", ""))
            met = bool(chk.get("met", False))
            status = "met" if met else "NOT MET"
            parts.append(f"Communicate '{info}': {status}")
            if not met and chk.get("justification"):
                parts.append(f"  Justification: {chk.get('justification')}")

    return "\n".join(parts)


def _compact_trace_preview(
    history: List[dict[str, Any]],
    *,
    max_tool_calls: int = 60,
    max_text_chars: int = 1200,
) -> str:
    """Compact, stable preview of tool usage + final text for GEPA side_info."""
    tool_lines: List[str] = []
    last_assistant_text: str | None = None

    for m in history:
        if m.get("role") != "assistant":
            continue
        text = m.get("content")
        if isinstance(text, str) and text.strip():
            last_assistant_text = text.strip()
        for tc in m.get("tool_calls", []) or []:
            name = tc.get("name") or ""
            args = tc.get("arguments") or {}
            if not name:
                continue
            try:
                import json

                args_str = json.dumps(args, ensure_ascii=False, sort_keys=True)
            except Exception:
                args_str = str(args)
            tool_lines.append(f"- {name}({args_str})")
            if len(tool_lines) >= max_tool_calls:
                break
        if len(tool_lines) >= max_tool_calls:
            break

    out: List[str] = []
    if tool_lines:
        out.append("Tool calls (ordered):")
        out.extend(tool_lines)
    if last_assistant_text:
        txt = last_assistant_text
        if len(txt) > max_text_chars:
            txt = txt[: max_text_chars - 1] + "…"
        out.append("")
        out.append("Final assistant message preview:")
        out.append(txt)

    return "\n".join(out).strip()


async def _run_sequence_get_hash(
    *,
    actions: List[Dict[str, Any]],
    mcp_command: str,
) -> str:
    """Run a sequence of tool calls against the retail MCP server and return DB hash."""
    cmd_str = (mcp_command or "").strip()
    if not cmd_str:
        raise ValueError("mcp_command is required for DB evaluation")

    import shlex

    parts = shlex.split(cmd_str)
    if not parts:
        raise ValueError(f"Invalid MCP command: {cmd_str!r}")

    cmd, *args = parts
    params = StdioServerParameters(command=cmd, args=args)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Replay actions
            for action in actions:
                name = action.get("name")
                if not name:
                    continue
                arguments = action.get("arguments") or {}
                await session.call_tool(name, arguments=arguments)

            # Ask server for DB hash after all actions
            result = await session.call_tool("get_db_hash", arguments={})

    # Extract hash from MCP response
    if hasattr(result, "structuredContent") and result.structuredContent:
        # Expecting something like {"hash": "..."}
        sc = result.structuredContent
        # structuredContent can be list[dict] or dict depending on SDK;
        # handle the common cases defensively.
        if isinstance(sc, dict) and "hash" in sc:
            return str(sc["hash"])
        if isinstance(sc, list) and sc and isinstance(sc[0], dict) and "hash" in sc[0]:
            return str(sc[0]["hash"])

    texts: List[str] = []
    for c in getattr(result, "content", []) or []:
        if hasattr(c, "text") and c.text:
            texts.append(c.text)
    if texts:
        # Either the tool returned the hash directly, or JSON with a 'hash' field.
        text = "\n".join(texts).strip()
        if text.startswith("{") and "hash" in text:
            try:
                import json

                payload = json.loads(text)
                if "hash" in payload:
                    return str(payload["hash"])
            except Exception:
                pass
        return text

    raise RuntimeError("MCP get_db_hash returned no content")


async def list_mcp_tools_documentation(*, mcp_command: str) -> str:
    """Return markdown lines of tool names and descriptions via MCP ``list_tools``."""
    cmd_str = (mcp_command or "").strip()
    if not cmd_str:
        return "(MCP command not configured; cannot list tools.)"

    import shlex

    parts = shlex.split(cmd_str)
    if not parts:
        return f"(Invalid MCP command: {cmd_str!r})"

    cmd, *args = parts
    params = StdioServerParameters(command=cmd, args=args)

    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
    except Exception as e:
        return f"(Failed to list MCP tools: {type(e).__name__}: {e})"

    lines: List[str] = []
    for t in getattr(tools_result, "tools", []) or []:
        name = getattr(t, "name", "") or ""
        if not name:
            continue
        desc = (getattr(t, "description", None) or "").strip()
        if desc:
            lines.append(f"- **{name}**: {desc}")
        else:
            lines.append(f"- **{name}**")

    return "\n".join(lines) if lines else "(MCP returned no tools.)"


def _extract_predicted_actions(history: List[dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten tool calls from LiteLLMAgent.history into a simple sequence.

    Each action is {"name": str, "arguments": dict}.
    """
    actions: List[Dict[str, Any]] = []
    for m in history:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []) or []:
            name = tc.get("name")
            args = tc.get("arguments") or {}
            if name:
                actions.append({"name": name, "arguments": dict(args)})
    return actions


def _extract_goto_node_sequence(history: List[dict[str, Any]]) -> List[str]:
    """Extract the sequence of node_ids from goto_node tool calls in assistant history."""
    path: List[str] = []
    for m in history:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []) or []:
            if tc.get("name") != "goto_node":
                continue
            args = tc.get("arguments") or {}
            node_id = args.get("node_id")
            if isinstance(node_id, str) and node_id.strip():
                path.append(node_id.strip())
    return path


def _path_match(predicted_path: List[str], golden_path: List[str]) -> bool:
    """True if predicted goto_node sequence matches the golden mermaid path (exact)."""
    if not golden_path:
        return True
    return predicted_path == golden_path


def _first_path_mismatch(
    predicted_path: List[str],
    golden_path: List[str],
) -> dict[str, Any] | None:
    """Return mismatch detail dict or None if exact match (or no golden path)."""
    if not golden_path:
        return None
    n = min(len(predicted_path), len(golden_path))
    for i in range(n):
        if predicted_path[i] != golden_path[i]:
            return {
                "index": i,
                "expected_node_id": golden_path[i],
                "actual_node_id": predicted_path[i],
                "reason": "node_id_mismatch",
            }
    if len(predicted_path) != len(golden_path):
        return {
            "index": n,
            "expected_node_id": golden_path[n] if n < len(golden_path) else None,
            "actual_node_id": predicted_path[n] if n < len(predicted_path) else None,
            "reason": "length_mismatch",
        }
    return None


async def evaluate_task_db(
    *,
    task: dict[str, Any],
    assistant_history: List[dict[str, Any]],
    db_path: Path,
    mcp_command: str,
) -> Dict[str, Any]:
    """Evaluate one task by comparing golden vs predicted DB hashes and (optional) mermaid path.

    Returns a dict with:
      - task_id
      - golden_hash
      - predicted_hash
      - db_match (bool)
      - golden_actions_count
      - predicted_actions_count
      - golden_mermaid_path (list or None)
      - predicted_goto_sequence (list)
      - path_match (bool; True if no golden path or predicted sequence equals golden)
    """
    eval_crit = task.get("evaluation_criteria") or {}
    golden_actions = eval_crit.get("actions") or []
    golden_mermaid_path: List[str] = list(eval_crit.get("golden_mermaid_path") or [])

    # Build golden DB hash by replaying golden actions via MCP server
    golden_hash = await _run_sequence_get_hash(
        actions=golden_actions,
        mcp_command=mcp_command,
    )

    # Build predicted DB hash by replaying actual tool calls from the run
    predicted_actions = _extract_predicted_actions(assistant_history)
    predicted_hash = await _run_sequence_get_hash(
        actions=predicted_actions,
        mcp_command=mcp_command,
    )

    predicted_goto = _extract_goto_node_sequence(assistant_history)
    path_match = _path_match(predicted_goto, golden_mermaid_path)
    mismatch = _first_path_mismatch(predicted_goto, golden_mermaid_path)
    trace_preview = _compact_trace_preview(assistant_history)

    return {
        "task_id": task.get("id"),
        "golden_hash": golden_hash,
        "predicted_hash": predicted_hash,
        "db_match": golden_hash == predicted_hash,
        "golden_actions_count": len(golden_actions),
        "predicted_actions_count": len(predicted_actions),
        "golden_mermaid_path": golden_mermaid_path if golden_mermaid_path else None,
        "predicted_goto_sequence": predicted_goto,
        "path_match": path_match,
        "path_mismatch": mismatch,
        "trace_preview": trace_preview,
    }


def evaluate_communication_from_history(
    *,
    task: dict[str, Any],
    assistant_history: List[dict[str, Any]],
) -> Dict[str, Any]:
    """Check that assistant text turns contain each string in evaluation_criteria.communicate_info.

    Mirrors tau2 ``CommunicateEvaluator`` (case-insensitive substring match, commas stripped
    from assistant text). Empty ``communicate_info`` yields ``communicate_match: True``.
    """
    eval_crit = task.get("evaluation_criteria") or {}
    communicate_info_raw = eval_crit.get("communicate_info")
    if not communicate_info_raw:
        return {
            "communicate_match": True,
            "communicate_checks": [],
            "communicate_eval_skipped": True,
        }

    communicate_info: List[str] = [
        str(s) for s in communicate_info_raw if isinstance(s, str) and s.strip()
    ]
    if not communicate_info:
        return {
            "communicate_match": True,
            "communicate_checks": [],
            "communicate_eval_skipped": True,
        }

    checks: List[Dict[str, Any]] = []
    all_met = True
    for info_str in communicate_info:
        found = False
        matched_content = ""
        for m in assistant_history:
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            normalized = content.lower().replace(",", "")
            if info_str.lower() in normalized:
                found = True
                matched_content = content
                break
        if found:
            checks.append(
                {
                    "info": info_str,
                    "met": True,
                    "justification": (
                        f"Information '{info_str}' communicated in the message:\n '{matched_content}'"
                    ),
                }
            )
        else:
            all_met = False
            checks.append(
                {
                    "info": info_str,
                    "met": False,
                    "justification": f"Information '{info_str}' not communicated.",
                }
            )

    return {
        "communicate_match": all_met,
        "communicate_checks": checks,
        "communicate_eval_skipped": False,
    }

