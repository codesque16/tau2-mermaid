"""Format ``user_scenario`` from retail tasks JSON (tau2-bench compatible)."""

from __future__ import annotations

import textwrap
from typing import Any


# Matches tau2 ``user_simulator.SYSTEM_PROMPT`` footer.
USER_SIM_FOOTER = (
    "DO NOT SAY OR ADD INFORMATION / REQUESTS / QUERIES OUTSIDE OF THE INSTRUCTIONS ABOVE. "
    "THIS IS ALL YOU KNOW"
)


def _format_structured_instructions(inst: dict[str, Any]) -> str:
    """Same string layout as tau2 ``StructuredUserInstructions.__str__``."""
    lines: list[str] = []
    tab = "\t"
    domain = inst.get("domain") or "retail"
    lines.append(f"Domain: {domain}")
    rfc = inst.get("reason_for_call") or ""
    lines.append(f"Reason for call:\n{textwrap.indent(str(rfc), tab)}")
    if inst.get("known_info") is not None:
        lines.append(f"Known info:\n{textwrap.indent(str(inst['known_info']), tab)}")
    if inst.get("unknown_info") is not None:
        lines.append(f"Unknown info:\n{textwrap.indent(str(inst['unknown_info']), tab)}")
    ti = inst.get("task_instructions") or ""
    lines.append(f"Task instructions:\n{textwrap.indent(str(ti), tab)}")
    return "\n".join(lines)


def user_instructions_to_string(task: dict[str, Any]) -> str:
    """Turn ``task['user_scenario']['instructions']`` into one scenario block."""
    us = task.get("user_scenario") or {}
    inst = us.get("instructions")
    if isinstance(inst, str):
        return inst.strip()
    if isinstance(inst, dict):
        return _format_structured_instructions(inst)
    return ""


def build_user_system_prompt(
    task: dict[str, Any],
    *,
    guidelines_text: str,
) -> str:
    """
    Full user-simulator system prompt: global guidelines + ``<scenario>`` + tau2 footer.

    ``guidelines_text`` is typically ``simulation_guidelines.md`` contents.
    """
    us = task.get("user_scenario") or {}
    lines: list[str] = [guidelines_text.strip(), "", "<scenario>"]
    persona = (us.get("persona") or "").strip()
    if persona:
        lines.append("Persona:")
        lines.append(textwrap.indent(persona, "\t"))
    lines.append("Instructions:")
    lines.append(textwrap.indent(user_instructions_to_string(task), "\t"))
    lines.append("</scenario>")
    lines.append("")
    lines.append(USER_SIM_FOOTER)
    return "\n".join(lines)


def initial_message_from_task(task: dict[str, Any]) -> str:
    """First customer message when none is set in YAML (short opener from known_info / reason)."""
    us = task.get("user_scenario") or {}
    inst = us.get("instructions")
    if isinstance(inst, dict):
        ki = (inst.get("known_info") or "").strip()
        if ki:
            return f"Hi — I need help. {ki}"
        rfc = (inst.get("reason_for_call") or "").strip()
        if rfc:
            first = rfc.split(".")[0].strip()
            if first:
                return f"Hi — I need help with something. {first}."
    return "Hi, I need help with my order."
