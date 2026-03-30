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
    # Optional Vertex dedicated endpoint mode (chatCompletions request format over :predict).
    # When set, GeminiAgent uses the dedicated endpoint instead of google.genai generate_content().
    vertex_endpoint_id: Optional[str] = None
    vertex_project: Optional[str] = None
    vertex_location: Optional[str] = None
    # Extra request fields merged into the chatCompletions instance (e.g. top_p, frequency_penalty).
    vertex_endpoint_parameters: Optional[Dict[str, Any]] = None
    # Dedicated endpoint host base, e.g.
    # https://mg-endpoint-....us-central1-<projectnum>.prediction.vertexai.goog
    # If omitted, falls back to env DEDICATED_ENDPOINT_DOMAIN, then
    # https://{vertex_endpoint_id}.{vertex_location}-{vertex_project}.prediction.vertexai.goog
    vertex_http_predict_base: Optional[str] = None
    # Path segment before projects/.../endpoints/...:predict (console sample uses v1).
    vertex_http_predict_api_version: Optional[str] = None
