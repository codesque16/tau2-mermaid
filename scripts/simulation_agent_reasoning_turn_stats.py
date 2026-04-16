#!/usr/bin/env python3
"""
Aggregate agent LLM turn stats for a tau2 simulation run folder under data/simulations/.

A "turn" here is one captured agent call: ``artifacts/**/agent/llm_*.json`` (see
``tau2.utils.sim_llm_io``). "No reasoning" means ``message.reasoning`` (or
``reasoning_content``) is missing, null, or only whitespace.

Usage:
  python scripts/simulation_agent_reasoning_turn_stats.py \\
    /path/to/tau3-bench-fork/data/simulations/<run_name>

  python scripts/simulation_agent_reasoning_turn_stats.py <run_name>  # if cwd is data/simulations
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve_run_dir(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    # Allow passing only the run folder name when cwd is .../data/simulations
    cwd_name = Path.cwd().name
    if cwd_name == "simulations":
        candidate = Path.cwd() / arg
        if candidate.is_dir():
            return candidate.resolve()
    return p.resolve()


def _reasoning_from_prediction(pred: dict | None) -> object | None:
    if not pred:
        return None
    ch0 = (pred.get("choices") or [None])[0]
    if not ch0:
        return None
    msg = ch0.get("message") or {}
    for key in ("reasoning", "reasoning_content"):
        if key in msg:
            return msg.get(key)
    return None


def _reasoning_from_envelope(data: dict) -> object | None:
    pred = (data.get("openai_io_json") or {}).get("prediction") or data.get(
        "prediction"
    )
    return _reasoning_from_prediction(pred)


def _is_no_reasoning(value: object | None) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count agent LLM turns and turns with no reasoning in a simulation run."
    )
    parser.add_argument(
        "run_dir",
        help="Path to a simulation output directory (e.g. .../data/simulations/<name>)",
    )
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="Print paths of turns with no reasoning (can be long).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object with the stats.",
    )
    args = parser.parse_args()

    run_dir = _resolve_run_dir(args.run_dir)
    if not run_dir.is_dir():
        print(f"error: not a directory: {run_dir}", file=sys.stderr)
        return 1

    paths = sorted(run_dir.glob("artifacts/**/agent/llm_*.json"))

    total_turns = 0
    no_reasoning = 0
    errors = 0
    missing_paths: list[str] = []

    for path in paths:
        total_turns += 1
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            errors += 1
            continue
        r = _reasoning_from_envelope(data)
        if _is_no_reasoning(r):
            no_reasoning += 1
            missing_paths.append(str(path))

    with_reasoning = total_turns - no_reasoning - errors
    payload = {
        "run_dir": str(run_dir),
        "total_agent_llm_files": total_turns,
        "parse_errors": errors,
        "turns_with_no_reasoning": no_reasoning,
        "turns_with_reasoning": with_reasoning,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Run: {run_dir}")
        print(f"Total agent LLM turns (agent/llm_*.json): {total_turns}")
        if errors:
            print(f"JSON parse errors (skipped): {errors}")
        print(f"Turns with no reasoning (null/empty/whitespace): {no_reasoning}")
        print(f"Turns with non-empty reasoning: {with_reasoning}")

    if args.list_missing:
        for p in missing_paths:
            print(p)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
