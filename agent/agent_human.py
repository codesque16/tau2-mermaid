"""Human agent: orchestrator prompts for user input from the terminal."""

import asyncio
from typing import Any, Callable, Awaitable

from .base import BaseAgent
from .config import AgentConfig


def _read_line(prompt: str) -> str:
    """Read a line from stdin (run in thread to avoid blocking event loop)."""
    return input(prompt).strip()


class HumanAgent(BaseAgent):
    """
    Agent that reads responses from the terminal. When the orchestrator calls
    respond_stream, the human is prompted for input; that input is returned
    as the reply. No model or API; usage/cost are zero. Use assistant_agent_type
    or user_agent_type: human in config.
    """
    use_streaming_display = False

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Prompt for human input and return it as the reply."""
        prompt = f"[{self.name} - Your response]: "
        reply = await asyncio.to_thread(_read_line, prompt)

        if on_chunk is not None:
            await on_chunk("text", reply)

        usage_info = {"usage": {}, "cost": 0.0}
        return reply, usage_info
