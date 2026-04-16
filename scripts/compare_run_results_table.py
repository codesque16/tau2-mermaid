#!/usr/bin/env python3
"""Build a task-by-trial pass/fail comparison table across run directories.

Each input directory must contain a `results.json` file with a `simulations` array.
The script reads `reward_info.reward` per simulation, converts it to 1/0, and
produces a CSV table:

- Rows: task_id
- Grouped columns: one group per run directory
- Subcolumns in each group: trial_1, trial_2, trial_3, trial_4
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def _to_binary_reward(value: object) -> int:
    """Convert reward-like values to 1/0."""
    if value is None:
        return 0
    try:
        return 1 if float(value) > 0 else 0
    except (TypeError, ValueError):
        return 0


def _sort_task_ids(task_ids: set[str]) -> list[str]:
    """Sort task IDs numerically when possible, otherwise lexicographically."""
    numeric: list[tuple[int, str]] = []
    non_numeric: list[str] = []
    for task_id in task_ids:
        try:
            numeric.append((int(task_id), task_id))
        except (TypeError, ValueError):
            non_numeric.append(task_id)
    numeric.sort(key=lambda x: x[0])
    non_numeric.sort()
    return [task for _, task in numeric] + non_numeric


def _load_run_scores(results_path: Path) -> dict[str, dict[int, int]]:
    """Return task_id -> trial -> binary reward from one results.json file."""
    with results_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    simulations = data.get("simulations", [])
    task_trial_reward: dict[str, dict[int, int]] = defaultdict(dict)

    for sim in simulations:
        task_id_raw = sim.get("task_id")
        trial_raw = sim.get("trial")
        if task_id_raw is None or trial_raw is None:
            continue

        task_id = str(task_id_raw)
        try:
            trial = int(trial_raw)
        except (TypeError, ValueError):
            continue

        reward = _to_binary_reward((sim.get("reward_info") or {}).get("reward"))
        task_trial_reward[task_id][trial] = reward

    return dict(task_trial_reward)


def build_table(
    run_dirs: list[Path], trial_ids: list[int], trial_header_start: int
) -> tuple[list[str], list[str], list[list[str]]]:
    """Build two header rows and data rows for CSV output."""
    run_scores: dict[str, dict[str, dict[int, int]]] = {}
    all_task_ids: set[str] = set()

    for run_dir in run_dirs:
        results_path = run_dir / "results.json"
        if not results_path.exists():
            raise FileNotFoundError(f"Missing file: {results_path}")
        scores = _load_run_scores(results_path)
        run_scores[run_dir.name] = scores
        all_task_ids.update(scores.keys())

    sorted_task_ids = _sort_task_ids(all_task_ids)

    header_1 = ["task_id"]
    header_2 = [""]
    for run_dir in run_dirs:
        run_name = run_dir.name
        header_1.extend([run_name] * len(trial_ids))
        header_2.extend(
            [f"trial_{trial_header_start + idx}" for idx in range(len(trial_ids))]
        )

    rows: list[list[str]] = []
    for task_id in sorted_task_ids:
        row = [task_id]
        for run_dir in run_dirs:
            run_name = run_dir.name
            task_scores = run_scores.get(run_name, {}).get(task_id, {})
            for trial_id in trial_ids:
                value = task_scores.get(trial_id)
                row.append("" if value is None else str(value))
        rows.append(row)

    return header_1, header_2, rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create a task-by-trial comparison CSV from multiple tau2 run directories."
        )
    )
    parser.add_argument(
        "run_dirs",
        nargs="+",
        type=Path,
        help="Run directories that contain results.json.",
    )
    parser.add_argument(
        "--trial-ids",
        nargs="+",
        type=int,
        default=[0, 1, 2, 3],
        help="Trial IDs to read from JSON (default: 0 1 2 3).",
    )
    parser.add_argument(
        "--trial-header-start",
        type=int,
        default=1,
        help=(
            "Starting number used in displayed trial headers. "
            "Default 1 generates trial_1..trial_4 for 4 trial IDs."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("task_trial_comparison.csv"),
        help="Output CSV file path (default: task_trial_comparison.csv).",
    )
    args = parser.parse_args()

    header_1, header_2, rows = build_table(
        args.run_dirs, args.trial_ids, args.trial_header_start
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header_1)
        writer.writerow(header_2)
        writer.writerows(rows)

    print(f"Wrote {len(rows)} task rows to: {args.output}")


if __name__ == "__main__":
    main()
