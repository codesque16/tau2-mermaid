from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class AgentConfig:
    system_prompt: str
    temperature: float = 0.7
    max_tokens: int = 1024


@dataclass
class SimulationConfig:
    model: str  # default for both roles when assistant_model/user_model not set
    max_turns: int
    stop_phrases: list[str]
    assistant: AgentConfig
    user: AgentConfig
    initial_message: str | None = None  # if missing/empty, assistant generates the first message
    assistant_model: str | None = None  # override model for assistant
    user_model: str | None = None  # override model for user
    assistant_agent_type: str | None = None  # e.g. anthropic, gemini, openai, litellm, human; else inferred from model
    user_agent_type: str | None = None
    assistant_agent_name: str | None = None  # required when assistant is mermaid (e.g. retail)
    user_agent_name: str | None = None
    mcp_server_url: str | None = None
    graph_id: str | None = None


def _normalize_agent_type(value: str | None) -> str | None:
    """Strip agent type from config so 'litellm   ' matches 'litellm'."""
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if s else None


def load_simulation_config(path: Path) -> SimulationConfig:
    """Load a single simulation YAML with model, run settings, and assistant/user agent configs."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return SimulationConfig(
        model=data["model"],
        max_turns=data["max_turns"],
        stop_phrases=data["stop_phrases"],
        initial_message=data.get("initial_message"),
        assistant=AgentConfig(**data["assistant"]),
        user=AgentConfig(**data["user"]),
        assistant_model=data.get("assistant_model"),
        user_model=data.get("user_model"),
        assistant_agent_type=_normalize_agent_type(data.get("assistant_agent_type")),
        user_agent_type=_normalize_agent_type(data.get("user_agent_type")),
        assistant_agent_name=data.get("assistant_agent_name") or None,
        user_agent_name=data.get("user_agent_name") or None,
        mcp_server_url=data.get("mcp_server_url") or None,
        graph_id=data.get("graph_id") or None,
    )
