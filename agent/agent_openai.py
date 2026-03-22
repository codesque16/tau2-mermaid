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
from .logfire_native_llm import async_openai_chat_with_logfire
from .utils.cost import compute_cost, usage_from_openai_response

DEFAULT_REQUEST_TIMEOUT = 300.0


def _reasoning_from_openai_message(msg: Any) -> str | None:
    v = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
    if isinstance(v, str) and v.strip():
        return v.strip()
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
        kw: dict[str, Any] = {
            "model": self.model,
            "temperature": self.config.temperature,
        }
        if self.config.max_tokens is not None:
            kw["max_tokens"] = self.config.max_tokens
        reff = getattr(self.config, "reasoning_effort", None)
        if reff is not None and str(reff).strip():
            kw["reasoning_effort"] = str(reff).strip()
        if getattr(self.config, "seed", None) is not None:
            kw["seed"] = self.config.seed
        return kw

    async def _chat_create_logged(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        kw = self._base_completion_kw()
        kw["messages"] = messages
        if tools is not None:
            kw["tools"] = tools
        if tool_choice is not None:
            kw["tool_choice"] = tool_choice
        extras = {k: v for k, v in kw.items() if k not in ("model", "messages")}

        max_attempts = 6
        last_err: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                self._sync_openai_client()
                assert self.client is not None

                async def _call() -> Any:
                    return await self.client.chat.completions.create(**kw)

                ak = mask_secret(self._openai_key or "")
                return await async_openai_chat_with_logfire(
                    agent_name=self.name,
                    model=self.model,
                    request_messages=list(messages),
                    request_extras=extras,
                    create_coro=_call,
                    api_key_masked=ak if ak else None,
                )
            except Exception as e:
                last_err = e
                inv = lambda: setattr(self, "_openai_key", None)
                rotated = maybe_rotate_after_provider_error(
                    "openai", e, invalidate_client=inv
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
        """Chat completion with optional MCP tools; Logfire + reasoning_content in history."""
        await self._ensure_mcp_initialized()
        mcps = getattr(self.config, "mcps", None) or []
        mermaid = getattr(self.config, "mermaid", None) or []
        self.history.append({"role": "user", "content": incoming})

        if not mcps and not mermaid:
            messages = [
                {"role": "system", "content": self.get_effective_system_prompt()},
                *self._messages_for_api(),
            ]
            response = await self._chat_create_logged(messages=messages)
            msg = response.choices[0].message if response.choices else None
            full_text = (getattr(msg, "content", None) or "").strip() if msg else ""
            reasoning = _reasoning_from_openai_message(msg) if msg else None
            record: dict[str, Any] = {"role": "assistant", "content": full_text}
            if reasoning:
                record["reasoning_content"] = reasoning
            self.history.append(record)
            if on_chunk is not None:
                if reasoning:
                    await on_chunk("reasoning", reasoning)
                await on_chunk("text", full_text)
            usage_dict = usage_from_openai_response(response.usage)
            cost = compute_cost(self.model, usage_dict) if usage_dict else 0.0
            return full_text, {"usage": usage_dict, "cost": cost}

        tools = self._get_mcp_tools_for_llm()
        self.log_llm_tools_in_request(tools, provider="openai", model=self.model)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.get_effective_system_prompt()},
            *self._messages_for_api(),
        ]
        total_cost = 0.0
        total_usage: dict[str, int] = {}
        max_tool_rounds = 20
        final_text = ""

        for _ in range(max_tool_rounds):
            response = await self._chat_create_logged(
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            choice = response.choices[0] if response.choices else None
            if not choice:
                break
            msg = choice.message
            content = (getattr(msg, "content", None) or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []
            reasoning = _reasoning_from_openai_message(msg)

            usage = usage_from_openai_response(getattr(response, "usage", None))
            if usage:
                total_usage = {
                    k: int(total_usage.get(k, 0)) + int(usage.get(k, 0))
                    for k in set(total_usage) | set(usage)
                }
            total_cost += compute_cost(self.model, usage) if usage else 0.0

            assistant_record: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if reasoning:
                assistant_record["reasoning_content"] = reasoning
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

            if reasoning and on_chunk is not None:
                await on_chunk("reasoning", reasoning)

            if not tool_calls:
                final_text = content or ""
                if on_chunk is not None:
                    await on_chunk("text", final_text)
                return final_text, {"usage": total_usage, "cost": total_cost}

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

        if on_chunk is not None and final_text:
            await on_chunk("text", final_text)
        return final_text, {"usage": total_usage, "cost": total_cost}
