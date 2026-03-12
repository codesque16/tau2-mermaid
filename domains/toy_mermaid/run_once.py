"""Run toy mermaid tasks once and print evaluation.

Example:
  uv run python -m domains.toy_mermaid.run_once --config configs/toy_once_original.yaml --task-id 0
  uv run python -m domains.toy_mermaid.run_once --config configs/toy_once_original.yaml  # all tasks
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

import yaml

from domains.toy_mermaid.run_gepa_toy import _build_simulation_config, _run_one


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {path}")
    return data


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--task-id", type=int, required=False, default=None)
    p.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Max concurrent task runs (default: domain.concurrency or 3).",
    )
    args = p.parse_args()

    raw = _load_yaml(args.config)
    sim_cfg = _build_simulation_config(raw)
    domain_cfg: Dict[str, Any] = raw.get("domain") or {}
    assistant_cfg: Dict[str, Any] = raw.get("assistant") or {}

    tasks = _load_tasks(Path(domain_cfg["tasks"]))
    if args.task_id is None:
        selected = tasks
    else:
        task = next((t for t in tasks if int(t.get("id")) == int(args.task_id)), None)
        if task is None:
            raise ValueError(f"task-id {args.task_id} not found")
        selected = [task]

    mermaid = assistant_cfg.get("mermaid") or {}
    graph_path = str(mermaid.get("graph") or "")
    if not graph_path:
        raise ValueError("assistant.mermaid.graph must be set in config")

    seed = int((raw.get("gepa") or {}).get("seed", 42))
    concurrency = int(args.concurrency or domain_cfg.get("concurrency") or 3)

    print(f"graph={graph_path}")
    print(f"concurrency={concurrency}")

    async def _run_all() -> list[dict[str, Any]]:
        import asyncio

        sem = asyncio.Semaphore(concurrency)
        results: list[dict[str, Any]] = []

        async def _one(t: dict[str, Any]) -> None:
            task_id = t.get("id")
            ticket = t.get("ticket") or ""
            golden = list((t.get("evaluation_criteria") or {}).get("golden_mermaid_path") or [])
            async with sem:
                predicted = await _run_one(
                    sim_cfg=sim_cfg,
                    mermaid_graph_path=graph_path,
                    ticket=ticket,
                    seed=seed,
                )
            ok = predicted == golden
            results.append(
                {
                    "task_id": task_id,
                    "path_match": bool(ok),
                    "golden": golden,
                    "predicted": predicted,
                }
            )

        await asyncio.gather(*(_one(t) for t in selected))
        return results

    results = __import__("asyncio").run(_run_all())
    results.sort(key=lambda r: int(r["task_id"]) if str(r["task_id"]).isdigit() else str(r["task_id"]))
    passed = sum(1 for r in results if r["path_match"])
    total = len(results)

    for r in results:
        print(f"\n--- task_id={r['task_id']} ---")
        print(f"path_match={r['path_match']}")
        print(f"golden={r['golden']}")
        print(f"predicted={r['predicted']}")

    print(f"\nsummary: {passed}/{total} path_match")


if __name__ == "__main__":
    main()

