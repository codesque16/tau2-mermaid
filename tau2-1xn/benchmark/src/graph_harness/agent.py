"""
Agent loop for graph traversal: calls LLM, executes transition_to_node tool, records path.
"""

from __future__ import annotations

import json
from typing import Any

import litellm

from .conditions import (
    CONDITION_MERMAID_HARNESS,
    get_system_prompt,
    TRANSITION_TOOL,
)
from .harness import GraphHarness


def run_agent(
    *,
    prose: str,
    mermaid: str,
    condition: str,
    user_prompt: str,
    model: str = "gpt-4o-mini",
    max_turns: int = 30,
    reminder_every_n: int = 5,
    expected_path: list[str] | None = None,
    decision_points: dict[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    """
    Run the agent on a single test case. Returns (path, messages, completed).

    If decision_points is provided and a decision point has outcome_for_llm, when
    the agent transitions to that node we add only that natural-language outcome
    to the tool return (e.g. "L1 can resolve: yes"). We do not tell the model
    the correct next node — it must infer from the workflow.
    """
    system_content = get_system_prompt(condition, prose, mermaid)

    # Path tracker: always used to record transitions.
    # Harness mode: validate transitions. Prose/Mermaid: record only.
    validate = condition == CONDITION_MERMAID_HARNESS
    harness = GraphHarness(mermaid, validate_transitions=validate)

    if condition == CONDITION_MERMAID_HARNESS:
        system_content = get_system_prompt(
            condition, prose, mermaid, state_reminder=harness.get_state_reminder()
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_prompt},
    ]

    tools = [TRANSITION_TOOL]
    turn = 0
    completed = False

    while turn < max_turns:
        turn += 1
        response = litellm.completion(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        assistant_content = msg.content or ""
        tool_calls = getattr(msg, "tool_calls", None) or []

        messages.append(
            {
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        transition_calls = [
            tc
            for tc in tool_calls
            if getattr(tc.function, "name", None) == "transition_to_node"
        ]

        for tc in transition_calls:
            try:
                args = json.loads(tc.function.arguments)
                node_id = args.get("node_id", "")
            except Exception:
                node_id = ""
            if not node_id:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"error": "Missing node_id"}),
                    }
                )
                continue

            ok, result_msg = harness.transition(node_id)
            content: dict[str, Any] = {"success": ok, "message": result_msg}
            # At a decision node: inject only the natural-language outcome (e.g. "L1 can resolve: yes").
            # Do NOT feed the path or correct next node — the model must infer the next step from the workflow.
            if ok and decision_points and node_id in decision_points:
                dp = decision_points[node_id]
                outcome = dp.get("outcome_for_llm")
                if outcome:
                    content["decision_outcome"] = outcome
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(content),
                }
            )
            if harness.is_at_end():
                completed = True
                break

        if completed:
            break

        # Harness reminder (only for mermaid+harness)
        if condition == CONDITION_MERMAID_HARNESS:
            reminder = harness.inject_reminder(every_n=reminder_every_n)
            if reminder:
                messages.append({"role": "user", "content": reminder})

        if not tool_calls:
            break

    path = harness.get_path()
    return path, messages, completed
