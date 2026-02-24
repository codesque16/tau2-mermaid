"""Utilities for loading mermaid-agent folder structure (AGENTS.md or index.md, agent-mermaid.md, nodes/*/index.md)."""

import re
from pathlib import Path
from typing import Any

import yaml

AGENTS_MD = "AGENTS.md"
INDEX_MD = "index.md"


def parse_agents_md(content: str) -> dict[str, Any]:
    """Parse AGENTS.md content into frontmatter, mermaid, node_prompts (raw YAML string), rest_md.
    node_prompts is the raw YAML block content under ## Node Prompts; parsing to dict is done by the MCP server in load_graph.
    """
    frontmatter = ""
    rest_md = ""
    mermaid = ""
    node_prompts_yaml = ""

    fm_start = content.find("---")
    if fm_start >= 0:
        fm_end = content.find("---", fm_start + 3)
        if fm_end >= 0:
            frontmatter = content[fm_start : fm_end + 3].strip()
            content = content[fm_end + 3 :].lstrip("\n")
    body = content

    sop_header = "## SOP Flowchart"
    idx_sop = body.find(sop_header)
    if idx_sop >= 0:
        rest_md = body[:idx_sop].strip()
        body = body[idx_sop:]
    else:
        rest_md = body.strip()
        body = ""

    if body:
        mm_start = body.find("```mermaid")
        if mm_start >= 0:
            mm_start = body.find("\n", mm_start) + 1
            mm_end = body.find("```", mm_start)
            if mm_end >= 0:
                mermaid = body[mm_start:mm_end].strip()
        np_header = "## Node Prompts"
        idx_np = body.find(np_header)
        if idx_np >= 0:
            prompts_section = body[idx_np + len(np_header) :].strip()
            yaml_start = prompts_section.find("```yaml")
            if yaml_start >= 0:
                yaml_start = prompts_section.find("\n", yaml_start) + 1
                yaml_end = prompts_section.find("```", yaml_start)
                if yaml_end >= 0:
                    node_prompts_yaml = prompts_section[yaml_start:yaml_end].strip()

    return {
        "frontmatter": frontmatter,
        "rest_md": rest_md,
        "mermaid": mermaid,
        "node_prompts": node_prompts_yaml,
    }


def compose_agents_md(
    frontmatter: str,
    rest_md: str,
    mermaid: str,
    node_prompts: dict[str, dict[str, Any]],
) -> str:
    """Compose full AGENTS.md content from parts (used by viewer save).
    node_prompts: node_id -> {prompt, tools?, examples?} per MCP spec.
    """
    out = []
    if frontmatter:
        out.append(
            frontmatter if frontmatter.startswith("---") else f"---\n{frontmatter}\n---"
        )
        out.append("")
    if rest_md:
        out.append(rest_md.strip())
        out.append("")
    out.append("## SOP Flowchart")
    out.append("")
    out.append("```mermaid")
    out.append(mermaid.strip() if mermaid.strip() else "flowchart TD")
    out.append("```")
    out.append("")
    out.append("## Node Prompts")
    out.append("")
    normalized = {
        nid: {
            "prompt": (val.get("prompt") or "").strip(),
            "tools": val.get("tools") or [],
            "examples": val.get("examples") or [],
        }
        for nid, val in sorted(node_prompts.items())
        if isinstance(val, dict)
    }
    yaml_block = {"node_prompts": normalized}
    out.append("```yaml")
    out.append(yaml.dump(yaml_block, default_flow_style=False, allow_unicode=True).strip())
    out.append("```")
    return "\n".join(out).rstrip() + "\n"


def get_mermaid_agent_dir(root: Path, agent_name: str) -> Path:
    """Return the directory for a named mermaid agent under the given root."""
    path = root / agent_name
    if not path.is_dir():
        raise FileNotFoundError(f"Mermaid agent directory not found: {path}")
    return path


def load_agent_system_prompt(agent_dir: Path) -> str:
    """Load the system prompt from AGENTS.md or index.md inside the agent directory."""
    agents_md = agent_dir / AGENTS_MD
    index_path = agent_dir / INDEX_MD
    if agents_md.is_file():
        return agents_md.read_text(encoding="utf-8").strip()
    if index_path.is_file():
        return index_path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Agent {AGENTS_MD} or {INDEX_MD} not found: {agent_dir}")


def load_agent_mermaid(agent_dir: Path) -> str:
    """Load the mermaid diagram from agent-mermaid.md or from AGENTS.md. Returns empty string if missing."""
    mermaid_path = agent_dir / "agent-mermaid.md"
    if mermaid_path.is_file():
        return mermaid_path.read_text(encoding="utf-8").strip()
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        return parsed.get("mermaid", "") or ""
    return ""


def load_sop_markdown(agent_dir: Path) -> dict[str, Any] | None:
    """Load SOP from AGENTS.md or retail-agent-sop.md (or *-agent-sop.md).
    Returns {"prose": str, "mermaid": str, "node_prompts": str} or None. node_prompts is raw YAML string.
    """
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        prose = parsed["frontmatter"]
        if parsed["rest_md"]:
            prose = f"{prose}\n\n{parsed['rest_md']}" if prose else parsed["rest_md"]
        return {
            "prose": prose.strip(),
            "mermaid": parsed.get("mermaid", "") or "",
            "node_prompts": parsed.get("node_prompts", "") or "",
        }
    retail_sop = agent_dir / "retail-agent-sop.md"
    if retail_sop.is_file():
        candidates = [retail_sop]
    else:
        candidates = list(agent_dir.glob("*-agent-sop.md"))
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").strip()
        match = re.search(r"```mermaid\s*(.*?)```", text, re.DOTALL)
        if match:
            return {"prose": text[: match.start()].strip(), "mermaid": match.group(1).strip(), "node_prompts": ""}
        return {"prose": text, "mermaid": "", "node_prompts": ""}
    return None


def list_mermaid_nodes(agent_dir: Path) -> list[str]:
    """List node IDs from AGENTS.md Node Prompts YAML (parses raw node_prompts string)."""
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        np_str = parsed.get("node_prompts") or ""
        if not np_str:
            return []
        try:
            data = yaml.safe_load(np_str)
            np_data = (data or {}).get("node_prompts") or data
            return sorted(np_data.keys()) if isinstance(np_data, dict) else []
        except Exception:
            return []
    return []


def get_node_instructions(agent_dir: Path, node_id: str) -> str:
    """Load node prompt text from AGENTS.md Node Prompts YAML (parses raw node_prompts string)."""
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        np_str = parsed.get("node_prompts") or ""
        if np_str:
            try:
                data = yaml.safe_load(np_str)
                np_data = (data or {}).get("node_prompts") or data
                if isinstance(np_data, dict) and node_id in np_data:
                    entry = np_data[node_id]
                    if isinstance(entry, dict):
                        return (entry.get("prompt") or "").strip()
            except Exception:
                pass
        return f"[Error: node '{node_id}' not found in AGENTS.md Node Prompts]"
    return f"[Error: node '{node_id}' not found in AGENTS.md Node Prompts]"
