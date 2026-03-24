"""GEPA :class:`~gepa.proposer.reflective_mutation.base.LanguageModel` via ``google.genai`` (not LiteLLM).

After ``instrument_logfire_gemini()``, proposal / seed-generation / diagnosis calls use the same
``Models.generate_content`` entrypoint as the solo Gemini agent, so Logfire records
native request/response shape, thought parts, and usage.

Usage: pass the return value of :func:`make_genai_gepa_lm` as ``ReflectionConfig.reflection_lm``
(a callable, not a model string) so ``optimize_anything`` does not wrap it with
``make_litellm_lm``.

``reflection_llm_backend: genai`` in ``gepa_retail_mermaid`` YAML selects this path.
"""

from __future__ import annotations

import time
import re
from typing import Any

from gepa.proposer.reflective_mutation.base import LanguageModel

from agent.api_key_rotation import (
    get_gemini_api_key,
    mask_secret,
    maybe_rotate_after_provider_error,
)
from agent.gemini_log import log_gemini_generate_io, to_jsonable


def _visible_text_from_response(response: Any) -> str:
    """Model-visible text only (omit ``Part`` entries with ``thought=True``)."""
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return ""
    content = getattr(cands[0], "content", None)
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


def _prompt_to_user_text(prompt: str | list[dict[str, Any]]) -> str:
    """Single-turn user text for reflection (GEPA normally passes a string)."""
    if isinstance(prompt, str):
        return prompt
    chunks: list[str] = []
    for m in prompt:
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str):
            chunks.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                else:
                    raise ValueError(
                        "GenAI GEPA LM supports text-only prompts. "
                        "Use reflection_llm_backend: litellm for multimodal reflection, "
                        "or extend agent/genai_gepa_lm.py."
                    )
            chunks.append(f"[{role}]\n" + "\n".join(text_parts))
        else:
            chunks.append(f"[{role}]\n{content!s}")
    return "\n\n".join(chunks)


def _extract_gepa_system_and_first_user_text(prompt_text: str) -> tuple[str, str]:
    """Extract system prompt + first user message from GEPA-tagged prompt text."""
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

    if sys_m:
        cleaned = prompt_text.replace(sys_m.group(0), "").strip()
        return system_text, cleaned

    return "", prompt_text.strip()


def genai_generate_user_text(
    model: str,
    user_text: str,
    *,
    system_instruction: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    include_thoughts_when_no_level: bool = True,
    io_phase: str = "gepa_genai",
    vertex_ai: bool = False,
) -> str:
    """Single user-message ``generate_content``; returns visible (non-thought) text.

    Used by GEPA reflection (:func:`make_genai_gepa_lm`) and retail failure diagnosis so both go
    through ``instrument_logfire_gemini``-patched ``generate_content``.

    ``io_phase`` is passed to :func:`agent.gemini_log.log_gemini_generate_io` (``TAU2_GEMINI_DUMP_PATH``
    / Logfire) alongside the raw request/response.
    """
    model = model.strip()
    if not model:
        raise ValueError("GenAI: model id must be non-empty.")
    from google import genai
    from google.genai import types

    gen_kw: dict[str, Any] = {}
    # Explicitly disable built-in tool/citation paths for reflection/diagnosis calls.
    gen_kw["tools"] = []
    if system_instruction is not None and str(system_instruction).strip():
        # Gemini SDK supports passing system instructions via config.
        gen_kw["system_instruction"] = str(system_instruction).strip()
    if temperature is not None:
        gen_kw["temperature"] = float(temperature)
    if max_output_tokens is not None:
        gen_kw["max_output_tokens"] = int(max_output_tokens)

    tc = _thinking_config_for_reasoning_effort(reasoning_effort)
    if tc is None and include_thoughts_when_no_level:
        try:
            tc = types.ThinkingConfig(include_thoughts=True)
        except Exception:
            tc = None
    if tc is not None:
        gen_kw["thinking_config"] = tc

    config = types.GenerateContentConfig(**gen_kw) if gen_kw else types.GenerateContentConfig()
    contents = [types.Content(role="user", parts=[types.Part(text=user_text)])]

    max_attempts = 6
    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            api_key = get_gemini_api_key("gepa")
            if not api_key.strip():
                raise ValueError("Set GOOGLE_API_KEY or GEMINI_API_KEY for GenAI GEPA LM.")
            api_key_masked = mask_secret(api_key) or None
            client = genai.Client(
                vertexai=bool(vertex_ai),
                api_key=api_key.strip(),
            )
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            log_gemini_generate_io(
                model=model,
                tool_round=0,
                contents=to_jsonable(contents),
                config=to_jsonable(config),
                response=resp,
                phase=io_phase,
                api_key_masked=api_key_masked,
            )
            return _visible_text_from_response(resp)
        except Exception as e:
            last_err = e
            rotated = maybe_rotate_after_provider_error("gemini", e)
            if rotated or attempt < max_attempts - 1:
                if attempt < max_attempts - 1:
                    time.sleep(min(2**attempt, 8))
                    continue
            raise
    raise last_err  # type: ignore[misc]


def _thinking_config_for_reasoning_effort(reasoning_effort: str | None) -> Any:
    from google.genai import types

    if not reasoning_effort or not str(reasoning_effort).strip():
        return None
    lvl = str(reasoning_effort).strip().lower()
    level_map = {
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
        "minimal": types.ThinkingLevel.MINIMAL,
    }
    tl = level_map.get(lvl)
    if tl is None:
        return None
    try:
        return types.ThinkingConfig(thinking_level=tl, include_thoughts=True)
    except Exception:
        try:
            return types.ThinkingConfig(thinking_level=tl)
        except Exception:
            return None


def make_genai_gepa_lm(
    model: str,
    *,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    include_thoughts_when_no_level: bool = True,
    io_phase: str = "gepa_reflection",
    vertex_ai: bool = False,
) -> LanguageModel:
    """Build a callable ``(prompt: str | list[dict]) -> str`` for GEPA reflection / seed generation.

    Args:
        model: Gemini API model id (e.g. ``gemini-2.0-flash``, ``gemini-3-flash-preview``).
        temperature: Passed to :class:`google.genai.types.GenerateContentConfig` when set.
        max_output_tokens: Maps to ``max_output_tokens`` on the config when set.
        reasoning_effort: Same keywords as solo agent (``low`` / ``medium`` / ``high`` / ``minimal``);
            enables thinking config with ``include_thoughts=True`` when supported.
        include_thoughts_when_no_level: If True and ``reasoning_effort`` is unset, try
            ``ThinkingConfig(include_thoughts=True)`` alone so summaries appear in Logfire when the API allows.
        io_phase: Label for :func:`agent.gemini_log.log_gemini_generate_io` (file + Logfire raw I/O).
    """
    model = model.strip()
    if not model:
        raise ValueError("GenAI GEPA LM: model id must be non-empty.")

    def lm(prompt: str | list[dict[str, Any]]) -> str:
        user_text = _prompt_to_user_text(prompt)
        system_text, extracted_user_text = _extract_gepa_system_and_first_user_text(user_text)
        if extracted_user_text:
            user_text = extracted_user_text
        return genai_generate_user_text(
            model,
            user_text,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            include_thoughts_when_no_level=include_thoughts_when_no_level,
            system_instruction=system_text or None,
            io_phase=io_phase,
            vertex_ai=vertex_ai,
        )

    return lm
