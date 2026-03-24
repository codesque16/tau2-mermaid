"""Configuration for LLM-backed agents."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AgentConfig:
    """Immutable config for agent behavior (system prompt, sampling, limits)."""

    system_prompt: str
    max_tokens: Optional[int] = None  # None = unbounded / use provider default
    temperature: float = 0.0
    reasoning_effort: Optional[str] = None  # None = no thinking; "low", "medium", "high" for Gemini etc.
    # Optional MCP server configs (per agent); structure mirrors YAML `mcps` blocks.
    mcps: Optional[List[Dict[str, Any]]] = None
    # Optional mermaid MCP(s): list of { graph, type, url, tools }; connect via HTTP, call load_graph(graph), expose tools.
    mermaid: Optional[List[Dict[str, Any]]] = None
    # Optional markdown file with per-tool descriptions/args (used to enrich LLM tool schemas).
    mcp_tools_markdown_path: Optional[str] = None
    # Optional deterministic seed for the underlying LLM, if supported.
    seed: Optional[int] = None
    # Optional Google GenAI transport switch (Gemini): use Vertex AI instead of API key endpoint.
    vertex_ai: Optional[bool] = None
