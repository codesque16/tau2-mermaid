"""Gemini generation prompts for each stage of the dataset pipeline.

These are deliberately verbose. Edit them based on review findings.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.retail_sop import render_retail_system_prompt


# ============================================================
# Stage 1: Graph generation
# ============================================================

STAGE1_SYSTEM = """You are an expert at writing Standard Operating Procedure (SOP) graphs for customer service agents. You produce SOPs in a strict format combining a Mermaid flowchart, global policies, and per-node policies."""

STAGE1_PROMPT_TEMPLATE = """Generate ONE complete SOP for a customer service agent in the domain: {domain}.

Constraints on the SOP:
- Target node count: {size_target} nodes (give or take 3)
- Must include these topology features: {features}
- The graph MUST start with `START([User contacts Agent])`.
- Every flow must be reachable from START.
- Every decision (rhombus) node must have outgoing edges that cover all its conditions and are mutually exclusive.
- At least one terminal stadium node (such as END).

Use these Mermaid conventions strictly:
- `flowchart TD` at the top
- Stadium: `NODE_ID([label])` for START, terminals, refusals, ends
- Rectangle: `NODE_ID[label]` for actions, info collection, tool-calling steps
- Rhombus: `NODE_ID{{label}}` for decisions, intent routing, status checks
- Edge conditions written as `|condition|` between `-->` and the target node

Global policies should be comma-separated phrases that apply across the entire SOP (authentication rules, confirmation requirements, scope limits, mutation rules, etc.).

Node policies should be specific to each node and reference tools the node may call via `tool_hints`.

Here is a complete, well-formed example in the retail domain. Match this style exactly:

---
{retail_example}
---

Now produce the SOP for: {domain}. Output ONLY valid JSON matching the SOPGraph schema. Do not include backticks or commentary.

Important:
- `mermaid` field should be the FULL Mermaid block including `flowchart TD` line.
- `global_policies` should be a list of strings; each string starts with a policy slug followed by `:` and the policy text (e.g., `"confirmation_before_mutations: Before any DB-updating action..."`).
- `node_policies` MUST contain an entry for EVERY node ID that appears in the Mermaid graph, including START and all terminal nodes. Before you finalize, mentally enumerate every node in your Mermaid and confirm each has a corresponding `node_policies` entry. Missing entries cause downstream pipeline failures.
- Use realistic tool names for the domain (e.g., for an airline: `get_booking_by_pnr`, `cancel_booking`, etc.).
- Do NOT invent tools that are not referenced anywhere; every tool in tool_hints should make sense for that node.
- Authentication tools belong on an AUTH node (a Rectangle), NOT on the START stadium node. START has empty tool_hints.
- Intent-routing nodes (Rhombus with label like "Identify user intent") have empty tool_hints — they make decisions based on the user's previous message, they do not call tools."""


def stage1_prompt(domain: str, size_target: int, features: list[str]) -> str:
    return STAGE1_PROMPT_TEMPLATE.format(
        domain=domain,
        size_target=size_target,
        features=", ".join(features),
        retail_example=render_retail_system_prompt(),
    )


# ============================================================
# Stage 2: Trajectory plan generation
# ============================================================

STAGE2_SYSTEM = """You are an expert at planning realistic customer service conversations against a given SOP graph. You produce structured trajectory plans that exercise specific paths through the SOP, including happy paths, edge cases, and adversarial scenarios."""

STAGE2_PROMPT_TEMPLATE = """Given the following SOP for the {domain} domain, generate {n_plans} distinct conversation plans.

SOP:
{sop_rendered}

Distribute the plans across these categories:
- happy_path (~40%): Standard successful flows. Cover ALL terminal nodes across plans.
- off_graph_redirect (~20%): User goes off-topic and the agent redirects back to the SOP.
- policy_violation (~15%): User attempts something explicitly denied by policy (different user, lost item refund, splitting payment, etc.). Agent must deny gracefully.
- cross_flow (~10%): User starts in one intent and transitions mid-conversation (e.g., return -> cancel, exchange -> modify).
- batch_multi (~10%): User wants multiple actions or multiple orders in one conversation.
- ambiguous (~5%): User provides unclear input requiring clarification.

For each plan, produce:
- `plan_id`: a unique short slug like "{domain}_p001"
- `user_persona`: 1-2 sentence description
- `user_goal`: what the user ultimately wants
- `expected_node_sequence`: ordered list of node IDs the agent visits. MUST begin with "START". Every consecutive pair MUST be a real edge in the graph. Include repeated visits to a node if the agent loops there to collect more info.
- `key_events`: list of {{turn_index, description}} marking critical moments (user reveals info, tool returns key data, user confirms, etc.)
- `category`: one of the categories above

Output ONLY a JSON array of plan objects. Do not include backticks or commentary."""


def stage2_prompt(domain: str, sop_rendered: str, n_plans: int) -> str:
    return STAGE2_PROMPT_TEMPLATE.format(
        domain=domain, sop_rendered=sop_rendered, n_plans=n_plans
    )


# ============================================================
# Stage 3: Conversation realization
# ============================================================

STAGE3_SYSTEM = """You realize a planned conversation as a faithful, turn-by-turn dialogue between a user and a customer service agent. The agent ALWAYS follows the SOP exactly: correct node transitions, correct tool calls, correct policy compliance, correct refusal language."""

STAGE3_PROMPT_TEMPLATE = """Given this SOP and this conversation plan, produce the full realized conversation.

SOP:
{sop_rendered}

PLAN:
{plan_json}

Produce a JSON object with field `turns` — a list of turns, where each turn is one of:
- `{{"kind": "user", "turn_index": N, "content": "..."}}`
- `{{"kind": "assistant", "turn_index": N, "current_node": "...", "next_node": "...", "edge_taken": "...", "tool_call": {{"name": "...", "arguments": {{...}}}}, "response_to_user": null}}`
- `{{"kind": "tool_result", "turn_index": N, "name": "...", "result": {{...}}}}`

Critical rules the agent MUST follow:
1. Authenticate via tool BEFORE any other action. The authentication tool belongs to an AUTH-type node, NOT to START. `current_node` for the auth tool call should be "AUTH" (or whatever the graph's authentication node is named).
2. ONE tool call per assistant turn. Never combine a tool call with a user-facing response.
3. `current_node` is where the agent is at the START of this turn (before transitioning).
4. `next_node` is where the agent will be after this turn. If the agent is collecting info or waiting for a tool result, `next_node` may equal `current_node`.
5. `tool_call.name` MUST be in the `tool_hints` of `current_node`. If a tool isn't listed in the current node's `tool_hints`, you cannot call it from there — transition to the node that owns the tool first. Use "calculate" as a global utility for arithmetic.
6. INTENT-ROUTING nodes (Rhombus nodes with empty tool_hints, often called ROUTE) cannot call any tools. The agent only passes through them — `current_node: ROUTE` means the agent is classifying user intent, not making a tool call. If a tool needs to be called, set `current_node` to the destination node (the one whose `tool_hints` contains the tool) and set `next_node` to where it goes next.
7. Before any mutation (cancel, return, exchange, modify, transfer), the agent lists the full action details and asks for confirmation, appending verbatim the confirmation phrase from `confirmation_before_mutations`.
8. The node sequence (consecutive `current_node` -> `next_node`) must follow the plan's `expected_node_sequence` and respect the graph's edges.
9. Tool results should be plausible structured data consistent with the user persona and prior conversation.
10. Use ONLY node IDs that appear in the Mermaid graph. Do NOT invent new node names (e.g., if the graph has CANCEL_CHECK but not CANCEL_FLOW, only use CANCEL_CHECK).

Output ONLY the JSON object. No backticks, no commentary."""


def stage3_prompt(sop_rendered: str, plan_json: str) -> str:
    return STAGE3_PROMPT_TEMPLATE.format(sop_rendered=sop_rendered, plan_json=plan_json)


# ============================================================
# Stage 5: Continuation generation
# ============================================================

STAGE5_SYSTEM = """You produce short, situational free-form reasoning that supplements an already-existing structured trace. Most turns do NOT need a continuation. Only produce one when the structured trace alone cannot capture the planning required."""

STAGE5_PROMPT_TEMPLATE = """Given a conversation context, a planned assistant turn, and the structured trace already generated for that turn, decide whether a free-form continuation of reasoning is warranted.

A continuation IS warranted when:
- The turn requires combining multiple facts (e.g., gift card balance vs. price diff)
- The turn requires planning a multi-step gather across future turns
- The turn involves a cross-flow jump (one intent's check node routing to another's)
- The user's input is ambiguous and the agent needs to decide how to disambiguate
- A policy conflict requires weighing options before responding

A continuation is NOT warranted for:
- Routine intent routing
- Single tool calls following an unambiguous policy
- Acknowledgments after tool returns
- Standard confirmations
- Most happy-path turns

When producing a continuation:
- 1 to 4 sentences MAX
- Must NOT contradict the structured trace
- Must NOT reference nodes, tools, or policies that aren't in the SOP
- Should describe planning the trace doesn't capture, not restate the trace

SOP:
{sop_rendered}

CONVERSATION SO FAR:
{conversation_so_far}

PLANNED TURN (with structured trace):
{turn_with_trace}

Output a JSON object: `{{"needed": true|false, "rationale": "<one sentence explaining your decision>", "text": "<continuation text, or empty string if not needed>"}}`. Output JSON only, no backticks."""


def stage5_prompt(sop_rendered: str, conversation_so_far: str, turn_with_trace: str) -> str:
    return STAGE5_PROMPT_TEMPLATE.format(
        sop_rendered=sop_rendered,
        conversation_so_far=conversation_so_far,
        turn_with_trace=turn_with_trace,
    )