"""Validators that filter generator output before it enters the dataset.

Each validator returns (ok: bool, errors: list[str]).
Errors are human-readable; non-empty list means the example should be reviewed/rejected.
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import SOPGraph, TrajectoryPlan, Conversation, AssistantTurn, UserTurn
from utils.mermaid_parser import parse_mermaid, ParsedGraph


# ============================================================
# Stage 1: Graph validator
# ============================================================

def validate_graph(g: SOPGraph) -> tuple[bool, list[str]]:
    errors = []

    # Parse the Mermaid
    try:
        parsed = parse_mermaid(g.mermaid)
    except Exception as e:
        return False, [f"Mermaid parse failed: {e}"]

    if not parsed.nodes:
        errors.append("No nodes parsed from Mermaid")
    if not parsed.edges:
        errors.append("No edges parsed from Mermaid")

    # Must have START node
    if "START" not in parsed.nodes:
        errors.append("Missing START node")
    elif parsed.nodes["START"]["shape"] != "stadium":
        errors.append("START must be a stadium ([text]) shape")

    # (Removed: derived fields nodes/edges are no longer stored on SOPGraph;
    # callers that need them should call parse_mermaid(g.mermaid) directly.)

    # Every node referenced in node_policies must exist
    for node_id in g.node_policies.keys():
        if node_id not in parsed.nodes:
            errors.append(f"node_policies references missing node: {node_id}")

    # Every non-terminal node in the Mermaid graph must have a node_policies entry.
    # Terminal stadium nodes (refusals, END, hand-off) are conversation-end markers
    # and don't need explicit policies. Missing entries on operational nodes cause
    # Stage 3 validation to fail because the agent can't look up tool_hints / policy.
    for node_id, data in parsed.nodes.items():
        if data["shape"] == "stadium":
            continue  # terminals/refusals are OK without a policy
        if node_id not in g.node_policies:
            errors.append(f"Mermaid node {node_id} missing from node_policies (non-terminal)")

    # Every non-terminal, non-stadium node should have at least one outgoing edge
    outgoing = {f for f, _, _ in parsed.edges}
    for nid, data in parsed.nodes.items():
        if data["shape"] != "stadium" and nid not in outgoing:
            errors.append(f"Non-terminal node {nid} has no outgoing edges")

    # Rhombus nodes must have labeled outgoing edges
    for nid, data in parsed.nodes.items():
        if data["shape"] == "rhombus":
            outs = [(t, c) for f, t, c in parsed.edges if f == nid]
            unlabeled = [t for t, c in outs if c is None]
            if unlabeled:
                errors.append(f"Rhombus {nid} has unlabeled edges to: {unlabeled}")

    # Tool hints referenced should not contain whitespace nonsense
    for nid, policy in g.node_policies.items():
        for tool in policy.tool_hints:
            if not tool or " " in tool:
                errors.append(f"Suspicious tool name '{tool}' in node {nid}")

    # Global policies must be non-empty
    if not g.global_policies:
        errors.append("global_policies is empty")

    return len(errors) == 0, errors


# ============================================================
# Stage 2: Plan validator
# ============================================================

def validate_plan(plan: TrajectoryPlan, graph: SOPGraph) -> tuple[bool, list[str]]:
    errors = []
    parsed = parse_mermaid(graph.mermaid)

    # Node sequence must start at START
    if not plan.expected_node_sequence:
        return False, ["Empty node sequence"]

    if plan.expected_node_sequence[0] != "START":
        errors.append(f"Sequence must start at START, got {plan.expected_node_sequence[0]}")

    # Every consecutive pair must be a valid edge
    ok, msg = parsed.is_valid_path(plan.expected_node_sequence)
    if not ok:
        errors.append(f"Invalid path: {msg}")

    # Persona and goal must be non-trivial
    if len(plan.user_goal.strip()) < 5:
        errors.append("user_goal is too short")
    if len(plan.user_persona.strip()) < 5:
        errors.append("user_persona is too short")

    return len(errors) == 0, errors


# ============================================================
# Stage 3: Conversation/turn validator
# ============================================================

# Confirmation phrases that must appear verbatim on certain mutation turns
CONFIRMATION_PHRASE = "Please confirm so I can process this for you. Please note that the action is not yet complete, and I will notify you once it is successfully processed."
MOD_ITEMS_PHRASE = "Please confirm you have listed all items you want to modify, as this action can only be performed once per order."

MUTATION_NODES = {"CANCEL", "MOD_ADDRESS", "MOD_PAYMENT", "MOD_ITEMS", "RETURN", "EXCHANGE"}


def validate_assistant_turn(
    turn: AssistantTurn,
    graph: SOPGraph,
    previous_assistant_turn: AssistantTurn | None = None,
) -> tuple[bool, list[str]]:
    errors = []
    parsed = parse_mermaid(graph.mermaid)

    # current_node must exist
    if turn.current_node not in parsed.nodes:
        errors.append(f"current_node '{turn.current_node}' not in graph")
        return False, errors

    if turn.next_node not in parsed.nodes:
        errors.append(f"next_node '{turn.next_node}' not in graph")

    # Edge from current to next must exist (or current == next for stays)
    if turn.current_node != turn.next_node:
        if not parsed.is_valid_edge(turn.current_node, turn.next_node):
            errors.append(
                f"No edge {turn.current_node} -> {turn.next_node}"
            )

    # Tool call must be in tool_hints for current_node
    if turn.tool_call:
        node_policy = graph.node_policies.get(turn.current_node)
        if node_policy is None:
            errors.append(f"No policy for current_node {turn.current_node}")
        elif turn.tool_call.name not in node_policy.tool_hints:
            errors.append(
                f"Tool '{turn.tool_call.name}' not in tool_hints for {turn.current_node}: "
                f"{node_policy.tool_hints}"
            )

    # One tool call per turn: if tool_call is set, response_to_user must be empty
    if turn.tool_call and turn.response_to_user:
        errors.append("Both tool_call and response_to_user set (violates one_tool_per_turn)")

    # If at a mutation node and about to confirm, response must contain confirmation phrase
    # (heuristic: any response on a mutation node that asks for confirmation)
    if (turn.current_node in MUTATION_NODES
        and turn.response_to_user
        and turn.tool_call is None
        and "confirm" in turn.response_to_user.lower()):
        if CONFIRMATION_PHRASE not in turn.response_to_user:
            errors.append(f"Mutation confirmation missing required phrase on {turn.current_node}")

    # MOD_ITEMS has its own required phrase
    if turn.current_node == "MOD_ITEMS" and turn.response_to_user:
        if "listed all items" in turn.response_to_user.lower() and MOD_ITEMS_PHRASE not in turn.response_to_user:
            errors.append("MOD_ITEMS reminder missing the exact required phrase")

    return len(errors) == 0, errors


def validate_conversation(conv: Conversation, graph: SOPGraph) -> tuple[bool, list[str]]:
    """Validate every assistant turn in a conversation."""
    errors = []
    prev_assistant = None
    for turn in conv.turns:
        if isinstance(turn, dict):
            # Convert dict back to model if needed (when loading from JSON)
            if turn.get("kind") == "assistant":
                turn = AssistantTurn(**{k: v for k, v in turn.items() if k != "kind"})
            else:
                continue
        if isinstance(turn, AssistantTurn):
            ok, errs = validate_assistant_turn(turn, graph, prev_assistant)
            if not ok:
                for e in errs:
                    errors.append(f"Turn {turn.turn_index}: {e}")
            prev_assistant = turn
    return len(errors) == 0, errors


if __name__ == "__main__":
    # Smoke test: build a SOPGraph from retail constants and validate
    from config.retail_sop import RETAIL_MERMAID, RETAIL_GLOBAL_POLICIES, RETAIL_NODE_POLICIES
    from config.schemas import NodePolicy

    g = SOPGraph(
        domain="retail",
        mermaid=RETAIL_MERMAID,
        global_policies=RETAIL_GLOBAL_POLICIES,
        node_policies={
            nid: NodePolicy(tool_hints=p["tool_hints"], policy=p["policy"])
            for nid, p in RETAIL_NODE_POLICIES.items()
        },
    )
    ok, errs = validate_graph(g)
    print(f"Retail SOP valid: {ok}")
    if errs:
        for e in errs:
            print(f"  - {e}")