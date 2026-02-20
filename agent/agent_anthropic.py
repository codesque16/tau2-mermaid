"""Anthropic (Claude) agent; supports streaming and tool-use display."""

from typing import Any, Callable, Awaitable

import anthropic

from .base import BaseAgent
from .config import AgentConfig
from .utils.cost import compute_cost, usage_from_response

DEFAULT_REQUEST_TIMEOUT = 300.0


class AnthropicAgent(BaseAgent):
    """
    Wraps the Anthropic API. Both user and assistant agents use this class;
    behavioral differences come from the system prompt in AgentConfig.
    """

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self.client = anthropic.AsyncAnthropic(timeout=DEFAULT_REQUEST_TIMEOUT)
        self.history: list[dict[str, Any]] = []

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Stream the response and optionally call on_chunk(event_type, data) for each update."""
        self.history.append({"role": "user", "content": incoming})

        accumulated_text: list[str] = []

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=self.config.system_prompt,
            messages=self.history,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    delta = getattr(event, "text", "") or ""
                    if delta:
                        accumulated_text.append(delta)
                        if on_chunk is not None:
                            await on_chunk("text", "".join(accumulated_text))
                elif event.type == "content_block_stop" and on_chunk is not None:
                    block = getattr(event, "content_block", None)
                    if block is not None and getattr(block, "type", None) == "tool_use":
                        await on_chunk(
                            "tool_use",
                            {
                                "name": getattr(block, "name", ""),
                                "id": getattr(block, "id", ""),
                                "input": getattr(block, "input", {}),
                            },
                        )

            message = await stream.get_final_message()

        text_parts = [
            block.text
            for block in message.content
            if hasattr(block, "type") and block.type == "text"
        ]
        full_text = "".join(text_parts)
        self.history.append({"role": "assistant", "content": full_text})

        usage = usage_from_response(getattr(message, "usage", None))
        cost = compute_cost(self.model, usage) if usage else 0.0
        usage_info = {"usage": usage, "cost": cost}
        return full_text, usage_info
