"""Print load_sop_markdown output for the retail AGENTS.md.

Run from repo root (with project venv activated so agent/ and deps are available):
  python -m agent.agent_mermaid.tests.test_load_sop_retail
"""

import json
from pathlib import Path

from agent.agent_mermaid.utils import load_sop_markdown

# Retail agent dir: mermaid-agents/retail
_AGENT_MERMAID_DIR = Path(__file__).resolve().parent.parent
RETAIL_AGENT_DIR = _AGENT_MERMAID_DIR / "mermaid-agents" / "retail"


def main() -> None:
    if not RETAIL_AGENT_DIR.is_dir():
        print(f"Retail agent dir not found: {RETAIL_AGENT_DIR}")
        return
    if not (RETAIL_AGENT_DIR / "AGENTS.md").is_file():
        print(f"AGENTS.md not found in {RETAIL_AGENT_DIR}")
        return

    result = load_sop_markdown(RETAIL_AGENT_DIR)
    if result is None:
        print("load_sop_markdown returned None")
        return

    print("=== load_sop_markdown(retail) ===\n")
    print("Keys:", list(result.keys()))
    print("\n--- prose (first 500 chars) ---")
    print((result.get("prose") or "")[:500])
    print("\n--- mermaid (first 800 chars) ---")
    print((result.get("mermaid") or "")[:800])
    print("\n--- node_prompts (raw YAML string, first 600 chars) ---")
    np_str = result.get("node_prompts") or ""
    print(f"Length: {len(np_str)} chars")
    print((np_str[:600] + "..." if len(np_str) > 600 else np_str) or "(empty)")

    # Full output as JSON (truncate long values for readability)
    def truncate(obj, max_len=400):
        if isinstance(obj, str) and len(obj) > max_len:
            return obj[:max_len] + "...[truncated]"
        if isinstance(obj, dict):
            return {k: truncate(v, max_len) for k, v in obj.items()}
        if isinstance(obj, list):
            return [truncate(x, max_len) for x in obj]
        return obj

    print("\n--- full result (long strings truncated) ---")
    print(json.dumps(truncate(result), indent=2, default=str))


if __name__ == "__main__":
    main()
