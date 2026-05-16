"""Schemas for the entire pipeline. Everything else validates against these."""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ============================================================
# Stage 1: SOP Graph
# ============================================================

class NodePolicy(BaseModel):
    tool_hints: list[str] = Field(default_factory=list)
    policy: str  # The instruction text shown in node_policies YAML


class SOPGraph(BaseModel):
    """A complete SOP artifact: graph + global policies + per-node policies."""
    domain: str  # "retail", "airline", etc.
    mermaid: str  # The flowchart TD ... block
    global_policies: list[str]  # Each policy is a markdown bullet's text
    node_policies: dict[str, NodePolicy]  # keyed by node ID
    # Note: previously had `nodes` and `edges` here as derived fields, but
    # Gemini's structured-output mode rejects tuple-typed fields (prefixItems).
    # The parsed graph (nodes + edges) is obtained on-demand via
    # utils.mermaid_parser.parse_mermaid(self.mermaid) rather than stored here.


# ============================================================
# Stage 2: Trajectory Plan
# ============================================================

class PlanEvent(BaseModel):
    """A key event in a planned conversation."""
    turn_index: int
    description: str  # "User provides email", "Tool returns order status=delivered", etc.


class TrajectoryPlan(BaseModel):
    """A planned conversation through a graph, before realization."""
    plan_id: str
    graph_domain: str
    user_persona: str
    user_goal: str
    expected_node_sequence: list[str]  # Must be a valid path in the graph
    key_events: list[PlanEvent]
    category: Literal[
        "happy_path", "off_graph_redirect", "policy_violation",
        "cross_flow", "batch_multi", "ambiguous"
    ]


# ============================================================
# Stage 3: Conversation Turn
# ============================================================

class ToolCall(BaseModel):
    name: str
    arguments: dict


class ToolResult(BaseModel):
    name: str
    result: dict | str


class AssistantTurn(BaseModel):
    """A single assistant turn in a conversation, fully labeled."""
    turn_index: int
    current_node: str  # Ground truth: node the assistant is at when responding
    next_node: str    # Ground truth: node after this turn completes
    edge_taken: Optional[str] = None  # Condition label of edge taken, if any
    tool_call: Optional[ToolCall] = None
    response_to_user: Optional[str] = None  # The user-facing text


class UserTurn(BaseModel):
    turn_index: int
    content: str


class Conversation(BaseModel):
    """A fully realized conversation with ground-truth labels."""
    conversation_id: str
    graph_domain: str
    plan_id: str
    turns: list  # interleaved UserTurn / AssistantTurn / ToolResult


# ============================================================
# Stage 4: Structured Trace (deterministic from above)
# ============================================================

class StructuredTrace(BaseModel):
    """The first part of the <think> block. Templated from ground truth."""
    current_node: str
    observation: str
    applicable_global_policies: list[str]  # policy IDs/slugs
    applicable_node_policy: str  # the node's policy text, abbreviated
    edge_decision: str  # human-readable: "took MOD_CHECK -->|yes| MOD_ROUTE"
    next_node: str
    next_action: str  # "call tool X", "ask user for Y", "send confirmation message"


# ============================================================
# Stage 5: Continuation
# ============================================================

class Continuation(BaseModel):
    """Optional free-form reasoning after the structured trace."""
    text: str  # Empty string means no continuation
    rationale: str  # Generator's self-assessment (for review only, not training)
    needed: bool


# ============================================================
# Final Training Example
# ============================================================

class TrainingExample(BaseModel):
    """One assistant turn formatted for training, with all metadata."""
    example_id: str
    conversation_id: str
    turn_index: int
    system_prompt: str  # The full SOP artifact rendered as text
    conversation_history: list[dict]  # [{role: "user", content: ...}, {role: "assistant", content: ...}, {role: "tool", ...}]
    # The target output:
    structured_trace: StructuredTrace
    continuation: Continuation
    tool_call: Optional[ToolCall] = None
    response_to_user: Optional[str] = None
    # Ground truth for eval:
    gt_current_node: str
    gt_next_node: str
    gt_edge: Optional[str] = None
