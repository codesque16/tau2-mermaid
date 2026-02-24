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
    model: str
    max_turns: int
    stop_phrases: list[str]
    assistant: AgentConfig
    user: AgentConfig
    assistant_model: str
    user_model: str
    initial_message: str | None = None
    assistant_agent_type: str | None = None
    user_agent_type: str | None = None
    assistant_agent_name: str | None = None
    user_agent_name: str | None = None
    mcp_server_url: str | None = None
    graph_id: str | None = None


def _normalize_agent_type(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if s else None


def _agent_config_from_block(block: dict | None) -> AgentConfig:
    """Build AgentConfig from an assistant/user block."""
    if not block:
        return AgentConfig(system_prompt="")
    return AgentConfig(
        system_prompt=block.get("system_prompt", ""),
        temperature=block.get("temperature", 0.7),
        max_tokens=block.get("max_tokens", 1024),
    )


def load_simulation_config(path: Path) -> SimulationConfig:
    """Load simulation YAML. Structure:
    - model: default for both roles (optional if each block has model)
    - max_turns, stop_phrases?, initial_message?, mcp_server_url?, graph_id?
    - assistant: agent_type, agent_name?, model?, system_prompt?, temperature?, max_tokens?
    - user: agent_type, agent_name?, model?, system_prompt?, temperature?, max_tokens?
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    asst = data.get("assistant") or {}
    user = data.get("user") or {}

    default_model = data.get("model") or ""

    return SimulationConfig(
        model=default_model,
        max_turns=data["max_turns"],
        stop_phrases=data.get("stop_phrases") or [],
        initial_message=data.get("initial_message"),
        assistant=_agent_config_from_block(asst),
        user=_agent_config_from_block(user),
        assistant_model=asst.get("model") or default_model,
        user_model=user.get("model") or default_model,
        assistant_agent_type=_normalize_agent_type(asst.get("agent_type")),
        user_agent_type=_normalize_agent_type(user.get("agent_type")),
        assistant_agent_name=(asst.get("agent_name") or "").strip() or None,
        user_agent_name=(user.get("agent_name") or "").strip() or None,
        mcp_server_url=data.get("mcp_server_url") or None,
        graph_id=data.get("graph_id") or None,
    )
