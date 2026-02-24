"""Utilities for loading mermaid-agent folder structure (AGENTS.md or index.md, agent-mermaid.md, nodes/*/index.md)."""

import re
from pathlib import Path
from typing import Any


AGENTS_MD = "AGENTS.md"
INDEX_MD = "index.md"


def parse_agents_md(content: str) -> dict[str, Any]:
    """Parse AGENTS.md content into frontmatter, mermaid, node_prompts, rest_md."""
    frontmatter = ""
    rest_md = ""
    mermaid = ""
    node_prompts: dict[str, str] = {}

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
            parts = re.split(r"\n###\s+", prompts_section, flags=re.IGNORECASE)
            for i, block in enumerate(parts):
                block = block.strip()
                if not block:
                    continue
                first_line = block.split("\n")[0].strip()
                if not first_line:
                    continue
                if i == 0 and first_line.lower() == "node prompts":
                    continue
                node_id = first_line
                prompt_content = "\n".join(block.split("\n")[1:]).strip()
                node_prompts[node_id] = prompt_content

    return {
        "frontmatter": frontmatter,
        "rest_md": rest_md,
        "mermaid": mermaid,
        "node_prompts": node_prompts,
    }


def compose_agents_md(
    frontmatter: str, rest_md: str, mermaid: str, node_prompts: dict[str, str]
) -> str:
    """Compose full AGENTS.md content from parts (used by viewer save)."""
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
    for node_id, prompt in sorted(node_prompts.items()):
        out.append(f"### {node_id}")
        out.append("")
        out.append(prompt.strip())
        out.append("")
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


def load_sop_markdown(agent_dir: Path) -> dict[str, str] | None:
    """Load SOP from AGENTS.md or retail-agent-sop.md (or *-agent-sop.md). Returns {"prose": str, "mermaid": str} or None."""
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        prose = parsed["frontmatter"]
        if parsed["rest_md"]:
            prose = f"{prose}\n\n{parsed['rest_md']}" if prose else parsed["rest_md"]
        return {"prose": prose.strip(), "mermaid": parsed.get("mermaid", "") or ""}
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
            return {"prose": text[: match.start()].strip(), "mermaid": match.group(1).strip()}
        return {"prose": text, "mermaid": ""}
    return None


def list_mermaid_nodes(agent_dir: Path) -> list[str]:
    """List node IDs from AGENTS.md Node Prompts or from nodes/ subdirs that have index.md."""
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        return sorted(parsed.get("node_prompts", {}).keys())
    nodes_dir = agent_dir / "nodes"
    if not nodes_dir.is_dir():
        return []
    return [
        d.name
        for d in sorted(nodes_dir.iterdir())
        if d.is_dir() and (d / INDEX_MD).is_file()
    ]


def get_node_instructions(agent_dir: Path, node_id: str) -> str:
    """Load node instructions from AGENTS.md Node Prompts or from nodes/<node_id>/index.md."""
    agents_md = agent_dir / AGENTS_MD
    if agents_md.is_file():
        parsed = parse_agents_md(agents_md.read_text(encoding="utf-8"))
        prompts = parsed.get("node_prompts", {})
        if node_id in prompts:
            return prompts[node_id]
        return f"[Error: node '{node_id}' not found in AGENTS.md Node Prompts]"
    node_path = agent_dir / "nodes" / node_id / INDEX_MD
    if not node_path.is_file():
        return f"[Error: node '{node_id}' not found or has no {INDEX_MD} at {node_path}]"
    return node_path.read_text(encoding="utf-8").strip()
