"""Batch conversational retail simulations over ``tasks_solo_comms.json`` (tau2-style user_scenario).

Same task filtering, DB evaluation, transcripts, and sweep layout as ``run_solo_tasks``,
but runs ``Orchestrator.run()`` with an LLM user simulator instead of ``run_solo``.

Usage::

    uv run python -m domains.retail.run_conversation_tasks --config configs/gemini_conversation_batch.yaml
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

import logfire
import yaml
from dotenv import load_dotenv

from agent.api_key_rotation import configure_from_simulation_dict
from chat.config import SimulationConfig
from chat.config import _agent_config_from_block, _normalize_agent_type
from domains.retail.evaluate import evaluate_communication_from_history, evaluate_task_db
from domains.retail.run_solo_tasks import (
    _default_run_id_from_assistant,
    _experiment_config_record,
    _expand_experiment_raw_configs,
    _fresh_run_suffix,
    _load_tasks,
    _safe_path_component,
    _sweep_list,
    _TRACE_EVAL_OMIT_KEYS,
    _write_json_atomic,
    make_orchestrator_for_conversation,
)
from domains.retail.user_scenario import build_user_system_prompt


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_conversation_simulation_config(raw: dict[str, Any], base_dir: Path) -> SimulationConfig:
    """Like ``load_simulation_config`` but from an in-memory dict + base dir for prompt files."""
    asst = raw.get("assistant") or {}
    user = raw.get("user") or {}
    default_model = raw.get("model") or ""
    return SimulationConfig(
        model=default_model,
        max_turns=int(raw.get("max_turns", 60)),
        stop_phrases=list(raw.get("stop_phrases") or []),
        initial_message=raw.get("initial_message"),
        assistant=_agent_config_from_block(asst, base_dir=base_dir),
        user=_agent_config_from_block(user, base_dir=base_dir),
        assistant_model=asst.get("model") or default_model,
        user_model=user.get("model") or default_model,
        assistant_agent_type=_normalize_agent_type(asst.get("agent_type")),
        user_agent_type=_normalize_agent_type(user.get("agent_type")),
        assistant_agent_name=(asst.get("agent_name") or "").strip() or None,
        user_agent_name=(user.get("agent_name") or "").strip() or None,
        mcp_server_url=raw.get("mcp_server_url") or None,
        graph_id=raw.get("graph_id") or None,
        mode=(raw.get("mode") or None),
        stop_on_user_stop_word=bool(raw.get("stop_on_user_stop_word", False)),
        first_agent_message=(
            str(raw["first_agent_message"]).strip() if raw.get("first_agent_message") else None
        ),
    )


def _resolve_data_path(rel_or_abs: str, *, config_path: Path) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    c = (config_path.parent / p).resolve()
    if c.exists():
        return c
    w = (Path.cwd() / p).resolve()
    if w.exists():
        return w
    return c


async def run_one_conversation_task(
    *,
    policy_text: str,
    guidelines_text: str,
    task: dict[str, Any],
    sim_cfg: SimulationConfig,
    raw_cfg: dict[str, Any],
    db_path: Path,
    mcp_command: str,
    seed: int | None,
    quiet: bool = False,
    evaluate_communication: bool = False,
    trace_dump: bool = False,
) -> tuple[bool, dict[str, Any]]:
    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    user_system_prompt = build_user_system_prompt(task, guidelines_text=guidelines_text)

    im_top = raw_cfg.get("initial_message")
    im_dom = domain_cfg.get("initial_message")
    if isinstance(im_top, str) and im_top.strip():
        initial_msg = im_top.strip()
    elif isinstance(im_dom, str) and im_dom.strip():
        initial_msg = im_dom.strip()
    else:
        # User simulator generates the opening line (see Orchestrator.run).
        initial_msg = None

    if not quiet:
        print(f"\n{'=' * 80}")
        print(f"Running task {task.get('id')}")
        print("-" * 80)

    orchestrator = make_orchestrator_for_conversation(
        policy_text,
        sim_cfg,
        user_system_prompt=user_system_prompt,
        initial_message=initial_msg,
        seed=seed,
    )
    await orchestrator.run()
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

    eval_result["assistant_history"] = list(history)
    if trace_dump:
        for _k in _TRACE_EVAL_OMIT_KEYS:
            eval_result.pop(_k, None)
        try:
            sys_prompt = orchestrator.assistant.get_effective_system_prompt()
        except Exception:
            cfg = getattr(orchestrator.assistant, "config", None)
            sys_prompt = getattr(cfg, "system_prompt", "") or ""
        eval_result["assistant_history"] = [
            {"role": "system", "content": sys_prompt if isinstance(sys_prompt, str) else str(sys_prompt)},
            *eval_result["assistant_history"],
        ]
        eval_result["mode"] = "conversation"
        eval_result["task_ticket"] = task.get("ticket") or ""
        eval_result["trial_seed"] = seed
    return success, eval_result


async def _run_task_conv(
    *,
    config_path: Path,
    policy_text: str,
    guidelines_text: str,
    task: dict[str, Any],
    sim_cfg: SimulationConfig,
    raw_cfg: dict[str, Any],
    db_path: Path,
    mcp_command: str,
    trial_idx: int,
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
            success, eval_result = await run_one_conversation_task(
                policy_text=policy_text,
                guidelines_text=guidelines_text,
                task=task,
                sim_cfg=sim_cfg,
                raw_cfg=raw_cfg,
                db_path=db_path,
                mcp_command=mcp_command,
                seed=seed,
                quiet=True,
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
        task_trace_path = trace_dir / (
            f"task_{_safe_path_component(str(task_id))}"
            f"__trial_{trial_idx}"
            f"__seed_{_safe_path_component(str(seed))}"
            f".json"
        )
        _write_json_atomic(task_trace_path, payload)


async def _run_conversation_experiment_async(
    raw_cfg: dict[str, Any],
    *,
    config_path: Path,
    conv_tasks: List[dict[str, Any]],
    experiment_index: int,
    experiment_total: int,
    fresh: bool = False,
) -> None:
    sim_cfg = _build_conversation_simulation_config(raw_cfg, config_path.parent)
    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    policy_path = domain_cfg.get("policy")
    if not policy_path:
        raise ValueError("domain.policy missing in conversation config.")

    guidelines_path = domain_cfg.get(
        "user_sim_guidelines",
        "gepa/examples/tau2_retail_mermaid/simulation_guidelines.md",
    )
    gp = _resolve_data_path(str(guidelines_path), config_path=config_path)
    guidelines_text = gp.read_text(encoding="utf-8")

    pp = _resolve_data_path(str(policy_path), config_path=config_path)
    policy_text = pp.read_text(encoding="utf-8")

    concurrency = int(domain_cfg.get("concurrency", 1))
    trials = int(domain_cfg.get("trials", 1))
    db_path = Path(domain_cfg.get("db_path", "domains/retail/db.json"))
    evaluate_communication = bool(domain_cfg.get("evaluate_communication", False))

    assistant_base_cfg = sim_cfg.assistant
    assistant_mcps = getattr(assistant_base_cfg, "mcps", None) or []
    mcp_command: str | None = None
    for server_cfg in assistant_mcps:
        if server_cfg.get("name") == "retail-tools" or not mcp_command:
            mcp_command = server_cfg.get("command") or server_cfg.get("commad")

    assistant_model_for_id = sim_cfg.assistant_model or sim_cfg.model or "unknown-model"
    domain_name = domain_cfg.get("name", "unknown-domain")
    policy_basename = pp.name
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

    _domain_seed = domain_cfg.get("seed")
    if _domain_seed is not None:
        rng = random.Random(int(_domain_seed))
    else:
        rng = random.Random(300)
    trial_seeds = [rng.randint(1, 10**9) for _ in range(trials)]
    results: List[Dict[str, Any]] = []

    cfg_record = _experiment_config_record(raw_cfg, sim_cfg, str(pp))
    cfg_record["mode"] = "conversation"
    cfg_record["domain.user_sim_guidelines"] = str(gp)
    cfg_json = json.dumps(cfg_record, default=str, sort_keys=True)

    with logfire.span(run_id) as top_span:
        logfire.info(
            "experiment_config",
            experiment_index=experiment_index,
            experiment_total=experiment_total,
            run_id=run_id,
            mode="conversation",
            config_json=cfg_json,
        )

        for trial_idx, seed in enumerate(trial_seeds, start=1):
            trial_span_name = f"Trial:{trial_idx} {seed}"
            with logfire.span(trial_span_name, trial=trial_idx, seed=seed):
                if concurrency <= 1:
                    for task in conv_tasks:
                        await _run_task_conv(
                            config_path=config_path,
                            policy_text=policy_text,
                            guidelines_text=guidelines_text,
                            task=task,
                            sim_cfg=sim_cfg,
                            raw_cfg=raw_cfg,
                            db_path=db_path,
                            mcp_command=mcp_command or "",
                            trial_idx=trial_idx,
                            seed=seed,
                            results=results,
                            evaluate_communication=evaluate_communication,
                            trace_dir=trace_dir,
                            trace_run_id=str(run_id),
                            trace_experiment_index=experiment_index,
                        )
                else:
                    sem = asyncio.Semaphore(concurrency)

                    async def _runner(t: dict[str, Any]) -> None:
                        async with sem:
                            await _run_task_conv(
                                config_path=config_path,
                                policy_text=policy_text,
                                guidelines_text=guidelines_text,
                                task=t,
                                sim_cfg=sim_cfg,
                                raw_cfg=raw_cfg,
                                db_path=db_path,
                                mcp_command=mcp_command or "",
                                trial_idx=trial_idx,
                                seed=seed,
                                results=results,
                                evaluate_communication=evaluate_communication,
                                trace_dir=trace_dir,
                                trace_run_id=str(run_id),
                                trace_experiment_index=experiment_index,
                            )

                    await asyncio.gather(*(_runner(t) for t in conv_tasks))

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
                n = len(outcomes)
                s = sum(1 for x in outcomes if x)
                if n < k or s < k:
                    continue
                total += math.comb(s, k) / math.comb(n, k)
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
    *,
    experiment_concurrency: int | None = None,
    fresh: bool = False,
) -> None:
    raw_cfg = _load_config(config_path)
    configure_from_simulation_dict(raw_cfg)
    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    fresh = fresh or bool(domain_cfg.get("fresh", False))
    tasks_path = domain_cfg.get("tasks")

    if str(raw_cfg.get("mode") or "").lower() != "conversation":
        raise ValueError("YAML must set ``mode: conversation`` for run_conversation_tasks.")

    if not tasks_path:
        raise ValueError("Domain config must set 'tasks' in the conversation YAML.")

    if not _sweep_list(domain_cfg.get("policy")):
        raise ValueError("domain.policy must be set (string or list of paths).")

    expanded_raw = _expand_experiment_raw_configs(raw_cfg)
    n_exp = len(expanded_raw)
    yaml_exp_conc = raw_cfg.get("experiment_concurrency")
    if experiment_concurrency is not None:
        exp_conc = max(1, int(experiment_concurrency))
    elif yaml_exp_conc is not None:
        exp_conc = max(1, int(yaml_exp_conc))
    else:
        exp_conc = max(1, min(8, n_exp))

    tasks = _load_tasks(_resolve_data_path(str(tasks_path), config_path=config_path))
    conv_tasks = [t for t in tasks if t.get("solo_convertible", True)]
    task_ids_cfg = domain_cfg.get("task_ids")
    if task_ids_cfg is not None and len(task_ids_cfg) > 0:
        allowed_ids = {str(i) for i in task_ids_cfg} | {int(i) for i in task_ids_cfg}
        conv_tasks = [t for t in conv_tasks if t.get("id") in allowed_ids]

    if n_exp > 1:
        print(
            f"Sweep: {n_exp} experiments (policy × model × temp × reasoning), "
            f"experiment_concurrency={exp_conc}",
            flush=True,
        )

    sem = asyncio.Semaphore(exp_conc)

    async def _run_one(idx: int, rc: dict[str, Any]) -> None:
        async with sem:
            await _run_conversation_experiment_async(
                rc,
                config_path=config_path,
                conv_tasks=conv_tasks,
                experiment_index=idx + 1,
                experiment_total=n_exp,
                fresh=fresh,
            )

    await asyncio.gather(*(_run_one(i, rc) for i, rc in enumerate(expanded_raw)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run conversational retail simulations over a tasks JSON file."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the retail conversation simulation YAML config (required).",
    )
    parser.add_argument(
        "--experiment-concurrency",
        type=int,
        default=None,
        help="Max concurrent sweep experiments. Overrides YAML experiment_concurrency.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        default=False,
        help="Append a timestamp to run_id so output transcripts don't overwrite.",
    )
    args = parser.parse_args()

    load_dotenv()
    logfire.configure(scrubbing=False, console=False)
    from agent.logfire_gemini_integration import instrument_logfire_gemini

    instrument_logfire_gemini()
    logfire.instrument_litellm()
    logging.getLogger("agent.base").setLevel(logging.INFO)

    asyncio.run(
        main_async(
            args.config,
            experiment_concurrency=args.experiment_concurrency,
            fresh=bool(args.fresh),
        )
    )


if __name__ == "__main__":
    main()
