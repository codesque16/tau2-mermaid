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
)

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


class MermaidAgent(BaseAgent):
    """
    Agent backed by a mermaid-agent folder: index.md (system prompt), agent-mermaid.md
    (diagram), and nodes/<node_id>/index.md (task-specific instructions).
    Uses progressive discovery: the agent can call enter_mermaid_node(node_id) to load
    instructions for a node on demand instead of having everything upfront.
    """

    def __init__(
        self,
        name: str,
        config: AgentConfig,
        model: str,
        *,
        agent_name: str,
        mermaid_agents_root: Path | str | None = None,
    ) -> None:
        super().__init__(name=name, config=config, model=model)
        root = Path(mermaid_agents_root) if mermaid_agents_root else _DEFAULT_MERMAID_AGENTS_ROOT
        self._agent_dir = get_mermaid_agent_dir(root, agent_name)
        self._nodes_list = list_mermaid_nodes(self._agent_dir)
        self._system_prompt = _build_system_prompt(self._agent_dir, self._nodes_list)
        self.history: list[dict[str, Any]] = []

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
                    "content": m["content"],
                    "tool_call_id": m["tool_call_id"],
                })
        return out

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Run a turn: optionally handle enter_mermaid_node tool calls, then return final reply."""
        self.history.append({"role": "user", "content": incoming})

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            *self._messages_for_api(),
        ]
        tools = [ENTER_NODE_TOOL]
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
            )

            choice = response.choices[0] if response.choices else None
            if not choice:
                break
            msg = choice.message
            content = (getattr(msg, "content", None) or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []

            # Accumulate usage/cost
            usage = usage_from_openai_response(getattr(response, "usage", None))
            if usage:
                total_usage = {k: total_usage.get(k, 0) + usage.get(k, 0) for k in set(total_usage) | set(usage)}
            total_cost += compute_cost(_cost_model(self.model), usage) if usage else 0.0

            # Record assistant message (with optional tool_calls)
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

            # Append assistant message (with tool_calls) and tool results to messages
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
                node_id = args.get("node_id", "")
                if fn_name == "enter_mermaid_node":
                    result = get_node_instructions(self._agent_dir, node_id)
                else:
                    result = f"[Unknown tool: {fn_name}]"
                self.history.append({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": tc_id,
                })
                messages.append({"role": "tool", "content": result, "tool_call_id": tc_id})

        if on_chunk is not None:
            await on_chunk("text", final_text)

        usage_info = {"usage": total_usage, "cost": total_cost}
        return final_text, usage_info
