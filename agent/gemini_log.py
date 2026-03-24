"""
Raw ``generate_content`` I/O for debugging (native Gemini shape: user / model / function).

Logfire: logs one string attribute ``gemini_io_json`` so the UI does not explode nested
``messages[]`` / ``tool_calls`` into awkward trees. Copy the string or use a JSON viewer.

Custom UI: In Logfire, open an event → attribute actions → **Enhance view** (JSX) if your
workspace offers it. Bind to ``gemini_io_json``; if the value is a string, ``JSON.parse``
it then render ``request.contents``. Example:

  const root = typeof data === "string" ? JSON.parse(data) : data;
  return (
    <pre className="text-xs whitespace-pre-wrap">{JSON.stringify(root.request?.contents, null, 2)}</pre>
  );

Env:
  TAU2_GEMINI_LOGFIRE_IO=0           — disable Logfire raw I/O (default: **on** — ``logfire.info``
                                     plus optional ``gemini_io_json`` on the generate_content span)
  TAU2_GEMINI_DUMP_PATH=/path.jsonl — append one JSON object per call (solo agent, GEPA GenAI
                                     reflection/diagnosis, native OpenAI / Anthropic chat (via
                                     ``logfire_native_llm``), LiteLLM reflection / refiner / eval judge
                                     when ``agent.gemini_log`` is importable). Each record may include
                                     ``api_key_masked`` (first/last chars + length).
  TAU2_GEMINI_LOGFIRE_JSON_PRETTY=0  — minify the string (default: indent=2)
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

__all__ = [
    "to_jsonable",
    "log_gemini_generate_io",
    "log_openai_chat_raw_io",
    "log_openai_responses_raw_io",
    "log_anthropic_messages_raw_io",
    "log_litellm_raw_io",
    "logfire_raw_io_enabled",
]


def env_on(name: str, default: bool = True) -> bool:
    v = os.environ.get(name, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def logfire_raw_io_enabled() -> bool:
    """Full request/response logging to Logfire (and span attribute when small enough). Opt out: ``TAU2_GEMINI_LOGFIRE_IO=0``."""
    return env_on("TAU2_GEMINI_LOGFIRE_IO", True)


def to_jsonable(o: Any, _depth: int = 0) -> Any:
    if _depth > 40:
        return "<max-depth>"
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, bytes):
        return {
            "_encoding": "base64",
            "_data": base64.standard_b64encode(o).decode("ascii"),
        }
    if isinstance(o, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(x, _depth + 1) for x in o]
    md = getattr(o, "model_dump", None)
    if callable(md):
        for kwargs in (
            {"mode": "json", "exclude_none": True},
            {"exclude_none": True},
        ):
            try:
                return to_jsonable(md(**kwargs), _depth + 1)
            except (TypeError, ValueError):
                continue
    return str(o)


def _dumps(payload: Any) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, bytes):
            return {
                "_encoding": "base64",
                "_data": base64.standard_b64encode(o).decode("ascii"),
            }
        raise TypeError(repr(o))

    indent = 2 if env_on("TAU2_GEMINI_LOGFIRE_JSON_PRETTY", True) else None
    try:
        return json.dumps(payload, ensure_ascii=False, indent=indent, default=default)
    except TypeError:
        return json.dumps(payload, ensure_ascii=False, indent=indent, default=str)


def log_gemini_generate_io(
    *,
    model: str,
    tool_round: int,
    contents: Any,
    config: Any,
    response: Any,
    phase: str | None = None,
    api_key_masked: str | None = None,
    emit_logfire_event: bool = True,
) -> None:
    """Log exactly what was sent to and received from ``models.generate_content``."""
    payload: dict[str, Any] = {
        "tool_round": tool_round,
        "request": {
            "model": model,
            "contents": contents,
            "config": config,
        },
        "response": to_jsonable(response),
    }
    if phase:
        payload["phase"] = phase
    if api_key_masked:
        payload["api_key_masked"] = api_key_masked

    path = os.environ.get("TAU2_GEMINI_DUMP_PATH", "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dumps(payload) + "\n")

    if not emit_logfire_event:
        return
    if not logfire_raw_io_enabled():
        return
    try:
        import logfire
    except ImportError:
        return
    logfire.info(
        "gemini.generate_content",
        model=model,
        tool_round=tool_round,
        **({"phase": phase} if phase else {}),
        **({"api_key_masked": api_key_masked} if api_key_masked else {}),
        gemini_io_json=_dumps(payload),
    )


def log_openai_chat_raw_io(
    *,
    phase: str | None = None,
    model: str,
    messages: list[dict[str, Any]],
    request_extras: dict[str, Any] | None = None,
    completion: Any,
    api_key_masked: str | None = None,
) -> None:
    """Append one JSON line (and optional Logfire event) for a native OpenAI ``chat.completions`` call."""
    payload: dict[str, Any] = {
        "provider": "openai",
        "request": {
            "model": model,
            "messages": messages,
            **(request_extras or {}),
        },
        "response": to_jsonable(completion),
    }
    if phase:
        payload["phase"] = phase
    if api_key_masked:
        payload["api_key_masked"] = api_key_masked

    path = os.environ.get("TAU2_GEMINI_DUMP_PATH", "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dumps(payload) + "\n")

    if not logfire_raw_io_enabled():
        return
    try:
        import logfire
    except ImportError:
        return
    logfire.info(
        "openai.chat.completions",
        model=model,
        **({"phase": phase} if phase else {}),
        **({"api_key_masked": api_key_masked} if api_key_masked else {}),
        llm_io_json=_dumps(payload),
    )


def log_openai_responses_raw_io(
    *,
    phase: str | None = None,
    model: str,
    request_kwargs: dict[str, Any] | None = None,
    response: Any,
    api_key_masked: str | None = None,
) -> None:
    """Append one JSON line (and optional Logfire event) for native OpenAI ``responses.create`` calls."""
    payload: dict[str, Any] = {
        "provider": "openai",
        "request": {
            "model": model,
            **(request_kwargs or {}),
        },
        "response": to_jsonable(response),
    }
    if phase:
        payload["phase"] = phase
    if api_key_masked:
        payload["api_key_masked"] = api_key_masked

    path = os.environ.get("TAU2_GEMINI_DUMP_PATH", "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dumps(payload) + "\n")

    if not logfire_raw_io_enabled():
        return
    try:
        import logfire
    except ImportError:
        return
    logfire.info(
        "openai.responses",
        model=model,
        **({"phase": phase} if phase else {}),
        **({"api_key_masked": api_key_masked} if api_key_masked else {}),
        llm_io_json=_dumps(payload),
    )


def log_anthropic_messages_raw_io(
    *,
    phase: str | None = None,
    model: str,
    system_text: str,
    api_messages: list[dict[str, Any]],
    request_extras: dict[str, Any] | None = None,
    message: Any,
    api_key_masked: str | None = None,
) -> None:
    """Append one JSON line (and optional Logfire event) for a native Anthropic ``messages.create`` call."""
    payload: dict[str, Any] = {
        "provider": "anthropic",
        "request": {
            "model": model,
            "system": system_text,
            "messages": api_messages,
            **(request_extras or {}),
        },
        "response": to_jsonable(message),
    }
    if phase:
        payload["phase"] = phase
    if api_key_masked:
        payload["api_key_masked"] = api_key_masked

    path = os.environ.get("TAU2_GEMINI_DUMP_PATH", "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dumps(payload) + "\n")

    if not logfire_raw_io_enabled():
        return
    try:
        import logfire
    except ImportError:
        return
    logfire.info(
        "anthropic.messages.create",
        model=model,
        **({"phase": phase} if phase else {}),
        **({"api_key_masked": api_key_masked} if api_key_masked else {}),
        llm_io_json=_dumps(payload),
    )


def log_litellm_raw_io(
    *,
    phase: str,
    model: str,
    messages: list[dict[str, Any]],
    completion: Any,
    extra_completion_kwargs: dict[str, Any] | None = None,
    api_key_masked: str | None = None,
) -> None:
    """Append one JSON line (and optional Logfire event) for a LiteLLM ``completion`` call."""
    mk = (api_key_masked or "").strip() or None
    if not mk:
        try:
            from agent.api_key_rotation import api_key_masked_for_litellm_model

            mk = api_key_masked_for_litellm_model(model).strip() or None
        except ImportError:
            mk = None

    payload: dict[str, Any] = {
        "phase": phase,
        "provider": "litellm",
        "request": {
            "model": model,
            "messages": messages,
            **(extra_completion_kwargs or {}),
        },
        "response": to_jsonable(completion),
    }
    if mk:
        payload["api_key_masked"] = mk

    path = os.environ.get("TAU2_GEMINI_DUMP_PATH", "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dumps(payload) + "\n")

    if not logfire_raw_io_enabled():
        return
    try:
        import logfire
    except ImportError:
        return
    logfire.info(
        "litellm.completion",
        phase=phase,
        model=model,
        **({"api_key_masked": mk} if mk else {}),
        llm_io_json=_dumps(payload),
    )
