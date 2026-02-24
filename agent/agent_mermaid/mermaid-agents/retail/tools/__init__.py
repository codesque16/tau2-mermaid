"""
Native tools for the retail mermaid agent.

Standard interface for mermaid-agent tools:
  - get_openai_tools(agent_dir) -> list[dict]   # OpenAI/litellm tool definitions
  - execute_tool(name, arguments, agent_dir) -> str

Uses the local data_model and tools in this package (same folder). When tau2 is
available, tools.py uses tau2's toolkit; when not, it uses toolkit_stub. db.json
is always loaded from agent_dir/tools/db.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def get_openai_tools(agent_dir: Path) -> list[dict[str, Any]]:
    """Return OpenAI-format tool list for the retail agent. Uses local .data_model and .tools."""
    try:
        from .data_model import RetailDB
        from .tools import RetailTools
    except ImportError:
        return []

    db_path = agent_dir / "tools" / "db.json"
    if not db_path.is_file():
        return []
    db = RetailDB.load(str(db_path))
    toolkit = RetailTools(db)
    tools_dict = toolkit.get_tools()
    out = []
    for name, tool in tools_dict.items():
        schema = getattr(tool, "openai_schema", None)
        if schema and isinstance(schema, dict):
            out.append({"type": "function", "function": schema["function"]})
        else:
            sig = getattr(tool, "params", None)
            params_schema = sig.model_json_schema() if hasattr(sig, "model_json_schema") else {"type": "object", "properties": {}}
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": getattr(tool, "short_desc", "") or str(tool),
                    "parameters": params_schema,
                },
            })
    return out


def execute_tool(name: str, arguments: dict[str, Any], agent_dir: Path, **context: Any) -> str:
    """Execute a retail tool by name. Uses local .data_model and .tools; db from agent_dir/tools/db.json."""
    try:
        from .data_model import RetailDB
        from .tools import RetailTools
    except ImportError:
        return json.dumps({"error": "retail tools package incomplete; native retail tools unavailable"})

    db_path = agent_dir / "tools" / "db.json"
    if not db_path.is_file():
        return json.dumps({"error": "db.json not found under agent tools directory"})
    db = RetailDB.load(str(db_path))
    toolkit = RetailTools(db)
    if not toolkit.has_tool(name):
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = toolkit.use_tool(name, **arguments)
        if isinstance(result, str):
            return result
        if hasattr(result, "model_dump"):
            return json.dumps(result.model_dump())
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})
