"""GEPA :class:`~gepa.proposer.reflective_mutation.base.LanguageModel` via Anthropic Messages API (native SDK).

Logfire spans match :mod:`agent.logfire_native_llm` / solo :class:`~agent.agent_anthropic.AnthropicAgent`.

``reflection_llm_backend: anthropic`` in ``gepa_retail_mermaid`` YAML selects this path.
"""

from __future__ import annotations

import time
from typing import Any

from gepa.proposer.reflective_mutation.base import LanguageModel

from agent.api_key_rotation import (
    get_anthropic_api_key,
    mask_secret,
    maybe_rotate_after_provider_error,
)

_DEFAULT_MAX = 4096


def _thinking_budget_from_effort(effort: str | None, max_tokens: int) -> int | None:
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


def _prompt_to_user_text(prompt: str | list[dict[str, Any]]) -> str:
    if isinstance(prompt, str):
        return prompt
    chunks: list[str] = []
    for m in prompt:
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str):
            chunks.append(f"[{role}]\n{content}")
        else:
            chunks.append(f"[{role}]\n{content!s}")
    return "\n\n".join(chunks)


def _thinking_budget(effort: str | None, max_tokens: int) -> tuple[dict[str, Any] | None, int]:
    b = _thinking_budget_from_effort(effort, max_tokens)
    if b is None:
        return None, max_tokens
    need = b + 2048
    mt = max(max_tokens, need)
    b2 = _thinking_budget_from_effort(effort, mt)
    if b2 is None:
        return None, mt
    return {"type": "enabled", "budget_tokens": b2}, mt


def anthropic_generate_user_text(
    model: str,
    user_text: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    import anthropic

    from agent.logfire_native_llm import sync_anthropic_messages_with_logfire

    model = model.strip()
    if not model:
        raise ValueError("Anthropic GEPA LM: model id must be non-empty.")

    mt = int(max_tokens) if max_tokens is not None else _DEFAULT_MAX
    thinking, mt2 = _thinking_budget(reasoning_effort, mt)

    api_messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_text},
    ]
    kw: dict[str, Any] = {
        "model": model,
        "max_tokens": mt2,
        "system": "",
        "messages": api_messages,
    }
    if thinking is not None:
        kw["thinking"] = thinking
    else:
        kw["temperature"] = 0.3 if temperature is None else float(temperature)

    max_attempts = 6
    last_err: BaseException | None = None
    used_key: str | None = None
    client: anthropic.Anthropic | None = None
    for attempt in range(max_attempts):
        try:
            api_key = get_anthropic_api_key()
            if not api_key:
                raise ValueError("Set ANTHROPIC_API_KEY for Anthropic GEPA LM.")
            if used_key != api_key:
                used_key = api_key
                client = anthropic.Anthropic(api_key=api_key)
            assert client is not None
            ak = mask_secret(api_key)
            message = sync_anthropic_messages_with_logfire(
                agent_name="gepa_reflection",
                model=model,
                system_text="",
                api_messages=list(api_messages),
                request_extras={
                    "thinking": thinking,
                    "temperature": kw.get("temperature"),
                },
                create_fn=lambda: client.messages.create(**kw),
                api_key_masked=ak if ak else None,
                io_phase="gepa_anthropic",
            )
            parts: list[str] = []
            for block in getattr(message, "content", None) or []:
                if getattr(block, "type", None) == "text":
                    parts.append(getattr(block, "text", "") or "")
            return "".join(parts).strip()
        except Exception as e:
            last_err = e
            rotated = maybe_rotate_after_provider_error("anthropic", e)
            if rotated or attempt < max_attempts - 1:
                if attempt < max_attempts - 1:
                    used_key = None
                    time.sleep(min(2**attempt, 8))
                    continue
            raise
    raise last_err  # type: ignore[misc]


def make_anthropic_gepa_lm(
    model: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> LanguageModel:
    def lm(prompt: str | list[dict[str, Any]]) -> str:
        return anthropic_generate_user_text(
            model,
            _prompt_to_user_text(prompt),
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    return lm
