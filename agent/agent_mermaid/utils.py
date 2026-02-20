"""Utilities for loading mermaid-agent folder structure (index.md, agent-mermaid.md, nodes/*/index.md)."""

from pathlib import Path


def get_mermaid_agent_dir(root: Path, agent_name: str) -> Path:
    """Return the directory for a named mermaid agent under the given root."""
    path = root / agent_name
    if not path.is_dir():
        raise FileNotFoundError(f"Mermaid agent directory not found: {path}")
    return path


def load_agent_system_prompt(agent_dir: Path) -> str:
    """Load the system prompt from index.md inside the agent directory."""
    index_path = agent_dir / "index.md"
    if not index_path.is_file():
        raise FileNotFoundError(f"Agent index.md not found: {index_path}")
    return index_path.read_text(encoding="utf-8").strip()


def load_agent_mermaid(agent_dir: Path) -> str:
    """Load the mermaid diagram from agent-mermaid.md (for visualization and traversal)."""
    mermaid_path = agent_dir / "agent-mermaid.md"
    if not mermaid_path.is_file():
        return ""
    return mermaid_path.read_text(encoding="utf-8").strip()


def list_mermaid_nodes(agent_dir: Path) -> list[str]:
    """List node IDs (subfolder names under nodes/) that have an index.md."""
    nodes_dir = agent_dir / "nodes"
    if not nodes_dir.is_dir():
        return []
    return [
        d.name
        for d in sorted(nodes_dir.iterdir())
        if d.is_dir() and (d / "index.md").is_file()
    ]


def get_node_instructions(agent_dir: Path, node_id: str) -> str:
    """Load task-specific instructions for a mermaid node from nodes/<node_id>/index.md."""
    node_path = agent_dir / "nodes" / node_id / "index.md"
    if not node_path.is_file():
        return f"[Error: node '{node_id}' not found or has no index.md at {node_path}]"
    return node_path.read_text(encoding="utf-8").strip()
