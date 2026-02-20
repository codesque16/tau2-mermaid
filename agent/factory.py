"""Factory for creating agents by type."""

from typing import Any, Literal

from .base import BaseAgent
from .config import AgentConfig
from .agent_anthropic import AnthropicAgent
from .agent_gemini import GeminiAgent
from .agent_openai import OpenAIAgent
from .agent_litellm import LiteLLMAgent
from .agent_human import HumanAgent
from .agent_mermaid import MermaidAgent

AgentType = Literal["anthropic", "gemini", "openai", "litellm", "human", "mermaid"]


def create_agent(
    agent_type: AgentType,
    name: str,
    config: AgentConfig,
    model: str,
    **kwargs: Any,
) -> BaseAgent:
    """Create an agent instance by type. Raises ValueError for unknown type.
    For agent_type='mermaid', pass agent_name=str and optionally mermaid_agents_root=Path|str.
    """
    if agent_type == "anthropic":
        return AnthropicAgent(name=name, config=config, model=model)
    if agent_type == "gemini":
        return GeminiAgent(name=name, config=config, model=model)
    if agent_type == "openai":
        return OpenAIAgent(name=name, config=config, model=model)
    if agent_type == "litellm":
        return LiteLLMAgent(name=name, config=config, model=model)
    if agent_type == "human":
        return HumanAgent(name=name, config=config, model=model)
    if agent_type == "mermaid":
        agent_name = kwargs.get("agent_name")
        if not agent_name:
            raise ValueError("agent_type='mermaid' requires agent_name=... (e.g. agent_name='airline')")
        return MermaidAgent(
            name=name,
            config=config,
            model=model,
            agent_name=agent_name,
            mermaid_agents_root=kwargs.get("mermaid_agents_root"),
        )
    raise ValueError(
        f"Unknown agent type: {agent_type!r}. Use one of: anthropic, gemini, openai, litellm, human, mermaid"
    )
