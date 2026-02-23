"""
Common loader for native tools attached to a mermaid agent.

Convention: each agent may have a `tools/` directory. That directory must be a
Python package and expose the standard interface:

  - get_openai_tools(agent_dir: Path) -> list[dict]
    Returns a list of tools in OpenAI/litellm format:
    [{"type": "function", "function": {"name": str, "description": str, "parameters": schema}}, ...]

  - execute_tool(name: str, arguments: dict, agent_dir: Path, **context) -> str
    Executes the named tool with the given arguments. Returns a string (typically JSON).

The agent uses this to merge native tools with MCP tools (or enter_mermaid_node)
and to dispatch tool calls via a single calling structure.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

def load_native_tools(agent_dir: Path) -> tuple[list[dict[str, Any]], Callable[[str, dict], str] | None]:
    """
    Load native tools for an agent from agent_dir/tools/.

    Returns:
        (tools_list, executor) where tools_list is OpenAI-format tool definitions
        and executor(name, arguments) runs a tool and returns a string.
        If no tools package or interface is found, returns ([], None).
    """
    agent_dir = Path(agent_dir).resolve()
    tools_dir = agent_dir / "tools"
    if not tools_dir.is_dir():
        return ([], None)

    init_py = tools_dir / "__init__.py"
    if not init_py.is_file():
        return ([], None)

    # Load the tools package: add agent_dir to path so "import tools" finds agent_dir/tools
    parent = str(agent_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    try:
        spec = importlib.util.spec_from_file_location("agent_tools", init_py, submodule_search_locations=[str(tools_dir)])
        if spec is None or spec.loader is None:
            return ([], None)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_tools"] = module
        spec.loader.exec_module(module)
    except Exception:
        return ([], None)
    finally:
        if parent in sys.path:
            sys.path.remove(parent)

    if not hasattr(module, "get_openai_tools") or not hasattr(module, "execute_tool"):
        return ([], None)

    try:
        tools_list = module.get_openai_tools(agent_dir)
    except Exception:
        return ([], None)
    if not isinstance(tools_list, list):
        return ([], None)

    def executor(name: str, arguments: dict[str, Any]) -> str:
        try:
            return module.execute_tool(name, arguments, agent_dir)
        except Exception as e:
            return str(e)

    return (tools_list, executor)
