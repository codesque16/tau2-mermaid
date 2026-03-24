"""GEPA :class:`~gepa.proposer.reflective_mutation.base.LanguageModel` via OpenAI Responses API (native SDK).

Uses the same Logfire span shape so reflection calls show up like solo OpenAI turns.

``reflection_llm_backend: openai`` in ``gepa_retail_mermaid`` YAML selects this path.
"""

from __future__ import annotations

import time
import re
from typing import Any

from gepa.proposer.reflective_mutation.base import LanguageModel

from agent.api_key_rotation import (
    get_openai_api_key,
    mask_secret,
    maybe_rotate_after_provider_error,
)
from agent.logfire_native_llm import sync_openai_responses_with_logfire


def _openai_is_gpt5(model: str) -> bool:
    return (model or "").strip().lower().startswith("gpt-5")


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


def _extract_gepa_system_and_first_user_text(prompt_text: str) -> tuple[str, str]:
    """Extract system prompt + first user message from GEPA-tagged prompt text.

    Expected tags (inserted by `reflection_prompts_md.py`):
      - `<gepa_system_prompt> ... </gepa_system_prompt>`
      - `<gepa_first_user_message> ... </gepa_first_user_message>`
    """
    if not prompt_text:
        return "", ""

    sys_m = re.search(
        r"<gepa_system_prompt>(.*?)</gepa_system_prompt>",
        prompt_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    user_m = re.search(
        r"<gepa_first_user_message>(.*?)</gepa_first_user_message>",
        prompt_text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    system_text = (sys_m.group(1).strip() if sys_m else "").strip()
    if user_m:
        user_text = user_m.group(1).strip()
        return system_text, user_text

    # Fallback: remove the system tag (if present) and treat the remainder as user text.
    if sys_m:
        cleaned = prompt_text.replace(sys_m.group(0), "").strip()
        return system_text, cleaned

    return "", prompt_text.strip()


def openai_generate_user_text(
    model: str,
    user_text: str,
    *,
    system_text: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Single user-message chat completion; returns assistant ``content`` string."""
    from openai import OpenAI

    model = model.strip()
    if not model:
        raise ValueError("OpenAI GEPA LM: model id must be non-empty.")

    def _omit_temperature(m: str) -> bool:
        return _openai_is_gpt5(m)

    # Responses API: use `input` + optional `reasoning` object.
    req_kw: dict[str, Any] = {
        "model": model,
        # Explicitly disable built-in tool/citation paths for reflection calls.
        "tools": [],
        "input": user_text,
    }
    if system_text is not None and str(system_text).strip():
        # OpenAI Responses API supports top-level "instructions" for system guidance.
        req_kw["instructions"] = str(system_text).strip()
    if temperature is not None and not _omit_temperature(model):
        req_kw["temperature"] = float(temperature)
    if max_tokens is not None:
        # Responses API uses `max_output_tokens`.
        req_kw["max_output_tokens"] = int(max_tokens)
    if reasoning_effort is not None and str(reasoning_effort).strip() and _openai_is_gpt5(model):
        req_kw["reasoning"] = {
            "effort": str(reasoning_effort).strip(),
            "summary": "detailed",
        }
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
            resp = sync_openai_responses_with_logfire(
                agent_name="gepa_reflection",
                model=model,
                request_kwargs=req_kw,
                create_fn=lambda: client.responses.create(**req_kw),
                api_key_masked=ak if ak else None,
                io_phase="openai_responses",
            )

            return (getattr(resp, "output_text", None) or "").strip()
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
        pt = _prompt_to_user_text(prompt)
        system_text, user_text = _extract_gepa_system_and_first_user_text(pt)
        return openai_generate_user_text(
            model,
            user_text,
            system_text=system_text or None,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    return lm
