"""Parse and apply MCP tool documentation from markdown.

Expected shape:
  - "## <tool_name>" heading
  - a fenced block containing free text with optional "Args:" section
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_TOOL_SECTION_RE = re.compile(
    r"^##\s+([A-Za-z0-9_]+)\s*$([\s\S]*?)(?=^##\s+[A-Za-z0-9_]+\s*$|\Z)",
    flags=re.MULTILINE,
)
_FENCED_RE = re.compile(r"```([\s\S]*?)```", flags=re.MULTILINE)
_ARGS_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")


def _extract_doc_block(section_body: str) -> str:
    m = _FENCED_RE.search(section_body)
    if m:
        return m.group(1).strip()
    return section_body.strip()


def _split_description_and_args(doc: str) -> tuple[str, dict[str, str]]:
    lines = doc.splitlines()
    desc_lines: list[str] = []
    args: dict[str, str] = {}

    in_args = False
    current_arg: str | None = None
    for raw in lines:
        line = raw.rstrip()
        s = line.strip()
        if not in_args:
            if s == "Args:":
                in_args = True
                continue
            if s in {"Returns:", "Raises:"}:
                break
            desc_lines.append(line)
            continue

        if s in {"Returns:", "Raises:"}:
            break
        m = _ARGS_LINE_RE.match(line)
        if m:
            current_arg = m.group(1)
            args[current_arg] = m.group(2).strip()
            continue
        if current_arg and s:
            args[current_arg] = (args[current_arg] + " " + s).strip()

    desc = "\n".join(x for x in desc_lines).strip()
    return desc, args


def parse_tools_markdown(path: str | Path) -> dict[str, dict[str, Any]]:
    """Return mapping: tool_name -> {"description": str, "args": {arg: description}}."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    text = p.read_text(encoding="utf-8")

    out: dict[str, dict[str, Any]] = {}
    for m in _TOOL_SECTION_RE.finditer(text):
        tool_name = m.group(1).strip()
        block = _extract_doc_block(m.group(2))
        if not block:
            continue
        desc, args = _split_description_and_args(block)
        out[tool_name] = {"description": desc, "args": args}
    return out

