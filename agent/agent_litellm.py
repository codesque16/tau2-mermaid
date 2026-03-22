"""LiteLLM agent: single client for OpenAI, Anthropic, Gemini, etc. via LiteLLM."""

from typing import Any, Callable, Awaitable, Dict, List
import json
import re

import litellm
import logfire

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_openai_response


def _raw_response_to_dict(response: Any) -> dict[str, Any]:
    """Convert LiteLLM response to a JSON-serializable dict for logging."""
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    out: Dict[str, Any] = {}
    for attr in ("id", "choices", "created", "model", "usage", "system_fingerprint"):
        if hasattr(response, attr):
            val = getattr(response, attr)
            if val is None:
                out[attr] = val
            elif hasattr(val, "model_dump"):
                out[attr] = val.model_dump()
            elif isinstance(val, list):
                out[attr] = [
                    c.model_dump() if hasattr(c, "model_dump") else c
                    for c in val
                ]
            else:
                out[attr] = val
    return out


def _cost_model(model: str) -> str:
    """Use last segment for cost lookup (e.g. gemini/gemini-2.5-flash -> gemini-2.5-flash)."""
    return model.split("/")[-1] if "/" in model else model


def _strip_tool_call_blocks_from_reasoning(text: str) -> str:
    """Remove entire <tool_call>...</tool_call> blocks from reasoning text (Qwen sometimes emits these as plain text)."""
    if not text or "<tool_call>" not in text:
        return text.strip()
    return re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL).strip()


class LiteLLMAgent(BaseAgent):
    """
    Agent using LiteLLM; supports any model (OpenAI, Anthropic, Gemini, etc.)
    via a single API. Set the corresponding API key in env (OPENAI_API_KEY,
    ANTHROPIC_API_KEY, GOOGLE_API_KEY / GEMINI_API_KEY, etc.).
    """

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self.history: list[dict[str, Any]] = []

    def _messages_for_api(self) -> list[dict[str, Any]]:
        """Build messages including any prior tool calls."""
        out: List[dict[str, Any]] = []
        for m in self.history:
            role = m["role"]
            content = m.get("content") or ""
            if role == "user":
                out.append({"role": "user", "content": content})
            elif role == "assistant":
                msg: Dict[str, Any] = {"role": "assistant", "content": content}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "content": content,
                        "tool_call_id": m["tool_call_id"],
                    }
                )
        return out

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Call LiteLLM; if MCP tools are configured, run a tool loop."""
        print(f"  Chat Completion with litellm:{self.model}")
        self.history.append({"role": "user", "content": incoming})

        mcps = getattr(self.config, "mcps", None) or []
        mermaid = getattr(self.config, "mermaid", None) or []
        if not mcps and not mermaid:
            # Simple non-tool path
            messages = [
                {"role": "system", "content": self.get_effective_system_prompt()},
                *self._messages_for_api(),
            ]

            completion_kw: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "reasoning_effort": getattr(self.config, "reasoning_effort", None),
                "seed": getattr(self.config, "seed", None),
            }
            if self.config.max_tokens is not None:
                completion_kw["max_tokens"] = self.config.max_tokens
            response = await litellm.acompletion(**completion_kw)
            logfire.info(
                "litellm_raw_response",
                model=self.model,
                raw_response=_raw_response_to_dict(response),
            )

            content = ""
            if response.choices:
                msg = response.choices[0].message
                if hasattr(msg, "content") and msg.content:
                    content = msg.content
            full_text = content or ""
            self.history.append({"role": "assistant", "content": full_text})

            if on_chunk is not None:
                await on_chunk("text", full_text)

            usage_dict = usage_from_openai_response(response.usage)
            cost = (
                compute_cost(_cost_model(self.model), usage_dict)
                if usage_dict
                else 0.0
            )
            return full_text, {"usage": usage_dict, "cost": cost}

        # MCP tools path (mermaid load_graph may have set _mermaid_system_prompt; we use it + ticket)
        await self._ensure_mcp_initialized()
        tools = self._get_mcp_tools_for_llm()
        self.log_llm_tools_in_request(tools, provider="litellm", model=self.model)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.get_effective_system_prompt()},
            *self._messages_for_api(),
        ]
        total_cost = 0.0
        total_usage: dict[str, int] = {}
        max_tool_rounds = 20
        final_text = ""

        for _ in range(max_tool_rounds):
            tool_completion_kw: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": self.config.temperature,
                "reasoning_effort": getattr(self.config, "reasoning_effort", None),
                "drop_params": True,
                "seed": getattr(self.config, "seed", None),
            }
            if self.config.max_tokens is not None:
                tool_completion_kw["max_tokens"] = self.config.max_tokens
            response = await litellm.acompletion(**tool_completion_kw)
            logfire.info(
                "litellm_raw_response",
                model=self.model,
                raw_response=_raw_response_to_dict(response),
            )

            choice = response.choices[0] if response.choices else None
            if not choice:
                break
            msg = choice.message
            content = (getattr(msg, "content", None) or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []

            usage = usage_from_openai_response(getattr(response, "usage", None))
            if usage:
                total_usage = {
                    k: total_usage.get(k, 0) + usage.get(k, 0)
                    for k in set(total_usage) | set(usage)
                }
            total_cost += (
                compute_cost(_cost_model(self.model), usage) if usage else 0.0
            )

            reasoning = (
                getattr(msg, "reasoning_content", None)
                or getattr(msg, "reasoning", None)
                or getattr(msg, "thought", None)
            )
            # Reasoning-only response (no content, no tool_calls): append as assistant message and continue until we get content with no tool_calls (same as OpenRouter).
            if not content and not tool_calls and isinstance(reasoning, str) and reasoning.strip():
                reasoning_text = _strip_tool_call_blocks_from_reasoning(reasoning)
                content = reasoning_text or reasoning.strip()
                assistant_record = {"role": "assistant", "content": content}
                self.history.append(assistant_record)
                messages.append({"role": "assistant", "content": content})
                continue

            assistant_record = {
                "role": "assistant",
                "content": content or "",
            }
            if tool_calls:
                assistant_record["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": getattr(tc.function, "name", ""),
                        "arguments": json.loads(
                            getattr(tc.function, "arguments", "{}") or "{}"
                        ),
                    }
                    for tc in tool_calls
                ]
            self.history.append(assistant_record)

            # Stop only when we have content and no tool_calls (first such response).
            if not tool_calls:
                final_text = content or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": getattr(tc.function, "name", ""),
                                "arguments": getattr(tc.function, "arguments", "{}")
                                or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                tc_id = tc.id
                fn_name = getattr(tc.function, "name", "")
                args_raw = getattr(tc.function, "arguments", "{}") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}

                if on_chunk is not None:
                    await on_chunk(
                        "tool_use",
                        {"name": fn_name, "id": tc_id, "input": args},
                    )

                result = await self._call_mcp_tool(fn_name, args)
                self.history.append(
                    {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tc_id,
                    }
                )
                messages.append(
                    {"role": "tool", "content": result, "tool_call_id": tc_id}
                )

        if on_chunk is not None:
            await on_chunk("text", final_text)

        return final_text, {"usage": total_usage, "cost": total_cost}
