#!/usr/bin/env python3
"""
vLLM Metrics Sampler
====================
Samples vLLM metrics over a time window and dumps a shareable
JSON + human-readable summary for tuning analysis.

Run for 10 minutes then dump:
    uv run --with rich --with requests python vllm_sampler.py --duration 600

Run continuously, dump every 5 minutes:
    uv run --with rich --with requests python vllm_sampler.py --duration 0 --dump-interval 300

Share the output file with Claude for optimal parameter recommendations.
"""

import argparse
import json
import time
import requests
import subprocess
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich import box
from rich.table import Table

console = Console()

parser = argparse.ArgumentParser()
parser.add_argument("--host",          default="localhost")
parser.add_argument("--port",          default=8000, type=int)
parser.add_argument("--interval",      default=5,    type=float, help="Sample every N seconds")
parser.add_argument("--duration",      default=300,  type=int,   help="Total seconds to sample (0=forever)")
parser.add_argument("--dump-interval", default=0,    type=int,   help="Dump summary every N seconds (0=only at end)")
parser.add_argument("--output",        default="vllm_metrics_{ts}.json", help="Output file path")
args = parser.parse_args()

BASE_URL = f"http://{args.host}:{args.port}"

# ── Metric keys we care about ─────────────────────────────────
METRIC_KEYS = [
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:num_requests_swapped",
    "vllm:gpu_cache_usage_perc",
    "vllm:avg_generation_throughput_toks_per_s",
    "vllm:avg_prompt_throughput_toks_per_s",
    "vllm:e2e_request_latency_seconds_sum",
    "vllm:e2e_request_latency_seconds_count",
    "vllm:request_success_total",
    "vllm:num_preemptions_total",
]


def parse_metrics(text: str) -> dict:
    m = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        try:
            key, val = line.rsplit(" ", 1)
            key = key.split("{")[0]
            m[key] = float(val)
        except Exception:
            pass
    return m


def fetch_metrics() -> dict | None:
    try:
        r = requests.get(f"{BASE_URL}/metrics", timeout=3)
        return parse_metrics(r.text) if r.status_code == 200 else None
    except Exception:
        return None


def fetch_health() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def get_gpu() -> dict:
    try:
        mem = subprocess.check_output([
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader"
        ], text=True, timeout=3).strip()

        gpu_mem = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=memory.used,memory.free,memory.total",
            "--format=csv,noheader,nounits"
        ], text=True, timeout=3).strip()

        parts = gpu_mem.split(",")
        used, free, total = int(parts[0]), int(parts[1]), int(parts[2])

        vllm_mem = 0
        for line in mem.splitlines():
            if line.strip():
                p = line.split(",")
                if len(p) == 2:
                    vllm_mem = int(p[1].strip().replace(" MiB", ""))

        return {
            "mem_used_mib":  used,
            "mem_free_mib":  free,
            "mem_total_mib": total,
            "mem_pct":       round(used / total * 100, 1),
            "vllm_mem_mib":  vllm_mem,
        }
    except Exception:
        return {}


def compute_stats(values: list[float]) -> dict:
    if not values:
        return {"min": 0, "max": 0, "avg": 0, "p95": 0}
    s = sorted(values)
    return {
        "min": round(min(s), 3),
        "max": round(max(s), 3),
        "avg": round(sum(s) / len(s), 3),
        "p95": round(s[int(len(s) * 0.95)], 3),
    }


def build_summary(samples: list[dict], config: dict) -> dict:
    """Aggregate samples into a shareable summary."""
    if not samples:
        return {}

    def col(key):
        return [s[key] for s in samples if key in s and s[key] is not None]

    # Latency from counter deltas
    latency_sum_vals  = col("vllm:e2e_request_latency_seconds_sum")
    latency_cnt_vals  = col("vllm:e2e_request_latency_seconds_count")
    avg_latency = None
    if len(latency_sum_vals) >= 2 and len(latency_cnt_vals) >= 2:
        d_sum = latency_sum_vals[-1] - latency_sum_vals[0]
        d_cnt = latency_cnt_vals[-1] - latency_cnt_vals[0]
        avg_latency = round(d_sum / d_cnt, 3) if d_cnt > 0 else None

    total_requests = None
    req_vals = col("vllm:request_success_total")
    if len(req_vals) >= 2:
        total_requests = int(req_vals[-1] - req_vals[0])

    preemptions = None
    pre_vals = col("vllm:num_preemptions_total")
    if len(pre_vals) >= 2:
        preemptions = int(pre_vals[-1] - pre_vals[0])

    return {
        "metadata": {
            "host":            BASE_URL,
            "sample_count":    len(samples),
            "sample_interval": args.interval,
            "duration_secs":   round((samples[-1]["ts"] - samples[0]["ts"]), 1),
            "start":           datetime.fromtimestamp(samples[0]["ts"]).isoformat(),
            "end":             datetime.fromtimestamp(samples[-1]["ts"]).isoformat(),
        },
        "config": config,
        "requests": {
            "running":          compute_stats(col("vllm:num_requests_running")),
            "waiting":          compute_stats(col("vllm:num_requests_waiting")),
            "swapped":          compute_stats(col("vllm:num_requests_swapped")),
            "total_completed":  total_requests,
            "preemptions":      preemptions,
            "avg_e2e_latency_s": avg_latency,
        },
        "throughput": {
            "generation_toks_per_s": compute_stats(col("vllm:avg_generation_throughput_toks_per_s")),
            "prompt_toks_per_s":     compute_stats(col("vllm:avg_prompt_throughput_toks_per_s")),
        },
        "kv_cache": {
            "usage_pct": compute_stats([v * 100 for v in col("vllm:gpu_cache_usage_perc")]),
        },
        "gpu_memory": {
            "used_mib":   compute_stats([s["gpu_mem_used"] for s in samples if "gpu_mem_used" in s]),
            "total_mib":  samples[-1].get("gpu_mem_total", 0),
            "vllm_mib":   compute_stats([s["gpu_vllm_mem"] for s in samples if "gpu_vllm_mem" in s]),
            "usage_pct":  compute_stats([s["gpu_mem_pct"] for s in samples if "gpu_mem_pct" in s]),
        },
        "alerts": {
            "kv_cache_over_90pct":   sum(1 for v in col("vllm:gpu_cache_usage_perc") if v > 0.90),
            "queue_over_5":          sum(1 for v in col("vllm:num_requests_waiting") if v > 5),
            "swaps_occurred":        any(v > 0 for v in col("vllm:num_requests_swapped")),
            "gpu_mem_over_95pct":    sum(1 for s in samples if s.get("gpu_mem_pct", 0) > 95),
        },
    }


def fetch_vllm_config() -> dict:
    """Try to get running config from vLLM."""
    try:
        r = requests.get(f"{BASE_URL}/v1/models", timeout=3)
        if r.status_code == 200:
            data = r.json()
            models = data.get("data", [])
            if models:
                return {"model": models[0].get("id"), "owned_by": models[0].get("owned_by")}
    except Exception:
        pass
    return {}


def print_summary(summary: dict):
    """Print a human-readable summary table."""
    console.print()

    meta = summary.get("metadata", {})
    req  = summary.get("requests", {})
    tput = summary.get("throughput", {})
    kv   = summary.get("kv_cache", {})
    gpu  = summary.get("gpu_memory", {})
    alrt = summary.get("alerts", {})

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
              title=f"[bold]Metrics Summary — {meta.get('duration_secs', 0):.0f}s window[/bold]",
              expand=True)
    t.add_column("Category",  style="cyan",  width=28)
    t.add_column("Metric",    style="white", width=30)
    t.add_column("Min",       justify="right", width=10)
    t.add_column("Avg",       justify="right", width=10)
    t.add_column("P95",       justify="right", width=10)
    t.add_column("Max",       justify="right", width=10)

    def row(cat, metric, stats, unit=""):
        t.add_row(
            cat, metric,
            f"{stats['min']}{unit}",
            f"{stats['avg']}{unit}",
            f"{stats['p95']}{unit}",
            f"{stats['max']}{unit}",
        )

    row("Requests",   "Running",              req.get("running", {}))
    row("Requests",   "Waiting (queue)",       req.get("waiting", {}))
    row("Requests",   "Swapped",               req.get("swapped", {}))
    row("KV Cache",   "Usage",                 kv.get("usage_pct", {}), "%")
    row("Throughput", "Generation (tok/s)",    tput.get("generation_toks_per_s", {}))
    row("Throughput", "Prompt (tok/s)",        tput.get("prompt_toks_per_s", {}))
    row("GPU Memory", "Used (MiB)",            gpu.get("used_mib", {}))
    row("GPU Memory", "vLLM process (MiB)",    gpu.get("vllm_mib", {}))
    row("GPU Memory", "Usage %",               gpu.get("usage_pct", {}), "%")

    console.print(t)

    # Scalar stats
    scalar = Table(box=box.SIMPLE, show_header=False, expand=True)
    scalar.add_column(style="cyan",  width=35)
    scalar.add_column(style="white", width=20)

    if req.get("avg_e2e_latency_s") is not None:
        scalar.add_row("Avg E2E Latency", f"{req['avg_e2e_latency_s']:.2f}s")
    if req.get("total_completed") is not None:
        scalar.add_row("Total Requests Completed", str(req["total_completed"]))
    if req.get("preemptions") is not None:
        scalar.add_row("KV Preemptions", str(req["preemptions"]))

    console.print(scalar)

    # Alerts
    alert_lines = []
    if alrt.get("kv_cache_over_90pct", 0) > 0:
        alert_lines.append(f"[red]⚠ KV cache > 90% for {alrt['kv_cache_over_90pct']} samples[/red]")
    if alrt.get("queue_over_5", 0) > 0:
        alert_lines.append(f"[red]⚠ Request queue > 5 for {alrt['queue_over_5']} samples[/red]")
    if alrt.get("swaps_occurred"):
        alert_lines.append("[red]⚠ KV cache swaps occurred — memory pressure[/red]")
    if alrt.get("gpu_mem_over_95pct", 0) > 0:
        alert_lines.append(f"[red]⚠ GPU memory > 95% for {alrt['gpu_mem_over_95pct']} samples[/red]")
    if not alert_lines:
        alert_lines.append("[green]✓ No alerts — parameters look healthy[/green]")

    console.print(Panel(
        "\n".join(alert_lines),
        title="[bold]Alerts[/bold]",
        border_style="yellow",
        padding=(0, 1),
    ))


def dump_to_file(summary: dict, samples: list[dict]) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = args.output.replace("{ts}", ts)
    output = {
        "summary": summary,
        "raw_samples": samples[-50:],  # last 50 samples for context
    }
    Path(path).write_text(json.dumps(output, indent=2))
    return path


# ── Main ─────────────────────────────────────────────────────

def main():
    console.print(Panel.fit(
        f"[bold cyan]vLLM Metrics Sampler[/bold cyan]\n\n"
        f"[dim]Server   :[/dim] [cyan]{BASE_URL}[/cyan]\n"
        f"[dim]Interval :[/dim] {args.interval}s\n"
        f"[dim]Duration :[/dim] {'∞ (Ctrl+C to stop)' if args.duration == 0 else f'{args.duration}s'}\n\n"
        "[dim]Share the output JSON with Claude for optimal parameter tuning.[/dim]",
        border_style="bright_cyan",
        padding=(1, 2),
    ))

    config = fetch_vllm_config()
    samples = []
    start_ts = time.time()
    last_dump_ts = start_ts

    try:
        while True:
            now = time.time()

            # Check duration
            if args.duration > 0 and (now - start_ts) >= args.duration:
                break

            # Sample
            metrics = fetch_metrics()
            gpu     = get_gpu()

            sample = {"ts": now}
            if metrics:
                for k in METRIC_KEYS:
                    sample[k] = metrics.get(k)
            if gpu:
                sample["gpu_mem_used"]  = gpu.get("mem_used_mib")
                sample["gpu_mem_free"]  = gpu.get("mem_free_mib")
                sample["gpu_mem_total"] = gpu.get("mem_total_mib")
                sample["gpu_mem_pct"]   = gpu.get("mem_pct")
                sample["gpu_vllm_mem"]  = gpu.get("vllm_mem_mib")

            samples.append(sample)

            # Progress line
            elapsed = now - start_ts
            running = int(sample.get("vllm:num_requests_running") or 0)
            waiting = int(sample.get("vllm:num_requests_waiting") or 0)
            kv_pct  = (sample.get("vllm:gpu_cache_usage_perc") or 0) * 100
            gpu_pct = sample.get("gpu_mem_pct") or 0
            gen_tps = sample.get("vllm:avg_generation_throughput_toks_per_s") or 0

            console.print(
                f"  [dim]{datetime.now().strftime('%H:%M:%S')}[/dim]  "
                f"[cyan]running={running:3d}[/cyan]  "
                f"waiting={waiting:2d}  "
                f"kv={kv_pct:5.1f}%  "
                f"gpu={gpu_pct:5.1f}%  "
                f"gen={gen_tps:6.1f} tok/s  "
                f"[dim]samples={len(samples)}[/dim]"
            )

            # Periodic dump
            if args.dump_interval > 0 and (now - last_dump_ts) >= args.dump_interval:
                summary = build_summary(samples, config)
                path = dump_to_file(summary, samples)
                console.print(f"  [green]→ Dumped to {path}[/green]")
                last_dump_ts = now

            time.sleep(args.interval)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — generating final summary...[/yellow]")

    # Final summary
    if samples:
        summary = build_summary(samples, config)
        print_summary(summary)
        path = dump_to_file(summary, samples)
        console.print(f"\n[bold green]✓ Summary saved to: {path}[/bold green]")
        console.print("[dim]Share this file with Claude for parameter tuning recommendations.[/dim]\n")
    else:
        console.print("[red]No samples collected.[/red]")


if __name__ == "__main__":
    main()
