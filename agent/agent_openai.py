"""OpenAI (GPT) agent: MCP tool loop, Logfire spans, reasoning_effort, GEPA-friendly history."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable, Dict, List

from agent.api_key_rotation import (
    get_openai_api_key,
    mask_secret,
    maybe_rotate_after_provider_error,
)

from openai import AsyncOpenAI

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_openai_response
from agent.logfire_native_llm import async_openai_responses_with_logfire

DEFAULT_REQUEST_TIMEOUT = 300.0


def _openai_omit_temperature(model: str) -> bool:
    """GPT-5 family rejects custom ``temperature`` (e.g. 0); omit the param so the API uses its default."""
    return (model or "").strip().lower().startswith("gpt-5")


def _openai_is_gpt5(model: str) -> bool:
    return (model or "").strip().lower().startswith("gpt-5")


def _reasoning_from_openai_message(msg: Any) -> str | None:
    v = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
    if isinstance(v, str) and v.strip():
        return v.strip()
    # GPT-5 can return structured reasoning summaries.
    if isinstance(v, dict):
        summ = v.get("summary")
        if isinstance(summ, str) and summ.strip():
            return summ.strip()
        if isinstance(summ, list):
            parts: list[str] = []
            for item in summ:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    t = item.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            if parts:
                return "\n".join(parts)
    if isinstance(v, list):
        parts2: list[str] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                parts2.append(item.strip())
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str) and t.strip():
                    parts2.append(t.strip())
        if parts2:
            return "\n".join(parts2)
    return None


class OpenAIAgent(BaseAgent):
    """OpenAI chat completions agent. Set OPENAI_API_KEY in env or .env."""

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self._openai_key: str | None = None
        self.client: AsyncOpenAI | None = None
        self._sync_openai_client()
        self.history: list[dict[str, Any]] = []

    def _sync_openai_client(self) -> None:
        api_key = get_openai_api_key()
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY for OpenAI models.")
        if self._openai_key != api_key:
            self._openai_key = api_key
            self.client = AsyncOpenAI(api_key=api_key, timeout=DEFAULT_REQUEST_TIMEOUT)

    def _messages_for_api(self) -> list[dict[str, Any]]:
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
                                "arguments": json.dumps(tc["arguments"])
                                if isinstance(tc.get("arguments"), dict)
                                else (tc.get("arguments") or "{}"),
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

    def _base_completion_kw(self) -> dict[str, Any]:
        # Responses API naming differs from chat.completions.
        kw: dict[str, Any] = {"model": self.model}
        if not _openai_omit_temperature(self.model):
            kw["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            kw["max_output_tokens"] = self.config.max_tokens
        reff = getattr(self.config, "reasoning_effort", None)
        if reff is not None and str(reff).strip() and _openai_is_gpt5(self.model):
            # GPT-5 reasoning summary for trace/debug.
            kw["reasoning"] = {
                "effort": str(reff).strip(),
                "summary": "detailed",
            }
        return kw

    def _tools_for_responses(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert our OpenAI-format MCP tools into Responses API tool definitions."""
        out: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function":
                fn = t.get("function") or {}
                if not isinstance(fn, dict):
                    fn = {}
                out.append(
                    {
                        "type": "function",
                        "name": fn.get("name") or "",
                        "description": fn.get("description") or "",
                        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    }
                )
            else:
                # Only `function` tools are emitted by MCP conversion; keep fallback for safety.
                out.append(t)
        return out

    def _extract_responses_message_text(self, resp: Any) -> str:
        items = getattr(resp, "output", None) or []
        for it in items:
            if getattr(it, "type", None) != "message":
                continue
            for c in getattr(it, "content", None) or []:
                if getattr(c, "type", None) == "output_text":
                    return getattr(c, "text", "") or ""
        return getattr(resp, "output_text", None) or ""

    def _extract_responses_reasoning_text(self, resp: Any) -> str | None:
        items = getattr(resp, "output", None) or []
        for it in items:
            if getattr(it, "type", None) != "reasoning":
                continue
            summaries = getattr(it, "summary", None) or []
            parts: list[str] = []
            for s in summaries:
                # In the SDK, summary items have `type="summary_text"` and `text`.
                txt = getattr(s, "text", None)
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
            if parts:
                return "\n\n".join(parts)
        return None

    def _extract_responses_function_calls(self, resp: Any) -> list[dict[str, Any]]:
        items = getattr(resp, "output", None) or []
        out: list[dict[str, Any]] = []
        for it in items:
            if getattr(it, "type", None) != "function_call":
                continue
            name = getattr(it, "name", None) or ""
            args_raw = getattr(it, "arguments", None) or "{}"
            call_id = getattr(it, "call_id", None) or getattr(it, "id", None) or ""
            args: dict[str, Any]
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}
            out.append({"id": call_id, "name": name, "arguments": args})
        return out

    async def _chat_create_logged(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        raise RuntimeError(
            "OpenAIAgent now uses the Responses API only; chat.completions.create is disabled."
        )

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """OpenAI Responses API with optional MCP tool loop."""

        await self._ensure_mcp_initialized()
        mcps = getattr(self.config, "mcps", None) or []
        mermaid = getattr(self.config, "mermaid", None) or []
        self.history.append({"role": "user", "content": incoming})

        base_kw = self._base_completion_kw()
        instructions = self.get_effective_system_prompt()

        # ── Simple (no tools) ────────────────────────────────────────────
        if not mcps and not mermaid:
            self._sync_openai_client()
            assert self.client is not None

            ak = mask_secret(self._openai_key or "")
            call_kwargs: dict[str, Any] = {
                **base_kw,
                "instructions": instructions,
                "input": incoming,
                "tools": [],
            }
            resp = await async_openai_responses_with_logfire(
                agent_name=self.name,
                model=self.model,
                request_kwargs=call_kwargs,
                create_coro=lambda: self.client.responses.create(**call_kwargs),  # type: ignore[func-returns-value]
                api_key_masked=ak if ak else None,
                io_phase="openai_responses",
                internal_messages=[{"role": "system", "content": instructions}, *self.history],
            )

            full_text = self._extract_responses_message_text(resp).strip()
            reasoning = self._extract_responses_reasoning_text(resp)
            record: dict[str, Any] = {"role": "assistant", "content": full_text}
            if reasoning:
                record["reasoning_content"] = reasoning
            self.history.append(record)

            if on_chunk is not None:
                if reasoning:
                    await on_chunk("reasoning", reasoning)
                await on_chunk("text", full_text)

            usage_dict = usage_from_openai_response(getattr(resp, "usage", None))
            cost = compute_cost(self.model, usage_dict) if usage_dict else 0.0
            return full_text, {"usage": usage_dict, "cost": cost}

        # ── Tool loop ────────────────────────────────────────────────────
        tools = self._get_mcp_tools_for_llm()
        self.log_llm_tools_in_request(tools, provider="openai", model=self.model)
        responses_tools = self._tools_for_responses(tools)

        total_cost = 0.0
        total_usage: dict[str, int] = {}
        max_tool_rounds = 20
        final_text = ""

        prev_response_id: str | None = None
        tool_outputs_items: list[dict[str, Any]] | None = None

        for _ in range(max_tool_rounds):
            self._sync_openai_client()
            assert self.client is not None

            ak = mask_secret(self._openai_key or "")
            call_kwargs = {
                **base_kw,
                "instructions": instructions,
                "tools": responses_tools,
                "tool_choice": "auto",
            }
            if prev_response_id:
                call_kwargs["previous_response_id"] = prev_response_id
                call_kwargs["input"] = tool_outputs_items or []
            else:
                call_kwargs["input"] = incoming

            resp = await async_openai_responses_with_logfire(
                agent_name=self.name,
                model=self.model,
                request_kwargs=call_kwargs,
                create_coro=lambda: self.client.responses.create(**call_kwargs),  # type: ignore[func-returns-value]
                api_key_masked=ak if ak else None,
                io_phase="openai_responses",
                internal_messages=[{"role": "system", "content": instructions}, *self.history],
            )

            # Usage / cost: accumulate across tool iterations.
            usage = usage_from_openai_response(getattr(resp, "usage", None))
            if usage:
                total_usage = {
                    k: int(total_usage.get(k, 0)) + int(usage.get(k, 0))
                    for k in set(total_usage) | set(usage)
                }
            total_cost += compute_cost(self.model, usage) if usage else 0.0

            tool_calls = self._extract_responses_function_calls(resp)
            reasoning = self._extract_responses_reasoning_text(resp)
            content = self._extract_responses_message_text(resp).strip()

            assistant_record: dict[str, Any] = {
                "role": "assistant",
                "content": content or "",
            }
            if reasoning:
                assistant_record["reasoning_content"] = reasoning
            if tool_calls:
                assistant_record["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    }
                    for tc in tool_calls
                ]
            self.history.append(assistant_record)

            if reasoning and on_chunk is not None:
                await on_chunk("reasoning", reasoning)

            if not tool_calls:
                final_text = content or ""
                if on_chunk is not None:
                    await on_chunk("text", final_text)
                return final_text, {"usage": total_usage, "cost": total_cost}

            # Execute all tool calls and send their outputs back.
            tool_outputs_items = []
            for tc in tool_calls:
                tc_id = tc.get("id") or ""
                fn_name = tc.get("name") or ""
                args = tc.get("arguments") or {}
                if on_chunk is not None:
                    await on_chunk("tool_use", {"name": fn_name, "id": tc_id, "input": args})

                result = await self._call_mcp_tool(fn_name, args)
                self.history.append(
                    {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tc_id,
                        # Needed for Logfire Model Run to match tool outputs to
                        # the corresponding tool_call (Gemini integration includes this).
                        "name": fn_name,
                    }
                )
                tool_outputs_items.append(
                    {"type": "function_call_output", "call_id": tc_id, "output": result}
                )

            prev_response_id = getattr(resp, "id", None) or getattr(resp, "response_id", None)

        if on_chunk is not None and final_text:
            await on_chunk("text", final_text)
        return final_text, {"usage": total_usage, "cost": total_cost}
