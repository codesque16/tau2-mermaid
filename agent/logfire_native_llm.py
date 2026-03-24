"""Logfire span helpers for native OpenAI / Anthropic chat APIs (parity with OpenRouter + Gemini I/O)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import logfire


def pydantic_or_dict(obj: Any) -> dict[str, Any]:
    """JSON-serializable dict for Logfire attributes."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()  # type: ignore[no-any-return]
    return {"repr": repr(obj)}


def openai_chat_completion_to_response_dict(completion: Any) -> dict[str, Any]:
    """Shape compatible with OpenRouter-style ``response_data`` / ``output.value``."""
    return pydantic_or_dict(completion)


def openai_responses_to_response_dict(response: Any) -> dict[str, Any]:
    """Shape compatible with Logfire attribute payloads (Responses API)."""
    return pydantic_or_dict(response)


def _extract_openai_responses_assistant_text(response: Any) -> str:
    # SDK provides `.output_text` convenience; fall back to parsing `output`.
    txt = getattr(response, "output_text", None)
    if isinstance(txt, str):
        return txt
    items = getattr(response, "output", None) or []
    for it in items:
        if getattr(it, "type", None) == "message":
            for c in getattr(it, "content", None) or []:
                if getattr(c, "type", None) == "output_text":
                    return getattr(c, "text", "") or ""
    return ""


def _extract_openai_responses_tool_calls(response: Any) -> list[dict[str, Any]]:
    items = getattr(response, "output", None) or []
    out: list[dict[str, Any]] = []
    for it in items:
        if getattr(it, "type", None) != "function_call":
            continue
        args_raw = getattr(it, "arguments", None)
        args: Any = args_raw if args_raw is not None else {}
        if isinstance(args_raw, str):
            s = args_raw.strip()
            if s:
                try:
                    args = json.loads(s)
                except Exception:
                    args = s
        out.append(
            {
                "id": getattr(it, "call_id", None) or getattr(it, "id", None) or "",
                "type": "function",
                "function": {
                    "name": getattr(it, "name", None) or "",
                    "arguments": args if args is not None else {},
                },
            }
        )
    return out


def _collect_texts_from_unknown(obj: Any) -> list[str]:
    """
    Extract text strings from a loosely-typed OpenAI Responses reasoning payload.
    We keep this intentionally defensive because SDK shapes vary.
    """
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        for k in ("text", "content", "summary", "summary_text", "value"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
        out: list[str] = []
        for v in obj.values():
            out.extend(_collect_texts_from_unknown(v))
        return out
    if isinstance(obj, (list, tuple)):
        out: list[str] = []
        for x in obj:
            out.extend(_collect_texts_from_unknown(x))
        return out
    # Generic object with common string attributes
    for attr in ("text", "content", "summary", "summary_text"):
        v = getattr(obj, attr, None)
        if isinstance(v, str) and v.strip():
            return [v.strip()]
    return []


def _tool_call_arguments_json(args: Any) -> str:
    """Convert tool call arguments into the string form Logfire expects."""
    if args is None:
        return "{}"
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return "{}"
        return s
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except TypeError:
        return str(args)


def _normalize_openai_tool_calls_for_logfire(
    internal_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Logfire Model Run expects OpenAI-shaped tool calls for assistant messages:
      tool_calls: [{ id, type: "function", function: { name, arguments } }]

    Our agent histories often store tool calls as:
      tool_calls: [{ id, name, arguments }]
    """
    out: list[dict[str, Any]] = []
    for m in internal_messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            out.append(m)
            continue
        tool_calls = m.get("tool_calls")
        if not isinstance(tool_calls, list):
            out.append(m)
            continue

        normalized_calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue

            call_id = tc.get("id") or ""
            # Our history: {id, name, arguments}
            fn_name = tc.get("name") or (tc.get("function") or {}).get("name")
            args = tc.get("arguments") or (tc.get("function") or {}).get("arguments")

            normalized_calls.append(
                {
                    "id": str(call_id),
                    "type": "function",
                    "function": {"name": str(fn_name or ""), "arguments": _tool_call_arguments_json(args)},
                }
            )

        m2 = dict(m)
        m2["tool_calls"] = normalized_calls
        out.append(m2)
    return out


def _extract_openai_responses_reasoning_text(response: Any) -> str | None:
    """Best-effort extraction of Responses API reasoning summaries."""
    items = getattr(response, "output", None) or []
    for it in items:
        it_type = getattr(it, "type", None)
        if not isinstance(it_type, str):
            continue
        if "reasoning" not in it_type.lower():
            continue

        # Common fields across SDK versions.
        for field in (
            "summary",
            "summaries",
            "summary_text",
            "summary_texts",
            "content",
            "encrypted_content",
        ):
            candidate = getattr(it, field, None)
            texts = _collect_texts_from_unknown(candidate)
            # For encrypted reasoning we typically won't have clear text; only use
            # it if nothing else exists.
            if texts:
                return "\n\n".join([t for t in texts if t.strip()])
    return None


def _finalize_openai_responses_span(
    span: Any,
    *,
    model: str,
    request_kwargs: dict[str, Any],
    response: Any,
    api_key_masked: str | None = None,
    io_phase: str | None = None,
    internal_messages: list[dict[str, Any]] | None = None,
) -> None:
    resp_dict = openai_responses_to_response_dict(response)
    usage = resp_dict.get("usage") or {}
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0

    tool_calls = _extract_openai_responses_tool_calls(response)
    finish_reasons = ["tool_calls"] if tool_calls else ["stop"]

    resp_model = resp_dict.get("model") or model
    span.set_attribute("gen_ai.response.model", resp_model)
    span.set_attribute("gen_ai.usage.input_tokens", inp)
    span.set_attribute("gen_ai.usage.output_tokens", out)
    span.set_attribute("gen_ai.response.finish_reasons", finish_reasons)

    assistant_text = _extract_openai_responses_assistant_text(response)
    reasoning_text = _extract_openai_responses_reasoning_text(response)
    visible_content: str = assistant_text.strip() if isinstance(assistant_text, str) else ""

    response_message: dict[str, Any] = {
        "role": "assistant",
        # Logfire's Model Run UI expects a string (or null) for completion content.
        # When the model is emitting tool calls, it can return empty content; use
        # "" (not null) to ensure tool call details stay visible.
        "content": visible_content or "",
    }
    if tool_calls:
        response_message["tool_calls"] = tool_calls

    if reasoning_text:
        response_message["reasoning_content"] = reasoning_text

    # Logfire Model Run tab expects request_data.messages in OpenAI chat-like shape.
    # For Responses API, request_kwargs may only contain a compressed "input" (tool outputs),
    # so we prefer the agent's internal transcript when available.
    instructions = request_kwargs.get("instructions") or ""
    input_payload = request_kwargs.get("input")

    ui_messages_fallback: list[dict[str, Any]] = []
    if isinstance(instructions, str) and instructions.strip():
        ui_messages_fallback.append({"role": "system", "content": instructions})

    if isinstance(input_payload, str):
        ui_messages_fallback.append({"role": "user", "content": input_payload})
    elif isinstance(input_payload, list):
        # Tool loop calls provide tool outputs as "input".
        for item in input_payload:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call_output":
                continue
            call_id = item.get("call_id") or ""
            output = item.get("output")
            content_str = (
                output
                if isinstance(output, str)
                else str(output)
                if output is not None
                else ""
            )
            ui_messages_fallback.append(
                {"role": "tool", "tool_call_id": str(call_id), "content": content_str, "name": ""}
            )
    else:
        if input_payload is not None:
            ui_messages_fallback.append({"role": "user", "content": str(input_payload)})

    ui_messages_for_input_value = internal_messages or ui_messages_fallback
    request_data = {"model": resp_model, "messages": ui_messages_for_input_value}

    if internal_messages is not None:
        try:
            # Adds request_data/response_data in the exact format Logfire expects,
            # including full-thread rendering and reasoning/tool-call split.
            from agent.logfire_gemini_integration import _attach_litellm_style_request_response

            _attach_litellm_style_request_response(
                span,
                model=resp_model,
                internal_messages=_normalize_openai_tool_calls_for_logfire(internal_messages),
                response_message=response_message,
            )
        except Exception:
            span.set_attribute("request_data", request_data)
            span.set_attribute("response_data", {"message": response_message})
    else:
        span.set_attribute("request_data", request_data)
        span.set_attribute("response_data", {"message": response_message})
    span.set_attribute("input.mime_type", "application/json")
    span.set_attribute("input.value", {"messages": ui_messages_for_input_value})
    span.set_attribute("output.mime_type", "application/json")
    span.set_attribute("output.value", resp_dict)

    try:
        from agent.gemini_log import log_openai_responses_raw_io

        log_openai_responses_raw_io(
            phase=io_phase,
            model=model,
            # Raw I/O log should capture the exact payload passed to responses.create(...)
            # (tools, tool_choice, seed, reasoning, previous_response_id, etc.), not the
            # reduced chat-shaped UI view used by Logfire Model Run rendering.
            request_kwargs=request_kwargs,
            response=response,
            api_key_masked=api_key_masked,
        )
    except Exception:
        pass


def anthropic_message_to_response_dict(message: Any) -> dict[str, Any]:
    return pydantic_or_dict(message)


def _finalize_openai_span(
    span: Any,
    *,
    model: str,
    request_messages: list[dict[str, Any]],
    request_extras: dict[str, Any],
    completion: Any,
    api_key_masked: str | None = None,
    io_phase: str | None = None,
) -> None:
    resp = openai_chat_completion_to_response_dict(completion)
    usage = resp.get("usage") or {}
    inp = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    choices = resp.get("choices") or []
    finish_reasons = [
        c.get("finish_reason") for c in choices if isinstance(c, dict)
    ]
    resp_model = resp.get("model") or model

    span.set_attribute("gen_ai.response.model", resp_model)
    span.set_attribute("gen_ai.usage.input_tokens", inp)
    span.set_attribute("gen_ai.usage.output_tokens", out)
    if finish_reasons:
        span.set_attribute("gen_ai.response.finish_reasons", finish_reasons)

    response_message: dict[str, Any] = {}
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict):
            response_message = msg.copy()
        elif msg is not None:
            response_message = pydantic_or_dict(msg)

    request_data: dict[str, Any] = {"messages": request_messages}
    if "tools" in request_extras:
        request_data["tools"] = request_extras["tools"]
    span.set_attribute("request_data", request_data)
    span.set_attribute("response_data", {"message": response_message})
    span.set_attribute("input.mime_type", "application/json")
    span.set_attribute("input.value", {"messages": request_messages})
    span.set_attribute("output.mime_type", "application/json")
    span.set_attribute("output.value", resp)

    try:
        from agent.gemini_log import log_openai_chat_raw_io

        log_openai_chat_raw_io(
            phase=io_phase,
            model=model,
            messages=request_messages,
            request_extras=request_extras,
            completion=completion,
            api_key_masked=api_key_masked,
        )
    except ImportError:
        pass


async def async_openai_chat_with_logfire(
    *,
    agent_name: str,
    model: str,
    request_messages: list[dict[str, Any]],
    request_extras: dict[str, Any],
    create_coro: Callable[[], Awaitable[Any]],
    api_key_masked: str | None = None,
    io_phase: str | None = None,
) -> Any:
    """Await ``create_coro`` inside a Logfire span (async OpenAI client)."""
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import get_openai_api_key_masked

            mk = (get_openai_api_key_masked() or "").strip() or None
        except ImportError:
            mk = None

    with logfire.span(
        "openai chat.completions",
        agent=agent_name,
        model=model,
        **{
            "gen_ai.system": "openai",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    ) as span:
        if mk:
            span.set_attribute("openai.api_key_masked", mk)
        completion = await create_coro()
        _finalize_openai_span(
            span,
            model=model,
            request_messages=request_messages,
            request_extras=request_extras,
            completion=completion,
            api_key_masked=mk,
            io_phase=io_phase,
        )
        return completion


def sync_openai_chat_with_logfire(
    *,
    agent_name: str,
    model: str,
    request_messages: list[dict[str, Any]],
    request_extras: dict[str, Any],
    create_fn: Callable[[], Any],
    api_key_masked: str | None = None,
    io_phase: str | None = None,
) -> Any:
    """Run sync ``create_fn`` inside a Logfire span (GEPA / sync OpenAI client)."""
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import get_openai_api_key_masked

            mk = (get_openai_api_key_masked() or "").strip() or None
        except ImportError:
            mk = None

    with logfire.span(
        "openai chat.completions",
        agent=agent_name,
        model=model,
        **{
            "gen_ai.system": "openai",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    ) as span:
        if mk:
            span.set_attribute("openai.api_key_masked", mk)
        completion = create_fn()
        _finalize_openai_span(
            span,
            model=model,
            request_messages=request_messages,
            request_extras=request_extras,
            completion=completion,
            api_key_masked=mk,
            io_phase=io_phase,
        )
        return completion


async def async_openai_responses_with_logfire(
    *,
    agent_name: str,
    model: str,
    request_kwargs: dict[str, Any],
    create_coro: Callable[[], Awaitable[Any]],
    api_key_masked: str | None = None,
    io_phase: str | None = None,
    internal_messages: list[dict[str, Any]] | None = None,
) -> Any:
    """Await Responses API create_coro inside a Logfire span (OpenAI native)."""
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import get_openai_api_key_masked

            mk = (get_openai_api_key_masked() or "").strip() or None
        except ImportError:
            mk = None

    with logfire.span(
        # Logfire's "Model run" UI is optimized for chat-style operations.
        # We still use the Responses API under the hood, but label it as chat
        # to keep the Model run tab consistent with OpenRouter/LiteLLM.
        "openai chat",
        agent=agent_name,
        model=model,
        **{
            "gen_ai.system": "openai",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    ) as span:
        if mk:
            span.set_attribute("openai.api_key_masked", mk)
        response = await create_coro()
        _finalize_openai_responses_span(
            span,
            model=model,
            request_kwargs=request_kwargs,
            response=response,
            api_key_masked=mk,
            io_phase=io_phase,
            internal_messages=internal_messages,
        )
        return response


def sync_openai_responses_with_logfire(
    *,
    agent_name: str,
    model: str,
    request_kwargs: dict[str, Any],
    create_fn: Callable[[], Any],
    api_key_masked: str | None = None,
    io_phase: str | None = None,
    internal_messages: list[dict[str, Any]] | None = None,
) -> Any:
    """Run sync Responses API create_fn inside a Logfire span (OpenAI native)."""
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import get_openai_api_key_masked

            mk = (get_openai_api_key_masked() or "").strip() or None
        except ImportError:
            mk = None

    with logfire.span(
        # See async_openai_responses_with_logfire for rationale.
        "openai chat",
        agent=agent_name,
        model=model,
        **{
            "gen_ai.system": "openai",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    ) as span:
        if mk:
            span.set_attribute("openai.api_key_masked", mk)
        response = create_fn()
        _finalize_openai_responses_span(
            span,
            model=model,
            request_kwargs=request_kwargs,
            response=response,
            api_key_masked=mk,
            io_phase=io_phase,
            internal_messages=internal_messages,
        )
        return response


def _finalize_anthropic_span(
    span: Any,
    *,
    model: str,
    system_text: str,
    api_messages: list[dict[str, Any]],
    request_extras: dict[str, Any],
    message: Any,
    api_key_masked: str | None = None,
    io_phase: str | None = None,
) -> None:
    resp = anthropic_message_to_response_dict(message)
    usage = resp.get("usage") or {}
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    stop_reason = resp.get("stop_reason")
    resp_model = resp.get("model") or model

    span.set_attribute("gen_ai.response.model", resp_model)
    span.set_attribute("gen_ai.usage.input_tokens", inp)
    span.set_attribute("gen_ai.usage.output_tokens", out)
    if stop_reason is not None:
        span.set_attribute("gen_ai.response.finish_reasons", [stop_reason])

    content_blocks = resp.get("content") or []
    assistant_text = ""
    for b in content_blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            assistant_text += str(b.get("text") or "")

    request_data: dict[str, Any] = {
        "system": system_text,
        "messages": api_messages,
    }
    for key in ("tools", "tool_choice", "thinking", "temperature"):
        if key in request_extras and request_extras[key] is not None:
            request_data[key] = request_extras[key]
    span.set_attribute("request_data", request_data)
    span.set_attribute(
        "response_data",
        {
            "content": content_blocks,
            "stop_reason": stop_reason,
            "text_preview": assistant_text[:2000],
        },
    )
    span.set_attribute("input.mime_type", "application/json")
    span.set_attribute(
        "input.value",
        json.loads(json.dumps(request_data, default=str)),
    )
    span.set_attribute("output.mime_type", "application/json")
    span.set_attribute("output.value", resp)

    try:
        from agent.gemini_log import log_anthropic_messages_raw_io

        log_anthropic_messages_raw_io(
            phase=io_phase,
            model=model,
            system_text=system_text,
            api_messages=api_messages,
            request_extras=request_extras,
            message=message,
            api_key_masked=api_key_masked,
        )
    except ImportError:
        pass


async def async_anthropic_messages_with_logfire(
    *,
    agent_name: str,
    model: str,
    system_text: str,
    api_messages: list[dict[str, Any]],
    request_extras: dict[str, Any],
    create_coro: Callable[[], Awaitable[Any]],
    api_key_masked: str | None = None,
    io_phase: str | None = None,
) -> Any:
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import get_anthropic_api_key_masked

            mk = (get_anthropic_api_key_masked() or "").strip() or None
        except ImportError:
            mk = None

    with logfire.span(
        "anthropic messages.create",
        agent=agent_name,
        model=model,
        **{
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    ) as span:
        if mk:
            span.set_attribute("anthropic.api_key_masked", mk)
        message = await create_coro()
        _finalize_anthropic_span(
            span,
            model=model,
            system_text=system_text,
            api_messages=api_messages,
            request_extras=request_extras,
            message=message,
            api_key_masked=mk,
            io_phase=io_phase,
        )
        return message


def sync_anthropic_messages_with_logfire(
    *,
    agent_name: str,
    model: str,
    system_text: str,
    api_messages: list[dict[str, Any]],
    request_extras: dict[str, Any],
    create_fn: Callable[[], Any],
    api_key_masked: str | None = None,
    io_phase: str | None = None,
) -> Any:
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import get_anthropic_api_key_masked

            mk = (get_anthropic_api_key_masked() or "").strip() or None
        except ImportError:
            mk = None

    with logfire.span(
        "anthropic messages.create",
        agent=agent_name,
        model=model,
        **{
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    ) as span:
        if mk:
            span.set_attribute("anthropic.api_key_masked", mk)
        message = create_fn()
        _finalize_anthropic_span(
            span,
            model=model,
            system_text=system_text,
            api_messages=api_messages,
            request_extras=request_extras,
            message=message,
            api_key_masked=mk,
            io_phase=io_phase,
        )
        return message
