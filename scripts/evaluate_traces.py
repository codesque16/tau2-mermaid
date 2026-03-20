#!/usr/bin/env python3
"""Evaluate collected Claude Code traces against the retail task golden DB state.

Reads task_<id>.jsonl files from a results directory, parses Claude Code's
JSONL trace format (which uses tool names prefixed with "mcp__retail-tools__"),
then checks three dimensions:

  1. DB hash match  — replay tool calls and compare final DB state (all tasks)
  2. communicate_info — agent's reply contains required strings (38/114 tasks)
  3. nl_assertions — LLM judge for behavioural checks (8/114 tasks)

Usage:
    uv run python scripts/evaluate_traces.py --traces-dir results/smoke_test
    uv run python scripts/evaluate_traces.py --traces-dir results/baseline_run_01 --workers 4
    uv run python scripts/evaluate_traces.py --traces-dir results/smoke_test --skip-nl-assertions
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
TASKS_PATH = REPO_ROOT / "domains/retail/tasks_solo_comms.json"
MCP_COMMAND = "uv run domains/retail/tools.py"

sys.path.insert(0, str(REPO_ROOT / "domains/retail"))
from evaluate import evaluate_task_db  # noqa: E402


# ---------------------------------------------------------------------------
# Trace parsing
# ---------------------------------------------------------------------------

_MCP_PREFIX = "mcp__retail-tools__"


def _strip_prefix(name: str) -> str:
    return name[len(_MCP_PREFIX):] if name.startswith(_MCP_PREFIX) else name


def parse_claude_trace(jsonl_path: Path) -> list[dict[str, Any]]:
    """Convert a Claude Code JSONL trace into the history format expected by evaluate.py.

    Each returned dict has:
        {"role": "assistant", "tool_calls": [{"name": str, "arguments": dict}], "content": str | None}

    Only assistant turns are included (that's all evaluate_task_db needs).
    """
    history: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "assistant":
            continue

        msg = event.get("message") or {}
        if msg.get("role") != "assistant":
            continue

        content_blocks = msg.get("content") or []
        tool_calls = []
        text_parts = []

        for block in content_blocks:
            btype = block.get("type")
            if btype == "tool_use":
                name = _strip_prefix(block.get("name") or "")
                arguments = block.get("input") or {}
                if name:
                    tool_calls.append({"name": name, "arguments": arguments})
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    text_parts.append(text)

        if tool_calls or text_parts:
            content_blocks = (
                [{"type": "text", "text": t} for t in text_parts]
                + [{"type": "tool_use", "name": _strip_prefix(tc["name"]), "arguments": tc["arguments"]} for tc in tool_calls]
            )
            history.append({
                "role": "assistant",
                "tool_calls": tool_calls,
                "content": "\n".join(text_parts) if text_parts else None,
                "content_blocks": content_blocks,
            })

    return history


# ---------------------------------------------------------------------------
# communicate_info check (substring, no LLM)
# ---------------------------------------------------------------------------

def check_communicate_info(
    history: list[dict[str, Any]],
    communicate_info: list[str],
) -> dict[str, Any]:
    """Check whether each required string appears in any assistant text message."""
    final_text = " ".join(
        (block.get("text") or "")
        for turn in history
        for block in (turn.get("content_blocks") or [])
        if block.get("type") == "text"
    ).lower().replace(",", "")

    checks = []
    for info_str in communicate_info:
        met = info_str.lower().replace(",", "") in final_text
        checks.append({"info": info_str, "met": met})

    return {
        "communicate_match": all(c["met"] for c in checks),
        "communicate_checks": checks,
    }


# ---------------------------------------------------------------------------
# nl_assertions check (LLM judge)
# ---------------------------------------------------------------------------

def _build_conversation_text(history: list[dict[str, Any]]) -> str:
    lines = []
    for turn in history:
        role = turn.get("role", "assistant")
        for block in (turn.get("content_blocks") or []):
            if block.get("type") == "text":
                lines.append(f"{role}: {block['text']}")
            elif block.get("type") == "tool_use":
                lines.append(f"assistant (tool): {block['name']}({json.dumps(block.get('arguments', {}))})")
    return "\n".join(lines)


def check_nl_assertions(
    history: list[dict[str, Any]],
    nl_assertions: list[str],
) -> dict[str, Any]:
    """Call an LLM to evaluate each nl_assertion against the conversation."""
    try:
        import litellm
    except ImportError:
        return {"nl_assertions_match": None, "nl_assertion_checks": [], "error": "litellm not installed"}

    model = os.getenv("NL_ASSERTIONS_MODEL", "gpt-4o-mini")
    conversation = _build_conversation_text(history)
    system_prompt = (
        "You evaluate whether an agent satisfies expected outcomes. "
        "Return a JSON object: {\"results\": [{\"expectedOutcome\": \"...\", \"metExpectation\": true/false, \"reasoning\": \"...\"}]}"
    )
    user_prompt = f"conversation:\n{conversation}\n\nexpectedOutcomes:\n{json.dumps(nl_assertions)}"

    try:
        resp = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        checks = [
            {
                "nl_assertion": r["expectedOutcome"],
                "met": bool(r["metExpectation"]),
                "justification": r.get("reasoning", ""),
            }
            for r in data.get("results", [])
        ]
        return {
            "nl_assertions_match": all(c["met"] for c in checks),
            "nl_assertion_checks": checks,
        }
    except Exception as exc:
        return {"nl_assertions_match": None, "nl_assertion_checks": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# Per-task evaluation
# ---------------------------------------------------------------------------

async def evaluate_one(
    task: dict[str, Any],
    traces_dir: Path,
    skip_nl_assertions: bool = False,
) -> dict[str, Any]:
    task_id = str(task["id"])
    trace_path = traces_dir / f"task_{task_id}.jsonl"

    if not trace_path.exists():
        return {
            "task_id": task_id,
            "error": f"trace not found: {trace_path.name}",
            "db_match": None,
            "communicate_match": None,
            "nl_assertions_match": None,
        }

    history = parse_claude_trace(trace_path)
    eval_crit = task.get("evaluation_criteria") or {}

    # 1. DB hash
    try:
        result = await evaluate_task_db(
            task=task,
            assistant_history=history,
            db_path=REPO_ROOT / "domains/retail/db.json",
            mcp_command=MCP_COMMAND,
        )
    except Exception as exc:
        result = {"task_id": task_id, "error": str(exc), "db_match": None}

    # 2. communicate_info (substring check)
    communicate_info = eval_crit.get("communicate_info") or []
    if communicate_info:
        result.update(check_communicate_info(history, communicate_info))
    else:
        result["communicate_match"] = None  # not applicable

    # 3. nl_assertions (LLM judge)
    nl_assertions = eval_crit.get("nl_assertions") or []
    if nl_assertions and not skip_nl_assertions:
        result.update(check_nl_assertions(history, nl_assertions))
    else:
        result["nl_assertions_match"] = None  # not applicable or skipped

    return result


async def run_all(
    tasks: list[dict],
    traces_dir: Path,
    workers: int,
    skip_nl_assertions: bool = False,
) -> list[dict]:
    sem = asyncio.Semaphore(workers)

    async def _bounded(task):
        async with sem:
            r = await evaluate_one(task, traces_dir, skip_nl_assertions=skip_nl_assertions)
            db = "DB:PASS" if r.get("db_match") else ("DB:ERR" if r.get("error") else "DB:FAIL")
            comm = "" if r.get("communicate_match") is None else (" COMM:PASS" if r["communicate_match"] else " COMM:FAIL")
            nl = "" if r.get("nl_assertions_match") is None else (" NL:PASS" if r["nl_assertions_match"] else " NL:FAIL")
            print(f"  task {str(r['task_id']):>4}  {db}{comm}{nl}"
                  + (f"  — {r['error']}" if r.get("error") else ""))
            return r

    return await asyncio.gather(*[_bounded(t) for t in tasks])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Claude Code retail traces")
    parser.add_argument("--traces-dir", required=True, help="Directory containing task_<id>.jsonl files")
    parser.add_argument("--workers", type=int, default=4, help="Parallel evaluation workers (default: 4)")
    parser.add_argument("--task-ids", nargs="+", help="Evaluate only these task IDs (default: all found traces)")
    parser.add_argument("--skip-nl-assertions", action="store_true", help="Skip LLM-based nl_assertions check (faster, cheaper)")
    args = parser.parse_args()

    traces_dir = Path(args.traces_dir)
    if not traces_dir.exists():
        print(f"ERROR: traces directory not found: {traces_dir}", file=sys.stderr)
        sys.exit(1)

    all_tasks: list[dict] = json.loads(TASKS_PATH.read_text())

    # Filter to tasks that have a trace file (or explicit --task-ids)
    if args.task_ids:
        allowed = set(str(i) for i in args.task_ids)
        tasks = [t for t in all_tasks if str(t["id"]) in allowed]
    else:
        found_ids = {p.stem.removeprefix("task_") for p in traces_dir.glob("task_*.jsonl")}
        tasks = [t for t in all_tasks if str(t["id"]) in found_ids]

    if not tasks:
        print("No tasks to evaluate.", file=sys.stderr)
        sys.exit(1)

    print(f"Evaluating {len(tasks)} tasks from {traces_dir} (workers={args.workers})\n")
    results = asyncio.run(run_all(tasks, traces_dir, workers=args.workers, skip_nl_assertions=args.skip_nl_assertions))

    # Summary stats
    total = len(results)
    db_pass  = sum(1 for r in results if r.get("db_match") is True)
    db_fail  = sum(1 for r in results if r.get("db_match") is False)
    errors   = sum(1 for r in results if r.get("error"))

    comm_applicable = [r for r in results if r.get("communicate_match") is not None]
    comm_pass = sum(1 for r in comm_applicable if r["communicate_match"])

    nl_applicable = [r for r in results if r.get("nl_assertions_match") is not None]
    nl_pass = sum(1 for r in nl_applicable if r["nl_assertions_match"])

    print(f"\n{'='*50}")
    print(f"DB hash:         {db_pass}/{total} pass  ({db_fail} fail, {errors} error)")
    if comm_applicable:
        print(f"communicate_info:{comm_pass}/{len(comm_applicable)} pass  (on tasks that have this check)")
    if nl_applicable:
        print(f"nl_assertions:   {nl_pass}/{len(nl_applicable)} pass  (on tasks that have this check)")
    print(f"\nOverall DB pass rate: {db_pass/total:.1%}" if total else "")

    out_path = traces_dir / "eval_results.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
