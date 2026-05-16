"""Parse Mermaid flowchart TD into a structured graph.

We use a regex-based parser rather than spinning up the JS Mermaid library —
flowchart TD syntax is simple enough that this is robust and dependency-free.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class ParsedGraph:
    nodes: dict[str, dict] = field(default_factory=dict)  # node_id -> {shape, label}
    edges: list[tuple[str, str, str | None]] = field(default_factory=list)  # (from, to, condition)

    def neighbors(self, node_id: str) -> list[tuple[str, str | None]]:
        return [(t, c) for f, t, c in self.edges if f == node_id]

    def is_valid_edge(self, from_node: str, to_node: str) -> bool:
        return any(f == from_node and t == to_node for f, t, _ in self.edges)

    def is_valid_path(self, sequence: list[str]) -> tuple[bool, str]:
        """Check that consecutive node pairs form valid edges."""
        for i in range(len(sequence) - 1):
            a, b = sequence[i], sequence[i + 1]
            if a not in self.nodes:
                return False, f"Unknown node: {a}"
            if b not in self.nodes:
                return False, f"Unknown node: {b}"
            if not self.is_valid_edge(a, b):
                return False, f"No edge from {a} to {b}"
        return True, "ok"

    def shape_of(self, node_id: str) -> str | None:
        return self.nodes.get(node_id, {}).get("shape")


# Node patterns: order matters — match more specific shapes first
NODE_PATTERNS = [
    (r"^([A-Za-z_][A-Za-z0-9_]*)\(\[(.*?)\]\)$", "stadium"),  # ([text])
    (r"^([A-Za-z_][A-Za-z0-9_]*)\{(.*?)\}$", "rhombus"),       # {text}
    (r"^([A-Za-z_][A-Za-z0-9_]*)\[(.*?)\]$", "rectangle"),     # [text]
]


def _extract_node(token: str) -> tuple[str, str, str] | None:
    """Extract (node_id, shape, label) from a token. Returns None for bare refs."""
    token = token.strip()
    for pattern, shape in NODE_PATTERNS:
        m = re.match(pattern, token)
        if m:
            return m.group(1), shape, m.group(2)
    # Bare reference (e.g., "MOD_CHECK" alone)
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", token):
        return token, "ref", ""
    return None


def parse_mermaid(mermaid_text: str) -> ParsedGraph:
    """Parse a Mermaid flowchart TD block into a ParsedGraph."""
    g = ParsedGraph()

    lines = mermaid_text.strip().splitlines()
    edge_re = re.compile(
        r"^(.+?)\s*-->\s*(?:\|([^|]+)\|\s*)?(.+?)$"
    )

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("%%") or line.startswith("flowchart"):
            continue

        m = edge_re.match(line)
        if not m:
            # Standalone node declaration (rare in our SOPs but possible)
            extracted = _extract_node(line)
            if extracted and extracted[1] != "ref":
                nid, shape, label = extracted
                g.nodes[nid] = {"shape": shape, "label": label}
            continue

        from_tok, cond, to_tok = m.group(1), m.group(2), m.group(3)
        cond = cond.strip() if cond else None

        for tok in [from_tok, to_tok]:
            extracted = _extract_node(tok)
            if extracted is None:
                continue
            nid, shape, label = extracted
            if shape != "ref" and nid not in g.nodes:
                g.nodes[nid] = {"shape": shape, "label": label}
            elif nid not in g.nodes:
                g.nodes[nid] = {"shape": "unknown", "label": ""}

        from_id = _extract_node(from_tok)[0] if _extract_node(from_tok) else None
        to_id = _extract_node(to_tok)[0] if _extract_node(to_tok) else None

        if from_id and to_id:
            g.edges.append((from_id, to_id, cond))

    return g


if __name__ == "__main__":
    # Sanity check against retail SOP
    import sys
    sys.path.insert(0, "..")
    from config.retail_sop import RETAIL_MERMAID
    g = parse_mermaid(RETAIL_MERMAID)
    print(f"Parsed {len(g.nodes)} nodes and {len(g.edges)} edges")
    print(f"Shapes: {set(n['shape'] for n in g.nodes.values())}")
    # Verify a known path
    path = ["START", "AUTH", "ROUTE", "RETURN_CHECK", "CANCEL_CHECK", "CANCEL", "END"]
    ok, msg = g.is_valid_path(path)
    print(f"Cross-flow path valid: {ok} ({msg})")
