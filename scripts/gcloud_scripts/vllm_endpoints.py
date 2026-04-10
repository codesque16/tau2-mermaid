"""
vLLM API Endpoints Demo — with Rich console output
===================================================
Run: uv run --with rich python vllm_endpoints.py
"""

import requests
import json
import math
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()

BASE_URL = "http://34.72.38.143:8000"
MODEL    = "google/gemma-4-E4B-it"

SYSTEM_PROMPT = (
    "You are Orion, a sharp and witty AI assistant who specializes in science and technology. "
    "You give concise, insightful answers with occasional dry humor. Never be verbose."
)

CHAT_MESSAGES = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user",   "content": "What makes a black hole different from a neutron star?"},
]

# Manually formatted with Gemma special tokens — for /v1/completions
RAW_PROMPT = (
    "<bos><start_of_turn>system\n"
    "You are a poet who answers every question with a haiku.<end_of_turn>\n"
    "<start_of_turn>user\n"
    "What is machine learning?<end_of_turn>\n"
    "<start_of_turn>model\n"
)


# ── Helpers ──────────────────────────────────────────────────

def header(title: str, subtitle: str = ""):
    console.print()
    console.rule(f"[bold cyan]{title}[/bold cyan]", style="cyan")
    if subtitle:
        for line in subtitle.split("\n"):
            console.print(f"  [dim]{line.strip()}[/dim]")
    console.print()


def ok(label: str, value):
    console.print(f"  [green]✓[/green] [bold]{label}:[/bold] {value}")


def safe_post(url, **kwargs):
    try:
        r = requests.post(url, **kwargs)
        return r
    except Exception as e:
        console.print(f"  [red]✗ Request failed:[/red] {e}")
        return None


def safe_get(url):
    try:
        return requests.get(url)
    except Exception as e:
        console.print(f"  [red]✗ Request failed:[/red] {e}")
        return None


def extract_tokens(data):
    """vLLM returns 'tokens' in some versions, 'token_ids' in others."""
    return data.get("tokens") or data.get("token_ids") or []


def extract_render_token_ids(data):
    """
    /render endpoints return either:
      - a dict with 'token_ids'
      - a list of dicts (one per request)
    """
    if isinstance(data, list):
        return data[0].get("token_ids") if data else []
    return data.get("token_ids", [])


def extract_render_sampling(data):
    if isinstance(data, list):
        return data[0].get("sampling_params", {}) if data else {}
    return data.get("sampling_params", {})


# ─────────────────────────────────────────────────────────────
console.print()
console.print(Panel.fit(
    "[bold white]vLLM API Endpoints Explorer[/bold white]\n"
    f"[dim]Server:[/dim] [cyan]{BASE_URL}[/cyan]   [dim]Model:[/dim] [cyan]{MODEL}[/cyan]",
    border_style="bright_cyan", padding=(1, 4)
))


# ── 1. Health ────────────────────────────────────────────────
header("GET /health", "Liveness check — is the server up?")
r = safe_get(f"{BASE_URL}/health")
if r and r.status_code == 200:
    console.print("  [green]● Server is healthy[/green]")
else:
    console.print(f"  [red]✗ Status: {r.status_code if r else 'no response'}[/red]")


# ── 2. Ping ──────────────────────────────────────────────────
header("GET + POST /ping", "Alias for /health used by some load balancers")
for method, fn in [("GET", requests.get), ("POST", requests.post)]:
    r = fn(f"{BASE_URL}/ping")
    status = "[green]200 OK[/green]" if r.status_code == 200 else f"[red]{r.status_code}[/red]"
    console.print(f"  [bold]{method:4s}[/bold] /ping → {status}")


# ── 3. Version ───────────────────────────────────────────────
header("GET /version", "Returns vLLM server version")
r = safe_get(f"{BASE_URL}/version")
if r:
    ok("vLLM version", r.json().get("version", "unknown"))


# ── 4. Load ──────────────────────────────────────────────────
header("GET /load", "Current request queue depth")
r = safe_get(f"{BASE_URL}/load")
if r:
    load = r.json().get("server_load", 0)
    console.print(f"  [bold]Server load:[/bold] {load}  [dim]{'(idle)' if load == 0 else '▓' * int(float(load) * 10)}[/dim]")


# ── 5. Metrics ───────────────────────────────────────────────
header("GET /metrics", "Prometheus metrics — tokens/sec, queue length, KV cache usage")
r = safe_get(f"{BASE_URL}/metrics")
if r:
    lines = r.text.strip().split('\n')
    # Pick the most useful vllm metrics (skip _created/_total noise)
    INTERESTING = [
        "num_requests_running", "num_requests_waiting",
        "kv_cache_usage_perc", "prefix_cache_hits_total",
        "prefix_cache_queries_total", "engine_sleep_state",
        "gpu_cache_usage_perc", "num_preemptions_total",
        "generation_tokens_total", "prompt_tokens_total",
        "time_to_first_token_seconds", "time_per_output_token_seconds",
    ]
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Metric",  style="cyan",  no_wrap=True)
    table.add_column("Value",   justify="right")
    shown = 0
    for line in lines:
        if line.startswith("#") or not line.startswith("vllm:"):
            continue
        if any(k in line for k in INTERESTING):
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                # shorten long label: strip engine/model_name labels
                label = parts[0].split("{")[0]
                # add sleep_state value if present
                if "sleep_state=" in parts[0]:
                    state = parts[0].split('sleep_state="')[1].split('"')[0]
                    label = f"{label}[{state}]"
                table.add_row(label, parts[1])
                shown += 1
    console.print(table)
    console.print(f"  [dim]({len(lines)} total Prometheus lines)[/dim]")


# ── 6. Models ────────────────────────────────────────────────
header("GET /v1/models", "Lists all loaded models")
r = safe_get(f"{BASE_URL}/v1/models")
if r:
    for m in r.json().get("data", []):
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column("Key",   style="dim",   width=12)
        table.add_column("Value", style="white")
        table.add_row("id",       m["id"])
        table.add_row("object",   m["object"])
        table.add_row("owned_by", m["owned_by"])
        console.print(table)


# ── 7. OpenAPI Schema ────────────────────────────────────────
header("GET /openapi.json", "Full OpenAPI schema — also browsable at /docs in browser")
r = safe_get(f"{BASE_URL}/openapi.json")
if r:
    schema = r.json()
    ok("Title",   schema["info"]["title"])
    ok("Version", schema["info"]["version"])
    ok("Paths",   len(schema["paths"]))
    console.print(f"  [dim]{list(schema['paths'].keys())}[/dim]")


# ── 8. Tokenize ──────────────────────────────────────────────
header("POST /tokenize",
       "Converts messages → token IDs with chat template applied.\n"
       "Use this to count tokens BEFORE sending a generation request.")
r = safe_post(f"{BASE_URL}/tokenize", json={
    "model": MODEL,
    "messages": [{"role": "user", "content": "Hello, world!"}],
    "add_special_tokens": True,
})
token_ids = []
token_strs = []
if r:
    data = r.json()
    token_ids  = extract_tokens(data)
    token_strs = data.get("token_strs", [])
    count      = data.get("count") or len(token_ids)
    console.print(f"  [bold]Response keys:[/bold] [dim]{list(data.keys())}[/dim]")
    console.print(f"  [bold]Count        :[/bold] {count}")
    console.print(f"  [bold]Token IDs    :[/bold] [cyan]{token_ids}[/cyan]")
    if token_strs:
        console.print(f"  [bold]Token strings:[/bold] [yellow]{token_strs}[/yellow]")


# ── 9. Detokenize ────────────────────────────────────────────
header("POST /detokenize",
       "Token IDs → raw text string.\n"
       "Special tokens like <bos> <start_of_turn> are fully visible here.")
if token_ids:
    r = safe_post(f"{BASE_URL}/detokenize", json={"model": MODEL, "tokens": token_ids})
    if r:
        decoded = r.json().get("prompt", "")
        console.print(Panel(
            f"[yellow]{repr(decoded)}[/yellow]",
            title="Decoded prompt (with special tokens)", border_style="yellow", padding=(0, 1)
        ))


# ── 10. Render Chat Prompt ───────────────────────────────────
header("POST /v1/chat/completions/render",
       "Shows the final prompt exactly as the model receives it.\n"
       "No generation — just the rendered input + sampling config.")
r = safe_post(f"{BASE_URL}/v1/chat/completions/render", json={
    "model": MODEL,
    "messages": [{"role": "user", "content": "Hello!"}],
})
if r:
    data = r.json()
    ids = extract_render_token_ids(data)
    sp  = extract_render_sampling(data)
    console.print(f"  [bold]Token IDs  :[/bold] [cyan]{ids}[/cyan]")
    console.print(f"  [bold]Temperature:[/bold] {sp.get('temperature')}  "
                  f"[bold]top_p:[/bold] {sp.get('top_p')}  "
                  f"[bold]top_k:[/bold] {sp.get('top_k')}")


# ── 11. Render Raw Completion ────────────────────────────────
header("POST /v1/completions/render",
       "Same as above but for raw /v1/completions (no chat template applied).")
r = safe_post(f"{BASE_URL}/v1/completions/render", json={
    "model": MODEL,
    "prompt": "The speed of light is",
})
if r:
    data = r.json()
    ids = extract_render_token_ids(data)
    console.print(f"  [bold]Token IDs:[/bold] [cyan]{ids}[/cyan]")


# ── 12. Chat Completions ─────────────────────────────────────
header("POST /v1/chat/completions",
       "Main chat endpoint. Applies chat template, supports system prompt + history.")
console.print(Panel(
    f"[bold]System:[/bold] [dim]{SYSTEM_PROMPT[:90]}...[/dim]\n\n"
    f"[bold]User:[/bold]   [white]{CHAT_MESSAGES[-1]['content']}[/white]",
    title="Input", border_style="dim", padding=(0, 1)
))
r = safe_post(f"{BASE_URL}/v1/chat/completions", json={
    "model": MODEL,
    "messages": CHAT_MESSAGES,
    "max_tokens": 120,
    "temperature": 0.7,
})
if r:
    data  = r.json()
    reply = data["choices"][0]["message"]["content"]
    usage = data["usage"]
    console.print(Panel(
        f"[bold green]{reply}[/bold green]",
        title="[green]Orion (assistant)[/green]", border_style="green", padding=(0, 1)
    ))
    console.print(
        f"  [dim]prompt={usage['prompt_tokens']} + "
        f"completion={usage['completion_tokens']} = "
        f"total={usage['total_tokens']} tokens[/dim]"
    )


# ── 13. Streaming Chat ───────────────────────────────────────
header("POST /v1/chat/completions (stream=True)",
       "Streams tokens back one-by-one via SSE.\n"
       "Use this for real-time typing-effect UIs.")
r = requests.post(f"{BASE_URL}/v1/chat/completions", json={
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a dramatic narrator. Be vivid but brief."},
        {"role": "user",   "content": "Describe a sunset in exactly 2 sentences."},
    ],
    "max_tokens": 80,
    "stream": True,
}, stream=True)
console.print("  [bold]Streaming:[/bold] ", end="")
for line in r.iter_lines():
    if line:
        line = line.decode("utf-8")
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[6:])
            delta = chunk["choices"][0]["delta"].get("content", "")
            console.print(f"[cyan]{delta}[/cyan]", end="")
console.print()


# ── 14. Batch Chat ───────────────────────────────────────────
header("POST /v1/chat/completions/batch",
       "Multiple independent chat requests in a single HTTP call.\n"
       "More efficient than N separate round trips.")
batch_requests = [
    {"model": MODEL, "messages": [{"role": "user", "content": "Name one planet."}],  "max_tokens": 10},
    {"model": MODEL, "messages": [{"role": "user", "content": "Name one element."}], "max_tokens": 10},
    {"model": MODEL, "messages": [{"role": "user", "content": "Name one ocean."}],   "max_tokens": 10},
]
r = safe_post(f"{BASE_URL}/v1/chat/completions/batch", json=batch_requests)
if r:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("#",        width=3)
    table.add_column("Question", style="dim")
    table.add_column("Answer",   style="green")
    for i, item in enumerate(r.json()):
        q = batch_requests[i]["messages"][0]["content"]
        a = item["choices"][0]["message"]["content"].strip()
        table.add_row(str(i + 1), q, a)
    console.print(table)


# ── 15. Raw Completions ──────────────────────────────────────
header("POST /v1/completions",
       "Raw text completion — NO chat template.\n"
       "You manually include special tokens for full control over the prompt.")
console.print(Panel(
    f"[dim]{RAW_PROMPT}[/dim]",
    title="Raw prompt (manually formatted with Gemma special tokens)",
    border_style="dim yellow", padding=(0, 1)
))
r = safe_post(f"{BASE_URL}/v1/completions", json={
    "model": MODEL,
    "prompt": RAW_PROMPT,
    "max_tokens": 30,
    "temperature": 0.9,
})
if r:
    text = r.json()["choices"][0]["text"].strip()
    console.print(Panel(
        f"[bold yellow]{text}[/bold yellow]",
        title="[yellow]Model completion[/yellow]", border_style="yellow", padding=(0, 1)
    ))


# ── 16. Responses API ────────────────────────────────────────
header("POST /v1/responses",
       "OpenAI Responses API — stateful conversations with persistent IDs.")
r = safe_post(f"{BASE_URL}/v1/responses", json={
    "model": MODEL,
    "input": "What is the Fermi paradox in one sentence?",
    "max_output_tokens": 60,
})
if r:
    data = r.json()
    response_id = data.get("id")
    ok("Response ID", response_id)
    for item in data.get("output", []):
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content", []):
                console.print(f"  [green]{c.get('text', '')}[/green]")
    if response_id:
        r2 = safe_get(f"{BASE_URL}/v1/responses/{response_id}")
        ok("GET by ID status", f"{r2.status_code}" if r2 else "failed")


# ── 17. Anthropic Messages API ───────────────────────────────
header("POST /v1/messages",
       "Anthropic Claude-compatible API.\n"
       "Point the `anthropic` Python SDK at this URL to use Gemma as a Claude drop-in.")
r = safe_post(f"{BASE_URL}/v1/messages", json={
    "model": MODEL,
    "max_tokens": 60,
    "system": "You are a laconic Zen master.",
    "messages": [{"role": "user", "content": "What is the meaning of life?"}],
}, headers={"anthropic-version": "2023-06-01"})
if r:
    data    = r.json()
    content = data.get("content", [])
    text    = content[0].get("text", "") if content else str(data)
    console.print(Panel(
        f"[bold magenta]{text}[/bold magenta]",
        title="[magenta]Anthropic-style response[/magenta]", border_style="magenta", padding=(0, 1)
    ))


# ── 18. Anthropic Count Tokens ───────────────────────────────
header("POST /v1/messages/count_tokens",
       "Anthropic-compatible token counter.\n"
       "Returns token count without generating any output.")
r = safe_post(f"{BASE_URL}/v1/messages/count_tokens", json={
    "model": MODEL,
    "messages": [{"role": "user", "content": "How many tokens does this sentence use?"}],
}, headers={"anthropic-version": "2023-06-01"})
if r:
    data = r.json()
    ok("Input tokens", data.get("input_tokens", data))


# ── 19. Logprobs ─────────────────────────────────────────────
header("POST /v1/chat/completions (logprobs=True)",
       "Per-token log probabilities.\n"
       "Use for confidence scoring, reranking outputs, or beam search.")
r = safe_post(f"{BASE_URL}/v1/chat/completions", json={
    "model": MODEL,
    "messages": [{"role": "user", "content": "Complete with one word: The sky is ___"}],
    "max_tokens": 5,
    "logprobs": True,
    "top_logprobs": 3,
})
if r:
    data  = r.json()
    reply = data["choices"][0]["message"]["content"]
    ok("Response", reply)
    lp = data["choices"][0].get("logprobs", {})
    if lp and lp.get("content"):
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("Token",   style="green",  width=14)
        table.add_column("logprob", justify="right", width=10)
        table.add_column("prob %",  justify="right", style="yellow", width=8)
        table.add_column("Top alternatives")
        for t in lp["content"]:
            prob = f"{math.exp(t['logprob']) * 100:.1f}%"
            alts = "  ".join(
                f"[dim]{repr(a['token'])}[/dim] {math.exp(a['logprob'])*100:.1f}%"
                for a in t.get("top_logprobs", [])[:3]
            )
            table.add_row(repr(t["token"]), f"{t['logprob']:.3f}", prob, alts)
        console.print(table)


# ── Done ─────────────────────────────────────────────────────
console.print()
console.rule("[bold green]✓ All endpoints complete[/bold green]", style="green")
console.print()
