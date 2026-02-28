"""
Graph harness: parses Mermaid flowcharts, validates transitions, tracks state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import re


def _mermaid_to_graph_json(mermaid_source: str) -> dict[str, Any]:
    lines = [
        s.strip()
        for s in mermaid_source.strip().splitlines()
        if s.strip() and not s.strip().startswith("%%")
    ]
    node_ids: set[str] = set()
    edges: list[tuple[str, str, str | None]] = []
    arrow_pattern = re.compile(r"\s*(?:-->|-\.->)\s*")

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
        return (edge_label, nid)

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

    json_edges = [
        {"source": a, "target": b, "label": lab}
        for (a, b, lab) in edges
        if lab is not None
    ] + [{"source": a, "target": b} for (a, b, lab) in edges if lab is None]
    json_nodes = [{"id": nid} for nid in node_ids]
    return {"nodes": json_nodes, "edges": json_edges}


@dataclass
class Graph:
    """Parsed graph from Mermaid with adjacency and terminal nodes."""

    node_ids: set[str]
    adjacency: dict[str, set[str]]  # node_id -> set of neighbor ids
    start_nodes: set[str]
    end_nodes: set[str]

    @classmethod
    def from_mermaid(cls, mermaid_str: str) -> "Graph":
        data = _mermaid_to_graph_json(mermaid_str)
        nodes = {n["id"] for n in data.get("nodes", [])}
        edges = data.get("edges", [])

        adjacency: dict[str, set[str]] = {n: set() for n in nodes}
        in_degree: dict[str, int] = {n: 0 for n in nodes}
        out_degree: dict[str, int] = {n: 0 for n in nodes}

        for e in edges:
            src = e.get("source")
            tgt = e.get("target")
            if src and tgt and src in nodes and tgt in nodes:
                adjacency[src].add(tgt)
                in_degree[tgt] = in_degree.get(tgt, 0) + 1
                out_degree[src] = out_degree.get(src, 0) + 1

        start_nodes = {n for n in nodes if in_degree.get(n, 0) == 0}
        end_nodes = {n for n in nodes if out_degree.get(n, 0) == 0}

        if not start_nodes:
            start_nodes = {n for n in nodes if "start" in n.lower() or n == "A"}
        if not end_nodes:
            end_nodes = {n for n in nodes if "end" in n.lower()}

        return cls(
            node_ids=nodes,
            adjacency=adjacency,
            start_nodes=start_nodes or nodes,
            end_nodes=end_nodes or nodes,
        )

    def neighbors(self, node_id: str) -> set[str]:
        return self.adjacency.get(node_id, set())

    def is_end(self, node_id: str) -> bool:
        return node_id in self.end_nodes

    def is_start(self, node_id: str) -> bool:
        return node_id in self.start_nodes


@dataclass
class GraphHarness:
    """
    Runtime harness that tracks state, validates transitions, and provides reminders.
    """

    graph: Graph
    current_node: str
    history: list[tuple[str, str]] = field(default_factory=list)
    turn_count: int = 0
    validate_transitions: bool = True

    def __init__(
        self,
        mermaid_str: str,
        *,
        validate_transitions: bool = True,
        start_node: str | None = None,
    ):
        self.graph = Graph.from_mermaid(mermaid_str)
        if start_node is not None:
            if start_node not in self.graph.node_ids:
                raise ValueError(f"Start node {start_node} not in graph")
            self.current_node = start_node
        else:
            starts = self.graph.start_nodes
            if len(starts) != 1:
                # Prefer node named "start"
                self.current_node = next(
                    (s for s in starts if s.lower() == "start"), next(iter(starts))
                )
            else:
                self.current_node = next(iter(starts))
        self.history = []
        self.turn_count = 0
        self.validate_transitions = validate_transitions

    def transition(self, next_node: str) -> tuple[bool, str]:
        """
        Attempt transition to next_node. Returns (success, message).
        """
        if next_node not in self.graph.node_ids:
            return False, f"Unknown node: {next_node}"
        valid = next_node in self.graph.neighbors(self.current_node)
        if self.validate_transitions and not valid:
            return False, (
                f"Invalid transition: {self.current_node} -> {next_node}. "
                f"Valid next steps: {sorted(self.graph.neighbors(self.current_node))}"
            )
        self.history.append((self.current_node, next_node))
        self.current_node = next_node
        return True, f"Moved to {next_node}"

    def get_state_reminder(self) -> str:
        neighbors = sorted(self.graph.neighbors(self.current_node))
        return (
            f"[CURRENT_STATE: {self.current_node}] "
            f"[VALID_NEXT: {neighbors}]"
        )

    def inject_reminder(self, every_n: int = 5) -> str | None:
        """Return reminder string every N turns, else None."""
        self.turn_count += 1
        if self.turn_count % every_n == 0:
            neighbors = sorted(self.graph.neighbors(self.current_node))
            return (
                f"REMINDER: You are at step '{self.current_node}'. "
                f"Refer to the workflow graph. Valid next steps: {neighbors}"
            )
        return None

    def get_path(self) -> list[str]:
        """Return the path traversed so far (including current node)."""
        path = []
        for a, b in self.history:
            if not path:
                path.append(a)
            path.append(b)
        if not path:
            path = [self.current_node]
        return path

    def is_at_end(self) -> bool:
        return self.graph.is_end(self.current_node)
