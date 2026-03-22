"""GEPA :class:`~gepa.proposer.reflective_mutation.base.LanguageModel` via OpenAI ``chat.completions`` (native SDK).

Uses the same Logfire span shape as :mod:`agent.logfire_native_llm` so reflection calls show up like solo OpenAI turns.

``reflection_llm_backend: openai`` in ``gepa_retail_mermaid`` YAML selects this path.
"""

from __future__ import annotations

import time
from typing import Any

from gepa.proposer.reflective_mutation.base import LanguageModel

from agent.api_key_rotation import (
    get_openai_api_key,
    mask_secret,
    maybe_rotate_after_provider_error,
)


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


def openai_generate_user_text(
    model: str,
    user_text: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Single user-message chat completion; returns assistant ``content`` string."""
    from openai import OpenAI

    from agent.logfire_native_llm import sync_openai_chat_with_logfire

    model = model.strip()
    if not model:
        raise ValueError("OpenAI GEPA LM: model id must be non-empty.")
    messages = [{"role": "user", "content": user_text}]
    kw: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        kw["temperature"] = float(temperature)
    if max_tokens is not None:
        kw["max_tokens"] = int(max_tokens)
    if reasoning_effort is not None and str(reasoning_effort).strip():
        kw["reasoning_effort"] = str(reasoning_effort).strip()

    extras = {k: v for k, v in kw.items() if k not in ("model", "messages")}
    max_attempts = 6
    last_err: BaseException | None = None
    used_key: str | None = None
    client: OpenAI | None = None
    for attempt in range(max_attempts):
        try:
            api_key = get_openai_api_key()
            if not api_key:
                raise ValueError("Set OPENAI_API_KEY for OpenAI GEPA LM.")
            if used_key != api_key:
                used_key = api_key
                client = OpenAI(api_key=api_key)
            assert client is not None
            ak = mask_secret(api_key)
            completion = sync_openai_chat_with_logfire(
                agent_name="gepa_reflection",
                model=model,
                request_messages=list(messages),
                request_extras=extras,
                create_fn=lambda: client.chat.completions.create(**kw),
                api_key_masked=ak if ak else None,
                io_phase="gepa_openai",
            )
            msg = completion.choices[0].message if completion.choices else None
            return (getattr(msg, "content", None) or "").strip() if msg else ""
        except Exception as e:
            last_err = e
            rotated = maybe_rotate_after_provider_error("openai", e)
            if rotated or attempt < max_attempts - 1:
                if attempt < max_attempts - 1:
                    used_key = None
                    time.sleep(min(2**attempt, 8))
                    continue
            raise
    raise last_err  # type: ignore[misc]


def make_openai_gepa_lm(
    model: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> LanguageModel:
    """Callable ``(prompt: str | list[dict]) -> str`` for GEPA reflection / seed generation."""

    def lm(prompt: str | list[dict[str, Any]]) -> str:
        return openai_generate_user_text(
            model,
            _prompt_to_user_text(prompt),
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    return lm
