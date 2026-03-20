#!/usr/bin/env python3
"""Run tau2-retail-agent subagent on all (or selected) tasks and collect traces.

Usage:
    # Run all 114 tasks sequentially
    python scripts/run_agent_tasks.py

    # Run specific task IDs
    python scripts/run_agent_tasks.py --task-ids 0 5 12

    # Run with concurrency (parallel subprocesses)
    python scripts/run_agent_tasks.py --workers 4

    # Custom output dir
    python scripts/run_agent_tasks.py --output-dir results/run_01

Output layout:
    <output-dir>/
        task_<id>.jsonl     — JSONL transcript (copied from ~/.claude/projects/...)
        task_<id>.txt       — plain text final reply from the agent
        summary.json        — per-task outcome: task_id, session_id, response
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

TASKS_PATH = Path(__file__).parent.parent / "domains/retail/tasks_solo_comms.json"
PROJECT_SLUG = "-Users-shiladityabanerjee-Projects-tau2-mermaid"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects" / PROJECT_SLUG


def load_tasks(task_ids: list[str] | None = None) -> list[dict]:
    tasks = json.loads(TASKS_PATH.read_text())
    tasks = [t for t in tasks if t.get("solo_convertible", True)]
    if task_ids:
        allowed = set(str(i) for i in task_ids)
        tasks = [t for t in tasks if str(t["id"]) in allowed]
    return tasks


def _find_trace(session_id: str) -> Path | None:
    """Locate the JSONL transcript for a session ID."""
    candidate = CLAUDE_PROJECTS_DIR / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Also check inside session sub-folder (subagent layout)
    sub = CLAUDE_PROJECTS_DIR / session_id / f"{session_id}.jsonl"
    if sub.exists():
        return sub
    return None


def run_task(task: dict, output_dir: Path, model: str | None = None) -> dict:
    """Run a single task; return a result dict."""
    task_id = str(task["id"])
    ticket = task["ticket"]
    session_id = str(uuid.uuid4())

    print(f"[task {task_id}] running (session {session_id[:8]}…)")

    cmd = [
        "claude",
        "--agent", "tau2-retail-agent",
        "--session-id", session_id,
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "-p", ticket,
    ]
    if model:
        cmd += ["--model", model]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,  # project root (so .mcp.json is found)
    )

    # Parse structured JSON output: {"type":"result","result":"...","session_id":"..."}
    response_text = ""
    output_session_id = session_id
    try:
        out = json.loads(result.stdout)
        response_text = out.get("result") or out.get("response") or result.stdout
        output_session_id = out.get("session_id") or session_id
    except json.JSONDecodeError:
        response_text = result.stdout

    # Copy trace file
    trace_src = _find_trace(output_session_id)
    if trace_src and trace_src.exists():
        dest = output_dir / f"task_{task_id}.jsonl"
        shutil.copy2(trace_src, dest)
        print(f"[task {task_id}] trace → {dest.name}")
    else:
        print(f"[task {task_id}] WARNING: trace not found for session {output_session_id}")

    # Write plain-text reply
    txt_path = output_dir / f"task_{task_id}.txt"
    txt_path.write_text(response_text, encoding="utf-8")

    if result.returncode != 0:
        print(f"[task {task_id}] STDERR: {result.stderr[:300]}")

    return {
        "task_id": task_id,
        "session_id": output_session_id,
        "exit_code": result.returncode,
        "response_preview": response_text[:200],
    }


async def run_task_async(task: dict, output_dir: Path, sem: asyncio.Semaphore, model: str | None = None) -> dict:
    async with sem:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, run_task, task, output_dir, model)


async def run_all(tasks: list[dict], output_dir: Path, workers: int, model: str | None = None) -> list[dict]:
    sem = asyncio.Semaphore(workers)
    return await asyncio.gather(*[run_task_async(t, output_dir, sem, model) for t in tasks])


def main() -> None:
    parser = argparse.ArgumentParser(description="Run tau2-retail-agent on all tasks")
    parser.add_argument("--task-ids", nargs="+", help="Specific task IDs to run (default: all)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (default: 1 = sequential)")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: results/traces_MMDD_HHMM)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model alias or full name: sonnet, opus, haiku, claude-sonnet-4-6, etc. (default: inherits Claude Code session default)",
    )
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%m%d_%H%M")
    output_dir = Path(args.output_dir) if args.output_dir else Path(f"results/traces_{timestamp}")
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {output_dir.resolve()}")

    tasks = load_tasks(args.task_ids)
    model_label = args.model or "default"
    print(f"Running {len(tasks)} tasks with {args.workers} worker(s), model={model_label}…\n")

    if args.workers > 1:
        results = asyncio.run(run_all(tasks, output_dir, args.workers, model=args.model))
    else:
        results = [run_task(t, output_dir, model=args.model) for t in tasks]

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    passed = sum(1 for r in results if r["exit_code"] == 0)
    print(f"\nDone. {passed}/{len(results)} tasks exited cleanly.")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
