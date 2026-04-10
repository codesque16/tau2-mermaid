"""
Gemma 4 Tool Calls + Reasoning — Raw Completions API + Client-Side Jinja2
==========================================================================
Identical multi-step tool chain to the original, but:

  1. Chat template is rendered CLIENT-SIDE using jinja2.Environment
     (the .jinja file is loaded from disk or embedded below)
  2. Raw /v1/completions is used instead of /v1/chat/completions
  3. The completion text is parsed client-side for:
       - <|channel>thought ... <channel|>   → reasoning
       - <|tool_call>call:name{...}<tool_call|>  → tool calls
       - everything else                     → content
  4. Injection prefix is appended to the rendered prompt string directly
     before the POST — no server-side chat_template_kwargs needed.
  5. Token panels use /tokenize and /detokenize just like the original.

Run:
    uv run --with rich --with jinja2 python tool_call_inspector_raw.py
"""

import re
import json
import math as _math
import requests
from pathlib import Path
from jinja2 import Environment, BaseLoader, Undefined
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich import box

console = Console()

BASE_URL = "http://34.29.173.120:8000"
MODEL    = "google/gemma-4-26B-A4B-it"

# ── Injection config (same modes as original) ─────────────────
# MODE A — no injection:
#   INJECTION_PREFIX = None
#   ENABLE_THINKING  = True   (adds <|think|> + suppresses empty thought block)
#
# MODE B — open thinking block:
#   INJECTION_PREFIX = "<|channel>thought\n"
#
# MODE C — full custom prefix:
#   INJECTION_PREFIX = "<|channel>thought\nI will plan step by step, but lets start with a contextual poem:\n"
#
# enable_thinking must be True for the system block to emit <|think|>.
# With no prefix (MODE A) the template emits <|channel>thought\n<channel|>
# after <|turn>model\n to force the model to skip thinking (matches original
# behavior when enable_thinking=True but no injection is set).

ENABLE_THINKING  = True
INJECTION_PREFIX = (
    "<|channel>thought\n"
    "I will plan step by step, but lets start with a contextual poem:\n"
)

if INJECTION_PREFIX:
    INJECTION_LABEL = f"prefix: {repr(INJECTION_PREFIX[:60])}"
else:
    INJECTION_LABEL = "none"

# ── Stop tokens for /v1/completions ──────────────────────────
# The model uses <turn|> to end its turn.
STOP_TOKENS = ["<turn|>"]

# ── Tools ─────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city. Returns temperature, conditions, humidity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city":  {"type": "string",  "description": "City name e.g. 'Tokyo'"},
                    "units": {"type": "string",  "enum": ["celsius", "fahrenheit"]},
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
        sym = "°F" if units == "fahrenheit" else "°C"
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


# ── Client-Side Jinja2 Template Engine ────────────────────────

# The Gemma4 template uses <|"|> as a quote character special token.
# Jinja2 treats | as a filter separator. We handle this by preprocessing
# the template to replace <|"|> with a placeholder, then post-processing.
# We also need to handle other Gemma special tokens that contain | like
# <|turn>, <|channel>, <tool|>, <turn|>, etc.
#
# Strategy: we replace all Gemma special tokens with safe ASCII placeholders
# before passing to Jinja2, then restore them in the rendered output.

# Map of Gemma special tokens → safe Jinja2-safe placeholders
_PLACEHOLDER_MAP = {
    "<|turn>":          "GEMMATOK_TURN_OPEN",
    "<turn|>":          "GEMMATOK_TURN_CLOSE",
    "<|channel>":       "GEMMATOK_CHAN_OPEN",
    "<channel|>":       "GEMMATOK_CHAN_CLOSE",
    "<|tool>":          "GEMMATOK_TOOL_OPEN",
    "<tool|>":          "GEMMATOK_TOOL_CLOSE",
    "<|tool_call>":     "GEMMATOK_TCALL_OPEN",
    "<tool_call|>":     "GEMMATOK_TCALL_CLOSE",
    "<|tool_response>": "GEMMATOK_TRESP_OPEN",
    "<tool_response|>": "GEMMATOK_TRESP_CLOSE",
    "<|think|>":        "GEMMATOK_THINK",
    "<|image|>":        "GEMMATOK_IMAGE",
    "<|audio|>":        "GEMMATOK_AUDIO",
    "<|video|>":        "GEMMATOK_VIDEO",
    # <|"|> — the Gemma quote special token contains a literal "
    # We build it programmatically to avoid Python string parsing issues
    # It will be added to the map after dict creation (see below)
}
# Add the Gemma quote token (<|"|>) — contains embedded " so built at runtime
_quote_tok = "<|" + '"' + "|>"
_PLACEHOLDER_MAP[_quote_tok] = "GEMMATOK_QUOTE"
_REVERSE_MAP = {v: k for k, v in _PLACEHOLDER_MAP.items()}


def _escape_template(tmpl_src: str) -> str:
    for tok, ph in _PLACEHOLDER_MAP.items():
        tmpl_src = tmpl_src.replace(tok, ph)
    return tmpl_src


def _unescape_output(text: str) -> str:
    for ph, tok in _REVERSE_MAP.items():
        text = text.replace(ph, tok)
    return text


def _load_jinja_env(template_path: str | None = None) -> Environment:
    """
    Load the Gemma4 Jinja template.
    If template_path is given, read from disk.
    Otherwise use the embedded template string (the one attached to this task).
    Falls back to the embedded copy if the file is not found.
    """
    raw = None
    if template_path:
        p = Path(template_path)
        if p.exists():
            raw = p.read_text()

    if raw is None:
        # Use the embedded template (copy from the attached document)
        raw = EMBEDDED_TEMPLATE

    escaped = _escape_template(raw)

    env = Environment(
        loader=BaseLoader(),
        keep_trailing_newline=True,
        undefined=Undefined,   # silently treat undefined vars as empty
    )

    # Register the BOS token as a global (Gemma uses it at template start)
    env.globals["bos_token"] = "<bos>"

    tmpl = env.from_string(escaped)
    return tmpl


def render_prompt_client(
    messages: list,
    tools: list | None = None,
    enable_thinking: bool = True,
    add_generation_prompt: bool = True,
    injection_prefix: str | None = None,
    template_path: str | None = None,
) -> str:
    """
    Render the Gemma4 chat template client-side.

    Returns the full prompt string including:
      - BOS token
      - System block with <|think|> if enable_thinking
      - Tool declarations
      - All message turns
      - Generation prompt (<|turn>model\\n)
      - Optional injection_prefix appended at the very end
    """
    tmpl = _load_jinja_env(template_path)
    rendered = tmpl.render(
        messages=messages,
        tools=tools or [],
        enable_thinking=enable_thinking,
        add_generation_prompt=add_generation_prompt,
        injection_prefix=injection_prefix or "",
        inject_thinking=False,
    )
    result = _unescape_output(rendered)
    return result


# ── Completion Text Parser ─────────────────────────────────────
# After /v1/completions returns raw text, we parse:
#   <|channel>thought\n...\n<channel|>   → reasoning
#   <|tool_call>call:name{...}<tool_call|> → tool_calls (list)
#   remaining text                         → content

_THINKING_RE  = re.compile(r"<\|channel>thought\n(.*?)<channel\|>", re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<\|tool_call>call:(\w+)\{(.*?)\}<tool_call\|>", re.DOTALL)

# Gemma uses <|"|> as quote char in its DSL, not JSON. We need a mini parser.
# The arguments block looks like: city:"Tokyo",units:"celsius"
# Key=value pairs, values may be strings (quoted with <|"|>), numbers, booleans,
# enums, arrays, or nested objects.
# We convert this DSL to JSON by replacing <|"|> with " then parsing.

def _gemma_args_to_dict(args_str: str) -> dict:
    """
    Convert Gemma tool-call argument DSL to a Python dict.
    The DSL uses <|"|> as quote chars instead of ".
    e.g.  city:<|"|>Tokyo<|"|>,units:<|"|>celsius<|"|>
    → {"city": "Tokyo", "units": "celsius"}
    """
    # Replace Gemma quote tokens with standard JSON quotes
    json_str = args_str.replace('<|"|>', '"')
    # Wrap in braces if not already present
    json_str = json_str.strip()
    if not json_str.startswith("{"):
        json_str = "{" + json_str + "}"
    # Keys in the DSL are unquoted identifiers; make them JSON strings
    # e.g.  city:"Tokyo"  →  "city":"Tokyo"
    json_str = re.sub(r'(?<=[{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'"\1":', json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # Fallback: return raw string as expression
        return {"_raw": args_str}


def parse_completion(
    raw_output: str,
    injection_prefix: str | None = None,
) -> tuple[str | None, list, str]:
    """
    Parse the model output into (reasoning, tool_calls, content).

    KEY INVARIANT — injection prefix must be prepended before parsing
    -----------------------------------------------------------------
    When an injection_prefix is active (e.g. '<|channel>thought\nmy poem:\n'),
    the model's raw_output is a *continuation* of that prefix.  The opening
    tag is NOT re-emitted by the model, so raw_output looks like:

        "In spring the cherry blossoms fall...\n<channel|><|tool_call>..."

    Missing its '<|channel>thought\n' opener — _THINKING_RE would not match.

    Fix: prepend injection_prefix to raw_output before regex parsing so we
    reconstruct the full logical model turn:

        "<|channel>thought\nmy poem:\nIn spring...\n<channel|><|tool_call>..."

    The reasoning extracted here is stored verbatim in the assistant message
    so the template can re-emit it as <|channel>thought...\n<channel|> in
    future prompt renders.  The raw_output panels still show only what the
    model generated (no prefix) — the combination is only for parsing.
    """
    # Reconstruct the full logical model turn by prepending the injection prefix
    full_text = (injection_prefix or "") + raw_output

    # 1. Extract reasoning from the combined text
    reasoning = None
    m = _THINKING_RE.search(full_text)
    if m:
        reasoning = m.group(1).strip()
        full_text = full_text[:m.start()] + full_text[m.end():]

    # 2. Extract tool calls
    tool_calls = []
    tc_id_counter = [0]
    def _tc_replacer(mc):
        name     = mc.group(1)
        args_str = mc.group(2)
        args     = _gemma_args_to_dict(args_str)
        tc_id_counter[0] += 1
        tool_calls.append({
            "id":       f"call_{tc_id_counter[0]:04d}",
            "type":     "function",
            "function": {"name": name, "arguments": args},
        })
        return ""
    full_text = _TOOL_CALL_RE.sub(_tc_replacer, full_text)

    # 3. Content = remainder after stripping thinking block + tool calls
    content = full_text.strip().removesuffix("<turn|>").strip()

    return reasoning, tool_calls, content


# ── API Helpers ───────────────────────────────────────────────

def tokenize_text(text: str) -> list:
    if not text:
        return []
    r = requests.post(f"{BASE_URL}/tokenize",
                      json={"model": MODEL, "prompt": text,
                            "add_special_tokens": False})
    data = r.json()
    return data.get("tokens") or data.get("token_ids") or []


def detokenize(token_ids: list) -> str:
    if not token_ids:
        return ""
    r = requests.post(f"{BASE_URL}/detokenize",
                      json={"model": MODEL, "tokens": token_ids})
    return r.json().get("prompt", "")


def completions_api(prompt: str, max_tokens: int = 1024) -> dict:
    """
    POST to /v1/completions with the fully-rendered prompt string.
    Returns the full API response dict.
    """
    r = requests.post(f"{BASE_URL}/v1/completions", json={
        "model":       MODEL,
        "prompt":      prompt,
        "max_tokens":  max_tokens,
        "temperature": 0.3,
        "stop":        STOP_TOKENS,
        "skip_special_tokens": False,
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
    icon  = next((v for k, v in icons.items() if k in label.upper()), "🤖")
    console.print(f"\n  {icon} [bold {color}]TURN {turn} — {label}[/bold {color}]")


def show_token_panel(label: str, token_ids: list, color: str, note: str = ""):
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
        title=f"[bold {color}]{label} — Decoded[/bold {color}]{title_suffix}",
        border_style=color, padding=(0, 1),
    ))


def show_prompt_panel(label: str, prompt_str: str, color: str, note: str = ""):
    """Show the prompt string directly (already decoded) alongside its token count."""
    ids       = tokenize_text(prompt_str)
    id_str    = " ".join(str(t) for t in ids[:120])
    if len(ids) > 120:
        id_str += f" ... (+{len(ids) - 120} more)"
    title_suffix = f"  [dim]{note}[/dim]" if note else ""
    console.print(Panel(
        f"[dim]{id_str}[/dim]",
        title=f"[bold {color}]{label} — {len(ids)} token IDs[/bold {color}]{title_suffix}",
        border_style=color, padding=(0, 1),
    ))
    console.print(Panel(
        f"[{color}]{repr(prompt_str)}[/{color}]",
        title=f"[bold {color}]{label} — Rendered prompt (client-side Jinja2)[/bold {color}]{title_suffix}",
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
            f"[dim]{' '.join(str(t) for t in r_ids[:80])}{'...' if len(r_ids) > 80 else ''}[/dim]",
            title=f"[dim yellow]Reasoning token IDs ({len(r_ids)} tokens)[/dim yellow]",
            border_style="dim yellow", padding=(0, 1),
        ))
    else:
        console.print(
            "  [dim yellow]ℹ No reasoning_content parsed — thinking may be absent "
            "or the injection prefix filled the block.[/dim yellow]"
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

MAX_TOOL_STEPS = 8


def _build_prompt(messages: list) -> str:
    """
    Render the current message list into a full prompt string.
    Injection prefix is appended at the very end (after <|turn>model\\n).
    """
    return render_prompt_client(
        messages=messages,
        tools=TOOLS,
        enable_thinking=ENABLE_THINKING,
        add_generation_prompt=True,
        injection_prefix=INJECTION_PREFIX,
    )


def chat_turn(user_msg: str, messages: list, turn: int) -> list:
    """
    Handle one user turn with full multi-step tool chain.
    All templating is done client-side. Raw /v1/completions is used.
    """
    messages.append({"role": "user", "content": user_msg})

    turn_header(turn, "USER", "bright_blue")
    console.print(Panel(
        f"[bright_blue]{user_msg}[/bright_blue]",
        title="User", border_style="bright_blue", padding=(0, 1),
    ))

    initial_prompt_tokens = None

    for step in range(MAX_TOOL_STEPS):
        lbl = "INITIAL" if step == 0 else f"STEP {step + 1}"

        # ── Build and display prompt ──────────────────────────
        section(f"Turn {turn} [{lbl}] RAW INPUT PROMPT (client-side rendered)", "yellow")
        prompt_str = _build_prompt(messages)
        inj_note   = f"injection: {INJECTION_LABEL}" if INJECTION_LABEL != "none" else ""
        show_prompt_panel(f"INPUT [{lbl}]", prompt_str, "yellow", note=inj_note)

        prompt_ids = tokenize_text(prompt_str)
        if initial_prompt_tokens is None:
            initial_prompt_tokens = len(prompt_ids)
            console.print(f"  [dim]Initial prompt: {initial_prompt_tokens} tokens[/dim]")
        else:
            console.print(
                f"  [dim]Context grew: {initial_prompt_tokens} → {len(prompt_ids)} tokens "
                f"(+{len(prompt_ids) - initial_prompt_tokens} from tool results)[/dim]"
            )

        if step == 0 and INJECTION_LABEL != "none":
            inj_ids = tokenize_text(INJECTION_PREFIX or "")
            console.print(Panel(
                f"  Injection prefix tokens : [yellow]{len(inj_ids)}[/yellow]\n"
                f"  Prefix string           : [yellow]{repr(INJECTION_PREFIX)}[/yellow]\n\n"
                f"  [dim]The prefix is appended directly to the rendered prompt string\n"
                f"  by the client — no server-side chat_template_kwargs needed.\n"
                f"  The model sees the prefix as the last tokens before it generates.[/dim]",
                title="[yellow]ℹ Client-side injection note[/yellow]",
                border_style="dim yellow", padding=(0, 1),
            ))

        # ── Call /v1/completions ──────────────────────────────
        response   = completions_api(prompt_str)
        choice     = response["choices"][0]
        raw_text   = choice["text"]
        finish     = choice["finish_reason"]
        usage      = response["usage"]

        # ── Parse completion text ─────────────────────────────
        # IMPORTANT: pass injection_prefix so the parser can reconstruct
        # the full model turn (prefix + output) before applying regexes.
        # The injection is only active on step 0 (subsequent steps after
        # tool results don't re-inject; the template suppresses it when
        # prev_message_type == tool_response).
        active_prefix = INJECTION_PREFIX if step == 0 else None
        reasoning, tool_calls, content = parse_completion(
            raw_output=raw_text,
            injection_prefix=active_prefix,
        )

        # ── Show raw OUTPUT ───────────────────────────────────
        # raw_text = what the model generated AFTER the prefix (prefix not included)
        # combined = prefix + raw_text = full logical model turn used for parsing
        combined_text = (active_prefix or "") + raw_text
        output_ids    = tokenize_text(raw_text) if raw_text else []
        combined_ids  = tokenize_text(combined_text) if combined_text else []
        inj_ids       = tokenize_text(active_prefix or "") if active_prefix else []

        section(f"Turn {turn} [{lbl}] RAW OUTPUT — Model Continuation (no prefix)", "green")
        output_note = (
            "continuation only — MISSING opening tag(s) from injection prefix"
            if active_prefix else ""
        )
        show_token_panel(f"OUTPUT [{lbl}] (raw, continuation only)", output_ids, "green", note=output_note)
        console.print(Panel(
            f"[green]{repr(raw_text)}[/green]",
            title=f"[bold green]OUTPUT [{lbl}] — Raw completion text (continuation)[/bold green]",
            border_style="green", padding=(0, 1),
        ))

        if active_prefix:
            section(f"Turn {turn} [{lbl}] COMBINED = prefix + output (used for parsing)", "yellow")
            console.print(Panel(
                f"[yellow]{repr(combined_text)}[/yellow]",
                title=f"[bold yellow]COMBINED [{lbl}] — Injection prefix prepended for parsing[/bold yellow]",
                border_style="yellow", padding=(0, 1),
            ))
            show_token_panel(f"COMBINED [{lbl}]", combined_ids, "yellow",
                             note="prefix + output — full logical model turn")

        console.print(
            f"  [dim]finish_reason={finish}  "
            f"prompt_tokens={usage['prompt_tokens']}  "
            f"completion_tokens={usage['completion_tokens']}  "
            f"prefix_tokens={len(inj_ids)}[/dim]"
        )

        if active_prefix:
            console.print(Panel(
                f"  Injected prefix tokens : [yellow]{len(inj_ids)}[/yellow]  "
                f"({repr(active_prefix)})\n"
                f"  Model output tokens    : [green]{len(output_ids)}[/green]  (raw continuation)\n"
                f"  Combined (for parsing) : [cyan]{len(combined_ids)}[/cyan]  tokens\n\n"
                f"  [dim]Parsing is done on COMBINED so that <|channel>thought…<channel|>\n"
                f"  and <|tool_call>…<tool_call|> blocks are always complete.\n"
                f"  The raw output alone is missing the prefix opening tags.[/dim]",
                title="[yellow]Token breakdown — prefix + continuation = parsed unit[/yellow]",
                border_style="dim yellow", padding=(0, 1),
            ))

        # ── Parsed breakdown ──────────────────────────────────
        section(f"Turn {turn} [{lbl}] PARSED: REASONING / TOOL CALLS / CONTENT", "magenta")
        show_reasoning_split(
            reasoning,
            content if not tool_calls else None,
            turn, step
        )

        # Build assistant message for history
        # For subsequent prompt rendering, we need to store reasoning so the
        # template can re-emit it as <|channel>thought...
        asst: dict = {"role": "assistant"}
        if tool_calls:
            asst["tool_calls"] = tool_calls
        if content:
            asst["content"] = content
        if reasoning:
            asst["reasoning"] = reasoning      # stored for template re-use
        messages.append(asst)

        # ── No tool calls → final answer ──────────────────────
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

        # ── Tool call dispatch ────────────────────────────────
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
                json.dumps(tc["function"]["arguments"]),
            )
        console.print(table)

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            result_str = call_tool(fn_name, fn_args)
            turn_header(turn, f"TOOL RESULT — {fn_name}()", "cyan")
            console.print(Panel(
                Syntax(json.dumps(json.loads(result_str), indent=2), "json", theme="nord"),
                title=f"[cyan]{fn_name}({json.dumps(fn_args)}) → result[/cyan]",
                border_style="cyan", padding=(0, 1),
            ))
            # Append tool result as role:tool (OpenAI-style)
            # The template's forward-scan will pick these up
            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      result_str,
            })

    else:
        console.print(
            f"  [red]⚠ Reached MAX_TOOL_STEPS={MAX_TOOL_STEPS} — stopping chain[/red]"
        )

    return messages


# ── Embedded Template ─────────────────────────────────────────
# This is the Gemma4 injected Jinja2 template (from the attached document).
# It uses special tokens that we escape/unescape around Jinja2 rendering.
# NOTE: All occurrences of Gemma special tokens are kept verbatim here;
# _escape_template() will replace them before Jinja2 sees them.

EMBEDDED_TEMPLATE = r"""{%- macro format_parameters(properties, required) -%}
    {%- set standard_keys = ['description', 'type', 'properties', 'required', 'nullable'] -%}
    {%- set ns = namespace(found_first=false) -%}
    {%- for key, value in properties | dictsort -%}
        {%- set add_comma = false -%}
        {%- if key not in standard_keys -%}
            {%- if ns.found_first %},{% endif -%}
            {%- set ns.found_first = true -%}
            {{ key }}:{
            {%- if value['description'] -%}
                description:<|"|>{{ value['description'] }}<|"|>
                {%- set add_comma = true -%}
            {%- endif -%}
            {%- if value['type'] | upper == 'STRING' -%}
                {%- if value['enum'] -%}
                    {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
                    enum:{{ format_argument(value['enum']) }}
                {%- endif -%}
            {%- elif value['type'] | upper == 'ARRAY' -%}
                {%- if value['items'] is mapping and value['items'] -%}
                    {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
                    items:{
                    {%- set ns_items = namespace(found_first=false) -%}
                    {%- for item_key, item_value in value['items'] | dictsort -%}
                        {%- if item_value is not none -%}
                            {%- if ns_items.found_first %},{% endif -%}
                            {%- set ns_items.found_first = true -%}
                            {%- if item_key == 'properties' -%}
                                properties:{
                                {%- if item_value is mapping -%}
                                    {{- format_parameters(item_value, value['items']['required'] | default([])) -}}
                                {%- endif -%}
                                }
                            {%- elif item_key == 'required' -%}
                                required:[
                                {%- for req_item in item_value -%}
                                    <|"|>{{- req_item -}}<|"|>
                                    {%- if not loop.last %},{% endif -%}
                                {%- endfor -%}
                                ]
                            {%- elif item_key == 'type' -%}
                                {%- if item_value is string -%}
                                    type:{{ format_argument(item_value | upper) }}
                                {%- else -%}
                                    type:{{ format_argument(item_value | map('upper') | list) }}
                                {%- endif -%}
                            {%- else -%}
                                {{ item_key }}:{{ format_argument(item_value) }}
                            {%- endif -%}
                        {%- endif -%}
                    {%- endfor -%}
                    }
                {%- endif -%}
            {%- endif -%}
            {%- if value['nullable'] %}
                {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
                nullable:true
            {%- endif -%}
            {%- if value['type'] | upper == 'OBJECT' -%}
                {%- if value['properties'] is defined and value['properties'] is mapping -%}
                    {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
                    properties:{
                    {{- format_parameters(value['properties'], value['required'] | default([])) -}}
                    }
                {%- elif value is mapping -%}
                    {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
                    properties:{
                    {{- format_parameters(value, value['required'] | default([])) -}}
                    }
                {%- endif -%}
                {%- if value['required'] -%}
                    {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
                    required:[
                    {%- for item in value['required'] | default([]) -%}
                        <|"|>{{- item -}}<|"|>
                        {%- if not loop.last %},{% endif -%}
                    {%- endfor -%}
                    ]
                {%- endif -%}
            {%- endif -%}
            {%- if add_comma %},{%- else -%} {%- set add_comma = true -%} {% endif -%}
            type:<|"|>{{ value['type'] | upper }}<|"|>}
        {%- endif -%}
    {%- endfor -%}
{%- endmacro -%}
{%- macro format_function_declaration(tool_data) -%}
    declaration:{{- tool_data['function']['name'] -}}{description:<|"|>{{- tool_data['function']['description'] -}}<|"|>
    {%- set params = tool_data['function']['parameters'] -%}
    {%- if params -%}
        ,parameters:{
        {%- if params['properties'] -%}
            properties:{ {{- format_parameters(params['properties'], params['required']) -}} },
        {%- endif -%}
        {%- if params['required'] -%}
            required:[
            {%- for item in params['required'] -%}
                <|"|>{{- item -}}<|"|>
                {{- ',' if not loop.last -}}
            {%- endfor -%}
            ],
        {%- endif -%}
        {%- if params['type'] -%}
            type:<|"|>{{- params['type'] | upper -}}<|"|>}
        {%- endif -%}
    {%- endif -%}
    {%- if 'response' in tool_data['function'] -%}
        {%- set response_declaration = tool_data['function']['response'] -%}
        ,response:{
        {%- if response_declaration['description'] -%}
            description:<|"|>{{- response_declaration['description'] -}}<|"|>,
        {%- endif -%}
        {%- if response_declaration['type'] | upper == 'OBJECT' -%}
            type:<|"|>{{- response_declaration['type'] | upper -}}<|"|>}
        {%- endif -%}
    {%- endif -%}
    }
{%- endmacro -%}
{%- macro format_argument(argument, escape_keys=True) -%}
    {%- if argument is string -%}
        {{- '<|"|>' + argument + '<|"|>' -}}
    {%- elif argument is boolean -%}
        {{- 'true' if argument else 'false' -}}
    {%- elif argument is mapping -%}
        {{- '{' -}}
        {%- set ns = namespace(found_first=false) -%}
        {%- for key, value in argument | dictsort -%}
            {%- if ns.found_first %},{% endif -%}
            {%- set ns.found_first = true -%}
            {%- if escape_keys -%}
                {{- '<|"|>' + key + '<|"|>' -}}
            {%- else -%}
                {{- key -}}
            {%- endif -%}
            :{{- format_argument(value, escape_keys=escape_keys) -}}
        {%- endfor -%}
        {{- '}' -}}
    {%- elif argument is sequence -%}
        {{- '[' -}}
        {%- for item in argument -%}
            {{- format_argument(item, escape_keys=escape_keys) -}}
            {%- if not loop.last %},{% endif -%}
        {%- endfor -%}
        {{- ']' -}}
    {%- else -%}
        {{- argument -}}
    {%- endif -%}
{%- endmacro -%}
{%- macro strip_thinking(text) -%}
    {%- set ns = namespace(result='') -%}
    {%- for part in text.split('<channel|>') -%}
        {%- if '<|channel>' in part -%}
            {%- set ns.result = ns.result + part.split('<|channel>')[0] -%}
        {%- else -%}
            {%- set ns.result = ns.result + part -%}
        {%- endif -%}
    {%- endfor -%}
    {{- ns.result | trim -}}
{%- endmacro -%}

{%- macro format_tool_response_block(tool_name, response) -%}
    {{- '<|tool_response>' -}}
    {%- if response is mapping -%}
        {{- 'response:' + tool_name + '{' -}}
        {%- for key, value in response | dictsort -%}
            {{- key -}}:{{- format_argument(value, escape_keys=False) -}}
            {%- if not loop.last %},{% endif -%}
        {%- endfor -%}
        {{- '}' -}}
    {%- else -%}
        {{- 'response:' + tool_name + '{value:' + format_argument(response, escape_keys=False) + '}' -}}
    {%- endif -%}
    {{- '<tool_response|>' -}}
{%- endmacro -%}

{%- set ns = namespace(prev_message_type=None) -%}
{%- set loop_messages = messages -%}
{{- bos_token -}}
{#- Handle System/Tool Definitions Block -#}
{%- if (enable_thinking is defined and enable_thinking) or tools or messages[0]['role'] in ['system', 'developer'] -%}
    {{- '<|turn>system\n' -}}

    {#- Inject Thinking token at the very top of the FIRST system turn -#}
    {%- if enable_thinking is defined and enable_thinking -%}
        {{- '<|think|>\n' -}}
        {%- set ns.prev_message_type = 'think' -%}
    {%- endif -%}

    {%- if messages[0]['role'] in ['system', 'developer'] -%}
        {{- messages[0]['content'] | trim -}}
        {%- set loop_messages = messages[1:] -%}
    {%- endif -%}

    {%- if tools -%}
        {%- for tool in tools %}
            {{- '<|tool>' -}}
            {{- format_function_declaration(tool) | trim -}}
            {{- '<tool|>' -}}
        {%- endfor %}
        {%- set ns.prev_message_type = 'tool' -%}
    {%- endif -%}

    {{- '<turn|>\n' -}}
{%- endif %}

{#- Pre-scan: find last user message index for reasoning guard -#}
{%- set ns_turn = namespace(last_user_idx=-1) -%}
{%- for i in range(loop_messages | length) -%}
    {%- if loop_messages[i]['role'] == 'user' -%}
        {%- set ns_turn.last_user_idx = i -%}
    {%- endif -%}
{%- endfor -%}

{#- Loop through messages -#}
{%- for message in loop_messages -%}
    {%- if message['role'] != 'tool' -%}
    {%- set ns.prev_message_type = None -%}
    {%- set role = 'model' if message['role'] == 'assistant' else message['role'] -%}
    {#- Detect continuation: suppress duplicate <|turn>model when previous non-tool message was also assistant -#}
    {%- set prev_nt = namespace(role=None, found=false) -%}
    {%- if loop.index0 > 0 -%}
        {%- for j in range(loop.index0 - 1, -1, -1) -%}
            {%- if not prev_nt.found -%}
                {%- if loop_messages[j]['role'] != 'tool' -%}
                    {%- set prev_nt.role = loop_messages[j]['role'] -%}
                    {%- set prev_nt.found = true -%}
                {%- endif -%}
            {%- endif -%}
        {%- endfor -%}
    {%- endif -%}
    {%- set continue_same_model_turn = (role == 'model' and prev_nt.role == 'assistant') -%}
    {%- if not continue_same_model_turn -%}
        {{- '<|turn>' + role + '\n' }}
    {%- endif -%}

    {#- Render reasoning/reasoning_content as thinking channel -#}
    {%- set thinking_text = message.get('reasoning') or message.get('reasoning_content') -%}
    {%- if thinking_text and loop.index0 > ns_turn.last_user_idx and message.get('tool_calls') -%}
        {{- '<|channel>thought\n' + thinking_text + '\n<channel|>' -}}
    {%- endif -%}

            {%- if message['tool_calls'] -%}
                {%- for tool_call in message['tool_calls'] -%}
                    {%- set function = tool_call['function'] -%}
                    {{- '<|tool_call>call:' + function['name'] + '{' -}}
                    {%- if function['arguments'] is mapping -%}
                        {%- set ns_args = namespace(found_first=false) -%}
                        {%- for key, value in function['arguments'] | dictsort -%}
                            {%- if ns_args.found_first %},{% endif -%}
                            {%- set ns_args.found_first = true -%}
                            {{- key -}}:{{- format_argument(value, escape_keys=False) -}}
                        {%- endfor -%}
                    {%- elif function['arguments'] is string -%}
                        {{- function['arguments'] -}}
                    {%- endif -%}
                    {{- '}<tool_call|>' -}}
                {%- endfor -%}
                {%- set ns.prev_message_type = 'tool_call' -%}
            {%- endif -%}

            {%- set ns_tr_out = namespace(flag=false) -%}
            {%- if message.get('tool_responses') -%}
                {%- for tool_response in message['tool_responses'] -%}
                    {{- format_tool_response_block(tool_response['name'] | default('unknown'), tool_response['response']) -}}
                    {%- set ns_tr_out.flag = true -%}
                    {%- set ns.prev_message_type = 'tool_response' -%}
                {%- endfor -%}
            {%- elif message.get('tool_calls') -%}
                {%- set ns_tool_scan = namespace(stopped=false) -%}
                {%- for k in range(loop.index0 + 1, loop_messages | length) -%}
                    {%- if ns_tool_scan.stopped -%}
                    {%- elif loop_messages[k]['role'] != 'tool' -%}
                        {%- set ns_tool_scan.stopped = true -%}
                    {%- else -%}
                        {%- set follow = loop_messages[k] -%}
                        {%- set ns_tname = namespace(name=follow.get('name') | default('unknown')) -%}
                        {%- for tc in message['tool_calls'] -%}
                            {%- if tc.get('id') == follow.get('tool_call_id') -%}
                                {%- set ns_tname.name = tc['function']['name'] -%}
                            {%- endif -%}
                        {%- endfor -%}
                        {%- set tool_body = follow.get('content') -%}
                        {%- if tool_body is string -%}
                            {{- format_tool_response_block(ns_tname.name, tool_body) -}}
                        {%- elif tool_body is sequence and tool_body is not string -%}
                            {%- set ns_txt = namespace(s='') -%}
                            {%- for part in tool_body -%}
                                {%- if part.get('type') == 'text' -%}
                                    {%- set ns_txt.s = ns_txt.s + (part.get('text') | default('')) -%}
                                {%- endif -%}
                            {%- endfor -%}
                            {{- format_tool_response_block(ns_tname.name, ns_txt.s) -}}
                        {%- else -%}
                            {{- format_tool_response_block(ns_tname.name, tool_body) -}}
                        {%- endif -%}
                        {%- set ns_tr_out.flag = true -%}
                        {%- set ns.prev_message_type = 'tool_response' -%}
                    {%- endif -%}
                {%- endfor -%}
            {%- endif -%}

            {%- if message['content'] is string -%}
                {%- if role == 'model' -%}
                    {{- strip_thinking(message['content']) -}}
                {%- else -%}
                    {{- message['content'] | trim -}}
                {%- endif -%}
            {%- elif message['content'] is sequence -%}
                {%- for item in message['content'] -%}
                    {%- if item['type'] == 'text' -%}
                        {%- if role == 'model' -%}
                            {{- strip_thinking(item['text']) -}}
                        {%- else -%}
                            {{- item['text'] | trim -}}
                        {%- endif -%}
                    {%- elif item['type'] == 'image' -%}
                        {{- '<|image|>' -}}
                        {%- set ns.prev_message_type = 'image' -%}
                    {%- elif item['type'] == 'audio' -%}
                        {{- '<|audio|>' -}}
                        {%- set ns.prev_message_type = 'audio' -%}
                    {%- elif item['type'] == 'video' -%}
                        {{- '<|video|>' -}}
                        {%- set ns.prev_message_type = 'video' -%}
                    {%- endif -%}
                {%- endfor -%}
            {%- endif -%}

        {%- if ns.prev_message_type == 'tool_call' and not ns_tr_out.flag -%}
            {{- '<|tool_response>' -}}
        {%- elif not (ns_tr_out.flag and not message.get('content')) -%}
            {{- '<turn|>\n' -}}
        {%- endif -%}
    {%- endif -%}
{%- endfor -%}

{%- if add_generation_prompt -%}
    {%- if ns.prev_message_type != 'tool_response' and ns.prev_message_type != 'tool_call' -%}
        {{- '<|turn>model\n' -}}
        {%- if not enable_thinking | default(false) -%}
            {{- '<|channel>thought\n<channel|>' -}}
        {%- endif -%}
        {%- if injection_prefix is defined and injection_prefix -%}
            {{- injection_prefix -}}
        {%- elif inject_thinking is defined and inject_thinking -%}
            {{- '<|channel>thought\n' -}}
        {%- endif -%}
    {%- endif -%}
{%- endif -%}
"""

# ── Intro Banner ──────────────────────────────────────────────

console.print()
console.print(Panel.fit(
    "[bold white]Gemma 4 Tool Calls + Reasoning — Raw Completions + Client-Side Jinja2[/bold white]\n\n"
    f"[dim]Server      :[/dim] [cyan]{BASE_URL}[/cyan]\n"
    f"[dim]Model       :[/dim] [cyan]{MODEL}[/cyan]\n"
    f"[dim]Endpoint    :[/dim] [cyan]/v1/completions[/cyan]  (raw, no chat wrapper)\n"
    f"[dim]Templating  :[/dim] [cyan]client-side Jinja2[/cyan]  (no server chat_template_kwargs)\n"
    f"[dim]Injection   :[/dim] [yellow]{INJECTION_LABEL}[/yellow]\n\n"
    "[dim]How to read the panels:[/dim]\n"
    "  [yellow]INPUT[/yellow]   — full prompt rendered by client-side Jinja2\n"
    "             injection prefix appended as plain string\n"
    "  [green]OUTPUT[/green]  — raw text from /v1/completions choice.text\n"
    "  [magenta]PARSED[/magenta]  — reasoning / tool_calls / content split by regex\n"
    "  [cyan]TOOLS[/cyan]   — dispatched calls + fake results\n\n"
    "[dim]Injection approach: render_prompt_client() appends INJECTION_PREFIX\n"
    "to the rendered string before POST. No server-side kwargs needed.[/dim]",
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

# Smoke-test the client-side renderer before running turns
section("TEMPLATE SMOKE TEST", "dim")
_test_msgs = [
    {"role": "system",    "content": "Test system."},
    {"role": "user",      "content": "Hello"},
]
_test_prompt = render_prompt_client(
    messages=_test_msgs, tools=TOOLS[:1],
    enable_thinking=ENABLE_THINKING,
    add_generation_prompt=True,
    injection_prefix=INJECTION_PREFIX,
)
console.print(Panel(
    f"[dim]{repr(_test_prompt[:600])}{'...' if len(_test_prompt) > 600 else ''}[/dim]",
    title="[dim]Smoke-test render (truncated to 600 chars)[/dim]",
    border_style="dim", padding=(0, 1),
))
console.print(f"  [dim]✓ Template rendered OK — {len(_test_prompt)} chars[/dim]")

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
