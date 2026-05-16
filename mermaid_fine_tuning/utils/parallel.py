"""Parallel-runner helper for data-gen stages.

Each stage produces a list of items to process (graphs, plans, conversations).
Wrapping the work function with `run_parallel` gives:
  - ThreadPoolExecutor with caller-controlled concurrency
  - Rich progress bar (works fine in a normal terminal; falls back gracefully
    if no TTY)
  - Ordered output collection (results indexed by input position)
  - Exception isolation: one item's failure doesn't kill the batch
  - Live count of in-flight / completed / failed

Usage:
    def work(item):
        return process(item)

    results = run_parallel(
        items=my_list,
        work_fn=work,
        concurrency=8,
        description="Stage 3: conversations",
    )
    # results is a list aligned with `items`; failures are ParallelFailure
"""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from rich.console import Console
from rich.progress import (
    Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn,
    MofNCompleteColumn, SpinnerColumn,
)


_console = Console()


@dataclass
class ParallelFailure:
    """Sentinel returned in place of a result when an item raised."""
    index: int
    item: Any
    exception: BaseException

    def __repr__(self) -> str:
        return f"ParallelFailure(idx={self.index}, exc={type(self.exception).__name__}: {self.exception})"


def run_parallel(
    items: Iterable[Any],
    work_fn: Callable[[Any], Any],
    concurrency: int = 8,
    description: str = "processing",
    progress: bool = True,
) -> list[Any]:
    """Run work_fn across items in parallel; return results in input order.

    Args:
        items: input items (will be materialized into a list).
        work_fn: callable taking one item, returning whatever the stage needs.
            Exceptions are caught and returned as ParallelFailure in that slot.
        concurrency: number of worker threads. 1 = serial (still useful for
            keeping the rich progress display).
        description: label shown in the progress bar.
        progress: if False, suppress the progress bar (useful in tests).

    Returns:
        list aligned with `items`. Successful slots hold work_fn's return
        value; failed slots hold a ParallelFailure.
    """
    items_list = list(items)
    n = len(items_list)
    results: list[Any] = [None] * n

    if n == 0:
        return results

    concurrency = max(1, min(concurrency, n))

    if not progress:
        return _run_no_progress(items_list, work_fn, concurrency, results)

    columns = [
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[green]✓ {task.fields[ok]}"),
        TextColumn("[red]✗ {task.fields[fail]}"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
    ]

    with Progress(*columns, console=_console, transient=False) as pbar:
        task_id = pbar.add_task(description, total=n, ok=0, fail=0)
        ok = 0
        fail = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_to_idx = {
                pool.submit(work_fn, item): idx
                for idx, item in enumerate(items_list)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result()
                    ok += 1
                except BaseException as e:
                    results[idx] = ParallelFailure(index=idx, item=items_list[idx], exception=e)
                    fail += 1
                pbar.update(task_id, advance=1, ok=ok, fail=fail)

    return results


def _run_no_progress(items_list, work_fn, concurrency, results):
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_idx = {
            pool.submit(work_fn, item): idx
            for idx, item in enumerate(items_list)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except BaseException as e:
                results[idx] = ParallelFailure(index=idx, item=items_list[idx], exception=e)
    return results


def split_results(results: list[Any]) -> tuple[list, list[ParallelFailure]]:
    """Convenience: split a results list into (successes, failures)."""
    ok = [r for r in results if not isinstance(r, ParallelFailure)]
    failed = [r for r in results if isinstance(r, ParallelFailure)]
    return ok, failed
