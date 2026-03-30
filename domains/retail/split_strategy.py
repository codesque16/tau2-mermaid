"""Difficulty-aware split utilities for retail GEPA task IDs.

This module builds train/val/test splits from:
- per-task difficulty categories (hard/medium/easy),
- split percentages (e.g. 0.7/0.15/0.15),
- target category proportions (global or custom).
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


BUCKETS = ("hard", "medium", "easy")
SPLITS = ("train", "val", "test")


def _normalize_weights(weights: dict[str, float], allowed_keys: tuple[str, ...]) -> dict[str, float]:
    vals: dict[str, float] = {}
    for k in allowed_keys:
        v = float(weights.get(k, 0.0))
        if v < 0:
            raise ValueError(f"Weight for {k!r} must be non-negative, got {v}.")
        vals[k] = v
    total = sum(vals.values())
    if total <= 0:
        raise ValueError(f"Weight sum must be > 0 for keys={allowed_keys}.")
    return {k: vals[k] / total for k in allowed_keys}


def _largest_remainder_allocation(total: int, proportions: dict[str, float], keys: tuple[str, ...]) -> dict[str, int]:
    exact = {k: float(proportions.get(k, 0.0)) * total for k in keys}
    floor = {k: int(exact[k]) for k in keys}
    rem = total - sum(floor.values())
    ranked = sorted(((exact[k] - floor[k], k) for k in keys), reverse=True)
    for _, k in ranked[:rem]:
        floor[k] += 1
    return floor


def load_task_difficulty_summary(path: str | Path) -> list[dict[str, Any]]:
    """Load a task summary from `results/gepa_balanced_split_experiment1.json` format."""
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    task_summary = payload.get("task_summary")
    if not isinstance(task_summary, list):
        raise ValueError(f"{p} does not contain a valid `task_summary` list.")
    return task_summary


def build_difficulty_proportional_splits(
    *,
    task_summary: list[dict[str, Any]],
    split_percentages: dict[str, float],
    category_proportions: str | dict[str, float] = "global",
    seed: int = 0,
    allowed_task_ids: set[int] | None = None,
) -> dict[str, list[int]]:
    """Build train/val/test IDs from percentages with category balancing.

    Args:
        task_summary: List containing at least task_id + difficulty_bucket.
        split_percentages: e.g. {"train": 0.7, "val": 0.15, "test": 0.15}.
        category_proportions:
            - "global": preserve dataset's hard/medium/easy distribution.
            - dict: custom target distribution across categories.
        seed: RNG seed for deterministic assignment within each category.
        allowed_task_ids: optional restriction set.
    """
    split_weights = _normalize_weights(split_percentages, SPLITS)

    grouped: dict[str, list[dict[str, Any]]] = {b: [] for b in BUCKETS}
    for row in task_summary:
        task_id = int(row["task_id"])
        if allowed_task_ids is not None and task_id not in allowed_task_ids:
            continue
        b = str(row.get("difficulty_bucket", "")).strip().lower()
        if b not in grouped:
            raise ValueError(f"Unknown difficulty bucket {b!r} for task_id={task_id}.")
        grouped[b].append(row)

    all_ids = [int(r["task_id"]) for b in BUCKETS for r in grouped[b]]
    if not all_ids:
        raise ValueError("No tasks available after filtering.")
    total_tasks = len(all_ids)

    split_sizes = _largest_remainder_allocation(total_tasks, split_weights, SPLITS)

    if category_proportions == "global":
        cat_weights = {b: len(grouped[b]) / total_tasks for b in BUCKETS}
    else:
        if not isinstance(category_proportions, dict):
            raise ValueError("category_proportions must be 'global' or a dict.")
        cat_weights = _normalize_weights(category_proportions, BUCKETS)

    # Desired counts by split x bucket.
    desired: dict[str, dict[str, int]] = {
        sp: _largest_remainder_allocation(split_sizes[sp], cat_weights, BUCKETS) for sp in SPLITS
    }

    # Capacity-aware adjustment when rounded desired counts are not globally feasible.
    bucket_capacity = {b: len(grouped[b]) for b in BUCKETS}
    bucket_remaining = dict(bucket_capacity)
    split_remaining = dict(split_sizes)
    allocated = {sp: {b: 0 for b in BUCKETS} for sp in SPLITS}

    # Pass 1: satisfy desired counts as much as possible.
    for sp in SPLITS:
        for b in BUCKETS:
            want = desired[sp][b]
            take = min(want, bucket_remaining[b], split_remaining[sp])
            allocated[sp][b] += take
            bucket_remaining[b] -= take
            split_remaining[sp] -= take

    # Pass 2: fill any split deficits from buckets with remaining capacity.
    for sp in SPLITS:
        while split_remaining[sp] > 0:
            choices = sorted(((bucket_remaining[b], b) for b in BUCKETS), reverse=True)
            room, b = choices[0]
            if room <= 0:
                raise ValueError("Unable to satisfy split sizes with available tasks.")
            allocated[sp][b] += 1
            bucket_remaining[b] -= 1
            split_remaining[sp] -= 1

    rng = random.Random(seed)
    for b in BUCKETS:
        grouped[b].sort(
            key=lambda r: (
                float(r.get("success_rate", 0.0)),
                abs(float(r.get("reasoning_gap", 0.0))),
                int(r["task_id"]),
            )
        )
        rng.shuffle(grouped[b])

    splits: dict[str, list[int]] = {sp: [] for sp in SPLITS}
    for b in BUCKETS:
        idx = 0
        for sp in SPLITS:
            k = allocated[sp][b]
            rows = grouped[b][idx : idx + k]
            splits[sp].extend(int(r["task_id"]) for r in rows)
            idx += k

    # Final safety checks.
    for sp in SPLITS:
        if len(splits[sp]) != split_sizes[sp]:
            raise ValueError(f"{sp} size mismatch: got={len(splits[sp])}, want={split_sizes[sp]}.")
    union = set(splits["train"]) | set(splits["val"]) | set(splits["test"])
    if len(union) != total_tasks:
        raise ValueError("Split coverage mismatch; tasks are duplicated or missing.")
    if set(splits["train"]) & set(splits["val"]):
        raise ValueError("Overlap detected between train and val.")
    if set(splits["train"]) & set(splits["test"]):
        raise ValueError("Overlap detected between train and test.")
    if set(splits["val"]) & set(splits["test"]):
        raise ValueError("Overlap detected between val and test.")

    return {k: sorted(v) for k, v in splits.items()}


def split_category_counts(task_summary: list[dict[str, Any]], split_ids: dict[str, list[int]]) -> dict[str, dict[str, int]]:
    """Compute hard/medium/easy counts for each split."""
    bucket_by_id = {int(r["task_id"]): str(r["difficulty_bucket"]) for r in task_summary}
    out: dict[str, dict[str, int]] = {}
    for sp, ids in split_ids.items():
        cnt = defaultdict(int)
        for tid in ids:
            cnt[bucket_by_id[int(tid)]] += 1
        out[sp] = {b: int(cnt.get(b, 0)) for b in BUCKETS}
    return out
