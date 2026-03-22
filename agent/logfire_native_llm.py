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
