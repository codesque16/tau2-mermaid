"""GEPA qualitative diagnosis for retail (tau2-mermaid solo / MCP path).

LLM feedback for failed policy evaluations — same prompt shape as the historical tau2-bench
``gepa_eval`` diagnosis, without importing tau2-bench.
"""

from __future__ import annotations

import asyncio
import re
import json
from typing import Any


def _inline_json_dict(obj: object | None) -> str:
    try:
        return json.dumps(obj or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(obj or {})


def format_openai_style_history_dicts(
    messages: list[dict[str, Any]] | None,
    *,
    max_messages: int | None = None,
) -> str:
    """Format LiteLLM/OpenAI-style message dicts (e.g. Gemini ``history``) for diagnosis prompts."""
    if not messages:
        return ""

    out: list[str] = []
    iterable = messages if max_messages is None else messages[:max_messages]
    for msg in iterable:
        role = str(msg.get("role") or "unknown").lower()
        content_raw = msg.get("content", None)
        content = str(content_raw).strip() if content_raw is not None else ""

        if role == "user":
            out.append("[User]:")
            out.append(f"  {content}" if content else "  (no text)")

        elif role == "assistant":
            out.append("[Assistant]:")
            reasoning_raw = msg.get("reasoning_content") or msg.get("thought")
            reasoning = str(reasoning_raw).strip() if reasoning_raw is not None else ""
            if reasoning:
                out.append("  (Reasoning)")
                out.extend([f"    {line}" for line in reasoning.splitlines()])
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get("name") or "?"
                        tool_id = tc.get("id") or tc.get("tool_id") or "unknown"
                        args = tc.get("arguments")
                        out.append(f"  (ToolCall : {tool_id}) {name} {_inline_json_dict(args)}")
            if content:
                out.extend([f"  {line}" for line in content.splitlines()])

        elif role == "tool":
            tool_id = msg.get("tool_call_id") or msg.get("id") or "unknown"
            out.append(f"  (Tool output: {tool_id})")
            if content:
                out.extend([f"    {line}" for line in content.splitlines()])
            else:
                out.append("    (empty)")

        else:
            out.append(f"[{role.title()}]:")
            out.append(f"  {content}" if content else "  (no text)")

        out.append("")

    if max_messages is not None and len(messages) > max_messages:
        out.append(f"... ({len(messages) - max_messages} more messages)")
        out.append("")

    return "\n".join(out).strip()


def retail_tools_list_for_gepa_diagnosis(
    *,
    mcp_command: str,
    tools_markdown_path: str | None = None,
) -> str:
    """Tool names/descriptions from the retail MCP server (stdio ``list_tools``)."""
    md_path = (tools_markdown_path or "").strip()
    if md_path:
        try:
            return open(md_path, encoding="utf-8").read().strip()
        except Exception as e:
            return f"(Failed to read tools markdown file {md_path!r}: {type(e).__name__}: {e})"

    cmd = (mcp_command or "").strip()
    if not cmd:
        return (
            "(No MCP command configured; tool list omitted — infer tools from "
            "<conversation_trace>.)"
        )
    from domains.retail.evaluate import list_mcp_tools_tau2_style

    try:
        return asyncio.run(list_mcp_tools_tau2_style(mcp_command=cmd))
    except Exception as e:
        return f"(Failed to list MCP tools: {type(e).__name__}: {e})"


def diagnose_single_retail_failure_for_gepa(
    *,
    task_description: str,
    tools_list: str,
    evaluation_text: str,
    conversation_trace: str,
    policy_preview: str,
    diagnosis_lm: str,
    diagnosis_prompt_template: str | None = None,
    diagnosis_llm_backend: str = "litellm",
    diagnosis_genai_temperature: float | None = None,
    diagnosis_genai_max_output_tokens: int | None = None,
    diagnosis_genai_reasoning_effort: str | None = None,
    diagnosis_genai_vertex_ai: bool = False,
) -> str:
    """LLM diagnosis + policy improvement suggestions (retail), one failed task.

    If ``diagnosis_prompt_template`` is set (from ``# Evaluator`` in reflection prompts markdown), it must be a
    :class:`string.Template` with ``$task_desc``, ``$tools_list``, ``$reward_info``, ``$trace``,
    ``$policy_preview`` (``evaluation_text`` is substituted as ``$reward_info``).

    ``diagnosis_llm_backend``:
      - ``litellm`` — ``diagnosis_lm`` is a LiteLLM model id (e.g. ``gemini/gemini-3-flash-preview``).
      - ``genai`` — ``diagnosis_lm`` is a Gemini API model id; uses ``google.genai`` (same Logfire hook as the solo agent).
      - ``openai`` / ``anthropic`` — native SDK + Logfire spans (``agent.openai_gepa_lm`` / ``agent.anthropic_gepa_lm``).
    """
    backend = (diagnosis_llm_backend or "litellm").strip().lower()
    if backend not in ("litellm", "genai", "openai", "anthropic"):
        return f"(Diagnosis error: unknown diagnosis_llm_backend {diagnosis_llm_backend!r})"

    if diagnosis_prompt_template and str(diagnosis_prompt_template).strip():
        from string import Template

        try:
            prompt = Template(diagnosis_prompt_template.strip()).substitute(
                task_desc=task_description,
                tools_list=tools_list,
                reward_info=evaluation_text,
                trace=conversation_trace,
                policy_preview=policy_preview,
            )
        except Exception as e:
            return f"(Diagnosis template error: {type(e).__name__}: {e})"
    else:
        prompt = f"""You are an evaluator producing feedback for a retail customer-service trace.

Your goal is to analyse the <current_policy> trace and reward info and give a diagnostic analysis of what went wrong along with policy improvements to the <current_policy>, 
BUT you are only allowed to suggest changes within EXACTLY these three sections:
1) SOP Global Policies
2) SOP Node Policies
3) SOP Flowchart

You MUST output feedback for this trace (it is a failed trace) in the following <format>. 

<format>
### Diagnostic Analysis
...

### Policy Improvements
1) SOP Global Policies
    ...
2) SOP Node Policies
    ...
3) SOP Flowchart
    ...
</format>

<task>
{task_description}
</task>

<tools_list>
{tools_list}
</tools_list>

<evaluation>
{evaluation_text}
</evaluation>

<conversation_trace>
{conversation_trace}
</conversation_trace>

<current_policy>
{policy_preview}
</current_policy>
"""

    if backend == "genai":
        try:
            from agent.genai_gepa_lm import genai_generate_user_text
        except ImportError as e:
            return f"(Diagnosis skipped: cannot import agent.genai_gepa_lm: {e})"
        try:
            sys_m = re.search(
                r"<gepa_system_prompt>(.*?)</gepa_system_prompt>",
                prompt,
                flags=re.DOTALL | re.IGNORECASE,
            )
            user_m = re.search(
                r"<gepa_first_user_message>(.*?)</gepa_first_user_message>",
                prompt,
                flags=re.DOTALL | re.IGNORECASE,
            )
            system_text = (sys_m.group(1).strip() if sys_m else "").strip()
            if user_m:
                user_text = user_m.group(1).strip()
            else:
                user_text = prompt.replace(sys_m.group(0), "").strip() if sys_m else prompt.strip()

            temp = 0.3 if diagnosis_genai_temperature is None else float(diagnosis_genai_temperature)
            text = genai_generate_user_text(
                diagnosis_lm.strip(),
                user_text,
                temperature=temp,
                max_output_tokens=diagnosis_genai_max_output_tokens,
                reasoning_effort=diagnosis_genai_reasoning_effort,
                system_instruction=system_text or None,
                io_phase="gepa_eval",
                vertex_ai=bool(diagnosis_genai_vertex_ai),
            )
            return text.strip()
        except Exception as e:
            return f"(Diagnosis error: {e})"

    if backend == "openai":
        try:
            from agent.openai_gepa_lm import openai_generate_user_text
        except ImportError as e:
            return f"(Diagnosis skipped: cannot import agent.openai_gepa_lm: {e})"
        try:
            dm = diagnosis_lm.strip()
            if dm.startswith("openai/"):
                dm = dm.split("/", 1)[1].strip()
            sys_m = re.search(
                r"<gepa_system_prompt>(.*?)</gepa_system_prompt>",
                prompt,
                flags=re.DOTALL | re.IGNORECASE,
            )
            user_m = re.search(
                r"<gepa_first_user_message>(.*?)</gepa_first_user_message>",
                prompt,
                flags=re.DOTALL | re.IGNORECASE,
            )
            system_text = (sys_m.group(1).strip() if sys_m else "").strip()
            if user_m:
                user_text = user_m.group(1).strip()
            else:
                user_text = prompt.replace(sys_m.group(0), "").strip() if sys_m else prompt.strip()

            temp = 0.3 if diagnosis_genai_temperature is None else float(diagnosis_genai_temperature)
            text = openai_generate_user_text(
                dm,
                user_text,
                system_text=system_text or None,
                temperature=temp,
                max_tokens=diagnosis_genai_max_output_tokens,
                reasoning_effort=diagnosis_genai_reasoning_effort,
            )
            return text.strip()
        except Exception as e:
            return f"(Diagnosis error: {e})"

    if backend == "anthropic":
        try:
            from agent.anthropic_gepa_lm import anthropic_generate_user_text
        except ImportError as e:
            return f"(Diagnosis skipped: cannot import agent.anthropic_gepa_lm: {e})"
        try:
            dm = diagnosis_lm.strip()
            if dm.startswith("anthropic/"):
                dm = dm.split("/", 1)[1].strip()
            sys_m = re.search(
                r"<gepa_system_prompt>(.*?)</gepa_system_prompt>",
                prompt,
                flags=re.DOTALL | re.IGNORECASE,
            )
            user_m = re.search(
                r"<gepa_first_user_message>(.*?)</gepa_first_user_message>",
                prompt,
                flags=re.DOTALL | re.IGNORECASE,
            )
            system_text = (sys_m.group(1).strip() if sys_m else "").strip()
            if user_m:
                user_text = user_m.group(1).strip()
            else:
                user_text = prompt.replace(sys_m.group(0), "").strip() if sys_m else prompt.strip()

            temp = 0.3 if diagnosis_genai_temperature is None else float(diagnosis_genai_temperature)
            text = anthropic_generate_user_text(
                dm,
                user_text,
                system_text=system_text or None,
                temperature=temp,
                max_tokens=diagnosis_genai_max_output_tokens,
                reasoning_effort=diagnosis_genai_reasoning_effort,
            )
            return text.strip()
        except Exception as e:
            return f"(Diagnosis error: {e})"

    try:
        from litellm import completion
    except ImportError:
        return "(qualitative ASI skipped: litellm not available)"

    try:
        sys_m = re.search(
            r"<gepa_system_prompt>(.*?)</gepa_system_prompt>",
            prompt,
            flags=re.DOTALL | re.IGNORECASE,
        )
        user_m = re.search(
            r"<gepa_first_user_message>(.*?)</gepa_first_user_message>",
            prompt,
            flags=re.DOTALL | re.IGNORECASE,
        )
        system_text = (sys_m.group(1).strip() if sys_m else "").strip()
        if user_m:
            user_text = user_m.group(1).strip()
        else:
            user_text = prompt.replace(sys_m.group(0), "").strip() if sys_m else prompt.strip()

        messages: list[dict[str, Any]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": user_text})

        resp = completion(
            model=diagnosis_lm,
            messages=messages,
            temperature=0.3,
        )
        try:
            from agent.gemini_log import log_litellm_raw_io

            log_litellm_raw_io(
                phase="gepa_eval",
                model=diagnosis_lm,
                messages=messages,
                completion=resp,
                extra_completion_kwargs={"temperature": 0.3},
            )
        except ImportError:
            pass
        text = resp.choices[0].message.content or ""
        return text.strip()
    except Exception as e:
        return f"(Diagnosis error: {e})"
