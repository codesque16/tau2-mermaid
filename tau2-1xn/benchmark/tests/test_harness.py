"""Tests for GraphHarness."""

import pytest

from graph_harness.harness import Graph, GraphHarness


SIMPLE_MERMAID = """
flowchart TD
    start([Start])
    start --> verify_order[Verify order]
    verify_order --> check_status[Check status]
    check_status --> end([End])
"""


def test_graph_parsing():
    g = Graph.from_mermaid(SIMPLE_MERMAID)
    assert "start" in g.node_ids
    assert "verify_order" in g.node_ids
    assert "check_status" in g.node_ids
    assert "end" in g.node_ids
    assert g.neighbors("start") == {"verify_order"}
    assert g.neighbors("verify_order") == {"check_status"}
    assert g.neighbors("check_status") == {"end"}
    assert g.is_start("start")
    assert g.is_end("end")


def test_harness_valid_transition():
    h = GraphHarness(SIMPLE_MERMAID, validate_transitions=True)
    assert h.current_node == "start"
    assert h.get_path() == ["start"]

    ok, msg = h.transition("verify_order")
    assert ok
    assert h.current_node == "verify_order"
    assert h.get_path() == ["start", "verify_order"]

    ok, msg = h.transition("check_status")
    assert ok
    ok, msg = h.transition("end")
    assert ok
    assert h.is_at_end()
    assert h.get_path() == ["start", "verify_order", "check_status", "end"]


def test_harness_invalid_transition():
    h = GraphHarness(SIMPLE_MERMAID, validate_transitions=True)
    ok, msg = h.transition("end")  # Cannot jump from start to end
    assert not ok
    assert "Invalid transition" in msg
    assert h.current_node == "start"


def test_harness_no_validation():
    h = GraphHarness(SIMPLE_MERMAID, validate_transitions=False)
    ok, msg = h.transition("end")  # Invalid but we don't validate
    assert ok
    assert h.current_node == "end"
