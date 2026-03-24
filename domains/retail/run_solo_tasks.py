"""Run solo simulations for the retail domain over a tasks file.

This script:
  - Reads a solo simulation config (YAML) for the retail domain
  - Loads a tasks JSON file (e.g. tasks_solo_comms.json)
  - For each task, builds a prompt (using the ticket) and runs a solo simulation
    via the shared Orchestrator.run_solo API.

Sweep mode: `domain.policy`, `assistant.model`, `assistant.temperature`, and
`assistant.reasoning_effort` may each be a scalar or a YAML list. The run expands
the Cartesian product and executes each combo as a separate top-level Logfire span
(see `experiment_concurrency` / `--experiment-concurrency` for parallel combos).

When ``domain.output_task_transcripts`` is true, each task JSON includes
``conversation_history`` (first message is ``role: system`` with the effective
system prompt, then user/assistant/tool turns) and ``evaluation`` (metrics,
hashes, optional DB snapshots) without duplicating the message list under
``evaluation``.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime
import json
import logging
import math
import random
from itertools import product
from pathlib import Path
from typing import Any, Dict, List
import re
import hashlib

import yaml
import logfire
import os
from dotenv import load_dotenv

from agent import create_agent
from agent.config import AgentConfig as AgentAgentConfig
from chat.config import AgentConfig as ChatAgentConfig, SimulationConfig
from domains.retail.evaluate import evaluate_communication_from_history, evaluate_task_db
from orchestrator.event_bus import EventBus
from orchestrator.orchestrator import Orchestrator, SoloStopMode

from agent.api_key_rotation import configure_from_simulation_dict


def _make_reference_steps_from_task(task: dict[str, Any]) -> str:
    """
    Build an ordered, guidance-only list of tool names for this task.

    Uses `task["evaluation_criteria"]["actions"]` and includes tool names only
    (no arguments / no tool outputs).
    """

    evaluation_criteria = task.get("evaluation_criteria") or {}
    actions = evaluation_criteria.get("actions") or []

    tool_names: list[str] = []
    for a in actions:
        requestor = a.get("requestor") or "assistant"
        if requestor != "assistant":
            continue
        name = a.get("name")
        if name:
            tool_names.append(str(name))

    # Fallback: if requestor metadata is missing, include anything with a name.
    if not tool_names:
        for a in actions:
            name = a.get("name")
            if name:
                tool_names.append(str(name))

    if tool_names:
        numbered = "\n".join([f"{i + 1}) {name}" for i, name in enumerate(tool_names)])
    else:
        numbered = "(no reference tool calls available)"

    return "For similar problems the following flow may be relevant:\n" + numbered


def _load_retail_solo_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_simulation_config(raw: dict[str, Any]) -> SimulationConfig:
    """Adapt the retail-specific solo YAML into a SimulationConfig."""
    assistant_block: Dict[str, Any] = raw.get("assistant") or {}

    assistant_chat_cfg = ChatAgentConfig(
        system_prompt=assistant_block.get("system_prompt", ""),
        temperature=assistant_block.get("temperature", 0.7),
        max_tokens=assistant_block.get("max_tokens"),  # omit or null = unbounded
        mcps=assistant_block.get("mcps") or None,
        mermaid=assistant_block.get("mermaid") or None,
        mcp_tools_markdown_path=assistant_block.get("mcp_tools_markdown_path"),
        reasoning_effort=assistant_block.get("reasoning_effort"),
        vertex_ai=assistant_block.get("vertex_ai"),
    )
    # Solo mode: user agent is not used, but SimulationConfig expects one.
    user_chat_cfg = ChatAgentConfig(system_prompt="", temperature=0.0, max_tokens=1)

    return SimulationConfig(
        model=raw.get("model") or "",
        max_turns=raw.get("max_turns", 60),
        stop_phrases=raw.get("stop_phrases") or [],
        initial_message=None,
        assistant=assistant_chat_cfg,
        user=user_chat_cfg,
        assistant_model=assistant_block.get("model") or "",
        user_model=assistant_block.get("model") or "",
        assistant_agent_type=(assistant_block.get("agent_type") or "").strip() or None,
        user_agent_type=(assistant_block.get("agent_type") or "").strip() or None,
        assistant_agent_name=None,
        user_agent_name=None,
        mcp_server_url=None,
        graph_id=None,
        mode=(raw.get("mode") or "solo"),
    )


def _to_agent_config(loaded: ChatAgentConfig) -> AgentAgentConfig:
    return AgentAgentConfig(
        system_prompt=loaded.system_prompt,
        max_tokens=loaded.max_tokens,
        temperature=loaded.temperature,
        reasoning_effort=getattr(loaded, "reasoning_effort", None),
        mcps=getattr(loaded, "mcps", None),
        mermaid=getattr(loaded, "mermaid", None),
        mcp_tools_markdown_path=getattr(loaded, "mcp_tools_markdown_path", None),
        vertex_ai=getattr(loaded, "vertex_ai", None),
    )


def _format_temp_for_run_id(t: Any) -> str:
    """Stable temp string for Logfire run_id (e.g. 0 not 0.0)."""
    if isinstance(t, bool):
        return str(int(t))
    if isinstance(t, float) and t.is_integer():
        return str(int(t))
    return str(t)


def _safe_path_component(value: str, *, max_len: int = 120) -> str:
    """
    Make a filesystem-safe path component without creating collisions too easily.
    """
    v = value or ""
    v = v.strip()
    # Replace common problematic path characters with underscores.
    v = re.sub(r"[^a-zA-Z0-9._-]+", "_", v)
    if len(v) <= max_len:
        return v
    digest = hashlib.sha1(v.encode("utf-8")).hexdigest()[:10]
    return v[: max_len - 11] + "_" + digest


# Omitted from written transcript ``evaluation`` object (keep DB/comm metrics only).
_TRACE_EVAL_OMIT_KEYS = frozenset(
    {
        "golden_mermaid_path",
        "path_mismatch",
        "golden_actions_count",
        "predicted_actions_count",
        "trace_preview",
    }
)


def _write_json_atomic(path: Path, payload: Any) -> None:
    """
    Write JSON to disk with an atomic rename, to avoid partial files if multiple
    tasks run concurrently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, default=str)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _default_run_id_from_assistant(
    domain_name: str,
    policy_basename: str,
    assistant_model: str,
    sim_cfg: SimulationConfig,
) -> str:
    """Logfire top span: [domain][policy_file][model][temp=…][reasoning="…"]."""
    base = f"[{domain_name}][{policy_basename}][{assistant_model}]"
    a = sim_cfg.assistant
    suffix = f"[temp={_format_temp_for_run_id(a.temperature)}]"
    reff = getattr(a, "reasoning_effort", None)
    if reff is not None and str(reff).strip():
        safe = str(reff).strip().replace('"', "'")
        suffix += f'[reasoning="{safe}"]'
    return base + suffix


def _fresh_run_suffix() -> str:
    """Suffix like '03-23_14-05' to keep transcript dirs from overwriting."""
    return datetime.now().strftime("%m-%d_%H-%M")


def _sweep_list(value: Any, *, default_when_missing: Any = None) -> List[Any]:
    """Scalar or YAML list → list of sweep values. `default_when_missing` used when value is None."""
    if value is None:
        return [default_when_missing] if default_when_missing is not None else []
    if isinstance(value, list):
        return list(value)
    return [value]


def _expand_experiment_raw_configs(raw_cfg: dict[str, Any]) -> List[dict[str, Any]]:
    """Cartesian product over domain.policy × assistant.model × temperature × reasoning_effort.

    Each list may be a single scalar or a YAML list. Missing assistant fields use sensible singles.
    """
    domain_cfg: Dict[str, Any] = dict(raw_cfg.get("domain") or {})
    assistant_block: Dict[str, Any] = dict(raw_cfg.get("assistant") or {})

    policies = _sweep_list(domain_cfg.get("policy"))
    if not policies:
        raise ValueError("domain.policy must be set (string or list of paths).")

    if assistant_block.get("model") is not None:
        models = _sweep_list(assistant_block.get("model"))
    elif raw_cfg.get("model") is not None:
        models = _sweep_list(raw_cfg.get("model"))
    else:
        models = [""]
    if not models:
        models = [""]

    if "temperature" in assistant_block:
        temps = _sweep_list(assistant_block.get("temperature"))
    else:
        temps = [0.7]
    if not temps:
        raise ValueError(
            "assistant.temperature must be a scalar or a non-empty list when sweeping."
        )

    if "reasoning_effort" in assistant_block:
        reasonings = _sweep_list(assistant_block.get("reasoning_effort"))
    else:
        reasonings = [None]
    if not reasonings:
        reasonings = [None]

    out: List[dict[str, Any]] = []
    for pol, model, temp, reff in product(policies, models, temps, reasonings):
        rc = copy.deepcopy(raw_cfg)
        dom = dict(rc.get("domain") or {})
        dom["policy"] = pol
        rc["domain"] = dom
        asst = dict(rc.get("assistant") or {})
        asst["model"] = model
        asst["temperature"] = temp
        asst["reasoning_effort"] = reff
        rc["assistant"] = asst
        out.append(rc)
    # A shared domain.run_id would collide across combos; only allow it for a single experiment.
    if len(out) > 1:
        for rc in out:
            d = rc.get("domain")
            if isinstance(d, dict):
                d.pop("run_id", None)
    return out


def _experiment_config_record(
    raw_cfg: dict[str, Any], sim_cfg: SimulationConfig, policy_path: str
) -> dict[str, Any]:
    """JSON-serializable snapshot for Logfire (one experiment)."""
    domain_cfg = raw_cfg.get("domain") or {}
    assistant_block = raw_cfg.get("assistant") or {}
    return {
        "domain.name": domain_cfg.get("name"),
        "domain.policy": str(policy_path),
        "domain.policy_basename": Path(str(policy_path)).name,
        "domain.tasks": domain_cfg.get("tasks"),
        "domain.instructions": domain_cfg.get("instructions"),
        "domain.stop_mode": domain_cfg.get("stop_mode"),
        "domain.concurrency": domain_cfg.get("concurrency"),
        "domain.trials": domain_cfg.get("trials"),
        "domain.task_ids": domain_cfg.get("task_ids"),
        "domain.evaluate_communication": domain_cfg.get("evaluate_communication"),
        "domain.seed": domain_cfg.get("seed"),
        "assistant.agent_type": assistant_block.get("agent_type"),
        "assistant.model": assistant_block.get("model") or raw_cfg.get("model"),
        "assistant.temperature": assistant_block.get("temperature"),
        "assistant.reasoning_effort": assistant_block.get("reasoning_effort"),
        "assistant.max_tokens": assistant_block.get("max_tokens"),
        "max_turns": raw_cfg.get("max_turns"),
        "mode": raw_cfg.get("mode"),
    }


def _load_tasks(path: Path) -> List[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a list of tasks in {path}, got {type(data)}")
    return data


def make_orchestrator_for_solo(
    instructions_text: str,
    policy_text: str,
    sim_cfg: SimulationConfig,
    seed: int | None = None,
    mermaid_graph_path: str | None = None,
    include_policy: bool = True,
    reference_steps: str = "",
):
    """Return a callable (ticket: str) -> Orchestrator using the same logic as solo runs.

    Reused by run_solo_tasks and run_gepa_optimize so mermaid, mcps, and prompt
    construction stay in one place.
    """
    assistant_base_cfg = sim_cfg.assistant
    assistant_model = sim_cfg.assistant_model or sim_cfg.model
    assistant_agent_type = sim_cfg.assistant_agent_type or "litellm"

    def _make(_ticket: str) -> Orchestrator:
        _ = instructions_text  # still passed by callers; not wrapped in <instructions> in prompt
        has_mermaid = bool(getattr(assistant_base_cfg, "mermaid", None))
        ref_block = (
            f"\n\n<reference_steps>\n{reference_steps}\n</reference_steps>"
            if reference_steps.strip()
            else ""
        )
        if has_mermaid:
            # Ticket is provided in the first *user* message (not system prompt)
            # so it shows up in conversation traces.
            full_system_prompt = ref_block.strip()
        else:
            # Policy = raw markdown (no <policy> wrapper). Ticket is provided
            # in the first user message (not the system prompt).
            # ``instructions_text`` is kept in the API for callers but not injected (see history below).
            # if include_policy:
            #     full_system_prompt = (
            #         "<instructions>\n"
            #         f"{instructions_text.strip()}\n"
            #         "</instructions>\n\n"
            #         "<policy>\n"
            #         f"{policy_text.strip()}\n"
            #         "</policy>\n\n"
            #         "<ticket>\n"
            #         f"{ticket.strip()}\n"
            #         "</ticket>"
            #         f"{ref_block}"
            #     )
            # else:
            #     full_system_prompt = (
            #         "<instructions>\n"
            #         f"{instructions_text.strip()}\n"
            #         "</instructions>\n\n"
            #         "<ticket>\n"
            #         f"{ticket.strip()}\n"
            #         "</ticket>"
            #         f"{ref_block}"
            #     )
            if include_policy:
                # For non-mermaid mode, embed the policy in the system prompt.
                # The ticket is supplied as the first user message.
                full_system_prompt = f"{policy_text.strip()}{ref_block}".strip()
            else:
                # GEPA path: no embedded policy block; keep system prompt empty
                # (except for optional <reference_steps>).
                full_system_prompt = ref_block.strip()

        assistant_cfg = AgentAgentConfig(
            system_prompt=full_system_prompt,
            max_tokens=assistant_base_cfg.max_tokens,
            temperature=assistant_base_cfg.temperature,
            reasoning_effort=getattr(assistant_base_cfg, "reasoning_effort", None),
            mcps=getattr(assistant_base_cfg, "mcps", None),
            mermaid=_override_mermaid_graph(getattr(assistant_base_cfg, "mermaid", None), mermaid_graph_path),
            mcp_tools_markdown_path=getattr(assistant_base_cfg, "mcp_tools_markdown_path", None),
            seed=seed,
            vertex_ai=getattr(assistant_base_cfg, "vertex_ai", None),
        )
        assistant = create_agent(
            assistant_agent_type,
            "assistant",
            assistant_cfg,
            assistant_model,
        )
        user_cfg = AgentAgentConfig(
            system_prompt="",
            max_tokens=1,
            temperature=0.0,
            reasoning_effort="low",
            seed=seed,
        )
        user = create_agent(
            assistant_agent_type,
            "user",
            user_cfg,
            assistant_model,
        )
        bus = EventBus()
        return Orchestrator(assistant, user, bus, sim_cfg)

    return _make


def _override_mermaid_graph(mermaid_cfg: Any, mermaid_graph_path: str | None) -> Any:
    """Return mermaid config with graph overridden (supports dict or list[dict])."""
    if not mermaid_graph_path:
        return mermaid_cfg
    if isinstance(mermaid_cfg, dict):
        out = dict(mermaid_cfg)
        out["graph"] = mermaid_graph_path
        return out
    if isinstance(mermaid_cfg, list):
        out_list: list[Any] = []
        for entry in mermaid_cfg:
            if isinstance(entry, dict):
                e2 = dict(entry)
                e2["graph"] = mermaid_graph_path
                out_list.append(e2)
            else:
                out_list.append(entry)
        return out_list
    return mermaid_cfg


async def run_one_solo_task(
    instructions_text: str,
    policy_text: str,
    task: dict[str, Any],
    sim_cfg: SimulationConfig,
    stop_mode: SoloStopMode,
    db_path: Path,
    mcp_command: str,
    include_reference_steps: bool = False,
    seed: int | None = None,
    mermaid_graph_path: str | None = None,
    quiet: bool = False,
    include_policy: bool = True,
    evaluate_communication: bool = False,
    trace_dump: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Run a single solo task and return (success, eval_result).

    Shared by run_solo_tasks and run_gepa_optimize. When quiet=True, skips
    printing task header and result.
    """
    ticket = task.get("ticket") or ""
    reference_steps = (
        _make_reference_steps_from_task(task) if include_reference_steps else ""
    )
    task_id = task.get("id")
    solo_user_prompt = (
        "Resolve this ticket and respond in 1 single final reply.\n\n"
        f"<ticket>\n{ticket.strip()}\n</ticket>"
    )

    if not quiet:
        print(f"\n{'=' * 80}")
        print(f"Running task {task_id}")
        print("-" * 80)
        print(ticket)
        print("-" * 80)

    make_orch = make_orchestrator_for_solo(
        instructions_text,
        policy_text,
        sim_cfg,
        seed=seed,
        mermaid_graph_path=mermaid_graph_path,
        include_policy=include_policy,
        reference_steps=reference_steps,
    )
    orchestrator = make_orch(ticket)
    await orchestrator.run_solo(
        prompt=solo_user_prompt,
        stop_mode=stop_mode,
        task_complete_tools=["task_complete", "task_done"],
    )
    if hasattr(orchestrator.assistant, "aclose_mcp"):
        await orchestrator.assistant.aclose_mcp()

    history = getattr(orchestrator.assistant, "history", [])
    eval_result = await evaluate_task_db(
        task=task,
        assistant_history=history,
        db_path=db_path,
        mcp_command=mcp_command,
        return_db_state=trace_dump,
    )
    db_ok = bool(eval_result.get("db_match", False))
    if evaluate_communication:
        comm = evaluate_communication_from_history(task=task, assistant_history=history)
        eval_result.update(comm)
        comm_ok = bool(comm.get("communicate_match", True))
        success = db_ok and comm_ok
    else:
        success = db_ok

    if not quiet:
        print(
            f"DB match for task {eval_result.get('task_id')}: "
            f"{'PASS' if db_ok else 'FAIL'}"
        )
        if evaluate_communication:
            skipped = bool(eval_result.get("communicate_eval_skipped"))
            if skipped:
                print(
                    f"Communication check for task {eval_result.get('task_id')}: "
                    "PASS (no communicate_info)"
                )
            else:
                print(
                    f"Communication check for task {eval_result.get('task_id')}: "
                    f"{'PASS' if eval_result.get('communicate_match') else 'FAIL'}"
                )
    # For GEPA qualitative diagnosis (e.g. tau2.gepa_eval); pop before serializing side_info.
    eval_result["assistant_history"] = list(history)
    if trace_dump:
        for _k in _TRACE_EVAL_OMIT_KEYS:
            eval_result.pop(_k, None)
        # Effective system prompt is stored only as the first message in the trace (role=system).
        try:
            sys_prompt = orchestrator.assistant.get_effective_system_prompt()
        except Exception:
            cfg = getattr(orchestrator.assistant, "config", None)
            sys_prompt = getattr(cfg, "system_prompt", "") or ""
        eval_result["assistant_history"] = [
            {"role": "system", "content": sys_prompt if isinstance(sys_prompt, str) else str(sys_prompt)},
            *eval_result["assistant_history"],
        ]
        eval_result["stop_mode"] = stop_mode.value if hasattr(stop_mode, "value") else str(stop_mode)
        eval_result["task_ticket"] = ticket
        eval_result["trial_seed"] = seed
    return success, eval_result


async def _run_task(
    instructions_text: str,
    policy_text: str,
    task: dict[str, Any],
    sim_cfg: SimulationConfig,
    *,
    stop_mode: SoloStopMode,
    db_path: Path,
    mcp_command: str,
    trial_idx: int,
    include_reference_steps: bool,
    seed: int | None,
    results: List[Dict[str, Any]],
    evaluate_communication: bool,
    trace_dir: Path | None,
    trace_run_id: str,
    trace_experiment_index: int,
) -> None:
    task_id = task.get("id")
    ticket = task.get("ticket") or ""

    print(f"\n{'=' * 80}")
    print(f"Running task {task_id}")
    print("-" * 80)
    print(ticket)
    print("-" * 80)

    span_name = f"Task:{task_id}"
    with logfire.span(span_name) as task_span:
        with logfire.span("task_details"):
            logfire.info(
                "task_details",
                task_id=task_id,
                ticket_preview=(ticket[:300] + "..." if len(ticket) > 300 else ticket),
            )

        with logfire.span("simulation"):
            success, eval_result = await run_one_solo_task(
                instructions_text=instructions_text,
                policy_text=policy_text,
                task=task,
                sim_cfg=sim_cfg,
                stop_mode=stop_mode,
                db_path=db_path,
                mcp_command=mcp_command,
                include_reference_steps=include_reference_steps,
                seed=seed,
                quiet=True,
                include_policy=True,
                evaluate_communication=evaluate_communication,
                trace_dump=trace_dir is not None,
            )

        with logfire.span("evaluation"):
            logfire.info(
                "DB evaluation",
                task_id=eval_result["task_id"],
                db_match=eval_result["db_match"],
                golden_hash=eval_result.get("golden_hash"),
                predicted_hash=eval_result.get("predicted_hash"),
                golden_actions_count=eval_result.get("golden_actions_count"),
                predicted_actions_count=eval_result.get("predicted_actions_count"),
            )
            if evaluate_communication:
                logfire.info(
                    "Communication check",
                    task_id=eval_result["task_id"],
                    communicate_match=eval_result.get("communicate_match"),
                    communicate_eval_skipped=eval_result.get("communicate_eval_skipped"),
                    communicate_checks=eval_result.get("communicate_checks"),
                )

        outcome = "pass" if success else "fail"
        task_span.message = f"Task:{task_id} [{outcome}]"
        db_ok = bool(eval_result.get("db_match", False))
        print(
            f"DB match for task {eval_result.get('task_id')}: "
            f"{'PASS' if db_ok else 'FAIL'}"
        )
        if evaluate_communication:
            if eval_result.get("communicate_eval_skipped"):
                print(
                    f"Communication check for task {eval_result.get('task_id')}: "
                    "PASS (no communicate_info)"
                )
            else:
                print(
                    f"Communication check for task {eval_result.get('task_id')}: "
                    f"{'PASS' if eval_result.get('communicate_match') else 'FAIL'}"
                )

        results.append(
            {
                "task_id": task_id,
                "trial": trial_idx,
                "success": success,
            }
        )

    if trace_dir is not None:
        # One file per (task_id, trial, seed) to enable later reconstruction.
        task_trace_path = trace_dir / (
            f"task_{_safe_path_component(str(task_id))}"
            f"__trial_{trial_idx}"
            f"__seed_{_safe_path_component(str(seed))}"
            f".json"
        )
        # Full LLM+MCP conversation (all roles, tool calls, tool outputs, reasoning) lives here.
        # Keep evaluation.* for scores/metadata only to avoid duplicating the big message list.
        eval_snapshot = dict(eval_result)
        conversation_history = list(eval_snapshot.pop("assistant_history", []))
        payload = {
            "trace_run_id": trace_run_id,
            "trace_experiment_index": trace_experiment_index,
            "task_id": task_id,
            "trial": trial_idx,
            "seed": seed,
            "success": success,
            "conversation_history": conversation_history,
            "evaluation": eval_snapshot,
        }
        _write_json_atomic(task_trace_path, payload)


async def _run_solo_experiment_async(
    raw_cfg: dict[str, Any],
    *,
    include_reference_steps: bool,
    instructions_text: str,
    solo_tasks: List[dict[str, Any]],
    experiment_index: int,
    experiment_total: int,
    fresh: bool = False,
) -> None:
    """One Cartesian combo: same tasks/instructions, possibly different policy/model/temp/reasoning."""
    sim_cfg = _build_simulation_config(raw_cfg)
    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    policy_path = domain_cfg.get("policy")
    if not policy_path:
        raise ValueError("domain.policy missing in experiment config.")

    concurrency = int(domain_cfg.get("concurrency", 1))
    trials = int(domain_cfg.get("trials", 1))
    stop_mode_str = str(domain_cfg.get("stop_mode", "task-complete-tool")).lower()

    if stop_mode_str == "first-text":
        stop_mode = SoloStopMode.FIRST_TEXT_ONLY
    else:
        stop_mode = SoloStopMode.TASK_COMPLETE_TOOL

    policy_text = Path(str(policy_path)).read_text(encoding="utf-8")
    assistant_base_cfg = sim_cfg.assistant
    db_path = Path(domain_cfg.get("db_path", "domains/retail/db.json"))
    evaluate_communication = bool(domain_cfg.get("evaluate_communication", False))

    assistant_mcps = getattr(assistant_base_cfg, "mcps", None) or []
    mcp_command: str | None = None
    for server_cfg in assistant_mcps:
        if server_cfg.get("name") == "retail-tools" or not mcp_command:
            mcp_command = server_cfg.get("command") or server_cfg.get("commad")

    assistant_model_for_id = sim_cfg.assistant_model or sim_cfg.model or "unknown-model"
    domain_name = domain_cfg.get("name", "unknown-domain")
    policy_basename = Path(str(policy_path)).name
    run_id = domain_cfg.get(
        "run_id",
        _default_run_id_from_assistant(
            domain_name, policy_basename, assistant_model_for_id, sim_cfg
        ),
    )
    if fresh:
        run_id = f"{run_id}_{_fresh_run_suffix()}"

    output_task_transcripts = bool(domain_cfg.get("output_task_transcripts", False))
    trace_dir: Path | None = None
    if output_task_transcripts:
        output_base_dir = Path(domain_cfg.get("output_base_dir") or "outputs")
        trace_dir = (
            output_base_dir
            / _safe_path_component(str(run_id))
            / f"experiment_{experiment_index}"
        )
        trace_dir.mkdir(parents=True, exist_ok=True)

    # Master RNG: if ``domain.seed`` is set, it seeds this RNG only (not passed directly to the LLM).
    # Each trial gets ``trial_seeds[i]`` from this stream; that value is passed as ``seed`` on every LLM call
    # for that trial (see ``make_orchestrator_for_solo`` / ``AgentConfig.seed``).
    _domain_seed = domain_cfg.get("seed")
    if _domain_seed is not None:
        rng = random.Random(int(_domain_seed))
    else:
        rng = random.Random(300)
    trial_seeds = [rng.randint(1, 10**9) for _ in range(trials)]
    results: List[Dict[str, Any]] = []

    cfg_record = _experiment_config_record(raw_cfg, sim_cfg, str(policy_path))
    cfg_json = json.dumps(cfg_record, default=str, sort_keys=True)

    with logfire.span(run_id) as top_span:
        logfire.info(
            "experiment_config",
            experiment_index=experiment_index,
            experiment_total=experiment_total,
            run_id=run_id,
            config_json=cfg_json,
        )

        for trial_idx, seed in enumerate(trial_seeds, start=1):
            trial_span_name = f"Trial:{trial_idx} {seed}"
            with logfire.span(trial_span_name, trial=trial_idx, seed=seed):
                if concurrency <= 1:
                    for task in solo_tasks:
                        await _run_task(
                            instructions_text,
                            policy_text,
                            task,
                            sim_cfg,
                            stop_mode=stop_mode,
                            db_path=db_path,
                            mcp_command=mcp_command or "",
                            trial_idx=trial_idx,
                            seed=seed,
                            include_reference_steps=include_reference_steps,
                            results=results,
                            evaluate_communication=evaluate_communication,
                            trace_dir=trace_dir,
                            trace_run_id=str(run_id),
                            trace_experiment_index=experiment_index,
                        )
                else:
                    sem = asyncio.Semaphore(concurrency)

                    async def _runner(task: dict[str, Any]) -> None:
                        async with sem:
                            await _run_task(
                                instructions_text,
                                policy_text,
                                task,
                                sim_cfg,
                                stop_mode=stop_mode,
                                db_path=db_path,
                                mcp_command=mcp_command or "",
                                trial_idx=trial_idx,
                                seed=seed,
                                include_reference_steps=include_reference_steps,
                                results=results,
                                evaluate_communication=evaluate_communication,
                                trace_dir=trace_dir,
                                trace_run_id=str(run_id),
                                trace_experiment_index=experiment_index,
                            )

                    await asyncio.gather(*(_runner(t) for t in solo_tasks))

        results_by_task: Dict[Any, List[bool]] = {}
        for r in results:
            tid = r.get("task_id")
            if tid is None:
                continue
            results_by_task.setdefault(tid, []).append(bool(r.get("success")))

        num_tasks = len(results_by_task)
        max_k = min(4, trials)
        pass_k: Dict[str, float] = {}

        for k in range(1, max_k + 1):
            total = 0.0
            for outcomes in results_by_task.values():
                N = len(outcomes)
                S = sum(1 for s in outcomes if s)
                if N < k or S < k:
                    continue
                total += math.comb(S, k) / math.comb(N, k)
            metric = (total / num_tasks) if num_tasks > 0 else 0.0
            pass_k[f"pass^{k}"] = metric

        with logfire.span("metrics"):
            logfire.info(
                "pass^k metrics",
                trials=trials,
                num_tasks=num_tasks,
                pass_k=pass_k,
            )

        pass_1 = pass_k.get("pass^1")
        if pass_1 is not None:
            top_span.message = f"{run_id} [{pass_1:.2f}]"

    if experiment_total > 1:
        print(
            f"\n[experiment {experiment_index}/{experiment_total}] finished: {run_id}\n",
            flush=True,
        )


async def main_async(
    config_path: Path,
    include_reference_steps: bool = False,
    experiment_concurrency: int | None = None,
    fresh: bool = False,
) -> None:
    raw_cfg = _load_retail_solo_config(config_path)
    configure_from_simulation_dict(raw_cfg)
    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    fresh = fresh or bool(domain_cfg.get("fresh", False))
    instructions_path = domain_cfg.get("instructions")
    tasks_path = domain_cfg.get("tasks")

    if not instructions_path or not tasks_path:
        raise ValueError(
            "Domain config must set 'instructions' and 'tasks' in the solo YAML."
        )
    if not _sweep_list(domain_cfg.get("policy")):
        raise ValueError(
            "domain.policy must be set (string or list of paths) for solo runs."
        )

    expanded_raw = _expand_experiment_raw_configs(raw_cfg)
    n_exp = len(expanded_raw)
    yaml_exp_conc = raw_cfg.get("experiment_concurrency")
    if experiment_concurrency is not None:
        exp_conc = max(1, int(experiment_concurrency))
    elif yaml_exp_conc is not None:
        exp_conc = max(1, int(yaml_exp_conc))
    else:
        exp_conc = max(1, min(8, n_exp))

    instructions_text = Path(instructions_path).read_text(encoding="utf-8")
    tasks = _load_tasks(Path(tasks_path))
    solo_tasks = [t for t in tasks if t.get("solo_convertible", True)]
    task_ids_cfg = domain_cfg.get("task_ids")
    if task_ids_cfg is not None and len(task_ids_cfg) > 0:
        allowed_ids = {str(i) for i in task_ids_cfg} | {int(i) for i in task_ids_cfg}
        solo_tasks = [t for t in solo_tasks if t.get("id") in allowed_ids]

    if n_exp > 1:
        print(
            f"Sweep: {n_exp} experiments (policy × model × temperature × reasoning_effort), "
            f"experiment_concurrency={exp_conc}",
            flush=True,
        )

    sem = asyncio.Semaphore(exp_conc)

    async def _run_one(idx: int, rc: dict[str, Any]) -> None:
        async with sem:
            await _run_solo_experiment_async(
                rc,
                include_reference_steps=include_reference_steps,
                instructions_text=instructions_text,
                solo_tasks=solo_tasks,
                experiment_index=idx + 1,
                experiment_total=n_exp,
                fresh=fresh,
            )

    await asyncio.gather(*(_run_one(i, rc) for i, rc in enumerate(expanded_raw)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run solo retail simulations over a tasks JSON file."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the retail solo simulation YAML config (required).",
    )
    parser.add_argument(
        "--include-reference-steps",
        action="store_true",
        default=False,
        help="Include <reference_steps> tool-name flow block in solo prompts. Default False.",
    )
    parser.add_argument(
        "--experiment-concurrency",
        type=int,
        default=None,
        help="Max concurrent sweep experiments (policy×model×temp×reasoning). "
        "Overrides YAML key experiment_concurrency. Default: min(8, num_experiments) or YAML.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        default=False,
        help="Append a timestamp to run_id so output transcripts don't overwrite.",
    )
    args = parser.parse_args()

    load_dotenv()
    # Google Gen AI OTel integration disabled for now (see agent.gemini_log for I/O).
    # os.environ.setdefault(
    #     "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true"
    # )
    logfire.configure(scrubbing=False, console=False)
    # logfire.instrument_google_genai()
    from agent.logfire_gemini_integration import instrument_logfire_gemini

    instrument_logfire_gemini()
    logfire.instrument_litellm()

    # Ensure MCP/mermaid connection logs from agent.base are visible
    logging.getLogger("agent.base").setLevel(logging.INFO)

    asyncio.run(
        main_async(
            args.config,
            include_reference_steps=bool(args.include_reference_steps),
            experiment_concurrency=args.experiment_concurrency,
            fresh=bool(args.fresh),
        )
    )


if __name__ == "__main__":
    main()

