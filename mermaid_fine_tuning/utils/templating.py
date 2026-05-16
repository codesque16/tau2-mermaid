"""Build structured-trace blocks deterministically from ground-truth labels.

This is NOT a generator — it produces consistent traces from facts.
The free-form continuation is added separately in stage 5.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import SOPGraph, AssistantTurn, StructuredTrace
from utils.mermaid_parser import parse_mermaid


# Map (current_node, action_pattern) -> list of global policy IDs that apply.
# Keep this conservative; better to under-cite than fabricate.
GLOBAL_POLICY_TRIGGERS = {
    # Authentication
    "AUTH": ["single_user_per_conversation"],
    # Routing
    "ROUTE": [],
    # Information lookup
    "INFO": ["lost_items", "no_fabrication"],
    # Cancel
    "CANCEL_CHECK": ["actionable_order_statuses", "order_discovery"],
    "CANCEL": ["confirmation_before_mutations", "single_action_per_order", "refund_timing"],
    # Modify
    "MOD_CHECK": ["actionable_order_statuses", "order_discovery"],
    "MOD_ROUTE": [],
    "MOD_ADDRESS": ["confirmation_before_mutations", "single_action_per_order"],
    "MOD_PAYMENT": ["confirmation_before_mutations", "single_action_per_order", "refund_timing"],
    "MOD_ITEMS": [
        "confirmation_before_mutations", "single_action_per_order",
        "calculations", "tie_breaking", "post_action_info", "batch_processing"
    ],
    # Return
    "RETURN_CHECK": ["actionable_order_statuses", "lost_items"],
    "RETURN": [
        "confirmation_before_mutations", "single_action_per_order",
        "refund_timing", "batch_processing"
    ],
    # Exchange
    "EXCHANGE_CHECK": ["actionable_order_statuses"],
    "EXCHANGE": [
        "confirmation_before_mutations", "single_action_per_order",
        "calculations", "batch_processing"
    ],
    # Transfer
    "TRANSFER": ["transfer_policy"],
    "END": [],
}

# Always-applicable policies
ALWAYS_APPLICABLE = ["one_tool_per_turn", "no_fabrication"]


def _abbreviate_policy(policy_text: str, max_chars: int = 200) -> str:
    """Shorten the node policy text for inclusion in the trace."""
    policy_text = " ".join(policy_text.split())  # collapse whitespace
    if len(policy_text) <= max_chars:
        return policy_text
    return policy_text[: max_chars - 3].rstrip() + "..."


def _describe_edge(graph: SOPGraph, current: str, next_node: str) -> str:
    """Human-readable edge description for the trace."""
    if current == next_node:
        return f"staying at {current} (gathering info / pending tool result)"
    parsed = parse_mermaid(graph.mermaid)
    for f, t, cond in parsed.edges:
        if f == current and t == next_node:
            if cond:
                return f"took {current} -->|{cond}| {next_node}"
            return f"took {current} --> {next_node}"
    return f"unmapped transition {current} -> {next_node}"


def _next_action_description(turn: AssistantTurn) -> str:
    if turn.tool_call:
        args_brief = ", ".join(f"{k}={v}" for k, v in list(turn.tool_call.arguments.items())[:3])
        return f"call tool: {turn.tool_call.name}({args_brief})"
    if turn.response_to_user:
        # Categorize: confirmation, info, ask, refusal
        text = turn.response_to_user.lower()
        if "confirm" in text and "please confirm" in text:
            return "send confirmation request to user"
        if any(x in text for x in ["cannot", "not eligible", "ineligible", "policy"]):
            return "deliver policy-based refusal / limitation to user"
        if "?" in turn.response_to_user:
            return "ask user for additional information"
        return "respond to user with information"
    return "no action"


def build_structured_trace(
    turn: AssistantTurn,
    graph: SOPGraph,
    observation: str,
) -> StructuredTrace:
    """Build a StructuredTrace deterministically from ground-truth labels.

    `observation` is the human-readable summary of what just happened
    (user message or tool result) that prompted this turn.
    """
    node_policy = graph.node_policies.get(turn.current_node)
    node_policy_text = node_policy.policy if node_policy else "(no policy)"

    triggered = list(GLOBAL_POLICY_TRIGGERS.get(turn.current_node, []))
    triggered = ALWAYS_APPLICABLE + [p for p in triggered if p not in ALWAYS_APPLICABLE]

    # Filter out always-applicable noise on trivial turns: keep them but minimal
    if turn.current_node in {"ROUTE", "END", "MOD_ROUTE"}:
        triggered = ["one_tool_per_turn"]

    return StructuredTrace(
        current_node=turn.current_node,
        observation=observation,
        applicable_global_policies=triggered,
        applicable_node_policy=_abbreviate_policy(node_policy_text),
        edge_decision=_describe_edge(graph, turn.current_node, turn.next_node),
        next_node=turn.next_node,
        next_action=_next_action_description(turn),
    )


def render_trace_block(trace: StructuredTrace, continuation_text: str = "") -> str:
    """Render the structured trace + optional continuation into a single <think> block.

    Note: Gemma 4's native thinking delimiters are applied by the tokenizer.
    We render the *body* of the thinking block here.
    """
    lines = [
        f"current_node: {trace.current_node}",
        f"observation: {trace.observation}",
        f"applicable_global_policies: {trace.applicable_global_policies}",
        f"applicable_node_policy: {trace.applicable_node_policy}",
        f"edge_decision: {trace.edge_decision}",
        f"next_node: {trace.next_node}",
        f"next_action: {trace.next_action}",
    ]
    body = "\n".join(lines)
    if continuation_text.strip():
        body = body + "\n\n---\n\n" + continuation_text.strip()
    return body


if __name__ == "__main__":
    # Smoke test
    from config.retail_sop import RETAIL_MERMAID, RETAIL_GLOBAL_POLICIES, RETAIL_NODE_POLICIES
    from config.schemas import SOPGraph, NodePolicy, AssistantTurn, ToolCall

    g = SOPGraph(
        domain="retail",
        mermaid=RETAIL_MERMAID,
        global_policies=RETAIL_GLOBAL_POLICIES,
        node_policies={nid: NodePolicy(tool_hints=p["tool_hints"], policy=p["policy"]) for nid, p in RETAIL_NODE_POLICIES.items()},
    )
    turn = AssistantTurn(
        turn_index=2,
        current_node="MOD_ITEMS",
        next_node="MOD_ITEMS",
        tool_call=ToolCall(name="calculate", arguments={"expression": "45.99 - 39.99"}),
    )
    # MOD_ITEMS doesn't list calculate in tool_hints, but calculate is a global utility;
    # in practice we'd whitelist it. This is a smoke test only.
    trace = build_structured_trace(turn, g, observation="User asked to swap item X for cheaper variant; computing price difference.")
    print(render_trace_block(trace, continuation_text="Three items requested. Item B is already cheapest per the policy — excluding from tool call. Computing diffs for A and C in sequence per one-tool-per-turn."))
