"""LiteLLM agent: single client for OpenAI, Anthropic, Gemini, etc. via LiteLLM."""

from typing import Any, Callable, Awaitable

import litellm

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_openai_response


def _cost_model(model: str) -> str:
    """Use last segment for cost lookup (e.g. gemini/gemini-2.5-flash -> gemini-2.5-flash)."""
    return model.split("/")[-1] if "/" in model else model


class LiteLLMAgent(BaseAgent):
    """
    Agent using LiteLLM; supports any model (OpenAI, Anthropic, Gemini, etc.)
    via a single API. Set the corresponding API key in env (OPENAI_API_KEY,
    ANTHROPIC_API_KEY, GOOGLE_API_KEY / GEMINI_API_KEY, etc.).
    """

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
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
        """Call LiteLLM acompletion; returns (full_text, usage_info). Calls on_chunk once with full text."""
        # Explicit log so it's clear this turn uses LiteLLM with the configured model
        print(f"  Chat Completion with litellm:{self.model}")
        self.history.append({"role": "user", "content": incoming})

        messages = [
            {"role": "system", "content": self.config.system_prompt},
            *self._messages_for_api(),
        ]

        # Call via module so Logfire's instrument_litellm() patch is always used
        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            reasoning_effort=self.config.reasoning_effort,
        )

        content = ""
        if response.choices:
            msg = response.choices[0].message
            if hasattr(msg, "content") and msg.content:
                content = msg.content
        full_text = content or ""
        self.history.append({"role": "assistant", "content": full_text})

        if on_chunk is not None:
            await on_chunk("text", full_text)

        usage_dict = usage_from_openai_response(response.usage)
        cost = (
            compute_cost(_cost_model(self.model), usage_dict)
            if usage_dict
            else 0.0
        )
        usage_info = {"usage": usage_dict, "cost": cost}
        return full_text, usage_info
