"""Rich terminal display: markdown, streaming, and tool call details."""

import json
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

console = Console()


def print_role_header(role: str, turn: int | None = None, seed: bool = False) -> None:
    """Print a section header for the given role."""
    label = f"[{role}]"
    if turn is not None:
        label += f" (turn {turn})"
    if seed:
        label += " (seed)"
    console.print()
    console.print(Rule(label, style="bold blue"))
    console.print()


def print_markdown(content: str) -> None:
    """Render and print content as markdown."""
    if not content.strip():
        return
    console.print(Markdown(content))


def print_tool_call(name: str, tool_id: str, input_data: dict[str, Any] | None) -> None:
    """Print a tool call in a panel with formatted input."""
    input_str = json.dumps(input_data or {}, indent=2)
    body = f"[bold cyan]Tool:[/] {name}\n[bold]ID:[/] {tool_id}\n\n[bold]Input:[/]\n{input_str}"
    console.print(Panel(body, title="[bold]Tool call[/]", border_style="yellow"))
    console.print()


def print_stop_phrase(reason: str) -> None:
    """Print stop phrase detection message."""
    console.print(f"\n[green]{reason}[/green]")


def print_max_turns_reached(max_turns: int) -> None:
    """Print message when max turns is reached."""
    console.print(f"\n[yellow]Max turns ({max_turns}) reached.[/yellow]")


def print_simulation_complete(message_count: int) -> None:
    """Print simulation completion summary."""
    console.print()
    console.print(Rule(style="dim"))
    console.print(f"[dim]Simulation complete. Total messages: {message_count}[/dim]")


def _format_usage(usage: dict[str, Any] | None) -> str:
    """Format usage dict as a short line for display."""
    if not usage:
        return "—"
    parts = []
    if usage.get("input_tokens") is not None:
        parts.append(f"in: {usage['input_tokens']:,}")
    if usage.get("output_tokens") is not None:
        parts.append(f"out: {usage['output_tokens']:,}")
    if usage.get("cache_read_input_tokens"):
        parts.append(f"cached: {usage['cache_read_input_tokens']:,}")
    if usage.get("cache_creation_input_tokens"):
        parts.append(f"cache_wr: {usage['cache_creation_input_tokens']:,}")
    return "  ".join(parts) if parts else "—"


def print_turn_cost(role: str, usage: dict[str, Any] | None, cost: float | None) -> None:
    """Print token usage and cost for one turn (assistant or user)."""
    usage_str = _format_usage(usage)
    cost_str = f"${cost:.4f}" if cost is not None and cost > 0 else "—"
    body = f"[bold]Tokens:[/] {usage_str}\n[bold]Cost this turn:[/] {cost_str}"
    console.print(Panel(body, title=f"[bold]Usage — {role}[/]", border_style="dim"))
    console.print()


def print_total_cost(agent_cost: float, user_cost: float) -> None:
    """Print total simulation cost (agent + user)."""
    total = agent_cost + user_cost
    body = (
        f"[bold]Assistant total:[/] ${agent_cost:.4f}\n"
        f"[bold]User total:[/]     ${user_cost:.4f}\n"
        f"[bold]Total:[/]          ${total:.4f}"
    )
    console.print(Panel(body, title="[bold]Total cost (simulation)[/]", border_style="green"))
    console.print()


class StreamingDisplay:
    """Live-updating display for streaming markdown and tool calls."""

    def __init__(self) -> None:
        self._live: Live | None = None
        self._accumulated = ""

    def __enter__(self) -> "StreamingDisplay":
        self._live = Live(
            Markdown(""),
            console=console,
            refresh_per_second=8,
            transient=False,
        )
        self._live.start()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def update_text(self, text: str) -> None:
        """Update the live display with accumulated text as markdown."""
        self._accumulated = text
        if self._live is not None:
            self._live.update(Markdown(text) if text.strip() else Markdown("*...*"))

    def finish(self) -> None:
        """Stop live display; final content is already shown."""
        if self._live is not None:
            self._live.stop()
            self._live = None
