"""Run solo simulations for the retail domain over a tasks file.

This script:
  - Reads a solo simulation config (YAML) for the retail domain
  - Loads a tasks JSON file (e.g. tasks_solo_comms.json)
  - For each task, builds a prompt (using the ticket) and runs a solo simulation
    via the shared Orchestrator.run_solo API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
from pathlib import Path
from typing import Any, Dict, List

import yaml
import logfire
import os

from agent import create_agent
from agent.config import AgentConfig as AgentAgentConfig
from chat.config import AgentConfig as ChatAgentConfig, SimulationConfig
from domains.retail.evaluate import evaluate_task_db
from orchestrator.event_bus import EventBus
from orchestrator.orchestrator import Orchestrator, SoloStopMode


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
        reasoning_effort=assistant_block.get("reasoning_effort"),
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
    )


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
):
    """Return a callable (ticket: str) -> Orchestrator using the same logic as solo runs.

    Reused by run_solo_tasks and run_gepa_optimize so mermaid, mcps, and prompt
    construction stay in one place.
    """
    assistant_base_cfg = sim_cfg.assistant
    assistant_model = sim_cfg.assistant_model or sim_cfg.model
    assistant_agent_type = sim_cfg.assistant_agent_type or "litellm"

    def _make(ticket: str) -> Orchestrator:
        has_mermaid = bool(getattr(assistant_base_cfg, "mermaid", None))
        if has_mermaid:
            full_system_prompt = f"<ticket>\n{ticket.strip()}\n</ticket>"
        else:
            if include_policy:
                full_system_prompt = (
                    "<instructions>\n"
                    f"{instructions_text.strip()}\n"
                    "</instructions>\n\n"
                    "<policy>\n"
                    f"{policy_text.strip()}\n"
                    "</policy>\n\n"
                    "<ticket>\n"
                    f"{ticket.strip()}\n"
                    "</ticket>"
                )
            else:
                # GEPA path: only instructions + ticket, no embedded policy block.
                full_system_prompt = (
                    "<instructions>\n"
                    f"{instructions_text.strip()}\n"
                    "</instructions>\n\n"
                    "<ticket>\n"
                    f"{ticket.strip()}\n"
                    "</ticket>"
                )

        assistant_cfg = AgentAgentConfig(
            system_prompt=full_system_prompt,
            max_tokens=assistant_base_cfg.max_tokens,
            temperature=assistant_base_cfg.temperature,
            reasoning_effort=getattr(assistant_base_cfg, "reasoning_effort", None),
            mcps=getattr(assistant_base_cfg, "mcps", None),
            mermaid=_override_mermaid_graph(getattr(assistant_base_cfg, "mermaid", None), mermaid_graph_path),
            seed=seed,
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
    seed: int | None = None,
    mermaid_graph_path: str | None = None,
    quiet: bool = False,
    include_policy: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Run a single solo task and return (success, eval_result).

    Shared by run_solo_tasks and run_gepa_optimize. When quiet=True, skips
    printing task header and result.
    """
    ticket = task.get("ticket") or ""
    task_id = task.get("id")

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
    )
    orchestrator = make_orch(ticket)
    await orchestrator.run_solo(
        prompt="Complete all the tasks and respond in 1 single reply.",
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
    )
    success = bool(eval_result.get("db_match", False))
    if not quiet:
        print(
            f"DB match for task {eval_result.get('task_id')}: "
            f"{'PASS' if success else 'FAIL'}"
        )
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
    seed: int | None,
    results: List[Dict[str, Any]],
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
                seed=seed,
                quiet=True,
                include_policy=True,
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

        outcome = "pass" if success else "fail"
        task_span.message = f"Task:{task_id} [{outcome}]"
        print(
            f"DB match for task {eval_result.get('task_id')}: "
            f"{'PASS' if success else 'FAIL'}"
        )

        results.append(
            {
                "task_id": task_id,
                "trial": trial_idx,
                "success": success,
            }
        )


async def main_async(config_path: Path) -> None:
    raw_cfg = _load_retail_solo_config(config_path)
    sim_cfg = _build_simulation_config(raw_cfg)

    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    instructions_path = domain_cfg.get("instructions")
    policy_path = domain_cfg.get("policy")
    tasks_path = domain_cfg.get("tasks")
    concurrency = int(domain_cfg.get("concurrency", 1))
    trials = int(domain_cfg.get("trials", 1))
    stop_mode_str = str(domain_cfg.get("stop_mode", "task-complete-tool")).lower()

    if not instructions_path or not policy_path or not tasks_path:
        raise ValueError(
            "Domain config must set 'instructions', 'policy', and 'tasks' in solo_simulation.yaml."
        )

    if stop_mode_str == "first-text":
        stop_mode = SoloStopMode.FIRST_TEXT_ONLY
    else:
        stop_mode = SoloStopMode.TASK_COMPLETE_TOOL

    instructions_text = Path(instructions_path).read_text(encoding="utf-8")
    policy_text = Path(policy_path).read_text(encoding="utf-8")

    assistant_base_cfg = sim_cfg.assistant

    # DB path for evaluation (relative to project root by default)
    db_path = Path(domain_cfg.get("db_path", "domains/retail/db.json"))

    # Reuse the same MCP server command the assistant uses (e.g. retail-tools).
    assistant_mcps = getattr(assistant_base_cfg, "mcps", None) or []
    mcp_command: str | None = None
    for server_cfg in assistant_mcps:
        if server_cfg.get("name") == "retail-tools" or not mcp_command:
            mcp_command = server_cfg.get("command") or server_cfg.get("commad")

    # Derive a run_id if not explicitly provided:
    # [<domain_name>][<mode>][<assistant_model>]
    assistant_model_for_id = sim_cfg.assistant_model or sim_cfg.model or "unknown-model"
    domain_name = domain_cfg.get("name", "unknown-domain")
    mode = raw_cfg.get("mode", "solo")
    run_id = domain_cfg.get(
        "run_id", f"[{domain_name}][{mode}][{assistant_model_for_id}]"
    )

    tasks = _load_tasks(Path(tasks_path))
    # Filter tasks that are allowed for solo mode
    solo_tasks = [t for t in tasks if t.get("solo_convertible", True)]

    # If domain.task_ids is set, run only those task ids (ids in JSON may be str or int)
    task_ids_cfg = domain_cfg.get("task_ids")
    if task_ids_cfg is not None and len(task_ids_cfg) > 0:
        allowed_ids = {str(i) for i in task_ids_cfg} | {int(i) for i in task_ids_cfg}
        solo_tasks = [t for t in solo_tasks if t.get("id") in allowed_ids]

    # Generate per-trial seeds deterministically from a base seed.
    base_seed = 300
    rng = random.Random(base_seed)
    trial_seeds = [rng.randint(1, 10**9) for _ in range(trials)]

    # Collect per-task, per-trial outcomes for pass^k metrics.
    results: List[Dict[str, Any]] = []

    with logfire.span(run_id) as top_span:
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
                            results=results,
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
                                results=results,
                            )

                    await asyncio.gather(*(_runner(t) for t in solo_tasks))

        # After all trials + tasks, compute pass^k metrics.
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
                # Excel IFERROR(COMBIN(S,k)/COMBIN(N,k),0) → 0 when invalid
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

        # Update top-level span with overall pass^1 score (same style as task [pass]/[fail]).
        pass_1 = pass_k.get("pass^1")
        if pass_1 is not None:
            top_span.message = f"{run_id} [{pass_1:.2f}]"


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
    args = parser.parse_args()

    # Basic Logfire configuration + LiteLLM instrumentation (for cost tracking).
    logfire.configure(scrubbing=False, console=False)
    os.environ.setdefault(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true"
    )
    logfire.instrument_litellm()

    # Ensure MCP/mermaid connection logs from agent.base are visible
    logging.getLogger("agent.base").setLevel(logging.INFO)

    asyncio.run(main_async(args.config))


if __name__ == "__main__":
    main()

