"""OpenAI (GPT) agent; same interface as BaseAgent with streaming."""

import os
from typing import Any, Callable, Awaitable

from openai import AsyncOpenAI

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_openai_response

DEFAULT_REQUEST_TIMEOUT = 300.0


class OpenAIAgent(BaseAgent):
    """OpenAI chat completions agent. Set OPENAI_API_KEY in env or .env."""

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY for OpenAI models.")
        self.client = AsyncOpenAI(api_key=api_key, timeout=DEFAULT_REQUEST_TIMEOUT)
        self.history: list[dict[str, Any]] = []

    def _messages_for_api(self) -> list[dict[str, str]]:
        out = []
        for m in self.history:
            out.append({"role": m["role"], "content": m["content"]})
        return out

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Non-streaming request so usage is always returned; calls on_chunk once with full text."""
        self.history.append({"role": "user", "content": incoming})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.config.system_prompt},
                *self._messages_for_api(),
            ],
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )

        content = response.choices[0].message.content if response.choices else ""
        full_text = content or ""
        self.history.append({"role": "assistant", "content": full_text})

        if on_chunk is not None:
            await on_chunk("text", full_text)

        usage_dict = usage_from_openai_response(response.usage)
        cost = compute_cost(self.model, usage_dict) if usage_dict else 0.0
        usage_info = {"usage": usage_dict, "cost": cost}
        return full_text, usage_info
