"""Gemini-backed agent; same interface as BaseAgent (respond_stream returns (text, usage_info))."""

import asyncio
from typing import Any, Callable, Awaitable

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_gemini_response

# Gemini API expects exactly these roles in contents; we keep them in history to avoid mapping bugs.
GEMINI_ROLE_USER = "user"
GEMINI_ROLE_MODEL = "model"


def _get_client():
    import os
    from google import genai

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    if not api_key.strip():
        raise ValueError("Set GOOGLE_API_KEY or GEMINI_API_KEY for Gemini models.")
    return genai.Client(api_key=api_key.strip())


def _content_to_text(content: Any) -> str:
    """Extract text from a Gemini Candidate content (Content with parts)."""
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


async def _with_retry(generate_fn: Callable[[], tuple[str, Any]], max_attempts: int = 3) -> tuple[str, Any]:
    """Run generate in a thread and retry on transient errors."""
    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await asyncio.to_thread(generate_fn)
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
    raise last_err  # type: ignore[misc]


class GeminiAgent(BaseAgent):
    """Gemini-backed agent (chat). Same interface as BaseAgent."""

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self._client: Any = None
        self.history: list[dict[str, Any]] = []

    def _get_client(self):
        if self._client is None:
            self._client = _get_client()
        return self._client

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Generate reply via Gemini. Returns (full_text, usage_info). Calls on_chunk with full text when done."""
        # Append the other party's message with Gemini's "user" role (what they said to us).
        self.history.append({"role": GEMINI_ROLE_USER, "content": incoming})

        def _generate() -> tuple[str, Any]:
            from google.genai import types

            client = self._get_client()
            # Build contents using API role names directly; history already uses GEMINI_ROLE_*.
            contents = []
            for m in self.history:
                role = m["role"]  # "user" or "model" only
                parts = [types.Part(text=m["content"])]
                contents.append(types.Content(role=role, parts=parts))
            gen_config = types.GenerateContentConfig(
                system_instruction=self.config.system_prompt,
                temperature=self.config.temperature,
                max_output_tokens=self.config.max_tokens,
            )
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=gen_config,
            )
            text = _content_to_text(
                response.candidates[0].content if response.candidates else None
            )
            return text, response

        text, response = await _with_retry(_generate)
        # Append our reply with Gemini's "model" role so next turn has correct user/model alternation.
        self.history.append({"role": GEMINI_ROLE_MODEL, "content": text})

        if on_chunk is not None:
            await on_chunk("text", text)

        usage = usage_from_gemini_response(response)
        cost = compute_cost(self.model, usage) if usage else 0.0
        usage_info = {"usage": usage, "cost": cost}
        return text, usage_info
