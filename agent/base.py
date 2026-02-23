"""Abstract base agent; all concrete agents implement this interface."""

from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

import logfire

from .config import AgentConfig


class BaseAgent(ABC):
    """
    Base agent interface. Subclasses must implement _do_respond_stream();
    respond() and respond_stream() are provided in terms of it with Logfire tracing.
    """

    # When False, orchestrator will not use a Live streaming display for this agent
    # (e.g. human agent so terminal input is visible while typing).
    use_streaming_display: bool = True

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        self.name = name
        self.config = config
        self.model = model

    async def respond(self, incoming: str) -> tuple[str, dict]:
        """Non-streaming response. Returns (full_text, usage_info)."""
        return await self.respond_stream(incoming, on_chunk=None)

    async def respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """
        Stream the response with Logfire tracing; delegates to _do_respond_stream.
        Returns (full_text, usage_info). usage_info: {"usage": {...}, "cost": float}.
        """
        # with logfire.span(
        #     "agent.respond_stream",
        #     agent=self.name,
        #     model=self.model,
        #     _span_name="agent.respond_stream",
        # ) as span:
        #     full_text, usage_info = await self._do_respond_stream(
        #         incoming, on_chunk=on_chunk
        #     )
        #     u = usage_info.get("usage") or {}
        #     span.set_attribute("input_tokens", u.get("input_tokens"))
        #     span.set_attribute("output_tokens", u.get("output_tokens"))
        #     if usage_info.get("cost") is not None:
        #         span.set_attribute("cost_usd", usage_info["cost"])
        #     return full_text, usage_info
        full_text, usage_info = await self._do_respond_stream(
            incoming, on_chunk=on_chunk
        )
        return full_text, usage_info

    @abstractmethod
    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """
        Implement streaming response. Returns (full_text, usage_info).
        usage_info: {"usage": {...}, "cost": float}.
        """
        ...
