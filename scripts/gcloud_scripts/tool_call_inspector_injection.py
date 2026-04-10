"""
Gemma 4 Tool Calls + Reasoning — Raw Token Inspector
=====================================================
Multi-step tool chain. Injection-aware rendering.

HOW INJECTION WORKS IN THIS SCRIPT
------------------------------------
When inject_thinking=True (or injection_prefix is set), vLLM appends the
injected tokens to the rendered INPUT prompt before the model generates.

  INPUT  (what render_prompt returns):
    ....<|turn>model\n<|channel>thought\n    ← injected prefix IS here
                                               because render uses same
                                               chat_template_kwargs

  OUTPUT (what the model returned from chat API):
    ...model continues after the prefix...   ← no prefix here, it was input

So the INPUT decoded panel naturally shows the injected tokens at the end.
The OUTPUT panel shows only what was generated after them. No manual
prepending needed — the render endpoint is the source of truth.

SERVE WITH INJECTION TEMPLATE:
  vllm serve google/gemma-4-E4B-it \\
      --gpu-memory-utilization 0.90 --max-model-len 8192 --dtype auto \\
      --enable-auto-tool-choice --tool-call-parser gemma4 \\
      --reasoning-parser gemma4 \\
      --chat-template ~/vllm/gemma4_injected.jinja

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

# ── Injection config ──────────────────────────────────────────
# Flip between three modes:
#
#   MODE A — no injection (baseline)
#     CHAT_TEMPLATE_KWARGS = {"enable_thinking": True}
#
#   MODE B — open thinking block, model fills it in
#     CHAT_TEMPLATE_KWARGS = {"enable_thinking": True, "inject_thinking": True}
#
#   MODE C — full custom prefix, model continues from it
#     CHAT_TEMPLATE_KWARGS = {
#         "enable_thinking": True,
#         "injection_prefix": "<|channel>thought\nLet's plan step by step:\n1) "
#     }
#
# The same kwargs go to BOTH /render and /chat/completions, so the INPUT
# decoded panel will show the injected tokens exactly as vLLM sees them.

CHAT_TEMPLATE_KWARGS = {
    "enable_thinking": True,
    # "inject_thinking": True,          # ← flip this to test injection
    "injection_prefix": "<|channel>thought\nLet's plan step by step:\n1) "
}

# Derive a display label for the panels
if "injection_prefix" in CHAT_TEMPLATE_KWARGS:
    INJECTION_LABEL = f"prefix: {repr(CHAT_TEMPLATE_KWARGS['injection_prefix'][:40])}"
elif CHAT_TEMPLATE_KWARGS.get("inject_thinking"):
    INJECTION_LABEL = "inject_thinking=True  (opens <|channel>thought\\n)"
else:
    INJECTION_LABEL = "none"


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
    """
    Call /v1/chat/completions/render with the SAME chat_template_kwargs
    used for generation. This means the rendered prompt INCLUDES the
    injected prefix — so the INPUT decoded panel shows exactly what
    vLLM fed to the model, injected tokens and all.
    """
    for payload in [
        {"model": MODEL, "messages": messages, "tools": TOOLS,
         "chat_template_kwargs": CHAT_TEMPLATE_KWARGS,
         "skip_special_tokens":  False},
        {"model": MODEL, "messages": messages,
         "chat_template_kwargs": CHAT_TEMPLATE_KWARGS,
         "skip_special_tokens":  False},
        {"model": MODEL, "messages": messages, "skip_special_tokens":  False},
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
    """
    Call /v1/chat/completions with the SAME chat_template_kwargs.
    The injection is applied server-side before generation.
    The OUTPUT only contains tokens generated AFTER the prefix.
    """
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json={
        "model":                MODEL,
        "messages":             messages,
        "tools":                TOOLS,
        "tool_choice":          "auto",
        "max_tokens":           max_tokens,
        "temperature":          0.3,
        "chat_template_kwargs": CHAT_TEMPLATE_KWARGS,
        "skip_special_tokens":  False,
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


def show_token_panel(label: str, token_ids: list, color: str,
                     note: str = ""):
    decoded = detokenize(token_ids)
    id_str  = " ".join(str(t) for t in token_ids[:120])
    if len(token_ids) > 120:
        id_str += f" ... (+{len(token_ids) - 120} more)"

    title_suffix = f"  [dim]{note}[/dim]" if note else ""

    console.print(Panel(
        f"[dim]{id_str}[/dim]",
        title=f"[bold {color}]{label} — {len(token_ids)} token IDs[/bold {color}]{title_suffix}",
        border_style=color, padding=(0, 1),
    ))
    console.print(Panel(
        f"[{color}]{repr(decoded)}[/{color}]",
        title=f"[bold {color}]{label} — Decoded (special tokens visible)[/bold {color}]{title_suffix}",
        border_style=color, padding=(0, 1),
    ))


def show_reasoning_split(reasoning, content, turn: int, step: int):
    step_lbl = f"step {step}" if step > 0 else "initial"
    if reasoning:
        console.print(Panel(
            f"[italic dim]{reasoning}[/italic dim]",
            title=f"[bold yellow]🧠 Turn {turn} [{step_lbl}] REASONING[/bold yellow]",
            border_style="yellow", padding=(0, 1),
        ))
        r_ids = tokenize_text(reasoning)
        console.print(Panel(
            f"[dim]{' '.join(str(t) for t in r_ids[:80])}{'...' if len(r_ids)>80 else ''}[/dim]",
            title=f"[dim yellow]Reasoning token IDs ({len(r_ids)} tokens)[/dim yellow]",
            border_style="dim yellow", padding=(0, 1),
        ))
    else:
        console.print(
            "  [dim yellow]ℹ reasoning_content: None — "
            "known vLLM parser bug. Thinking IS in the prompt: "
            "check INPUT decoded panel, look for <|channel>thought\\n at the end "
            "(that's the injected prefix) and in INPUT+TOOLS for previous steps.[/dim yellow]"
        )
    if content:
        console.print(Panel(
            f"[bold bright_green]{content}[/bold bright_green]",
            title=f"[bold bright_green]Turn {turn} [{step_lbl}] CONTENT[/bold bright_green]",
            border_style="bright_green", padding=(0, 1),
        ))
        c_ids = tokenize_text(content)
        console.print(Panel(
            f"[dim]{' '.join(str(t) for t in c_ids[:80])}{'...' if len(c_ids)>80 else ''}[/dim]",
            title=f"[dim green]Content token IDs ({len(c_ids)} tokens)[/dim green]",
            border_style="dim green", padding=(0, 1),
        ))


# ── Multi-Step Tool Chain Handler ─────────────────────────────

MAX_TOOL_STEPS = 8

def chat_turn(user_msg: str, messages: list, turn: int) -> list:
    """
    Handle one user turn with full multi-step tool chain.

    Injection awareness:
      - render_prompt() uses CHAT_TEMPLATE_KWARGS, so the INPUT decoded
        panel naturally ends with the injected prefix tokens. No manual
        reconstruction needed — vLLM's /render shows the ground truth.
      - The OUTPUT panel shows only what the model generated AFTER the
        prefix. This is correct — the prefix was input, not output.
      - To see the injected tokens: look at the INPUT decoded panel's
        tail end. You'll see e.g.:
          ...<|turn>model\n<|channel>thought\nLet's plan step by step:\n1)
        That's exactly what the model saw before generating.
    """
    messages.append({"role": "user", "content": user_msg})

    turn_header(turn, "USER", "bright_blue")
    console.print(Panel(
        f"[bright_blue]{user_msg}[/bright_blue]",
        title="User", border_style="bright_blue", padding=(0, 1),
    ))

    initial_ctx = None

    for step in range(MAX_TOOL_STEPS):
        lbl = "INITIAL" if step == 0 else f"STEP {step + 1}"

        # ── INPUT prompt ──────────────────────────────────────
        # render_prompt uses same CHAT_TEMPLATE_KWARGS → injected prefix
        # appears at the end of the rendered prompt automatically.
        section(f"Turn {turn} [{lbl}] RAW INPUT PROMPT", "yellow")
        input_ids, sampling = render_prompt(messages)
        if initial_ctx is None:
            initial_ctx = len(input_ids)

        # Note in the panel title when injection is active
        inj_note = f"injection: {INJECTION_LABEL}" if INJECTION_LABEL != "none" else ""
        show_token_panel(f"INPUT [{lbl}]", input_ids, "yellow", note=inj_note)

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

        if step == 0 and INJECTION_LABEL != "none":
            console.print(Panel(
                f"[dim]The injected prefix is visible at the tail of the INPUT decoded panel above.\n"
                f"Look for the end of the string: it will show\n"
                f"  [yellow]...<|turn>model\\n<|channel>thought\\n...[/yellow]\n"
                f"That is what vLLM actually fed to the model as the generation starting point.\n"
                f"The OUTPUT panel below contains only tokens generated AFTER that point.[/dim]",
                title="[yellow]ℹ Injection note[/yellow]",
                border_style="dim yellow", padding=(0, 1),
            ))

        # ── Model call ────────────────────────────────────────
        response   = chat_api(messages)
        choice     = response["choices"][0]
        message    = choice["message"]
        finish     = choice["finish_reason"]
        usage      = response["usage"]
        tool_calls = message.get("tool_calls") or []
        reasoning  = message.get("reasoning")
        content    = message.get("content") or ""

        # ── OUTPUT: only what model generated after the prefix ─
        section(f"Turn {turn} [{lbl}] RAW OUTPUT (generated AFTER injected prefix)", "green")

        raw_out = ""
        if reasoning:
            raw_out += f"<|channel>thought\n{reasoning}<channel|>"
        raw_out += content
        for tc in tool_calls:
            fn   = tc["function"]["name"]
            args = tc["function"]["arguments"]
            raw_out += f"\n<|tool_call>call:{fn}{{{args}}}<tool_call|>"

        output_ids = tokenize_text(raw_out) if raw_out else []

        output_note = (
            "model output only — injected prefix is in INPUT above"
            if INJECTION_LABEL != "none" else ""
        )
        show_token_panel(f"OUTPUT [{lbl}]", output_ids, "green", note=output_note)
        console.print(
            f"  [dim]finish_reason={finish}  "
            f"prompt_tokens={usage['prompt_tokens']}  "
            f"completion_tokens={usage['completion_tokens']}[/dim]"
        )

        if step == 0 and INJECTION_LABEL != "none":
            # Show token count breakdown to make injection visible
            inj_prefix = CHAT_TEMPLATE_KWARGS.get(
                "injection_prefix",
                "<|channel>thought\n" if CHAT_TEMPLATE_KWARGS.get("inject_thinking") else ""
            )
            inj_ids = tokenize_text(inj_prefix) if inj_prefix else []
            console.print(Panel(
                f"  Injected prefix tokens : [yellow]{len(inj_ids)}[/yellow]  "
                f"({repr(inj_prefix)})\n"
                f"  Model output tokens    : [green]{len(output_ids)}[/green]\n"
                f"  Total generation cost  : [cyan]{len(inj_ids) + len(output_ids)}[/cyan]  "
                f"(prefix counted as INPUT, only output billed as completion_tokens)\n\n"
                f"  [dim]Verify: prompt_tokens above includes the {len(inj_ids)} prefix tokens.[/dim]",
                title="[yellow]Token breakdown — injection vs generation[/yellow]",
                border_style="dim yellow", padding=(0, 1),
            ))

        # ── Reasoning / Content split ─────────────────────────
        section(f"Turn {turn} [{lbl}] REASONING vs CONTENT SPLIT", "magenta")
        show_reasoning_split(reasoning, content if not tool_calls else None, turn, step)

        # Append assistant turn
        asst = {"role": "assistant"}
        if tool_calls:
            asst["tool_calls"] = tool_calls
        if content:
            asst["content"] = content
        if reasoning:
            asst["reasoning"] = reasoning
        messages.append(asst)

        # ── No tool calls → done ──────────────────────────────
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

        # ── Tool calls → show + execute ───────────────────────
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

        # Note: from step 2 onwards, the injection prefix is NOT re-injected.
        # The render prompt for subsequent steps will NOT have the prefix at
        # the tail — instead the prompt ends with the tool_response tokens,
        # because prev_message_type == 'tool_response' suppresses <|turn>model\n
        # in the Jinja template. The thinking is preserved in the prompt body
        # from the previous step's output, not re-injected.

    else:
        console.print(
            f"  [red]⚠ Reached MAX_TOOL_STEPS={MAX_TOOL_STEPS} — stopping chain[/red]"
        )

    return messages


# ── Intro ─────────────────────────────────────────────────────

console.print()
console.print(Panel.fit(
    "[bold white]Gemma 4 Tool Calls + Reasoning — Raw Token Inspector[/bold white]\n\n"
    f"[dim]Server    :[/dim] [cyan]{BASE_URL}[/cyan]\n"
    f"[dim]Model     :[/dim] [cyan]{MODEL}[/cyan]\n"
    f"[dim]Injection :[/dim] [yellow]{INJECTION_LABEL}[/yellow]\n\n"
    "[dim]How to read the panels:[/dim]\n"
    "  [yellow]INPUT[/yellow]   — full rendered prompt including injected prefix at the tail\n"
    "  [green]OUTPUT[/green]  — only tokens the model generated AFTER the prefix\n"
    "  [magenta]SPLIT[/magenta]   — reasoning_content vs final content\n"
    "  [cyan]TOOLS[/cyan]   — tool call table + results\n\n"
    "[dim]To see injection: look at the decoded INPUT panel tail end.\n"
    "You should see: ...<|turn>model\\n<|channel>thought\\n[/dim]",
    border_style="bright_cyan", padding=(1, 3),
))

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

messages = chat_turn(
    "What's the weather in Tokyo right now? And calculate sqrt(2) raised to the power 10.",
    messages, turn=1,
)

messages = chat_turn(
    "Check NVIDIA's stock price. Then calculate exactly how many whole shares "
    "I can buy with $10,000 and how much cash I'd have left over.",
    messages, turn=2,
)

messages = chat_turn(
    "Search the web for 'Gemma 4 vLLM tool calling' and give me a summary.",
    messages, turn=3,
)

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
