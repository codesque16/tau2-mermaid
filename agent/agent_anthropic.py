"""Anthropic (Claude) agent: MCP tool loop, Logfire spans, extended thinking, history parity with Gemini/OpenAI."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable, Dict, List

from agent.api_key_rotation import (
    get_anthropic_api_key,
    mask_secret,
    maybe_rotate_after_provider_error,
)

import anthropic

from .base import BaseAgent
from .config import AgentConfig
from .logfire_native_llm import async_anthropic_messages_with_logfire
from .utils.cost import compute_cost, usage_from_response

DEFAULT_REQUEST_TIMEOUT = 300.0
ANTHROPIC_DEFAULT_MAX_TOKENS = 4096


def _openai_tools_to_anthropic(openai_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in openai_tools:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        name = (fn.get("name") or "").strip()
        if not name:
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append(
            {
                "name": name,
                "description": "",
                "input_schema": schema,
            }
        )
    return out


def _thinking_budget_from_effort(effort: str | None, max_tokens: int) -> int | None:
    """Map ``reasoning_effort`` to extended-thinking ``budget_tokens`` (must be < max_tokens, >= 1024)."""
    if effort is None or not str(effort).strip():
        return None
    cap = max(1025, max_tokens - 1)
    raw_map = {
        "minimal": 1024,
        "low": 2048,
        "medium": 6000,
        "high": 12000,
    }
    raw = raw_map.get(str(effort).strip().lower(), 2048)
    return min(max(raw, 1024), cap)


def _block_to_thinking_dict(block: Any) -> dict[str, Any] | None:
    t = getattr(block, "type", None)
    if t == "thinking":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", "") or "",
            "signature": getattr(block, "signature", "") or "",
        }
    return None


def _parse_anthropic_assistant_message(
    message: Any,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str]:
    """Returns (visible_text, tool_calls, thinking_blocks, reasoning_text_for_ui)."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    thinking_blocks: list[dict[str, Any]] = []
    reasoning_chunks: list[str] = []

    for block in getattr(message, "content", None) or []:
        bt = getattr(block, "type", None)
        if bt == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif bt == "tool_use":
            inp = getattr(block, "input", None)
            if not isinstance(inp, dict):
                inp = {}
            tool_calls.append(
                {
                    "id": getattr(block, "id", "") or "",
                    "name": getattr(block, "name", "") or "",
                    "arguments": dict(inp),
                }
            )
        elif bt == "thinking":
            td = _block_to_thinking_dict(block)
            if td:
                thinking_blocks.append(td)
                th = td.get("thinking") or ""
                if isinstance(th, str) and th.strip():
                    reasoning_chunks.append(th.strip())

    visible = "".join(text_parts).strip()
    reasoning = "\n\n".join(reasoning_chunks) if reasoning_chunks else ""
    return visible, tool_calls, thinking_blocks, reasoning


class AnthropicAgent(BaseAgent):
    """
    Wraps the Anthropic Messages API. Behavioral differences come from ``AgentConfig``
    (system prompt, thinking / tools).
    """

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self._anthropic_key: str | None = None
        self.client: anthropic.AsyncAnthropic | None = None
        self._sync_anthropic_client()
        self.history: list[dict[str, Any]] = []

    def _sync_anthropic_client(self) -> None:
        api_key = get_anthropic_api_key("assistant")
        if not api_key:
            raise ValueError("Set ANTHROPIC_API_KEY for Anthropic models.")
        if self._anthropic_key != api_key:
            self._anthropic_key = api_key
            self.client = anthropic.AsyncAnthropic(
                api_key=api_key, timeout=DEFAULT_REQUEST_TIMEOUT
            )

    def _history_to_anthropic_api_messages(self) -> list[dict[str, Any]]:
        api: list[dict[str, Any]] = []
        i = 0
        hist = self.history
        while i < len(hist):
            m = hist[i]
            role = m.get("role")
            if role == "user":
                api.append({"role": "user", "content": m.get("content") or ""})
                i += 1
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                for tb in m.get("anthropic_thinking_blocks") or []:
                    if isinstance(tb, dict) and tb.get("type") == "thinking":
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": tb.get("thinking") or "",
                                "signature": tb.get("signature") or "",
                            }
                        )
                text = (m.get("content") or "").strip()
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in m.get("tool_calls") or []:
                    args = tc.get("arguments")
                    if not isinstance(args, dict):
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": args,
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                api.append({"role": "assistant", "content": blocks})
                i += 1
            elif role == "tool":
                tr_blocks: list[dict[str, Any]] = []
                while i < len(hist) and hist[i].get("role") == "tool":
                    t = hist[i]
                    tr_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": t.get("tool_call_id") or "",
                            "content": t.get("content") or "",
                        }
                    )
                    i += 1
                api.append({"role": "user", "content": tr_blocks})
            else:
                i += 1
        return api

    def _resolve_max_tokens(self, thinking_budget: int | None) -> int:
        base = (
            self.config.max_tokens
            if self.config.max_tokens is not None
            else ANTHROPIC_DEFAULT_MAX_TOKENS
        )
        if thinking_budget is None:
            return base
        need = thinking_budget + 2048
        return max(base, need)

    async def _messages_create_logged(
        self,
        *,
        api_messages: list[dict[str, Any]],
        anthropic_tools: list[dict[str, Any]] | None,
        max_tokens: int,
        thinking: dict[str, Any] | None,
    ) -> Any:
        system_text = self.get_effective_system_prompt()
        kw: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_text,
            "messages": api_messages,
        }
        if thinking is not None:
            kw["thinking"] = thinking
        else:
            kw["temperature"] = self.config.temperature
        if anthropic_tools:
            kw["tools"] = anthropic_tools

        extras = {
            "tools": anthropic_tools,
            "thinking": thinking,
            "temperature": kw.get("temperature"),
        }

        max_attempts = 6
        last_err: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                self._sync_anthropic_client()
                assert self.client is not None

                async def _call() -> Any:
                    return await self.client.messages.create(**kw)

                ak = mask_secret(self._anthropic_key or "")
                return await async_anthropic_messages_with_logfire(
                    agent_name=self.name,
                    model=self.model,
                    system_text=system_text,
                    api_messages=list(api_messages),
                    request_extras=extras,
                    create_coro=_call,
                    api_key_masked=ak if ak else None,
                )
            except Exception as e:
                last_err = e
                inv = lambda: setattr(self, "_anthropic_key", None)
                rotated = maybe_rotate_after_provider_error(
                    "anthropic", e, invalidate_client=inv
                )
                if rotated or attempt < max_attempts - 1:
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(min(2**attempt, 8))
                        continue
                raise
        raise last_err  # type: ignore[misc]

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        await self._ensure_mcp_initialized()
        mcps = getattr(self.config, "mcps", None) or []
        mermaid = getattr(self.config, "mermaid", None) or []
        self.history.append({"role": "user", "content": incoming})

        reff = getattr(self.config, "reasoning_effort", None)
        max_tokens = self._resolve_max_tokens(None)
        thinking_budget = _thinking_budget_from_effort(reff, max_tokens)
        max_tokens = self._resolve_max_tokens(thinking_budget)
        thinking_budget = _thinking_budget_from_effort(reff, max_tokens)
        thinking_kw: dict[str, Any] | None = None
        if thinking_budget is not None:
            thinking_kw = {"type": "enabled", "budget_tokens": thinking_budget}

        if not mcps and not mermaid:
            api_messages = self._history_to_anthropic_api_messages()
            message = await self._messages_create_logged(
                api_messages=api_messages,
                anthropic_tools=None,
                max_tokens=max_tokens,
                thinking=thinking_kw,
            )
            visible, _, thinking_blocks, reasoning = _parse_anthropic_assistant_message(
                message
            )
            record: dict[str, Any] = {"role": "assistant", "content": visible}
            if reasoning:
                record["reasoning_content"] = reasoning
            if thinking_blocks:
                record["anthropic_thinking_blocks"] = thinking_blocks
            self.history.append(record)

            if on_chunk is not None:
                if reasoning:
                    await on_chunk("reasoning", reasoning)
                await on_chunk("text", visible)

            usage = usage_from_response(getattr(message, "usage", None))
            cost = compute_cost(self.model, usage) if usage else 0.0
            return visible, {"usage": usage, "cost": cost}

        openai_tools = self._get_mcp_tools_for_llm()
        self.log_llm_tools_in_request(
            openai_tools, provider="anthropic", model=self.model
        )
        anthropic_tools = _openai_tools_to_anthropic(openai_tools)

        total_cost = 0.0
        total_usage: dict[str, int] = {}
        max_tool_rounds = 20
        final_text = ""

        for _ in range(max_tool_rounds):
            api_messages = self._history_to_anthropic_api_messages()
            message = await self._messages_create_logged(
                api_messages=api_messages,
                anthropic_tools=anthropic_tools,
                max_tokens=max_tokens,
                thinking=thinking_kw,
            )
            visible, tool_calls, thinking_blocks, reasoning = (
                _parse_anthropic_assistant_message(message)
            )

            usage = usage_from_response(getattr(message, "usage", None))
            if usage:
                total_usage = {
                    k: int(total_usage.get(k, 0)) + int(usage.get(k, 0))
                    for k in set(total_usage) | set(usage)
                }
            total_cost += compute_cost(self.model, usage) if usage else 0.0

            assistant_record: dict[str, Any] = {
                "role": "assistant",
                "content": visible or "",
            }
            if reasoning:
                assistant_record["reasoning_content"] = reasoning
            if thinking_blocks:
                assistant_record["anthropic_thinking_blocks"] = thinking_blocks
            if tool_calls:
                assistant_record["tool_calls"] = tool_calls
            self.history.append(assistant_record)

            if reasoning and on_chunk is not None:
                await on_chunk("reasoning", reasoning)

            if not tool_calls:
                final_text = visible or ""
                if on_chunk is not None:
                    await on_chunk("text", final_text)
                return final_text, {"usage": total_usage, "cost": total_cost}

            for tc in tool_calls:
                if on_chunk is not None:
                    await on_chunk(
                        "tool_use",
                        {
                            "name": tc["name"],
                            "id": tc["id"],
                            "input": tc["arguments"],
                        },
                    )
                result = await self._call_mcp_tool(tc["name"], tc["arguments"])
                self.history.append(
                    {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                    }
                )

        if on_chunk is not None and final_text:
            await on_chunk("text", final_text)
        return final_text, {"usage": total_usage, "cost": total_cost}
