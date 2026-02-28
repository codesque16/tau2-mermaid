#!/usr/bin/env python3
"""
Graph Traversal Benchmark Eval Script

Runs the ablation across three conditions (prose, mermaid, mermaid_harness)
and computes pass^k metrics. Uses LiteLLM for multi-model support.
Rich console + Logfire for observability.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

# Allow running without install: python run_eval.py
_BENCH_DIR = Path(__file__).resolve().parent
if str(_BENCH_DIR / "src") not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR / "src"))

import logfire

from graph_harness.agent import run_agent
from graph_harness.conditions import CONDITIONS, load_scenario
from graph_harness.eval_utils import compute_pass_at_least_once, compute_pass_k, paths_match
from graph_harness.logging_config import (
    configure_logfire,
    console,
    create_progress,
    log_agent_run,
    log_header,
    log_results_table,
    log_scenario_start,
)


BENCHMARK_ROOT = Path(__file__).resolve().parent
SCENARIOS_DIR = BENCHMARK_ROOT / "scenarios"
INDEX_PATH = BENCHMARK_ROOT / "scenarios" / "index.json"


def load_scenario_index() -> dict:
    """Load scenario number -> scenario_id and scenario_id -> test numbers from index.json."""
    if not INDEX_PATH.exists():
        return {}
    data = json.loads(INDEX_PATH.read_text())
    by_number: dict[int, str] = {}
    tests_by_scenario: dict[str, list[int]] = {}
    for s in data.get("scenarios", []):
        num = s.get("scenario_number")
        sid = s.get("scenario_id")
        tests = s.get("tests", [])
        if num is not None and sid:
            by_number[int(num)] = sid
            tests_by_scenario[sid] = tests
    return {"by_number": by_number, "tests_by_scenario": tests_by_scenario}


def load_test_cases(scenario_dir: Path) -> list[dict]:
    """Load all test_XX.json from scenario test_cases/."""
    tc_dir = scenario_dir / "test_cases"
    if not tc_dir.exists():
        return []
    cases = []
    for p in sorted(tc_dir.glob("test_*.json")):
        cases.append(json.loads(p.read_text()))
    return cases


def load_metadata(scenario_dir: Path) -> dict:
    """Load metadata.json."""
    p = scenario_dir / "metadata.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


def list_scenarios() -> list[Path]:
    """List scenario directories."""
    if not SCENARIOS_DIR.exists():
        return []
    return [d for d in SCENARIOS_DIR.iterdir() if d.is_dir() and d.name.startswith("scenario_")]


def _run_one(args: dict) -> bool:
    """Single run for concurrent execution. Returns passed (path match)."""
    path, _, _ = run_agent(
        prose=args["prose"],
        mermaid=args["mermaid"],
        condition=args["condition"],
        user_prompt=args["user_prompt"],
        model=args["model"],
        max_turns=args["max_turns"],
        expected_path=args["expected_path"],
        decision_points=args.get("decision_points"),
    )
    return paths_match(path, args["expected_path"])


def run_eval(
    *,
    model: str = "gpt-4o-mini",
    trials: int = 5,
    scenario_ids: list[str] | None = None,
    test_numbers: list[int] | None = None,
    conditions: list[str] | None = None,
    max_turns: int = 30,
    verbose: bool = False,
    concurrency: int = 1,
) -> dict:
    """
    Run full evaluation. Returns results dict.
    test_numbers: 1-based test indices to run (e.g. [2] = only test_02). Applied per scenario.
    concurrency: max parallel runs (1 = sequential; >1 uses ThreadPoolExecutor). Logfire per-run spans only when concurrency=1.
    """
    conditions = conditions or CONDITIONS
    scenario_dirs = list_scenarios()
    if scenario_ids:
        scenario_dirs = [d for d in scenario_dirs if d.name in scenario_ids]

    # Count total work for progress bar
    total_runs = 0
    scenario_test_info: list[tuple[Path, list[dict]]] = []
    for scenario_dir in scenario_dirs:
        prose, mermaid = load_scenario(scenario_dir)
        test_cases = load_test_cases(scenario_dir)
        if not mermaid or not test_cases:
            continue
        if test_numbers is not None:
            test_cases = [tc for i, tc in enumerate(test_cases, 1) if i in test_numbers]
        if not test_cases:
            continue
        scenario_test_info.append((scenario_dir, test_cases))
        total_runs += len(test_cases) * len(conditions) * trials

    log_header(model, trials, conditions, len(scenario_test_info))
    if concurrency > 1:
        console.print(f"[dim]Concurrency: {concurrency} workers[/dim]")

    # results[cond] = list of [trial1, trial2, ..., trialk] per (scenario, test_case)
    results_by_cond: dict[str, list[list[bool]]] = {c: [] for c in conditions}

    # Build flat work list (same order as nested loop: scenario -> tc -> cond -> trial)
    work_items: list[dict] = []
    for scenario_dir, test_cases in scenario_test_info:
        sid = scenario_dir.name
        prose, mermaid = load_scenario(scenario_dir)
        for tc in test_cases:
            for cond in conditions:
                for trial in range(trials):
                    work_items.append({
                        "prose": prose,
                        "mermaid": mermaid,
                        "condition": cond,
                        "user_prompt": tc.get("user_prompt", ""),
                        "expected_path": tc.get("expected_path", []),
                        "decision_points": tc.get("decision_points"),
                        "model": model,
                        "max_turns": max_turns,
                    })

    progress = create_progress(total_runs, "Evaluating")
    conditions_str = ", ".join(conditions)
    progress_lock = Lock()

    with logfire.span(
        f"eval: {conditions_str}",
        model=model,
        trials=trials,
        conditions=conditions,
    ) as eval_span:
        with progress:
            task_id = progress.add_task("Evaluating", total=total_runs)
            if concurrency <= 1:
                # Sequential: preserve per-run Logfire spans and verbose order
                idx = 0
                for scenario_dir, test_cases in scenario_test_info:
                    sid = scenario_dir.name
                    prose, mermaid = load_scenario(scenario_dir)
                    with logfire.span(f"scenario: {sid}", scenario_id=sid, test_cases=len(test_cases)):
                        log_scenario_start(sid, len(test_cases))
                        for tc in test_cases:
                            test_id = tc.get("test_id", "unknown")
                            for cond in conditions:
                                trial_results: list[bool] = []
                                for trial in range(trials):
                                    span_name = f"{cond} | {sid} | {test_id} | trial {trial + 1}"
                                    with logfire.span(
                                        span_name,
                                        scenario_id=sid,
                                        test_id=test_id,
                                        condition=cond,
                                        trial=trial + 1,
                                    ) as span:
                                        path, messages, completed = run_agent(
                                            prose=prose,
                                            mermaid=mermaid,
                                            condition=cond,
                                            user_prompt=tc.get("user_prompt", ""),
                                            model=model,
                                            max_turns=max_turns,
                                            expected_path=tc.get("expected_path", []),
                                            decision_points=tc.get("decision_points"),
                                        )
                                        passed = paths_match(path, tc.get("expected_path", []))
                                        trial_results.append(passed)
                                        span.set_attribute("path", path)
                                        span.set_attribute("expected_path", tc.get("expected_path", []))
                                        span.set_attribute("passed", passed)
                                        span.set_attribute("completed", completed)
                                        span.set_attribute("turns", len(messages) // 2)
                                    if verbose:
                                        log_agent_run(
                                            cond, test_id, len(messages) // 2,
                                            path, tc.get("expected_path", []), passed,
                                        )
                                    progress.update(
                                        task_id,
                                        advance=1,
                                        description=f"{'✓' if passed else '✗'} {sid} | {cond} | trial {trial + 1}/{trials}",
                                    )
                                results_by_cond[cond].append(trial_results)
            else:
                # Parallel: run work_items, collect results in order
                results_ordered: list[bool] = [False] * len(work_items)
                completed_count = 0
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    future_to_idx = {executor.submit(_run_one, item): i for i, item in enumerate(work_items)}
                    for future in as_completed(future_to_idx):
                        i = future_to_idx[future]
                        try:
                            results_ordered[i] = future.result()
                        except Exception as e:
                            console.print(f"[red]Run {i} failed: {e}[/red]")
                        completed_count += 1
                        with progress_lock:
                            progress.update(
                                task_id,
                                advance=1,
                                description=f"Completed {completed_count}/{len(work_items)}",
                            )
                # Group into results_by_cond (same order as nested loop)
                idx = 0
                for _scenario_dir, test_cases in scenario_test_info:
                    for _tc in test_cases:
                        for cond in conditions:
                            trial_results = [results_ordered[idx + t] for t in range(trials)]
                            idx += trials
                            results_by_cond[cond].append(trial_results)

        # Aggregate: each cond has list of [t1..tk] per test case
        agg: dict[str, dict[str, float]] = {}
        for cond in conditions:
            chunks = results_by_cond[cond]
            if not chunks:
                agg[cond] = {"pass^1": 0.0, f"pass^{trials}": 0.0, "path_accuracy": 0.0, "n": 0}
                continue
            flat = [b for c in chunks for b in c]
            pass_1 = compute_pass_at_least_once(flat, trials)
            pass_k = compute_pass_k(flat, trials)
            path_acc = sum(flat) / len(flat) if flat else 0.0
            agg[cond] = {
                "pass^1": pass_1,
                f"pass^{trials}": pass_k,
                "path_accuracy": path_acc,
                "n": len(chunks),
            }

        eval_span.set_attribute("aggregate", agg)

    return {
        "model": model,
        "trials": trials,
        "conditions": conditions,
        "aggregate": agg,
        "raw": {c: r for c, r in results_by_cond.items()},
    }


def main():
    parser = argparse.ArgumentParser(description="Graph Traversal Benchmark Eval")
    parser.add_argument(
        "--model",
        "-m",
        default="gpt-4o-mini",
        help="LiteLLM model string (e.g. gpt-4o-mini, claude-3-5-sonnet-20241022, gemini/gemini-2.0-flash)",
    )
    parser.add_argument(
        "--trials",
        "-k",
        type=int,
        default=5,
        help="Number of trials per test case for pass^k (default 5)",
    )
    parser.add_argument(
        "--scenarios",
        nargs="*",
        help="Limit to scenario IDs (e.g. scenario_01_order_cancellation). Overridden by --scenario.",
    )
    parser.add_argument(
        "--scenario",
        "-s",
        type=int,
        nargs="*",
        metavar="N",
        help="Scenario number(s) from index (e.g. --scenario 10 or -s 1 5 10). See scenarios/index.json.",
    )
    parser.add_argument(
        "--test",
        "-t",
        type=int,
        nargs="*",
        metavar="N",
        help="Test number(s) within each scenario (e.g. --test 2 = only test_02). Omit to run all tests.",
    )
    parser.add_argument(
        "--conditions",
        "-c",
        nargs="*",
        choices=["prose", "mermaid", "mermaid_harness"],
        help="Conditions to run: --conditions prose (only prose), -c prose mermaid (two), or omit for all three.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=30,
        help="Max agent turns per run (default 30)",
    )
    parser.add_argument(
        "--concurrency",
        "-j",
        type=int,
        default=1,
        metavar="N",
        help="Max parallel runs (default 1). Use e.g. -j 4 for 4 concurrent runs. Sequential (1) preserves per-run Logfire spans.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write results JSON to file",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log each agent run (path, expected, pass/fail)",
    )
    parser.add_argument(
        "--no-logfire",
        action="store_true",
        help="Disable Logfire (no tracing, no instrument_litellm)",
    )
    args = parser.parse_args()

    # Resolve --scenario to scenario_ids via index
    scenario_ids = args.scenarios
    test_numbers = args.test if args.test else None
    if args.scenario is not None:
        index = load_scenario_index()
        by_num = index.get("by_number", {})
        scenario_ids = [by_num[n] for n in args.scenario if n in by_num]
        if not scenario_ids and args.scenario:
            console.print(f"[red]Unknown scenario number(s): {args.scenario}. Use 1-10 (see scenarios/index.json).[/red]")
            sys.exit(1)

    if not args.no_logfire:
        configure_logfire(console=not args.verbose)
    else:
        # Still need litellm for API calls
        import litellm  # noqa: F401

    results = run_eval(
        model=args.model,
        trials=args.trials,
        scenario_ids=scenario_ids,
        test_numbers=test_numbers,
        conditions=args.conditions,
        max_turns=args.max_turns,
        verbose=args.verbose,
        concurrency=args.concurrency,
    )

    log_results_table(results["aggregate"], results["trials"])

    if args.output:
        out = args.output
        j = {
            "model": results["model"],
            "trials": results["trials"],
            "conditions": results["conditions"],
            "aggregate": results["aggregate"],
        }
        out.write_text(json.dumps(j, indent=2))
        console.print(f"[dim]Wrote {out}[/dim]")


if __name__ == "__main__":
    main()
