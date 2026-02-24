"""Mermaid agent: structured tools and harness with progressive discovery via mermaid nodes."""

import json
from pathlib import Path
from typing import Any, Callable, Awaitable

import litellm

from agent.base import BaseAgent
from agent.config import AgentConfig
from agent.utils.cost import compute_cost, usage_from_openai_response

from .utils import (
    get_mermaid_agent_dir,
    get_node_instructions,
    list_mermaid_nodes,
    load_agent_mermaid,
    load_agent_system_prompt,
    load_sop_markdown,
)
from .tools_loader import load_native_tools

# Default root for mermaid-agents (relative to this package)
_DEFAULT_MERMAID_AGENTS_ROOT = Path(__file__).resolve().parent / "mermaid-agents"

ENTER_NODE_TOOL = {
    "type": "function",
    "function": {
        "name": "enter_mermaid_node",
        "description": "Enter a mermaid workflow node to load task-specific instructions for that node. Call this when you want to follow the workflow and get the exact steps/instructions for a particular capability (e.g. intake, new_booking, cancel_booking). Only one node_id per call.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "The node identifier (e.g. intake, classify, new_booking, cancel_booking, modify_booking, handle_complaint, escalate, confirm, general_info). Must match a node in the mermaid diagram.",
                }
            },
            "required": ["node_id"],
        },
    },
}


def _cost_model(model: str) -> str:
    """Use last segment for cost lookup."""
    return model.split("/")[-1] if "/" in model else model


def _mcp_tools_to_openai_format(tools_response: Any) -> list[dict[str, Any]]:
    """Convert MCP ListToolsResult to OpenAI/litellm tools list.
    Strip server-injected params (ctx, session_id) from schema so the model only sees tool args.
    """
    _HIDDEN_PARAMS = {"session_id", "ctx"}
    out = []
    for tool in getattr(tools_response, "tools", []) or []:
        name = getattr(tool, "name", "") or ""
        description = getattr(tool, "description", None) or ""
        input_schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
        if isinstance(input_schema, dict):
            props = input_schema.get("properties") or {}
            required = input_schema.get("required") or []
            input_schema = {
                **input_schema,
                "properties": {k: v for k, v in props.items() if k not in _HIDDEN_PARAMS},
                "required": [r for r in required if r not in _HIDDEN_PARAMS],
            }
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": input_schema,
            },
        })
    return out


def _build_system_prompt(agent_dir: Path, nodes_list: list[str]) -> str:
    """Build system prompt from index.md, agent-mermaid.md, and tool instructions."""
    prompt = load_agent_system_prompt(agent_dir)
    mermaid = load_agent_mermaid(agent_dir)
    if mermaid:
        prompt += "\n\n## Workflow (Mermaid)\n\nYou may enter any node to get task-specific instructions. The diagram:\n\n```mermaid\n" + mermaid + "\n```"
    if nodes_list:
        prompt += "\n\n## Available nodes\n\nYou can call the tool `enter_mermaid_node` with one of these node_id values to load instructions for that step: " + ", ".join(nodes_list) + "."
    prompt += "\n\nWhen you need to perform or follow a specific workflow step, call enter_mermaid_node with that node_id; the tool returns the instructions for that task. You may then reply to the user or call another node as needed."
    return prompt


def _build_sop_system_prompt(prose: str, mermaid: str, graph_id: str) -> str:
    """Build system prompt from SOP prose and full mermaid flowchart from AGENTS.md."""
    prompt = prose.strip()
    prompt += "\n\n## SOP Flowchart\n\nUse the tools load_graph (already called), goto_node, and todo to follow the flow.\n\n```mermaid\n" + (mermaid or "flowchart TD\n  START") + "\n```"
    prompt += f"\n\n**Important:** For every goto_node call use graph_id \"{graph_id}\" (same as load_graph). The todo tool only needs a list of todos (each with content and status: pending, in_progress, or completed)."
    return prompt


class MermaidAgent(BaseAgent):
    """
    Agent backed by a mermaid-agent folder: index.md (system prompt), agent-mermaid.md
    (diagram), and nodes/<node_id>/index.md (task-specific instructions).
    Uses progressive discovery: the agent can call enter_mermaid_node(node_id) to load
    instructions for a node on demand.

    When mcp_server_url is set and the agent dir has SOP markdown (e.g. retail-agent-sop.md),
    on first use the agent will:
    - Connect to the SOP MCP server (streamable HTTP)
    - Call load_graph with the mermaid source; the full mermaid from the SOP is included in the system prompt
    - Use the MCP server's tools (load_graph, goto_node, todo) as the agent's tool list
    """

    def __init__(
        self,
        name: str,
        config: AgentConfig,
        model: str,
        *,
        agent_name: str,
        mermaid_agents_root: Path | str | None = None,
        mcp_server_url: str | None = None,
        graph_id: str | None = None,
    ) -> None:
        super().__init__(name=name, config=config, model=model)
        root = Path(mermaid_agents_root) if mermaid_agents_root else _DEFAULT_MERMAID_AGENTS_ROOT
        self._agent_dir = get_mermaid_agent_dir(root, agent_name)
        self._mcp_server_url = (mcp_server_url or "").strip() or None
        self._graph_id = graph_id or "retail_customer_support"
        self._sop_data = load_sop_markdown(self._agent_dir)

        use_sop_mcp = bool(self._mcp_server_url and self._sop_data)
        if use_sop_mcp:
            self._sop_mcp_initialized = False
            self._mcp_http_context = None
            self._mcp_session_context = None
            self._mcp_session = None
            self._sop_tools: list[dict[str, Any]] = []
            self._mcp_tool_names: set[str] = set()
            self._system_prompt = ""
        else:
            self._nodes_list = list_mermaid_nodes(self._agent_dir)
            self._system_prompt = _build_system_prompt(self._agent_dir, self._nodes_list)

        native_list, native_executor = load_native_tools(self._agent_dir)
        self._native_tools: list[dict[str, Any]] = native_list
        self._native_tool_names: set[str] = {t["function"]["name"] for t in native_list} if native_list else set()
        self._native_executor: Callable[[str, dict], str] | None = native_executor

        self.history: list[dict[str, Any]] = []

    async def _ensure_sop_mcp_initialized(self) -> None:
        """Connect to MCP server, call load_graph, and set tools + system prompt."""
        if getattr(self, "_sop_mcp_initialized", False):
            return
        assert self._mcp_server_url and self._sop_data
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = self._mcp_server_url.rstrip("/") + "/mcp" if "/mcp" not in self._mcp_server_url else self._mcp_server_url
        self._mcp_http_context = streamable_http_client(url)
        read_stream, write_stream, _ = await self._mcp_http_context.__aenter__()
        self._mcp_session_context = ClientSession(read_stream, write_stream)
        self._mcp_session = await self._mcp_session_context.__aenter__()
        await self._mcp_session.initialize()

        mermaid = self._sop_data["mermaid"]
        load_result = await self._mcp_session.call_tool(
            "load_graph",
            arguments={
                "graph_id": self._graph_id,
                "mermaid_source": mermaid,
            },
        )
        if getattr(load_result, "isError", False):
            content = getattr(load_result, "content", [])
            err_text = content[0].text if content else str(load_result)
            raise RuntimeError(f"load_graph failed: {err_text}")

        # Use the full mermaid from SOP (same as sent to load_graph) in the system prompt
        self._system_prompt = _build_sop_system_prompt(
            self._sop_data["prose"], self._sop_data["mermaid"], self._graph_id
        )

        tools_response = await self._mcp_session.list_tools()
        self._sop_tools = _mcp_tools_to_openai_format(tools_response)
        self._mcp_tool_names = {t["function"]["name"] for t in self._sop_tools}
        self._sop_mcp_initialized = True

    def _messages_for_api(self) -> list[dict[str, Any]]:
        """Build OpenAI-format messages (including tool calls) for the API."""
        out = []
        for m in self.history:
            role = m["role"]
            content = m.get("content") or ""
            if role == "user":
                out.append({"role": "user", "content": content})
            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": content}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"],
                            },
                        }
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "content": content,
                    "tool_call_id": m["tool_call_id"],
                })
        return out

    async def _handle_tool_call(self, fn_name: str, args: dict[str, Any]) -> str:
        """Execute one tool call: MCP tools go to the server; enter_mermaid_node stays local."""
        if getattr(self, "_mcp_tool_names", None) and fn_name in self._mcp_tool_names:
            mcp_args = dict(args)
            if fn_name == "load_graph" and "mermaid_source" in mcp_args:
                mcp_args["graph_id"] = mcp_args.get("graph_id") or self._graph_id
            if fn_name == "goto_node":
                mcp_args["graph_id"] = mcp_args.get("graph_id") or self._graph_id
            result = await self._mcp_session.call_tool(fn_name, arguments=mcp_args)
            if getattr(result, "isError", False):
                parts = []
                for c in getattr(result, "content", []) or []:
                    if hasattr(c, "text"):
                        parts.append(c.text)
                return json.dumps({"error": " ".join(parts) or "Unknown error"})
            out = []
            for c in getattr(result, "content", []) or []:
                if hasattr(c, "text"):
                    out.append(c.text)
            if hasattr(result, "structuredContent") and result.structuredContent:
                return json.dumps(result.structuredContent)
            return "\n".join(out) if out else "{}"
        if fn_name == "enter_mermaid_node":
            return get_node_instructions(self._agent_dir, args.get("node_id", ""))
        if getattr(self, "_native_executor", None) and fn_name in getattr(self, "_native_tool_names", set()):
            return self._native_executor(fn_name, args)
        return f"[Unknown tool: {fn_name}]"

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Run a turn: optionally handle tool calls (MCP or enter_mermaid_node), then return final reply."""
        self.history.append({"role": "user", "content": incoming})

        if self._mcp_server_url and self._sop_data:
            await self._ensure_sop_mcp_initialized()
            # Exclude load_graph from tools sent to the LLM — we already call it once in _ensure_sop_mcp_initialized
            tools = [
                t for t in self._sop_tools
                if (t.get("function") or {}).get("name") != "load_graph"
            ]
        else:
            tools = [ENTER_NODE_TOOL]
        tools = tools + getattr(self, "_native_tools", [])

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            *self._messages_for_api(),
        ]
        total_cost = 0.0
        total_usage: dict[str, int] = {}
        max_tool_rounds = 20
        final_text = ""

        for _ in range(max_tool_rounds):
            response = await litellm.acompletion(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                reasoning_effort=getattr(self.config, "reasoning_effort", "low"),
                drop_params=True,
            )

            choice = response.choices[0] if response.choices else None
            if not choice:
                break
            msg = choice.message
            content = (getattr(msg, "content", None) or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []

            usage = usage_from_openai_response(getattr(response, "usage", None))
            if usage:
                total_usage = {k: total_usage.get(k, 0) + usage.get(k, 0) for k in set(total_usage) | set(usage)}
            total_cost += compute_cost(_cost_model(self.model), usage) if usage else 0.0

            assistant_record: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant_record["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": getattr(tc.function, "name", ""),
                        "arguments": json.loads(getattr(tc.function, "arguments", "{}") or "{}"),
                    }
                    for tc in tool_calls
                ]
            self.history.append(assistant_record)

            if not tool_calls:
                final_text = content or ""
                break

            messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": getattr(tc.function, "name", ""),
                            "arguments": getattr(tc.function, "arguments", "{}") or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                tc_id = tc.id
                fn_name = getattr(tc.function, "name", "")
                args_raw = getattr(tc.function, "arguments", "{}") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
                if on_chunk is not None:
                    await on_chunk("tool_use", {"name": fn_name, "id": tc_id, "input": args})
                result = await self._handle_tool_call(fn_name, args)
                self.history.append({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": tc_id,
                })
                messages.append({"role": "tool", "content": result, "tool_call_id": tc_id})

        if on_chunk is not None:
            await on_chunk("text", final_text)

        return final_text, {"usage": total_usage, "cost": total_cost}
