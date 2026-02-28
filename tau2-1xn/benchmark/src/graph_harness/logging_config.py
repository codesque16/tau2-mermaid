"""
Rich console logging and Logfire integration for the benchmark.
"""

from __future__ import annotations

from typing import Any

import logfire

# Rich console and helpers
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

console = Console()


def configure_logfire(
    *,
    service_name: str = "graph-traversal-benchmark",
    console: bool = True,
) -> None:
    """Configure Logfire (tracing + optional console). Instruments LiteLLM for LLM spans."""
    logfire.configure(
        scrubbing=False,
        service_name=service_name,
        console=console,
    )
    logfire.instrument_litellm()


def log_header(model: str, trials: int, conditions: list[str], scenario_count: int) -> None:
    """Print run header with rich panel."""
    console.print(
        Panel(
            f"[bold]Model:[/bold] {model}\n"
            f"[bold]Trials:[/bold] {trials}\n"
            f"[bold]Conditions:[/bold] {', '.join(conditions)}\n"
            f"[bold]Scenarios:[/bold] {scenario_count}",
            title="[bold blue]Graph Traversal Benchmark[/bold blue]",
            border_style="blue",
        )
    )


def log_scenario_start(scenario_id: str, test_case_count: int) -> None:
    """Log start of a scenario."""
    console.print(f"\n[dim]→ Scenario:[/dim] [cyan]{scenario_id}[/cyan] ({test_case_count} test case(s))")


def log_trial_progress(
    progress: Progress,
    task_id: int,
    scenario_id: str,
    test_id: str,
    condition: str,
    trial: int,
    total_trials: int,
    passed: bool,
) -> None:
    """Update progress and optionally log a single trial result."""
    status = "[green]✓[/green]" if passed else "[red]✗[/red]"
    progress.update(
        task_id,
        advance=1,
        description=f"{status} {scenario_id} | {test_id} | {condition} | trial {trial + 1}/{total_trials}",
    )


def log_results_table(aggregate: dict[str, dict[str, Any]], trials: int) -> None:
    """Print aggregate results as a rich table."""
    table = Table(title="Results (aggregate)", show_header=True, header_style="bold")
    table.add_column("Condition", style="cyan")
    table.add_column("pass^1", justify="right")
    table.add_column(f"pass^{trials}", justify="right")
    table.add_column("Path accuracy", justify="right")
    table.add_column("n", justify="right")

    for cond, m in aggregate.items():
        n = m.get("n", 0)
        pass_1 = m.get("pass^1", 0.0)
        pass_k = m.get(f"pass^{trials}", 0.0)
        path_acc = m.get("path_accuracy", 0.0)
        table.add_row(
            cond,
            f"{pass_1:.1%}",
            f"{pass_k:.1%}",
            f"{path_acc:.1%}",
            str(n),
        )

    console.print()
    console.print(table)


def log_agent_run(
    condition: str,
    test_id: str,
    turn: int,
    path: list[str],
    expected_path: list[str],
    passed: bool,
) -> None:
    """Log a single agent run (for verbose / debug)."""
    status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
    path_str = " → ".join(path) if path else "(empty)"
    expected_str = " → ".join(expected_path) if expected_path else "(empty)"
    console.print(
        f"  {status} [dim]{condition}[/dim] [dim]{test_id}[/dim] turn={turn} "
        f"[dim]path=[/dim]{path_str}"
    )
    if not passed:
        console.print(f"    [dim]expected:[/dim] {expected_str}")


def create_progress(total: int, description: str = "Running") -> Progress:
    """Create a Rich Progress with one task (call start_task and update in a loop)."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        SpinnerColumn(),
        console=console,
    )
