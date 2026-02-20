"""Configuration for LLM-backed agents."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    """Immutable config for agent behavior (system prompt, sampling, limits)."""

    system_prompt: str
    max_tokens: int = 4096
    temperature: float = 0.0
    reasoning_effort: str = "low"  # e.g. "low", "medium", "high" for Gemini etc.
