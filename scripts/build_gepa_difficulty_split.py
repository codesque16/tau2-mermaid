"""Build difficulty-aware train/val/test split from task summary JSON.

Usage:
  uv run python scripts/build_gepa_difficulty_split.py \
    --task-summary results/gepa_balanced_split_experiment1.json \
    --split-percentages '{"train":0.7,"val":0.15,"test":0.15}' \
    --category-proportions global \
    --out configs/gepa_experiment1_autosplit.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domains.retail.split_strategy import (
    build_difficulty_proportional_splits,
    load_task_difficulty_summary,
    split_category_counts,
)


def _parse_category_proportions(raw: str) -> str | dict[str, float]:
    if raw.strip().lower() == "global":
        return "global"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--category-proportions must be 'global' or a JSON object.")
    return {str(k): float(v) for k, v in parsed.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build difficulty-aware GEPA split IDs.")
    parser.add_argument("--task-summary", required=True, help="Path to JSON with task_summary list.")
    parser.add_argument(
        "--split-percentages",
        required=True,
        help='JSON, e.g. \'{"train":0.7,"val":0.15,"test":0.15}\'',
    )
    parser.add_argument(
        "--category-proportions",
        default="global",
        help='Either "global" or JSON, e.g. \'{"hard":0.33,"medium":0.33,"easy":0.34}\'',
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for deterministic split.")
    parser.add_argument("--out", required=True, help="Output split config JSON path.")
    args = parser.parse_args()

    task_summary = load_task_difficulty_summary(Path(args.task_summary))
    split_percentages = json.loads(args.split_percentages)
    if not isinstance(split_percentages, dict):
        raise ValueError("--split-percentages must be a JSON object.")
    split_percentages = {str(k): float(v) for k, v in split_percentages.items()}
    category_proportions = _parse_category_proportions(args.category_proportions)

    split_ids = build_difficulty_proportional_splits(
        task_summary=task_summary,
        split_percentages=split_percentages,
        category_proportions=category_proportions,
        seed=args.seed,
    )
    counts = split_category_counts(task_summary, split_ids)

    payload = {
        "train_task_ids": split_ids["train"],
        "val_task_ids": split_ids["val"],
        "test_task_ids": split_ids["test"],
        "meta": {
            "split_percentages": split_percentages,
            "category_proportions": category_proportions,
            "seed": args.seed,
            "category_counts": counts,
            "source_task_summary_path": str(args.task_summary),
        },
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
