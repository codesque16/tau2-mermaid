"""Gemini-backed agent; same interface as BaseAgent (respond_stream returns (text, usage_info))."""

import asyncio
import base64
import json
from typing import Any, Callable, Awaitable

from .base import BaseAgent
from .config import AgentConfig
from .gemini_log import log_gemini_generate_io, to_jsonable
from .utils.cost import compute_cost, usage_from_gemini_response

# Gemini API roles for `contents` (see https://ai.google.dev/gemini-api/docs/function-calling ).
GEMINI_ROLE_USER = "user"
GEMINI_ROLE_MODEL = "model"
GEMINI_ROLE_FUNCTION = "function"  # tool / function_call results (not OpenAI's "tool" role)


def _content_to_text(content: Any) -> str:
    """Extract visible assistant text; skip thought parts (``part.thought`` is true)."""
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    out: list[str] = []
    for p in parts:
        if getattr(p, "thought", None):
            continue
        t = getattr(p, "text", None)
        if isinstance(t, str) and t:
            out.append(t)
    return "".join(out)


def _thought_text_from_content(content: Any) -> str:
    """Concatenate thought-summary text from parts where ``thought`` is true (needs ``include_thoughts``)."""
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    chunks: list[str] = []
    for p in parts:
        if not getattr(p, "thought", None):
            continue
        t = getattr(p, "text", None)
        if isinstance(t, str) and t.strip():
            chunks.append(t.strip())
    return "\n\n".join(chunks) if chunks else ""


def _thought_text_from_response(response: Any) -> str:
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return ""
    return _thought_text_from_content(getattr(cands[0], "content", None))


def _thought_signature_for_history(sig: Any) -> str | None:
    """Serialize Part.thought_signature (opaque bytes) for JSON-safe tool_call records."""
    if sig is None:
        return None
    if isinstance(sig, bytes):
        return base64.standard_b64encode(sig).decode("ascii")
    if isinstance(sig, str):
        return sig
    return None


def _thought_signature_from_history(stored: Any) -> bytes | None:
    """Restore bytes for types.Part(thought_signature=...) when replaying tool turns."""
    if stored is None:
        return None
    if isinstance(stored, bytes):
        return stored
    if isinstance(stored, str) and stored.strip():
        return base64.standard_b64decode(stored.encode("ascii"))
    return None


async def _with_retry_gemini(
    agent: "GeminiAgent",
    generate_fn: Callable[[], tuple[str, Any]],
    max_attempts: int = 6,
) -> tuple[str, Any]:
    """Run generate in a thread; retry on transient errors and API key rotation (429 / backup fail)."""
    from agent.api_key_rotation import maybe_rotate_after_provider_error

    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await asyncio.to_thread(generate_fn)
        except Exception as e:
            last_err = e
            inv = lambda: setattr(agent, "_client", None)
            rotated = maybe_rotate_after_provider_error(
                "gemini", e, invalidate_client=inv
            )
            if rotated or attempt < max_attempts - 1:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
            raise
    raise last_err  # type: ignore[misc]


class GeminiAgent(BaseAgent):
    """Gemini-backed agent (chat). Same interface as BaseAgent."""

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self._client: Any = None
        self.history: list[dict[str, Any]] = []

    def _get_client(self):
        from google import genai

        from agent.api_key_rotation import get_gemini_api_key

        api_key = get_gemini_api_key()
        if not api_key.strip():
            raise ValueError("Set GOOGLE_API_KEY or GEMINI_API_KEY for Gemini models.")
        if self._client is None or getattr(self, "_gemini_client_key", None) != api_key:
            self._client = genai.Client(api_key=api_key.strip())
            self._gemini_client_key = api_key.strip()
        return self._client

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Generate reply via Gemini. Returns (full_text, usage_info). Calls on_chunk with full text when done."""
        # In solo mode (retail), tools are required. We mimic LiteLLM's approach:
        # 1) initialize MCP tool schemas via BaseAgent
        # 2) pass tool declarations to Gemini
        # 3) parse model-emitted function_call parts
        # 4) execute MCP tool calls and feed tool results back until final text

        await self._ensure_mcp_initialized()

        def _openai_tools_to_gemini_tool(openai_tools: list[dict[str, Any]]):
            from google.genai import types

            fn_decls: list[Any] = []
            for t in openai_tools:
                if t.get("type") != "function":
                    continue
                fn = t.get("function") or {}
                name = fn.get("name") or ""
                if not name:
                    continue
                params = fn.get("parameters") or {"type": "object", "properties": {}}
                json_schema = types.JSONSchema.model_validate(params)
                schema = types.Schema.from_json_schema(json_schema=json_schema)
                # Match OpenAI path: no MCP docstrings in the tool list.
                fn_decls.append(
                    types.FunctionDeclaration(
                        name=name,
                        description="",
                        parameters=schema,
                    )
                )
            if not fn_decls:
                return None
            return [types.Tool(function_declarations=fn_decls)]

        def _tool_output_to_response_dict(raw: Any) -> dict[str, Any]:
            """Build FunctionResponse.response dict from MCP JSON/text string."""
            if raw is None:
                return {"result": ""}
            s = str(raw).strip()
            if not s:
                return {"result": ""}
            if s.startswith("{") or s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"result": parsed}
                except json.JSONDecodeError:
                    pass
            return {"result": s}

        def _tool_name_for_history_index(hist: list[dict[str, Any]], idx: int) -> str:
            """Resolve function name for a tool message (prefers `name` field)."""
            t = hist[idx]
            n = (t.get("name") or "").strip()
            if n:
                return n
            tc_id = t.get("tool_call_id")
            for j in range(idx - 1, -1, -1):
                if hist[j].get("role") != "assistant":
                    continue
                for tc in hist[j].get("tool_calls") or []:
                    if tc.get("id") == tc_id:
                        return str(tc.get("name") or "")
            return ""

        def _history_to_gemini_contents():
            """Map OpenAI-style history to Gemini contents: model=function_call, function=function_response."""
            from google.genai import types

            contents: list[Any] = []
            hist = self.history
            i = 0
            while i < len(hist):
                m = hist[i]
                role = m.get("role")

                if role == "user":
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=m.get("content") or "")],
                        )
                    )
                    i += 1
                    continue

                if role == "assistant":
                    tool_calls = m.get("tool_calls") or []
                    text = (m.get("content") or "").strip()
                    parts: list[Any] = []
                    reasoning = (
                        m.get("reasoning_content") or m.get("thought") or ""
                    )
                    if isinstance(reasoning, str) and reasoning.strip():
                        parts.append(
                            types.Part(text=reasoning.strip(), thought=True)
                        )
                    if text:
                        parts.append(types.Part(text=text))
                    for tc in tool_calls:
                        fn = str(tc.get("name") or "").strip()
                        args = tc.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args) if args.strip() else {}
                            except json.JSONDecodeError:
                                args = {}
                        if not isinstance(args, dict):
                            args = {}
                        tid = tc.get("id")
                        tid_s = str(tid).strip() if tid is not None else None
                        fc = types.FunctionCall(
                            name=fn,
                            args=dict(args),
                            id=tid_s or None,
                        )
                        sig = _thought_signature_from_history(
                            tc.get("thought_signature")
                        )
                        part_kw: dict[str, Any] = {"function_call": fc}
                        if sig is not None:
                            part_kw["thought_signature"] = sig
                        parts.append(types.Part(**part_kw))
                    if not parts:
                        parts.append(types.Part(text=""))
                    contents.append(types.Content(role="model", parts=parts))
                    i += 1
                    continue

                if role == "tool":
                    fr_parts: list[Any] = []
                    while i < len(hist) and hist[i].get("role") == "tool":
                        t = hist[i]
                        fname = _tool_name_for_history_index(hist, i)
                        if not fname:
                            fname = "unknown_tool"
                        tid = t.get("tool_call_id")
                        tid_s = str(tid).strip() if tid is not None else None
                        fr_parts.append(
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=fname,
                                    id=tid_s or None,
                                    response=_tool_output_to_response_dict(
                                        t.get("content")
                                    ),
                                )
                            )
                        )
                        i += 1
                    contents.append(
                        types.Content(role=GEMINI_ROLE_FUNCTION, parts=fr_parts)
                    )
                    continue

                i += 1

            return contents

        def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
            tool_calls: list[dict[str, Any]] = []
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                return tool_calls
            content = candidates[0].content
            parts = getattr(content, "parts", None) or []
            for p in parts:
                fc = getattr(p, "function_call", None)
                if not fc:
                    continue
                tool_calls.append(
                    {
                        "id": getattr(fc, "id", None) or "",
                        "name": getattr(fc, "name", None) or "",
                        "arguments": getattr(fc, "args", None) or {},
                        # Required when using thinking + tools; must be echoed on replay.
                        "thought_signature": getattr(p, "thought_signature", None),
                    }
                )
            return tool_calls

        def _extract_text(response: Any) -> str:
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                return ""
            return _content_to_text(candidates[0].content)

        openai_tools = self._get_mcp_tools_for_llm()
        if openai_tools:
            self.log_llm_tools_in_request(
                openai_tools, provider="gemini", model=self.model
            )
        gemini_tools = _openai_tools_to_gemini_tool(openai_tools) if openai_tools else None

        # Store user message in our OpenAI-compatible history structure so evaluators can replay tool calls.
        self.history.append({"role": "user", "content": incoming})

        max_tool_rounds = 20
        final_text = ""
        total_usage: dict[str, int] = {}
        total_cost = 0.0

        for _round in range(max_tool_rounds):
            def _generate() -> tuple[str, str, Any, list[dict[str, Any]], dict[str, Any]]:
                from google.genai import types

                client = self._get_client()
                contents = _history_to_gemini_contents()

                gen_config_kw: dict = {
                    "system_instruction": self.get_effective_system_prompt(),
                    "temperature": self.config.temperature,
                }
                reff = getattr(self.config, "reasoning_effort", None)
                if reff is not None and str(reff).strip():
                    lvl = str(reff).strip().lower()
                    level_map = {
                        "low": types.ThinkingLevel.LOW,
                        "medium": types.ThinkingLevel.MEDIUM,
                        "high": types.ThinkingLevel.HIGH,
                        "minimal": types.ThinkingLevel.MINIMAL,
                    }
                    tl = level_map.get(lvl)
                    if tl is not None:
                        # ``include_thoughts`` returns summaries in ``Part(text=..., thought=True)``.
                        # ``thought_signature`` on tool-call parts is still required for replay.
                        try:
                            gen_config_kw["thinking_config"] = types.ThinkingConfig(
                                thinking_level=tl,
                                include_thoughts=True,
                            )
                        except Exception:
                            gen_config_kw["thinking_config"] = types.ThinkingConfig(
                                thinking_level=tl
                            )
                if self.config.max_tokens is not None:
                    gen_config_kw["max_output_tokens"] = self.config.max_tokens
                if gemini_tools:
                    gen_config_kw["tools"] = gemini_tools
                    # We manage tool execution ourselves; this prevents the SDK from trying to "help".
                    gen_config_kw["automaticFunctionCalling"] = types.AutomaticFunctionCallingConfig(
                        disable=True
                    )

                gen_config = types.GenerateContentConfig(**gen_config_kw)
                # Raw I/O: ``gemini_io_json`` on the generate_content span (if small enough) +
                # ``log_gemini_generate_io`` below (includes ``tool_round`` in the log payload).
                response = client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=gen_config,
                )
                log_bundle = {
                    "gemini_contents": to_jsonable(contents),
                    "gemini_config": to_jsonable(gen_config),
                }
                thought_text = _thought_text_from_response(response)
                return (
                    _extract_text(response),
                    thought_text,
                    response,
                    _extract_tool_calls(response),
                    log_bundle,
                )

            text, thought_text, response, tool_calls, log_bundle = await _with_retry_gemini(
                self, _generate
            )
            log_gemini_generate_io(
                model=self.model,
                tool_round=_round,
                contents=log_bundle["gemini_contents"],
                config=log_bundle["gemini_config"],
                response=response,
            )
            usage = usage_from_gemini_response(response)
            if usage:
                total_usage = {
                    k: int(total_usage.get(k, 0)) + int(usage.get(k, 0))
                    for k in set(total_usage) | set(usage)
                }
            total_cost += compute_cost(self.model, usage) if usage else 0.0

            if tool_calls:
                # Record assistant tool calls in LiteLLM-compatible history format.
                # This is required for evaluation (`_extract_predicted_actions`).
                tool_call_records = [
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        "thought_signature": _thought_signature_for_history(
                            tc.get("thought_signature")
                        ),
                    }
                    for tc in tool_calls
                ]
                asst_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": [
                        {
                            "id": tr["id"],
                            "name": tr["name"],
                            "arguments": tr["arguments"],
                            "thought_signature": tr.get("thought_signature"),
                        }
                        for tr in tool_call_records
                    ],
                }
                if thought_text.strip():
                    asst_msg["reasoning_content"] = thought_text.strip()
                self.history.append(asst_msg)
                if on_chunk is not None and thought_text.strip():
                    await on_chunk("reasoning", thought_text.strip())

                for tr in tool_call_records:
                    if on_chunk is not None:
                        await on_chunk(
                            "tool_use",
                            {"name": tr["name"], "id": tr["id"], "input": tr["arguments"]},
                        )
                    result = await self._call_mcp_tool(tr["name"], tr["arguments"])
                    self.history.append(
                        {
                            "role": "tool",
                            "name": tr["name"],
                            "content": result,
                            "tool_call_id": tr["id"],
                        }
                    )
                # Next round: Gemini should incorporate tool results.
                continue

            # No tool calls: final assistant text.
            final_text = text.strip()
            final_msg: dict[str, Any] = {"role": "assistant", "content": final_text}
            if thought_text.strip():
                final_msg["reasoning_content"] = thought_text.strip()
            self.history.append(final_msg)
            if on_chunk is not None:
                if thought_text.strip():
                    await on_chunk("reasoning", thought_text.strip())
                await on_chunk("text", final_text)
            return final_text, {"usage": total_usage, "cost": total_cost}

        # Safety fallback: if the model keeps requesting tools, return whatever text we last got.
        final_text = final_text.strip()
        if on_chunk is not None and final_text:
            await on_chunk("text", final_text)
        return final_text, {"usage": total_usage, "cost": total_cost}
