"""
Three representation conditions for the ablation benchmark:
A) Plain Markdown (prose)
B) Mermaid Only
C) Mermaid + Harness
"""

from __future__ import annotations

from pathlib import Path

CONDITION_PROSE = "prose"
CONDITION_MERMAID = "mermaid"
CONDITION_MERMAID_HARNESS = "mermaid_harness"

CONDITIONS = [CONDITION_PROSE, CONDITION_MERMAID, CONDITION_MERMAID_HARNESS]


def load_scenario(scenario_dir: Path) -> tuple[str, str]:
    """Load graph_prose.md and graph.mermaid from scenario dir."""
    prose_path = scenario_dir / "graph_prose.md"
    mermaid_path = scenario_dir / "graph.mermaid"
    prose = prose_path.read_text() if prose_path.exists() else ""
    mermaid = mermaid_path.read_text() if mermaid_path.exists() else ""
    return prose, mermaid


def get_system_prompt(
    condition: str,
    prose: str,
    mermaid: str,
    *,
    state_reminder: str | None = None,
) -> str:
    """
    Build system prompt for the given condition.
    """
    base = """You are a customer service agent following a structured workflow. Your job is to help the user by following the workflow steps in order.

IMPORTANT: Once the user sends their request, perform ALL necessary steps from start to end. Do NOT ask the user for any additional input, clarification, or confirmation. Assume you have all the information you need from their message. Execute the complete workflow path autonomously in a single response (or sequence of tool calls).

You MUST call the transition_to_node tool each time you move to a new step. You begin at the start node. As you complete each step, call transition_to_node with the NEXT node ID you are moving to. Do not skip steps. Follow the workflow precisely. Use the exact node IDs from the workflow (e.g. verify_order, check_status, end)."""

    if condition == CONDITION_PROSE:
        return f"""{base}

## Workflow (Natural Language Description)

{prose}

Proceed through the workflow from start to end. Call transition_to_node for each step you move to. You MUST include the final step: call transition_to_node with the end node (e.g. "end") when the workflow is complete. Do not stop before reaching the end node."""

    if condition == CONDITION_MERMAID:
        return f"""{base}

## Workflow (Mermaid Diagram)

```mermaid
{mermaid}
```

Proceed through the workflow from start to end. Call transition_to_node with the exact node ID for each step as you progress. Follow the edges in the diagram."""

    if condition == CONDITION_MERMAID_HARNESS:
        reminder = f"\n\n{state_reminder}" if state_reminder else ""
        return f"""{base}

## Workflow (Mermaid Diagram)

```mermaid
{mermaid}
```
{reminder}

You are in a harness that validates your transitions. Only valid next steps (per the graph edges) will be accepted. Invalid transitions will return an error. Call transition_to_node with the exact node ID for each step."""

    raise ValueError(f"Unknown condition: {condition}")


# Tool schema for transition_to_node (OpenAI function format)
TRANSITION_TOOL = {
    "type": "function",
    "function": {
        "name": "transition_to_node",
        "description": "Call this when you move to a workflow step. Pass the exact node ID from the workflow graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "The node ID from the workflow (e.g. verify_order, check_status, end)",
                }
            },
            "required": ["node_id"],
        },
    },
}
