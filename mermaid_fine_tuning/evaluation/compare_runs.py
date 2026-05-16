"""Compare eval results across multiple experiments.

Usage:
    python -m evaluation.compare_runs \
        experiments/baseline \
        experiments/qlora_26b_pilot \
        experiments/qlora_data_1k \
        experiments/qlora_data_full \
        experiments/full_sft_26b

Outputs a side-by-side metrics table. Higher-is-better for everything except
`off_graph_rate_pct` (lower is better, flagged in the output).
"""
from __future__ import annotations
import sys, os, json, argparse
from rich.console import Console
from rich.table import Table
from rich import box


_console = Console()

# Lower-is-better metrics, displayed differently from the rest
LOWER_IS_BETTER = {"off_graph_rate_pct"}


def load_experiment(exp_dir: str) -> tuple[str, dict]:
    """Load (name, summary_dict) for one experiment."""
    eval_path = os.path.join(exp_dir, "eval.json")
    manifest_path = os.path.join(exp_dir, "manifest.yaml")
    if not os.path.isfile(eval_path):
        raise FileNotFoundError(f"{eval_path} not found — has eval run for this experiment?")

    with open(eval_path) as f:
        data = json.load(f)
    summary = data.get("summary", {})

    # Use the manifest name if available, else the directory name
    name = os.path.basename(os.path.normpath(exp_dir))
    if os.path.isfile(manifest_path):
        try:
            import yaml
            with open(manifest_path) as f:
                m = yaml.safe_load(f)
            name = m.get("name", name)
        except Exception:
            pass

    return name, summary


def main(exp_dirs: list[str]):
    runs = [load_experiment(d) for d in exp_dirs]
    if not runs:
        print("No experiments to compare.")
        return

    # Collect all metric names across runs
    all_metrics = set()
    for _, s in runs:
        all_metrics.update(s.keys())
    all_metrics = sorted(all_metrics)

    # Render a table
    table = Table(title="Experiment comparison", box=box.SIMPLE_HEAVY)
    table.add_column("metric", style="cyan", no_wrap=True)
    for name, _ in runs:
        table.add_column(name, justify="right")

    # First run is the baseline for delta highlighting
    baseline_name, baseline_summary = runs[0]

    for metric in all_metrics:
        row = [metric]
        baseline_val = baseline_summary.get(metric)
        for name, summary in runs:
            val = summary.get(metric)
            if val is None:
                row.append("—")
                continue
            if not isinstance(val, (int, float)):
                row.append(str(val))
                continue

            cell = f"{val:.2f}"
            # Color cells vs baseline (except the baseline column itself)
            if name != baseline_name and isinstance(baseline_val, (int, float)):
                delta = val - baseline_val
                higher_is_better = metric not in LOWER_IS_BETTER
                improving = (delta > 0) if higher_is_better else (delta < 0)
                if abs(delta) < 0.5:
                    cell = f"[dim]{val:.2f}[/dim]"
                elif improving:
                    cell = f"[green]{val:.2f}[/green] [dim]({'+' if delta > 0 else ''}{delta:.2f})[/dim]"
                else:
                    cell = f"[red]{val:.2f}[/red] [dim]({'+' if delta > 0 else ''}{delta:.2f})[/dim]"
            row.append(cell)
        table.add_row(*row)

    _console.print(table)
    _console.print(f"\n[dim]Baseline: {baseline_name}. Green/red = better/worse than baseline. "
                   f"off_graph_rate_pct is lower-is-better.[/dim]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment_dirs", nargs="+", help="paths to experiment directories")
    args = ap.parse_args()
    main(args.experiment_dirs)
