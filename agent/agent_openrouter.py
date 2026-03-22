"""OpenRouter agent: calls OpenRouter API directly (no LiteLLM)."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Awaitable, Dict, List

import logfire

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_openai_response

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _cost_model(model: str) -> str:
    """Use last segment for cost lookup (e.g. qwen/qwen3.5-9b -> qwen3.5-9b)."""
    return model.split("/")[-1] if "/" in model else model


def _strip_tool_call_blocks_from_reasoning(text: str) -> str:
    """Remove entire <tool_call>...</tool_call> blocks from reasoning text (Qwen sometimes emits these as plain text)."""
    if not text or "<tool_call>" not in text:
        return text.strip()
    return re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL).strip()


def _message_from_choice(choice: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenRouter choice.message to a dict we can use like LiteLLM message."""
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    if content is None:
        content = ""
    content = (content or "").strip()
    tool_calls_raw = msg.get("tool_calls") or []
    tool_calls: List[Dict[str, Any]] = []
    for tc in tool_calls_raw:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            tool_calls.append({
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "{}",
            })
    return {
        "content": content,
        "tool_calls": tool_calls,
        "reasoning_content": msg.get("reasoning_content") or msg.get("reasoning") or msg.get("thought"),
    }


class OpenRouterAgent(BaseAgent):
    """
    Agent that calls the OpenRouter API directly (no LiteLLM).
    Set OPENROUTER_API_KEY in the environment.
    Optional: OPENROUTER_HTTP_REFERER, OPENROUTER_X_TITLE for rankings.
    """

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self.history: list[dict[str, Any]] = []
        self._client: Any = None

    def _get_headers(self) -> dict[str, str]:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ValueError("OPENROUTER_API_KEY environment variable is required for OpenRouterAgent")
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        ref = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
        if ref:
            headers["HTTP-Referer"] = ref
        title = os.environ.get("OPENROUTER_X_TITLE", "").strip()
        if title:
            headers["X-Title"] = title
        return headers

    def _messages_for_api(self) -> list[dict[str, Any]]:
        """Build messages including any prior tool calls (same as LiteLLM)."""
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
                                "arguments": json.dumps(tc["arguments"]) if isinstance(tc.get("arguments"), dict) else (tc.get("arguments") or "{}"),
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

    async def _post_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to OpenRouter and return JSON response."""
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers=self._get_headers(),
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def _post_completion_with_logfire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Call OpenRouter and record the request/response in a span with GenAI semantic attributes and conversation data for the Model Run tab."""
        model = payload.get("model") or self.model
        with logfire.span(
            "openrouter chat",
            model=model,
            **{
                "gen_ai.system": "openrouter",
                "gen_ai.request.model": model,
                "gen_ai.operation.name": "chat",
            },
        ) as span:
            data = await self._post_completion(payload)
            resp_model = data.get("model") or model
            span.set_attribute("gen_ai.response.model", resp_model)
            usage = data.get("usage") or {}
            inp_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            out_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            span.set_attribute("gen_ai.usage.input_tokens", inp_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
            choices = data.get("choices") or []
            finish_reasons = [c.get("finish_reason") for c in choices if isinstance(c, dict)]
            if finish_reasons:
                span.set_attribute("gen_ai.response.finish_reasons", finish_reasons)

            # Details tab "Arguments" and Model Run conversation view: request_data + response_data
            request_data: Dict[str, Any] = {"messages": payload.get("messages") or []}
            if "tools" in payload:
                request_data["tools"] = payload["tools"]
            span.set_attribute("request_data", request_data)

            response_message: Dict[str, Any] = {}
            if choices and isinstance(choices[0], dict):
                response_message = (choices[0].get("message") or {}).copy()
            response_data: Dict[str, Any] = {"message": response_message}
            span.set_attribute("response_data", response_data)

            # Input/output for LLM panels (same shape as LiteLLM/OpenInference instrumentation)
            span.set_attribute("input.mime_type", "application/json")
            span.set_attribute("input.value", {"messages": payload.get("messages") or []})
            span.set_attribute("output.mime_type", "application/json")
            span.set_attribute("output.value", data)
            return data

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Call OpenRouter API; if MCP tools are configured, run a tool loop (same flow as LiteLLM)."""
        print(f"  Chat Completion with openrouter:{self.model}")
        self.history.append({"role": "user", "content": incoming})

        mcps = getattr(self.config, "mcps", None) or []
        if not mcps:
            messages = [
                {"role": "system", "content": self.config.system_prompt},
                *self._messages_for_api(),
            ]
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.config.temperature,
            }
            if self.config.max_tokens is not None:
                payload["max_tokens"] = self.config.max_tokens
            reasoning_effort = getattr(self.config, "reasoning_effort", None)
            if reasoning_effort is not None:
                payload["reasoning_effort"] = reasoning_effort
            if getattr(self.config, "seed", None) is not None:
                payload["seed"] = self.config.seed

            data = await self._post_completion_with_logfire(payload)

            content = ""
            choices = data.get("choices") or []
            if choices:
                msg_data = _message_from_choice(choices[0])
                content = msg_data.get("content") or ""
            full_text = content or ""
            self.history.append({"role": "assistant", "content": full_text})

            if on_chunk is not None:
                await on_chunk("text", full_text)

            usage = usage_from_openai_response(data.get("usage"))
            cost = compute_cost(_cost_model(self.model), usage) if usage else 0.0
            return full_text, {"usage": usage, "cost": cost}

        # MCP tools path
        await self._ensure_mcp_initialized()
        tools = self._get_mcp_tools_for_llm()
        self.log_llm_tools_in_request(tools, provider="openrouter", model=self.model)

        messages = [
            {"role": "system", "content": self.config.system_prompt},
            *self._messages_for_api(),
        ]
        total_cost = 0.0
        total_usage: dict[str, int] = {}
        max_tool_rounds = 20
        final_text = ""

        for _ in range(max_tool_rounds):
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": self.config.temperature,
            }
            if self.config.max_tokens is not None:
                payload["max_tokens"] = self.config.max_tokens
            if getattr(self.config, "reasoning_effort", None) is not None:
                payload["reasoning_effort"] = self.config.reasoning_effort
            if getattr(self.config, "seed", None) is not None:
                payload["seed"] = self.config.seed

            data = await self._post_completion_with_logfire(payload)

            choices = data.get("choices") or []
            if not choices:
                break

            msg_data = _message_from_choice(choices[0])
            content = (msg_data.get("content") or "").strip()
            tool_calls = msg_data.get("tool_calls") or []

            usage = usage_from_openai_response(data.get("usage"))
            if usage:
                total_usage = {
                    k: total_usage.get(k, 0) + usage.get(k, 0)
                    for k in set(total_usage) | set(usage)
                }
            total_cost += compute_cost(_cost_model(self.model), usage) if usage else 0.0

            reasoning = msg_data.get("reasoning_content")
            # Reasoning-only response (no content, no tool_calls): append as assistant message and continue until we get content with no tool_calls
            if not content and not tool_calls and isinstance(reasoning, str) and reasoning.strip():
                reasoning_text = _strip_tool_call_blocks_from_reasoning(reasoning)
                content = reasoning_text or reasoning.strip()
                assistant_record = {"role": "assistant", "content": content}
                self.history.append(assistant_record)
                messages.append({"role": "assistant", "content": content})
                continue

            assistant_record = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant_record["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "name": tc.get("name", ""),
                        "arguments": json.loads(tc.get("arguments") or "{}") if isinstance(tc.get("arguments"), str) else (tc.get("arguments") or {}),
                    }
                    for tc in tool_calls
                ]
            self.history.append(assistant_record)

            # Stop only when we have content and no tool_calls (first such response)
            if not tool_calls:
                final_text = content or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": tc.get("arguments") if isinstance(tc.get("arguments"), str) else json.dumps(tc.get("arguments") or {}),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                fn_name = tc.get("name", "")
                args = tc.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                args = args or {}

                if on_chunk is not None:
                    await on_chunk("tool_use", {"name": fn_name, "id": tc_id, "input": args})

                result = await self._call_mcp_tool(fn_name, args)
                self.history.append({"role": "tool", "content": result, "tool_call_id": tc_id})
                messages.append({"role": "tool", "content": result, "tool_call_id": tc_id})

        if on_chunk is not None:
            await on_chunk("text", final_text)

        return final_text, {"usage": total_usage, "cost": total_cost}
