"""
Mermaid flowchart TD ↔ graph JSON conversion.

Graph JSON schema:
- nodes: [{ id, label, shape, node_type, x, y }]
  - shape: "rectangle" | "stadium" | "rhombus" (for round-trip mermaid)
  - node_type: "terminal" | "normal" | "decision" (for UI highlighting)
- edges: [{ source, target, label? }]
"""

from __future__ import annotations

import re
from typing import Any

# Shape → node type for UI (terminal = start/end, normal = process, decision = branch)
SHAPE_TO_NODE_TYPE = {
    "stadium": "terminal",
    "rectangle": "normal",
    "rhombus": "decision",
}

NODE_TYPE_TO_SHAPE = {v: k for k, v in SHAPE_TO_NODE_TYPE.items()}


def _extract_node_label_from_mermaid_part(part: str, nid: str) -> str:
    """Extract display label from a mermaid segment like INFO["..."] or A([...])."""
    part = part.strip()
    if "([" in part and "])" in part:
        m = re.search(r"\(\s*\[(.*?)\]\s*\)", part, re.DOTALL)
        if m:
            return m.group(1).strip().strip('"\'')
    if "[" in part and "]" in part:
        # Match content inside first [...] (allowing any chars, then strip quotes)
        m = re.search(r"\[([^\]]*)\]", part)
        if m:
            raw = m.group(1).strip().strip('"\'')
            return raw if raw else nid
    if "{" in part and "}" in part:
        m = re.search(r"\{(.*?)\}", part, re.DOTALL)
        if m:
            return m.group(1).strip()
    return nid


def _shape_to_node_type(shape: str) -> str:
    """Map mermaid shape to node_type (terminal | normal | decision)."""
    return SHAPE_TO_NODE_TYPE.get(shape, "normal")


def mermaid_to_graph_json(
    mermaid_source: str,
    *,
    layout_dx: int = 260,
    layout_dy: int = 100,
) -> dict[str, Any]:
    """
    Convert mermaid flowchart TD to graph JSON.

    Returns dict with:
    - nodes: list of { id, label, shape, node_type, x, y }
    - edges: list of { source, target, label? }

    Handles chained edges on one line (e.g. A --> B["..."] --> C) and optional edge labels.
    layout_dx: horizontal spacing between nodes; layout_dy: vertical spacing between levels.
    """
    lines = [
        s.strip()
        for s in mermaid_source.strip().splitlines()
        if s.strip() and not s.strip().startswith("%%")
    ]
    node_ids: set[str] = set()
    node_id_to_label: dict[str, str] = {}
    node_id_to_shape: dict[str, str] = {}
    edges: list[tuple[str, str, str | None]] = []

    def parse_segment(seg: str) -> tuple[str | None, str | None]:
        seg = seg.strip()
        edge_label: str | None = None
        if seg.startswith("|"):
            idx = seg.find("|", 1)
            if idx >= 0:
                edge_label = seg[1:idx].strip()
                seg = seg[idx + 1 :].strip()
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", seg)
        if not m:
            return (edge_label, None)
        nid = m.group(1)
        node_ids.add(nid)
        # Only set label/shape when segment contains shape syntax (avoid overwriting with bare id from "B --> C")
        if "([" in seg or "])" in seg or ("[" in seg and "]" in seg) or ("{" in seg and "}" in seg):
            node_id_to_label[nid] = _extract_node_label_from_mermaid_part(seg, nid)
            if "([" in seg or "])" in seg:
                node_id_to_shape[nid] = "stadium"
            elif "{" in seg and "}" in seg:
                node_id_to_shape[nid] = "rhombus"
            else:
                node_id_to_shape.setdefault(nid, "rectangle")
        else:
            node_id_to_shape.setdefault(nid, "rectangle")
        return (edge_label, nid)

    arrow_pattern = re.compile(r"\s*(?:-->|-\.->)\s*")
    for line in lines:
        if "-->" not in line and "-.->" not in line:
            continue
        parts = arrow_pattern.split(line)
        if len(parts) < 2:
            continue
        prev_id: str | None = None
        for part in parts:
            edge_label, nid = parse_segment(part)
            if nid is None:
                continue
            if prev_id is not None:
                edges.append((prev_id, nid, edge_label))
            prev_id = nid

    in_degree: dict[str, int] = {n: 0 for n in node_ids}
    for (_, to_id, _) in edges:
        in_degree[to_id] = in_degree.get(to_id, 0) + 1
    level_map: dict[str, int] = {}
    queue = [n for n in node_ids if in_degree[n] == 0]
    lvl = 0
    while queue:
        for n in queue:
            level_map[n] = lvl
        next_q: list[str] = []
        for u in queue:
            for (a, b, _) in edges:
                if a == u:
                    in_degree[b] -= 1
                    if in_degree[b] == 0:
                        next_q.append(b)
        queue = next_q
        lvl += 1
    for n in node_ids:
        if n not in level_map:
            level_map[n] = lvl
    levels: dict[int, list[str]] = {}
    for n, lv in level_map.items():
        levels.setdefault(lv, []).append(n)
    max_lv = max(levels.keys()) if levels else 0
    dx, dy = layout_dx, layout_dy
    json_nodes: list[dict[str, Any]] = []
    for i in range(max_lv + 1):
        row = levels.get(i, [])
        for j, nid in enumerate(row):
            shape = node_id_to_shape.get(nid, "rectangle")
            json_nodes.append({
                "id": nid,
                "label": node_id_to_label.get(nid, nid),
                "shape": shape,
                "node_type": _shape_to_node_type(shape),
                "x": j * dx,
                "y": i * dy,
            })
    json_edges = [
        {"source": a, "target": b, "label": lab} for (a, b, lab) in edges if lab is not None
    ] + [{"source": a, "target": b} for (a, b, lab) in edges if lab is None]
    return {"nodes": json_nodes, "edges": json_edges}


def graph_json_to_mermaid(graph: dict[str, Any]) -> str:
    """Convert graph JSON back to mermaid flowchart TD. Uses shape for syntax."""
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    lines = ["flowchart TD"]
    id_to_node = {n.get("id", ""): n for n in nodes}

    def escape(s: str) -> str:
        return s.replace('"', "#quot;").replace("\n", " ").strip() or " "

    def node_segment(nid: str) -> str:
        """Format one node for mermaid (id + shape + label) so round-trip parsing gets shape."""
        n = id_to_node.get(nid, {})
        label = escape(str(n.get("label", nid)))
        shape = n.get("shape", "rectangle")
        if shape == "stadium":
            return f"{nid}([{label}])"
        if shape == "rhombus":
            return f"{nid}{{{label}}}"
        return f'{nid}["{label}"]'

    for n in nodes:
        nid = n.get("id", "")
        if not nid:
            continue
        lines.append(f"    {node_segment(nid)}")
    for e in edges:
        src = e.get("source", "")
        tgt = e.get("target", "")
        lab = e.get("label")
        src_seg = node_segment(src) if src in id_to_node else src
        tgt_seg = node_segment(tgt) if tgt in id_to_node else tgt
        if lab and str(lab).strip():
            lines.append(f"    {src_seg} -->|{escape(str(lab))}| {tgt_seg}")
        else:
            lines.append(f"    {src_seg} --> {tgt_seg}")
    return "\n".join(lines)
