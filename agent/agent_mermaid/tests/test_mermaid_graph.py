"""Tests for mermaid_graph mermaid ↔ JSON conversion."""

import pytest

from agent.agent_mermaid.mermaid_graph import (
    mermaid_to_graph_json,
    graph_json_to_mermaid,
    SHAPE_TO_NODE_TYPE,
)


def test_simple_chain():
    m = """
    flowchart TD
        A([Start]) --> B["Step one"]
        B --> C{Decision?}
        C -->|yes| D["End"]
    """
    out = mermaid_to_graph_json(m)
    assert "nodes" in out
    assert "edges" in out
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"A", "B", "C", "D"}
    labels = {n["id"]: n["label"] for n in out["nodes"]}
    assert labels["A"] == "Start"
    assert labels["B"] == "Step one"
    assert labels["C"] == "Decision?"
    assert labels["D"] == "End"
    shapes = {n["id"]: n["shape"] for n in out["nodes"]}
    assert shapes["A"] == "stadium"
    assert shapes["B"] == "rectangle"
    assert shapes["C"] == "rhombus"
    assert shapes["D"] == "rectangle"
    node_types = {n["id"]: n["node_type"] for n in out["nodes"]}
    assert node_types["A"] == "terminal"
    assert node_types["B"] == "normal"
    assert node_types["C"] == "decision"
    assert node_types["D"] == "normal"
    edges = {(e["source"], e["target"]): e.get("label") for e in out["edges"]}
    assert ("A", "B") in edges and edges[("A", "B")] is None
    assert ("B", "C") in edges and edges[("B", "C")] is None
    assert ("C", "D") in edges and edges[("C", "D")] == "yes"


def test_chained_edges_same_line():
    m = """
    flowchart TD
        ROUTE -->|info request| INFO["Provide info"] --> END_INFO([End / Restart])
    """
    out = mermaid_to_graph_json(m)
    assert len(out["nodes"]) == 3
    ids = [n["id"] for n in out["nodes"]]
    assert "ROUTE" in ids and "INFO" in ids and "END_INFO" in ids
    edges = [(e["source"], e["target"], e.get("label")) for e in out["edges"]]
    assert ("ROUTE", "INFO", "info request") in edges
    assert ("INFO", "END_INFO", None) in edges or any(
        e[0] == "INFO" and e[1] == "END_INFO" for e in edges
    )


def test_roundtrip():
    m = """
    flowchart TD
        START([User contacts Agent]) --> AUTH["Authenticate"]
        AUTH --> ROUTE{User intent?}
        ROUTE -->|cancel| CANCEL["Cancel order"] --> END([End])
    """
    out = mermaid_to_graph_json(m)
    m2 = graph_json_to_mermaid(out)
    out2 = mermaid_to_graph_json(m2)
    assert set(n["id"] for n in out["nodes"]) == set(n["id"] for n in out2["nodes"])
    # Shapes round-trip via graph_json_to_mermaid (we write stadium/rhombus/rectangle)
    assert len(out["nodes"]) == len(out2["nodes"])
    id_to_shape1 = {n["id"]: n["shape"] for n in out["nodes"]}
    id_to_shape2 = {n["id"]: n["shape"] for n in out2["nodes"]}
    assert id_to_shape1 == id_to_shape2
    assert len(out["edges"]) == len(out2["edges"])


def test_shape_to_node_type():
    assert SHAPE_TO_NODE_TYPE["stadium"] == "terminal"
    assert SHAPE_TO_NODE_TYPE["rectangle"] == "normal"
    assert SHAPE_TO_NODE_TYPE["rhombus"] == "decision"
