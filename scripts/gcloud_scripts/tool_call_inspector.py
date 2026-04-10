"""
Gemma 4 Tool Call + Reasoning — Raw Token Inspector
====================================================
Multi-step tool chain: model can call tools multiple times per user turn
before giving a final answer.

vLLM serve command:
  vllm serve google/gemma-4-E4B-it \
      --gpu-memory-utilization 0.90 \
      --max-model-len 8192 \
      --dtype auto \
      --enable-auto-tool-choice \
      --tool-call-parser gemma4 \
      --reasoning-parser gemma4

Run:
  uv run --with rich python tool_call_inspector.py
"""

import requests
import json
import math as _math
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich import box

console = Console()

BASE_URL = "http://34.72.38.143:8000"
BASE_URL = "http://34.29.173.120:8000"
MODEL    = "google/gemma-4-E4B-it"
MODEL    = "google/gemma-4-26B-A4B-it"
CHAT_TEMPLATE_KWARGS = {"enable_thinking": True}
MAX_TOOL_STEPS = 8   # max tool call rounds per user turn

# ── Tool Definitions ──────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city. Returns temperature, conditions, humidity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city":  {"type": "string", "description": "City name e.g. 'Tokyo'"},
                    "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for recent information on a topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string",  "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results (1-10)", "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a mathematical expression. Supports +,-,*,/,**,sqrt,log,floor,ceil,int,min,max,pow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "e.g. 'sqrt(144) + 2**8'"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "Get current stock price and daily change for a ticker symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "e.g. 'AAPL'"},
                },
                "required": ["ticker"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are Nexus, a highly capable AI assistant with access to real-time tools.\n"
    "Think carefully before each action. Use tools when needed and synthesize results "
    "into concise, direct answers.\n"
    "When reasoning, be thorough. When answering, be brief."
)


# ── Fake Tool Implementations ─────────────────────────────────
def call_tool(name: str, args: dict) -> str:
    if name == "get_weather":
        city  = args.get("city", "Unknown")
        units = args.get("units", "celsius")
        temps = {"Tokyo": 22, "London": 14, "New York": 18, "Sydney": 25}
        temp  = temps.get(city, 20)
        if units == "fahrenheit":
            temp = temp * 9 // 5 + 32
        sym = "\u00b0F" if units == "fahrenheit" else "\u00b0C"
        return json.dumps({"city": city, "temperature": f"{temp}{sym}",
                           "conditions": "Partly cloudy", "humidity": "65%",
                           "wind": "12 km/h NW"})

    elif name == "search_web":
        q = args.get("query", "")
        return json.dumps({"results": [
            {"title": f"Latest on: {q}",
             "snippet": (f"Recent 2026 developments in {q} show significant progress. "
                         "New benchmarks demonstrate state-of-the-art performance."),
             "url": "https://example.com/1"},
            {"title": f"{q} — Comprehensive Overview",
             "snippet": (f"{q} covers key concepts including architecture, training, and deployment. "
                         "Community adoption has accelerated in recent months."),
             "url": "https://example.com/2"},
        ]})

    elif name == "calculate":
        expr = args.get("expression", "0")
        try:
            result = eval(expr, {"__builtins__": {}}, {
                "sqrt": _math.sqrt, "log": _math.log, "log2": _math.log2,
                "log10": _math.log10, "sin": _math.sin, "cos": _math.cos,
                "pi": _math.pi, "e": _math.e, "abs": abs, "round": round,
                "floor": _math.floor, "ceil": _math.ceil, "int": int,
                "min": min, "max": max, "pow": pow,
            })
            return json.dumps({"expression": expr, "result": round(result, 6)})
        except Exception as ex:
            return json.dumps({"error": str(ex)})

    elif name == "get_stock_price":
        ticker = args.get("ticker", "?").upper()
        prices = {"AAPL": (189.50, +1.23), "GOOGL": (171.20, -0.45),
                  "NVDA": (875.30, +12.50), "TSLA": (248.10, -3.20)}
        price, change = prices.get(ticker, (100.00, 0.00))
        return json.dumps({"ticker": ticker, "price": f"${price:.2f}",
                           "change": f"{change:+.2f}", "currency": "USD"})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── API Helpers ───────────────────────────────────────────────

def render_prompt(messages: list) -> tuple[list, dict]:
    """Get token IDs for the full current prompt via /render."""
    for payload in [
        {"model": MODEL, "messages": messages, "tools": TOOLS,
         "chat_template_kwargs": CHAT_TEMPLATE_KWARGS},
        {"model": MODEL, "messages": messages,
         "chat_template_kwargs": CHAT_TEMPLATE_KWARGS},
        {"model": MODEL, "messages": messages},
    ]:
        try:
            r = requests.post(f"{BASE_URL}/v1/chat/completions/render", json=payload)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    data = data[0] if data else {}
                ids = data.get("token_ids", [])
                if ids:
                    return ids, data.get("sampling_params", {})
        except Exception:
            pass
    return [], {}


def detokenize(token_ids: list) -> str:
    if not token_ids:
        return ""
    r = requests.post(f"{BASE_URL}/detokenize",
                      json={"model": MODEL, "tokens": token_ids})
    return r.json().get("prompt", "")


def tokenize_text(text: str) -> list:
    if not text:
        return []
    r = requests.post(f"{BASE_URL}/tokenize",
                      json={"model": MODEL, "prompt": text,
                            "add_special_tokens": False})
    data = r.json()
    return data.get("tokens") or data.get("token_ids") or []


def chat_api(messages: list, max_tokens: int = 1024) -> dict:
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json={
        "model":                 MODEL,
        "messages":              messages,
        "tools":                 TOOLS,
        "tool_choice":           "auto",
        "max_tokens":            max_tokens,
        "temperature":           0.3,
        "chat_template_kwargs":  CHAT_TEMPLATE_KWARGS,
    })
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(f"API error: {json.dumps(data, indent=2)}")
    return data


# ── Display Helpers ───────────────────────────────────────────

def section(title: str, color: str = "cyan"):
    console.print()
    console.rule(f"[bold {color}]{title}[/bold {color}]", style=color)
    console.print()


def turn_header(turn: int, label: str, color: str):
    icons = {"USER": "👤", "TOOL RESULT": "🔧", "THINKING": "🧠"}
    icon = next((v for k, v in icons.items() if k in label.upper()), "🤖")
    console.print(f"\n  {icon} [bold {color}]TURN {turn} — {label}[/bold {color}]")


def show_token_panel(label: str, token_ids: list, color: str):
    """Two stacked panels: token IDs then decoded string."""
    decoded = detokenize(token_ids)
    id_str  = " ".join(str(t) for t in token_ids[:120])
    if len(token_ids) > 120:
        id_str += f" ... (+{len(token_ids) - 120} more)"

    console.print(Panel(
        f"[dim]{id_str}[/dim]",
        title=f"[bold {color}]{label} — {len(token_ids)} token IDs[/bold {color}]",
        border_style=color, padding=(0, 1),
    ))
    console.print(Panel(
        f"[{color}]{repr(decoded)}[/{color}]",
        title=f"[bold {color}]{label} — Decoded (special tokens visible)[/bold {color}]",
        border_style=color, padding=(0, 1),
    ))


def show_reasoning_split(reasoning, content, turn: int, step: int):
    """Show reasoning block and content block separately with token IDs."""
    step_lbl = f"step {step}" if step > 0 else "initial"

    if reasoning:
        console.print(Panel(
            f"[italic dim]{reasoning}[/italic dim]",
            title=f"[bold yellow]🧠 Turn {turn} [{step_lbl}] REASONING[/bold yellow]",
            border_style="yellow", padding=(0, 1),
        ))
        r_ids = tokenize_text(reasoning)
        console.print(Panel(
            f"[dim]{' '.join(str(t) for t in r_ids[:80])}{'...' if len(r_ids) > 80 else ''}[/dim]",
            title=f"[dim yellow]Reasoning token IDs ({len(r_ids)} tokens)[/dim yellow]",
            border_style="dim yellow", padding=(0, 1),
        ))
    else:
        console.print(
            f"  [dim yellow]ℹ reasoning_content: None "
            f"(known vLLM parser issue — thinking IS in the prompt, "
            f"visible in INPUT+TOOLS decoded panel)[/dim yellow]"
        )

    if content:
        console.print(Panel(
            f"[bold bright_green]{content}[/bold bright_green]",
            title=f"[bold bright_green]Turn {turn} [{step_lbl}] CONTENT[/bold bright_green]",
            border_style="bright_green", padding=(0, 1),
        ))
        c_ids = tokenize_text(content)
        console.print(Panel(
            f"[dim]{' '.join(str(t) for t in c_ids[:80])}{'...' if len(c_ids) > 80 else ''}[/dim]",
            title=f"[dim green]Content token IDs ({len(c_ids)} tokens)[/dim green]",
            border_style="dim green", padding=(0, 1),
        ))


# ── Multi-Step Tool Chain Handler ─────────────────────────────

def chat_turn(user_msg: str, messages: list, turn: int) -> list:
    """
    Handle one user turn with full multi-step tool chain.

    Flow per step:
      1. Show raw INPUT token IDs + decoded (what model sees)
      2. Call model
      3. Show raw OUTPUT token IDs + decoded (what model generated)
      4. Show reasoning vs content split
      5. If tool_calls:
           a. Show tool call table
           b. Execute each tool
           c. Inject tool results into messages
           d. Loop back to step 1 (model may call more tools)
      6. If no tool_calls (finish_reason=stop):
           Show final answer, break loop
    """
    # Add user message
    messages.append({"role": "user", "content": user_msg})

    turn_header(turn, "USER", "bright_blue")
    console.print(Panel(
        f"[bright_blue]{user_msg}[/bright_blue]",
        title="User", border_style="bright_blue", padding=(0, 1),
    ))

    initial_ctx = None  # tokens at step 0, for growth tracking

    for step in range(MAX_TOOL_STEPS):
        lbl = "INITIAL" if step == 0 else f"STEP {step + 1}"

        # ── 1. Raw input prompt ───────────────────────────────
        section(f"Turn {turn} [{lbl}] RAW INPUT PROMPT", "yellow")
        input_ids, sampling = render_prompt(messages)
        if initial_ctx is None:
            initial_ctx = len(input_ids)

        show_token_panel(f"INPUT [{lbl}]", input_ids, "yellow")
        sp = sampling
        console.print(
            f"  [dim]temp={sp.get('temperature')}  top_p={sp.get('top_p')}  "
            f"top_k={sp.get('top_k')}  max_tokens={sp.get('max_tokens')}[/dim]"
        )
        if step > 0:
            console.print(
                f"  [dim]Context grew: {initial_ctx} → {len(input_ids)} tokens "
                f"(+{len(input_ids) - initial_ctx} from tool results)[/dim]"
            )

        # ── 2. Call model ─────────────────────────────────────
        response   = chat_api(messages)
        choice     = response["choices"][0]
        message    = choice["message"]
        finish     = choice["finish_reason"]
        usage      = response["usage"]
        tool_calls = message.get("tool_calls") or []
        reasoning  = message.get("reasoning")
        content    = message.get("content") or ""

        # ── 3. Raw output ─────────────────────────────────────
        section(f"Turn {turn} [{lbl}] RAW OUTPUT", "green")

        # Reconstruct what model generated as a single string for tokenisation.
        # Gemma 4 format: <|channel>thought\n...<channel|> then tool calls or content
        raw_out = ""
        if reasoning:
            raw_out += f"<|channel>thought\n{reasoning}<channel|>"
        raw_out += content
        for tc in tool_calls:
            fn   = tc["function"]["name"]
            args = tc["function"]["arguments"]
            raw_out += f"\n<|tool_call>call:{fn}{{{args}}}<tool_call|>"

        output_ids = tokenize_text(raw_out) if raw_out else []
        show_token_panel(f"OUTPUT [{lbl}]", output_ids, "green")
        console.print(
            f"  [dim]finish_reason={finish}  "
            f"prompt_tokens={usage['prompt_tokens']}  "
            f"completion_tokens={usage['completion_tokens']}[/dim]"
        )

        # ── 4. Reasoning vs content split ─────────────────────
        section(f"Turn {turn} [{lbl}] REASONING vs CONTENT SPLIT", "magenta")
        # Only show content panel if no tool calls (i.e. this is the final answer)
        show_reasoning_split(reasoning, content if not tool_calls else None, turn, step)

        # Append assistant message
        asst = {"role": "assistant"}
        if tool_calls:
            asst["tool_calls"] = tool_calls
        if content:
            asst["content"] = content
        if reasoning:
            asst["reasoning_content"] = reasoning
        messages.append(asst)

        # ── 5a. No tool calls → final answer ──────────────────
        if not tool_calls:
            if content:
                console.print(Panel(
                    f"[bold bright_green]{content}[/bold bright_green]",
                    title=(
                        f"[bright_green]🤖 Nexus — Final Answer "
                        f"(completed in {step} tool round{'s' if step != 1 else ''})[/bright_green]"
                    ),
                    border_style="bright_green", padding=(0, 1),
                ))
            break

        # ── 5b. Tool calls → show table ───────────────────────
        n = len(tool_calls)
        section(
            f"Turn {turn} [{lbl}] TOOL CALLS — {n} call{'s' if n > 1 else ''} dispatched",
            "cyan",
        )
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("#",         width=3)
        table.add_column("ID",        style="dim",   width=24)
        table.add_column("Function",  style="cyan",  width=18)
        table.add_column("Arguments", style="white")
        for i, tc in enumerate(tool_calls):
            table.add_row(
                str(i + 1),
                tc.get("id", "")[:22],
                tc["function"]["name"],
                tc["function"]["arguments"],
            )
        console.print(table)

        # ── 5c. Execute tools, inject results ─────────────────
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except Exception:
                fn_args = {}

            result_str = call_tool(fn_name, fn_args)

            turn_header(turn, f"TOOL RESULT — {fn_name}()", "cyan")
            console.print(Panel(
                Syntax(json.dumps(json.loads(result_str), indent=2), "json", theme="nord"),
                title=f"[cyan]{fn_name}({json.dumps(fn_args)}) → result[/cyan]",
                border_style="cyan", padding=(0, 1),
            ))

            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      result_str,
            })

        # Loop: model now sees tool results and responds again

    else:
        console.print(
            f"  [red]⚠ Reached MAX_TOOL_STEPS={MAX_TOOL_STEPS} — stopping chain[/red]"
        )

    return messages


# ── Intro ─────────────────────────────────────────────────────

console.print()
console.print(Panel.fit(
    "[bold white]Gemma 4 Tool Calls + Reasoning — Raw Token Inspector[/bold white]\n\n"
    f"[dim]Server :[/dim] [cyan]{BASE_URL}[/cyan]\n"
    f"[dim]Model  :[/dim] [cyan]{MODEL}[/cyan]\n"
    f"[dim]Thinking:[/dim] [yellow]enabled via chat_template_kwargs[/yellow]\n"
    f"[dim]Tools  :[/dim] [yellow]get_weather · search_web · calculate · get_stock_price[/yellow]\n\n"
    "[dim]Multi-step tool chains: model loops until finish_reason=stop[/dim]\n\n"
    "[dim]At each step you see:[/dim]\n"
    "  [yellow]●[/yellow] Full input prompt token IDs + decoded (everything model sees)\n"
    "  [green]●[/green] Raw output token IDs + decoded (thinking + tool calls / answer)\n"
    "  [magenta]●[/magenta] reasoning_content vs content split\n"
    "  [cyan]●[/cyan] Tool call table + simulated results\n"
    "  [dim]Repeats until model stops calling tools[/dim]",
    border_style="bright_cyan", padding=(1, 3),
))

# Tool summary table
section("REGISTERED TOOLS", "yellow")
t = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow")
t.add_column("Tool",          style="yellow", width=20)
t.add_column("Description",   style="dim",    width=55)
t.add_column("Required Args", style="cyan")
for tool in TOOLS:
    fn  = tool["function"]
    req = ", ".join(fn["parameters"].get("required", []))
    t.add_row(fn["name"], fn["description"][:55], req)
console.print(t)

# ── Conversation ──────────────────────────────────────────────
messages = [{"role": "system", "content": SYSTEM_PROMPT}]

# Turn 1: two parallel tool calls (weather + calculate)
messages = chat_turn(
    "What's the weather in Tokyo right now? And calculate sqrt(2) raised to the power 10.",
    messages, turn=1,
)

# Turn 2: sequential tool chain — get stock price THEN calculate shares
# (model must call get_stock_price first, then use result in calculate)
messages = chat_turn(
    "Check NVIDIA's stock price. Then calculate exactly how many whole shares "
    "I can buy with $10,000 and how much cash I'd have left over.",
    messages, turn=2,
)

# Turn 3: single tool call then summarise
messages = chat_turn(
    "Search the web for 'Gemma 4 vLLM tool calling' and give me a summary.",
    messages, turn=3,
)

# Turn 4: no tools needed — pure synthesis from conversation history
messages = chat_turn(
    "Based on everything we've discussed — Tokyo weather, NVIDIA stock, and Gemma 4 — "
    "give me a brief synthesis of what you know.",
    messages, turn=4,
)

# ── History Summary ───────────────────────────────────────────
section("FULL CONVERSATION HISTORY", "bright_white")
t2 = Table(box=box.SIMPLE, show_header=True, header_style="bold")
t2.add_column("#",       width=3)
t2.add_column("Role",    style="cyan", width=14)
t2.add_column("Content preview", style="dim")
for i, m in enumerate(messages):
    role    = m["role"]
    preview = str(m.get("content") or m.get("tool_calls") or "")[:90]
    color   = {
        "system": "dim", "user": "bright_blue",
        "assistant": "green", "tool": "cyan",
    }.get(role, "white")
    t2.add_row(str(i), f"[{color}]{role}[/{color}]", preview)
console.print(t2)
console.print(f"\n  [dim]Total messages in history: {len(messages)}[/dim]")
console.print()
console.rule("[bold green]✓ All turns complete[/bold green]", style="green")
console.print()
