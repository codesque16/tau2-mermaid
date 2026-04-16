#!/usr/bin/env python3
"""
vLLM Rich Console Monitor
=========================
Live dashboard for vLLM instance metrics.

Run:
    uv run --with rich --with requests python vllm_monitor.py
    uv run --with rich --with requests python vllm_monitor.py --host 34.29.173.120
"""

import argparse
import time
import requests
import subprocess
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich import box

# ── Config ────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--host", default="localhost", help="vLLM host")
parser.add_argument("--port", default=8000, type=int, help="vLLM port")
parser.add_argument("--interval", default=3, type=float, help="Refresh interval seconds")
args = parser.parse_args()

BASE_URL = f"http://{args.host}:{args.port}"
console = Console()

# ── Helpers ───────────────────────────────────────────────────

def parse_metrics(text: str) -> dict:
    """Parse Prometheus text format into a flat dict."""
    metrics = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        try:
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                key = parts[0].split("{")[0]  # strip labels
                metrics[key] = float(parts[1])
        except Exception:
            pass
    return metrics


def fetch_metrics() -> dict | None:
    try:
        r = requests.get(f"{BASE_URL}/metrics", timeout=3)
        return parse_metrics(r.text) if r.status_code == 200 else None
    except Exception:
        return None


def fetch_health() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def get_nvidia_smi() -> dict:
    """Get GPU stats via nvidia-smi."""
    try:
        out = subprocess.check_output([
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader"
        ], text=True, timeout=3).strip()

        mem_out = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=memory.used,memory.free,memory.total",
            "--format=csv,noheader,nounits"
        ], text=True, timeout=3).strip()

        mem_parts = mem_out.split(",")
        used = int(mem_parts[0].strip())
        free = int(mem_parts[1].strip())
        total = int(mem_parts[2].strip())

        # Find vLLM process memory
        vllm_mem = 0
        for line in out.splitlines():
            if line.strip():
                parts = line.split(",")
                if len(parts) == 2:
                    vllm_mem = int(parts[1].strip().replace(" MiB", ""))

        return {
            "mem_used": used,
            "mem_free": free,
            "mem_total": total,
            "mem_pct": used / total * 100,
            "vllm_mem": vllm_mem,
        }
    except Exception:
        return {}


def color_bar(pct: float, width: int = 20) -> Text:
    """Render a colored progress bar."""
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    if pct < 60:
        color = "green"
    elif pct < 85:
        color = "yellow"
    else:
        color = "red"
    t = Text()
    t.append(f"[{bar}] ", style=color)
    t.append(f"{pct:5.1f}%", style=f"bold {color}")
    return t


def status_dot(ok: bool) -> Text:
    t = Text()
    t.append("● ", style="bold green" if ok else "bold red")
    t.append("UP" if ok else "DOWN", style="bold green" if ok else "bold red")
    return t


# ── Render ────────────────────────────────────────────────────

def build_dashboard(metrics: dict | None, gpu: dict, healthy: bool) -> Panel:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Header ─────────────────────────────────────────────
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(
        Text(f"vLLM Monitor — {BASE_URL}", style="bold cyan"),
        Text(now, style="dim"),
    )

    # ── Health + Request stats ──────────────────────────────
    req_table = Table(box=box.SIMPLE, show_header=True,
                      header_style="bold white", expand=True)
    req_table.add_column("Metric", style="cyan", width=30)
    req_table.add_column("Value", justify="right", width=12)
    req_table.add_column("Status", width=30)

    if metrics:
        running  = int(metrics.get("vllm:num_requests_running", 0))
        waiting  = int(metrics.get("vllm:num_requests_waiting", 0))
        swapped  = int(metrics.get("vllm:num_requests_swapped", 0))
        kv_pct   = metrics.get("vllm:gpu_cache_usage_perc", 0) * 100
        gen_tps  = metrics.get("vllm:avg_generation_throughput_toks_per_s", 0)
        prompt_tps = metrics.get("vllm:avg_prompt_throughput_toks_per_s", 0)
        total_req  = int(metrics.get("vllm:request_success_total", 0) or
                         metrics.get("vllm:num_requests_running", 0))

        running_bar = color_bar(running / 20 * 100)   # out of max-num-seqs=20
        waiting_color = "green" if waiting == 0 else ("yellow" if waiting < 5 else "red")
        kv_bar = color_bar(kv_pct)

        req_table.add_row("Health",             "",          status_dot(healthy))
        req_table.add_row("Requests Running",   str(running), running_bar)
        req_table.add_row("Requests Waiting",   str(waiting),
                          Text(f"{'OK' if waiting == 0 else 'QUEUED'}", style=f"bold {waiting_color}"))
        req_table.add_row("Requests Swapped",   str(swapped),
                          Text("⚠ KV pressure" if swapped > 0 else "OK",
                               style="bold red" if swapped > 0 else "dim"))
        req_table.add_row("KV Cache Used",      f"{kv_pct:.1f}%", kv_bar)
        req_table.add_row("Generation TPS",     f"{gen_tps:.1f}",
                          Text("tok/s out", style="dim"))
        req_table.add_row("Prompt TPS",         f"{prompt_tps:.1f}",
                          Text("tok/s in", style="dim"))
    else:
        req_table.add_row("Health", "", status_dot(False))
        req_table.add_row("Metrics", "unavailable",
                          Text("Is vLLM running?", style="dim yellow"))

    # ── GPU stats ───────────────────────────────────────────
    gpu_table = Table(box=box.SIMPLE, show_header=True,
                      header_style="bold white", expand=True)
    gpu_table.add_column("GPU Metric", style="cyan", width=30)
    gpu_table.add_column("Value", justify="right", width=12)
    gpu_table.add_column("Bar", width=30)

    if gpu:
        mem_pct   = gpu["mem_pct"]
        used_gb   = gpu["mem_used"] / 1024
        total_gb  = gpu["mem_total"] / 1024
        vllm_gb   = gpu["vllm_mem"] / 1024

        gpu_table.add_row(
            "GPU Memory Used",
            f"{used_gb:.1f} GB",
            color_bar(mem_pct),
        )
        gpu_table.add_row(
            "vLLM Process Memory",
            f"{vllm_gb:.1f} GB",
            Text(f"of {total_gb:.0f} GB total", style="dim"),
        )
        gpu_table.add_row(
            "GPU Memory Free",
            f"{gpu['mem_free']/1024:.1f} GB",
            Text("available", style="dim green"),
        )
    else:
        gpu_table.add_row("nvidia-smi", "unavailable",
                          Text("not accessible from this host", style="dim"))

    # ── Alerts ─────────────────────────────────────────────
    alerts = []
    if metrics:
        if int(metrics.get("vllm:num_requests_waiting", 0)) > 5:
            alerts.append(Text("⚠ Request queue > 5 — consider scaling out", style="bold red"))
        kv = metrics.get("vllm:gpu_cache_usage_perc", 0) * 100
        if kv > 90:
            alerts.append(Text(f"⚠ KV cache at {kv:.0f}% — reduce max-num-seqs or max-model-len", style="bold red"))
        if int(metrics.get("vllm:num_requests_swapped", 0)) > 0:
            alerts.append(Text("⚠ Requests being swapped to CPU — KV cache pressure", style="bold red"))
    if gpu and gpu.get("mem_pct", 0) > 95:
        alerts.append(Text("⚠ GPU memory > 95% — OOM risk", style="bold red"))
    if not healthy:
        alerts.append(Text("✗ vLLM health check failing", style="bold red"))

    alert_text = Text("\n").join(alerts) if alerts else Text("✓ All systems nominal", style="bold green")

    # ── Assemble ────────────────────────────────────────────
    from rich.columns import Columns
    content = Table.grid(expand=True)
    content.add_column(ratio=1)
    content.add_row(header)
    content.add_row(Text(""))
    content.add_row(Panel(req_table, title="[bold]Requests & KV Cache[/bold]",
                          border_style="cyan", padding=(0, 1)))
    content.add_row(Panel(gpu_table, title="[bold]GPU Memory[/bold]",
                          border_style="blue", padding=(0, 1)))
    content.add_row(Panel(alert_text, title="[bold]Alerts[/bold]",
                          border_style="yellow", padding=(0, 1)))

    return Panel(content, border_style="bright_cyan",
                 title="[bold bright_cyan]vLLM Live Monitor[/bold bright_cyan]",
                 padding=(0, 1))


# ── Main loop ─────────────────────────────────────────────────

def main():
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            metrics = fetch_metrics()
            gpu     = get_nvidia_smi()
            healthy = fetch_health()
            live.update(build_dashboard(metrics, gpu, healthy))
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
