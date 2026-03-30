from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class AgentConfig:
    system_prompt: str
    temperature: float = 0.7
    max_tokens: int | None = None  # None = unbounded / use provider default
    mcps: list[dict] | None = None
    mermaid: list[dict] | None = None  # Mermaid MCP(s): [{ graph, type, url, tools }]
    mcp_tools_markdown_path: str | None = None
    reasoning_effort: str | None = None  # None = no thinking; "low", "medium", "high" for Gemini etc.
    vertex_ai: bool | None = None
    vertex_endpoint_id: str | None = None
    vertex_project: str | None = None
    vertex_location: str | None = None
    vertex_endpoint_parameters: dict | None = None
    vertex_http_predict_base: str | None = None
    vertex_http_predict_api_version: str | None = None


@dataclass
class SimulationConfig:
    model: str
    max_turns: int
    stop_phrases: list[str]
    assistant: AgentConfig
    user: AgentConfig
    assistant_model: str
    user_model: str
    # Optional mode for higher-level orchestration, e.g. "conversation" vs "solo"
    mode: str | None = None
    initial_message: str | None = None
    assistant_agent_type: str | None = None
    user_agent_type: str | None = None
    assistant_agent_name: str | None = None
    user_agent_name: str | None = None
    mcp_server_url: str | None = None
    graph_id: str | None = None
    # When True, a user message that is exactly "stop" (case-insensitive) ends the run
    # (in addition to ``stop_phrases``). Checked on user turns only.
    stop_on_user_stop_word: bool = False
    # tau2-bench style: when ``initial_message`` is unset, seed with this assistant line (no LLM), then user speaks.
    first_agent_message: str | None = None


def _normalize_agent_type(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if s else None


def _resolve_system_prompt(block: dict, *, base_dir: Path) -> str:
    """Inline ``system_prompt``, optional ``system_prompt_file`` (relative to the YAML directory).

    If both are set, the file is loaded first and the inline text is appended inside ``<scenario>``…
    (same pattern as tau2 user simulator: global guidelines + scenario instructions).
    """
    inline = str(block.get("system_prompt", "") or "").strip()
    sp_file = block.get("system_prompt_file")
    from_file = ""
    if sp_file:
        p = Path(str(sp_file).strip())
        if not p.is_absolute():
            p = base_dir / p
        from_file = p.read_text(encoding="utf-8").strip()
    if from_file and inline:
        return f"{from_file}\n\n<scenario>\n{inline}\n</scenario>"
    return from_file or inline


def _agent_config_from_block(block: dict | None, *, base_dir: Path) -> AgentConfig:
    """Build AgentConfig from an assistant/user block."""
    if not block:
        return AgentConfig(system_prompt="")
    return AgentConfig(
        system_prompt=_resolve_system_prompt(block, base_dir=base_dir),
        temperature=block.get("temperature", 0.7),
        max_tokens=block.get("max_tokens"),  # omit or null = unbounded
        mcps=block.get("mcps") or None,
        mermaid=block.get("mermaid") or None,
        mcp_tools_markdown_path=block.get("mcp_tools_markdown_path"),
        reasoning_effort=block.get("reasoning_effort"),
        vertex_ai=block.get("vertex_ai"),
        vertex_endpoint_id=block.get("vertex_endpoint_id"),
        vertex_project=block.get("vertex_project"),
        vertex_location=block.get("vertex_location"),
        vertex_endpoint_parameters=block.get("vertex_endpoint_parameters"),
        vertex_http_predict_base=block.get("vertex_http_predict_base"),
        vertex_http_predict_api_version=block.get("vertex_http_predict_api_version"),
    )


def load_simulation_config(path: Path) -> SimulationConfig:
    """Load simulation YAML. Structure:
    - model: default for both roles (optional if each block has model)
    - max_turns, stop_phrases?, initial_message?, mcp_server_url?, graph_id?
    - assistant: agent_type, agent_name?, model?, system_prompt? | system_prompt_file?, temperature?, max_tokens?
    - user: agent_type, agent_name?, model?, system_prompt? | system_prompt_file?, temperature?, max_tokens?
    """
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    asst = data.get("assistant") or {}
    user = data.get("user") or {}
    base_dir = path.parent

    default_model = data.get("model") or ""

    return SimulationConfig(
        model=default_model,
        max_turns=data["max_turns"],
        stop_phrases=data.get("stop_phrases") or [],
        initial_message=data.get("initial_message"),
        assistant=_agent_config_from_block(asst, base_dir=base_dir),
        user=_agent_config_from_block(user, base_dir=base_dir),
        assistant_model=asst.get("model") or default_model,
        user_model=user.get("model") or default_model,
        assistant_agent_type=_normalize_agent_type(asst.get("agent_type")),
        user_agent_type=_normalize_agent_type(user.get("agent_type")),
        assistant_agent_name=(asst.get("agent_name") or "").strip() or None,
        user_agent_name=(user.get("agent_name") or "").strip() or None,
        mcp_server_url=data.get("mcp_server_url") or None,
        graph_id=data.get("graph_id") or None,
        mode=(data.get("mode") or None),
        stop_on_user_stop_word=bool(data.get("stop_on_user_stop_word", False)),
        first_agent_message=(
            str(data["first_agent_message"]).strip() if data.get("first_agent_message") else None
        ),
    )
