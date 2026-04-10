"""
Gemma 4 — Prompt Experiments
==============================

EXPERIMENT 1: Recreate a multi-step tool-call conversation via /v1/completions,
              building all special tokens manually — with CORRECT thinking persistence.

EXPERIMENT 2: Prompt injection — force a thinking prefix at the start of each
              model turn via /v1/completions.

KEY RULE (from Google's official docs):
  - Within a single TURN (user→tools→final answer), thinking blocks MUST be
    preserved between tool-call steps. The model uses them as a "save state"
    to continue reasoning after each tool result.
  - Across TURNS (in multi-turn history), thinking is STRIPPED — only the
    final response text is kept.

    Turn N (KEEP thinking within the turn):
      <|turn>model\n
        <|channel>thought\n...step 1 thinking...<channel|>   ← KEEP
        <|tool_call>call:get_stock{...}<tool_call|>
        <|tool_response>response:get_stock{...}<tool_response|>
        <|channel>thought\n...step 2 thinking...<channel|>   ← KEEP
        <|tool_call>call:calculate{...}<tool_call|>
        <|tool_response>response:calculate{...}<tool_response|>
        Final answer text
      <turn|>

    Turn N+1 history (STRIP thinking from Turn N):
      <|turn>model\n
        Final answer text only
      <turn|>

vLLM serve:
  vllm serve google/gemma-4-E4B-it \
      --gpu-memory-utilization 0.90 --max-model-len 8192 --dtype auto \
      --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4

Run:
  uv run --with rich python prompt_experiments.py
"""

import re
import requests
import json
import math as _math
from dataclasses import dataclass, field
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich import box

console = Console()

BASE_URL = "http://34.72.38.143:8000"
MODEL    = "google/gemma-4-E4B-it"

# ── Special tokens ────────────────────────────────────────────
T_BOS           = "<bos>"
T_TURN_OPEN     = "<|turn>"
T_TURN_CLOSE    = "<turn|>\n"
T_THINK         = "<|think|>"
T_CHANNEL_OPEN  = "<|channel>thought\n"
T_CHANNEL_CLOSE = "<channel|>"
T_TOOL_OPEN     = "<|tool>"
T_TOOL_CLOSE    = "<tool|>"
T_CALL_OPEN     = "<|tool_call>call:"
T_CALL_CLOSE    = "<tool_call|>"
T_RESP_OPEN     = "<|tool_response>response:"
T_RESP_CLOSE    = "<tool_response|>"
T_STR           = '<|"|>'


# ── Data structures ───────────────────────────────────────────

@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ToolResponse:
    name: str
    result: str   # raw JSON string


@dataclass
class ToolStep:
    """
    One step within a single model turn:
      thinking -> tool_calls -> tool_responses
    Multiple steps can exist in one turn (sequential tool calling).
    """
    thinking: Optional[str] = None          # <|channel>...<channel|> content
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_responses: list[ToolResponse] = field(default_factory=list)


@dataclass
class ModelTurn:
    """
    A complete model turn, potentially spanning multiple tool-call steps.
    final_content is the last text response after all tool steps are done.
    """
    steps: list[ToolStep] = field(default_factory=list)
    final_thinking: Optional[str] = None   # thinking before final answer (if any)
    final_content: str = ""


# ── Tool Definitions ──────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "Get current stock price for a ticker symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "e.g. NVDA"},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression. Supports sqrt,floor,ceil,log,abs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression"},
                },
                "required": ["expression"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are Nexus, a concise AI assistant with access to tools.\n"
    "Think carefully before each action. Use tools when needed."
)

INJECTION_PREFIX = (
    f"{T_CHANNEL_OPEN}"
    "Let's plan step by step:\n"
    "1) Understand what the user needs\n"
    "2) Identify which tools are required\n"
    "3) Call tools in the right order\n"
    "4) Synthesize results\n"
)


# ── Fake Tools ────────────────────────────────────────────────
def call_tool(name: str, args: dict) -> str:
    if name == "get_weather":
        city = args.get("city", "Unknown")
        return json.dumps({"city": city, "temperature": "22\u00b0C",
                           "conditions": "Partly cloudy", "humidity": "65%"})
    elif name == "get_stock_price":
        ticker = args.get("ticker", "?").upper()
        prices = {"NVDA": (875.30, +12.50), "AAPL": (189.50, +1.23)}
        price, change = prices.get(ticker, (100.0, 0.0))
        return json.dumps({"ticker": ticker, "price": f"${price:.2f}",
                           "change": f"{change:+.2f}", "currency": "USD"})
    elif name == "calculate":
        expr = args.get("expression", "0")
        try:
            result = eval(expr, {"__builtins__": {}}, {
                "sqrt": _math.sqrt, "floor": _math.floor, "ceil": _math.ceil,
                "log": _math.log, "abs": abs, "round": round,
                "pi": _math.pi, "e": _math.e,
            })
            return json.dumps({"expression": expr, "result": round(result, 6)})
        except Exception as ex:
            return json.dumps({"error": str(ex)})
    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Template Builder ──────────────────────────────────────────

def fmt_str(s: str) -> str:
    return f"{T_STR}{s}{T_STR}"


def fmt_tool_declaration(tool: dict) -> str:
    fn = tool["function"]
    params = fn.get("parameters", {})
    props = params.get("properties", {})
    required = params.get("required", [])
    prop_parts = []
    for k in sorted(props.keys()):
        v = props[k]
        prop_parts.append(
            f"{k}{{description:{fmt_str(v.get('description',''))}"
            f",type:{fmt_str(v.get('type','STRING').upper())}}}"
        )
    props_str = ",".join(prop_parts)
    req_str = ",".join(fmt_str(r) for r in required)
    return (
        f"declaration:{fn['name']}"
        f"{{description:{fmt_str(fn['description'])}"
        f",parameters:{{properties:{{{props_str}}}"
        f",required:[{req_str}]"
        f",type:{fmt_str('OBJECT')}}}}}"
    )


def fmt_tool_call_str(name: str, args: dict) -> str:
    arg_parts = []
    for k in sorted(args.keys()):
        v = args[k]
        arg_parts.append(f"{k}:{fmt_str(v) if isinstance(v, str) else v}")
    return f"{T_CALL_OPEN}{name}{{{','.join(arg_parts)}}}{T_CALL_CLOSE}"


def fmt_tool_response_str(name: str, result_json: str) -> str:
    return f"{T_RESP_OPEN}{name}{{value:{fmt_str(result_json)}}}{T_RESP_CLOSE}"


def build_system_block(system_prompt: str, tools: list, enable_thinking: bool = True) -> str:
    out = f"{T_BOS}{T_TURN_OPEN}system\n"
    if enable_thinking:
        out += T_THINK + "\n"
    out += system_prompt.strip()
    for tool in tools:
        out += T_TOOL_OPEN + fmt_tool_declaration(tool).strip() + T_TOOL_CLOSE
    out += T_TURN_CLOSE
    return out


def build_user_turn(content: str) -> str:
    return f"{T_TURN_OPEN}user\n{content.strip()}{T_TURN_CLOSE}"


def build_model_turn_raw(
    model_turn: ModelTurn,
    for_history: bool = False,
) -> str:
    """
    Build a complete model turn.

    for_history=False (WITHIN current turn):
        Preserve ALL thinking blocks — this is what the model receives
        during active tool-call processing so it can continue its reasoning.

        Structure:
          <|turn>model\n
          [step 1]
            <|channel>thought\n...thinking...<channel|>   ← KEPT
            <|tool_call>call:...<tool_call|>
            <|tool_response>response:...<tool_response|>
          [step 2]
            <|channel>thought\n...thinking...<channel|>   ← KEPT
            <|tool_call>call:...<tool_call|>
            <|tool_response>response:...<tool_response|>
          [final]
            <|channel>thought\n...thinking...<channel|>   ← KEPT (if any)
            Final answer text
          <turn|>

    for_history=True (PAST turn in multi-turn context):
        Strip ALL thinking — only the final answer text is kept.
        This matches what the Jinja strip_thinking() macro does.

        Structure:
          <|turn>model\n
          Final answer text only
          <turn|>
    """
    out = f"{T_TURN_OPEN}model\n"

    if for_history:
        # Strip everything — only the final text response survives
        if model_turn.final_content:
            out += model_turn.final_content.strip()
    else:
        # Preserve all thinking blocks within the turn
        for step in model_turn.steps:
            # Thinking before this step's tool calls
            if step.thinking:
                out += f"{T_CHANNEL_OPEN}{step.thinking.strip()}{T_CHANNEL_CLOSE}"
            # Tool calls
            for tc in step.tool_calls:
                out += fmt_tool_call_str(tc.name, tc.args)
            # Tool responses
            for tr in step.tool_responses:
                out += fmt_tool_response_str(tr.name, tr.result)
        # Final thinking + content
        if model_turn.final_thinking:
            out += f"{T_CHANNEL_OPEN}{model_turn.final_thinking.strip()}{T_CHANNEL_CLOSE}"
        if model_turn.final_content:
            out += model_turn.final_content.strip()

    out += T_TURN_CLOSE
    return out


def build_model_prompt_start(injection_prefix: str = "") -> str:
    """Open model turn — model generates from here."""
    return f"{T_TURN_OPEN}model\n{injection_prefix}"


# ── Parser ────────────────────────────────────────────────────

def parse_output(text: str) -> tuple[Optional[str], list[ToolCall]]:
    """Extract (thinking_text, [ToolCall...]) from raw model output."""
    thinking_match = re.search(
        r'<\|channel>thought\n(.*?)<channel\|>', text, re.DOTALL
    )
    thinking = thinking_match.group(1) if thinking_match else None

    calls = []
    # Gemma 4 native: <|tool_call>call:name{key:<|"|>val<|"|>}<tool_call|>
    for m in re.finditer(r'<\|tool_call>call:(\w+)\{([^}]*)\}<tool_call\|>', text):
        name, args_raw = m.group(1), m.group(2)
        args = {}
        for am in re.finditer(r'(\w+):<\|"\|>([^<]*)<\|"\|>|(\w+):([^\s,}]+)', args_raw):
            if am.group(1):
                args[am.group(1)] = am.group(2)
            elif am.group(3):
                try:
                    args[am.group(3)] = float(am.group(4))
                except ValueError:
                    args[am.group(3)] = am.group(4)
        calls.append(ToolCall(name=name, args=args))
    return thinking, calls


# ── API Helpers ───────────────────────────────────────────────

def completions_api(prompt: str, max_tokens: int = 512,
                    stop: list = None) -> dict:
    payload = {"model": MODEL, "prompt": prompt,
                "max_tokens": max_tokens, "temperature": 0.3}
    if stop:
        payload["stop"] = stop
    return requests.post(f"{BASE_URL}/v1/completions", json=payload).json()


def tokenize_text(text: str) -> list:
    r = requests.post(f"{BASE_URL}/tokenize",
                      json={"model": MODEL, "prompt": text,
                            "add_special_tokens": False})
    d = r.json()
    return d.get("tokens") or d.get("token_ids") or []


# ── Display Helpers ───────────────────────────────────────────

def section(title: str, color: str = "cyan"):
    console.print()
    console.rule(f"[bold {color}]{title}[/bold {color}]", style=color)
    console.print()


def show_prompt(label: str, prompt: str, color: str = "yellow"):
    ids = tokenize_text(prompt)
    id_str = " ".join(str(t) for t in ids[:100])
    if len(ids) > 100:
        id_str += f" ... (+{len(ids)-100} more)"
    console.print(Panel(f"[dim]{id_str}[/dim]",
        title=f"[bold {color}]{label} — {len(ids)} tokens[/bold {color}]",
        border_style=color, padding=(0, 1)))
    console.print(Panel(f"[{color}]{repr(prompt)}[/{color}]",
        title=f"[bold {color}]{label} — Raw string[/bold {color}]",
        border_style=color, padding=(0, 1)))


def show_output(label: str, text: str, color: str = "green"):
    ids = tokenize_text(text)
    id_str = " ".join(str(t) for t in ids[:80])
    if len(ids) > 80:
        id_str += f" ... (+{len(ids)-80} more)"
    console.print(Panel(f"[dim]{id_str}[/dim]",
        title=f"[bold {color}]{label} — {len(ids)} tokens[/bold {color}]",
        border_style=color, padding=(0, 1)))
    console.print(Panel(f"[bold {color}]{text}[/bold {color}]",
        title=f"[bold {color}]{label} — Decoded[/bold {color}]",
        border_style=color, padding=(0, 1)))


def show_thinking(thinking: str, label: str = "THINKING"):
    if thinking:
        console.print(Panel(
            f"[italic dim]{thinking}[/italic dim]",
            title=f"[yellow]🧠 {label}[/yellow]",
            border_style="yellow", padding=(0, 1)))
    else:
        console.print(f"  [dim yellow]ℹ No thinking extracted[/dim yellow]")


def show_tool_calls(tool_calls: list[ToolCall]):
    if not tool_calls:
        return
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Tool",      style="cyan", width=18)
    table.add_column("Arguments", style="white")
    for tc in tool_calls:
        table.add_row(tc.name, str(tc.args))
    console.print(table)


# ═══════════════════════════════════════════════════════════════
# EXPERIMENT 1: Multi-step tool chain with CORRECT thinking persistence
# ═══════════════════════════════════════════════════════════════

console.print()
console.print(Panel.fit(
    "[bold white]Experiment 1 — Manual /v1/completions with Correct Thinking[/bold white]\n\n"
    "Builds a multi-step tool-call conversation manually.\n\n"
    "[bold]The key fix:[/bold] Thinking blocks are KEPT within a turn across tool steps,\n"
    "and only STRIPPED when a turn moves into history.\n\n"
    "  Within turn  : <|channel>thought\\n...<channel|> → PRESERVED\n"
    "  In history   : <|channel>thought\\n...<channel|> → STRIPPED\n\n"
    "[dim]Reference: ai.google.dev/gemma/docs/core/model_card_4[/dim]",
    border_style="bright_cyan", padding=(1, 3),
))

# ── System block (shared across all turns) ────────────────────
system_block = build_system_block(SYSTEM_PROMPT, TOOLS, enable_thinking=True)

# ═════════════════════════════════════════════
# TURN 1: Weather (single tool call)
# ═════════════════════════════════════════════
section("EXP 1 — TURN 1: Weather in Tokyo", "bright_cyan")

user1 = "What's the weather in Tokyo right now?"
user_turn_1 = build_user_turn(user1)

# Build initial prompt
prompt_1_init = system_block + user_turn_1 + build_model_prompt_start()
show_prompt("Turn 1 — Initial input prompt", prompt_1_init, "yellow")

# Step 1: Model generates (thinking + tool call)
resp_1_s1 = completions_api(prompt_1_init, max_tokens=200, stop=["<turn|>"])
out_1_s1 = resp_1_s1["choices"][0]["text"]
thinking_1_s1, calls_1_s1 = parse_output(out_1_s1)

show_output("Turn 1 Step 1 — Raw output", out_1_s1, "green")
show_thinking(thinking_1_s1, "Step 1 thinking")
show_tool_calls(calls_1_s1)

# Execute tools
step1 = ToolStep(thinking=thinking_1_s1, tool_calls=calls_1_s1)
for tc in calls_1_s1:
    result = call_tool(tc.name, tc.args)
    step1.tool_responses.append(ToolResponse(name=tc.name, result=result))
    console.print(Panel(
        Syntax(json.dumps(json.loads(result), indent=2), "json", theme="nord"),
        title=f"[cyan]{tc.name}({tc.args}) → result[/cyan]",
        border_style="cyan", padding=(0, 1)))

# Build prompt with tool results — KEEP thinking in place
turn1_obj = ModelTurn(steps=[step1])
prompt_1_after = (
    system_block
    + user_turn_1
    + build_model_turn_raw(turn1_obj, for_history=False)  # keeps thinking
    + build_model_prompt_start()
)
show_prompt("Turn 1 — Prompt AFTER tool results (thinking preserved)", prompt_1_after, "yellow")

# Final answer
resp_1_final = completions_api(prompt_1_after, max_tokens=150, stop=["<turn|>"])
final_1 = resp_1_final["choices"][0]["text"]
final_thinking_1, _ = parse_output(final_1)

show_output("Turn 1 — Final answer", final_1, "bright_green")
show_thinking(final_thinking_1, "Final step thinking (if any)")

# Complete turn 1 object
turn1_obj.final_thinking = final_thinking_1
turn1_obj.final_content = re.sub(r'<\|channel>.*?<channel\|>', '', final_1, flags=re.DOTALL).strip()

console.print(f"  [dim]Total tokens: input={resp_1_final['usage']['prompt_tokens']}  "
              f"output={resp_1_final['usage']['completion_tokens']}[/dim]")

# ═════════════════════════════════════════════
# TURN 2: NVDA stock → calculate shares
# (Sequential multi-step: get_stock_price → calculate)
# ═════════════════════════════════════════════
section("EXP 1 — TURN 2: NVDA stock price → calculate shares (multi-step)", "bright_cyan")

user2 = "Check NVIDIA's stock price, then calculate how many whole shares I can buy with $10,000."
user_turn_2 = build_user_turn(user2)

# History: Turn 1 with thinking STRIPPED (for_history=True)
history_turn_1 = build_model_turn_raw(turn1_obj, for_history=True)

console.print(Panel(
    f"[dim]{repr(history_turn_1)}[/dim]",
    title="[dim]Turn 1 in history (thinking stripped)[/dim]",
    border_style="dim", padding=(0, 1)))

# Build Turn 2 initial prompt
prompt_2_init = (
    system_block
    + user_turn_1
    + history_turn_1         # ← thinking stripped from Turn 1
    + user_turn_2
    + build_model_prompt_start()
)
show_prompt("Turn 2 — Initial input prompt (Turn 1 history, thinking stripped)", prompt_2_init, "yellow")

# Step 1 of Turn 2: Model thinks → calls get_stock_price
resp_2_s1 = completions_api(prompt_2_init, max_tokens=200, stop=["<turn|>"])
out_2_s1 = resp_2_s1["choices"][0]["text"]
thinking_2_s1, calls_2_s1 = parse_output(out_2_s1)

show_output("Turn 2 Step 1 — Raw output", out_2_s1, "green")
show_thinking(thinking_2_s1, "Step 1 thinking (KEEP within turn)")
show_tool_calls(calls_2_s1)

step2_1 = ToolStep(thinking=thinking_2_s1, tool_calls=calls_2_s1)
stock_price = None
for tc in calls_2_s1:
    result = call_tool(tc.name, tc.args)
    step2_1.tool_responses.append(ToolResponse(name=tc.name, result=result))
    parsed_result = json.loads(result)
    if tc.name == "get_stock_price":
        stock_price = parsed_result.get("price", "$875.30").replace("$", "")
    console.print(Panel(
        Syntax(json.dumps(parsed_result, indent=2), "json", theme="nord"),
        title=f"[cyan]{tc.name}({tc.args}) → result[/cyan]",
        border_style="cyan", padding=(0, 1)))

# Build prompt for Step 2 — CRITICAL: keep Step 1's thinking in the prompt!
turn2_obj = ModelTurn(steps=[step2_1])
prompt_2_s2 = (
    system_block
    + user_turn_1
    + history_turn_1
    + user_turn_2
    + build_model_turn_raw(turn2_obj, for_history=False)  # ← thinking from step 1 KEPT
    + build_model_prompt_start()
)

section("EXP 1 — TURN 2 STEP 2: Prompt showing Step 1 thinking is preserved", "bright_cyan")
show_prompt("Turn 2 Step 2 — Input (Step 1 thinking PRESERVED in prompt)", prompt_2_s2, "yellow")

console.print(Panel(
    "[bold]Why we preserve thinking here:[/bold]\n\n"
    "The model's Step 1 reasoning about the stock price problem is still\n"
    "in the prompt. When it sees the tool result, it can continue its chain\n"
    "of thought about how to use $10,000 / stock_price to get shares.\n\n"
    "Without this, the model would have to re-derive its plan from scratch.",
    title="[yellow]Why thinking is preserved within a turn[/yellow]",
    border_style="yellow", padding=(0, 1)))

# Step 2: Model sees stock price result → thinks → calls calculate
resp_2_s2 = completions_api(prompt_2_s2, max_tokens=200, stop=["<turn|>"])
out_2_s2 = resp_2_s2["choices"][0]["text"]
thinking_2_s2, calls_2_s2 = parse_output(out_2_s2)

show_output("Turn 2 Step 2 — Raw output", out_2_s2, "green")
show_thinking(thinking_2_s2, "Step 2 thinking (also KEPT within turn)")
show_tool_calls(calls_2_s2)

step2_2 = ToolStep(thinking=thinking_2_s2, tool_calls=calls_2_s2)
for tc in calls_2_s2:
    result = call_tool(tc.name, tc.args)
    step2_2.tool_responses.append(ToolResponse(name=tc.name, result=result))
    console.print(Panel(
        Syntax(json.dumps(json.loads(result), indent=2), "json", theme="nord"),
        title=f"[cyan]{tc.name}({tc.args}) → result[/cyan]",
        border_style="cyan", padding=(0, 1)))

# Build prompt for final answer — both step thinkings preserved
turn2_obj.steps.append(step2_2)
prompt_2_final = (
    system_block
    + user_turn_1
    + history_turn_1
    + user_turn_2
    + build_model_turn_raw(turn2_obj, for_history=False)  # both steps' thinking kept
    + build_model_prompt_start()
)
show_prompt("Turn 2 Final — Prompt (BOTH step thinkings preserved)", prompt_2_final, "yellow")

resp_2_final = completions_api(prompt_2_final, max_tokens=200, stop=["<turn|>"])
final_2 = resp_2_final["choices"][0]["text"]
final_thinking_2, _ = parse_output(final_2)

show_output("Turn 2 — Final answer", final_2, "bright_green")
show_thinking(final_thinking_2, "Final step thinking (if any)")

turn2_obj.final_thinking = final_thinking_2
turn2_obj.final_content = re.sub(r'<\|channel>.*?<channel\|>', '', final_2, flags=re.DOTALL).strip()

console.print(f"  [dim]Total prompt tokens for final answer: {resp_2_final['usage']['prompt_tokens']}[/dim]")

# Show what Turn 2 looks like in history (thinking stripped)
history_turn_2 = build_model_turn_raw(turn2_obj, for_history=True)
console.print(Panel(
    f"[dim]{repr(history_turn_2)}[/dim]",
    title="[dim]Turn 2 in history (thinking stripped — only final answer kept)[/dim]",
    border_style="dim", padding=(0, 1)))


# ═══════════════════════════════════════════════════════════════
# EXPERIMENT 2: Prompt injection — force thinking prefix
# ═══════════════════════════════════════════════════════════════

console.print()
console.print(Panel.fit(
    "[bold white]Experiment 2 — Thinking Prefix Injection via /v1/completions[/bold white]\n\n"
    "We inject a structured thinking prefix at the start of the model turn.\n"
    "The model continues from our prefix, producing our structured CoT.\n\n"
    f"[yellow]Prefix:[/yellow]\n[dim]{repr(INJECTION_PREFIX)}[/dim]\n\n"
    "[dim]Note: This only works via /v1/completions (raw prompt control).\n"
    "For /v1/chat/completions, the strip_thinking() macro would remove any\n"
    "thinking injected via the content field. The correct approach for\n"
    "chat/completions is to modify the --chat-template Jinja file.[/dim]",
    border_style="bright_magenta", padding=(1, 3),
))

section("EXP 2 — Injected thinking prefix + tool chain", "magenta")

user_inject = "Get NVIDIA stock price and calculate how many shares I can buy with $5,000."

prompt_inject = (
    build_system_block(SYSTEM_PROMPT, TOOLS, enable_thinking=True)
    + build_user_turn(user_inject)
    + build_model_prompt_start(injection_prefix=INJECTION_PREFIX)
)

show_prompt("Injected prompt (our thinking prefix is already in place)", prompt_inject, "magenta")
console.print(Panel(
    f"[bold magenta]{repr(INJECTION_PREFIX)}[/bold magenta]",
    title="[magenta]Our injected prefix — model continues from '4) Synthesize results'[/magenta]",
    border_style="magenta", padding=(0, 1)))

# Step 1: model continues our thinking prefix
resp_inj_s1 = completions_api(prompt_inject, max_tokens=300, stop=["<turn|>"])
out_inj_s1 = resp_inj_s1["choices"][0]["text"]

# The full thinking = our prefix + model's continuation
full_thinking_inj = (
    INJECTION_PREFIX.replace(T_CHANNEL_OPEN, "")
    + re.sub(r'.*?<channel\|>', '', out_inj_s1, count=1, flags=re.DOTALL)
    if T_CHANNEL_CLOSE in out_inj_s1
    else INJECTION_PREFIX.replace(T_CHANNEL_OPEN, "") + out_inj_s1
)

show_output("Step 1 — Raw output (model continued our prefix)", out_inj_s1, "green")

thinking_inj, calls_inj = parse_output(out_inj_s1)
# Full thinking = our prefix content + model's continuation
prefix_content = INJECTION_PREFIX.replace(T_CHANNEL_OPEN, "")
full_thinking = prefix_content + (thinking_inj or "")
console.print(Panel(
    f"[italic yellow]{full_thinking}[/italic yellow]",
    title="[yellow]🧠 Complete thinking (our prefix + model continuation)[/yellow]",
    border_style="yellow", padding=(0, 1)))
show_tool_calls(calls_inj)

# Execute tools
step_inj = ToolStep(
    thinking=full_thinking,  # store the full thinking including our prefix
    tool_calls=calls_inj,
)
for tc in calls_inj:
    result = call_tool(tc.name, tc.args)
    step_inj.tool_responses.append(ToolResponse(name=tc.name, result=result))
    console.print(Panel(
        Syntax(json.dumps(json.loads(result), indent=2), "json", theme="nord"),
        title=f"[cyan]{tc.name}({tc.args}) → result[/cyan]",
        border_style="cyan", padding=(0, 1)))

# Build next prompt with thinking preserved
turn_inj = ModelTurn(steps=[step_inj])
prompt_inj_s2 = (
    build_system_block(SYSTEM_PROMPT, TOOLS, enable_thinking=True)
    + build_user_turn(user_inject)
    + build_model_turn_raw(turn_inj, for_history=False)  # full thinking kept
    + build_model_prompt_start()
)
show_prompt("Step 2 — Prompt (injected + model thinking preserved)", prompt_inj_s2, "magenta")

resp_inj_s2 = completions_api(prompt_inj_s2, max_tokens=200, stop=["<turn|>"])
out_inj_s2 = resp_inj_s2["choices"][0]["text"]
thinking_inj_s2, calls_inj_s2 = parse_output(out_inj_s2)

show_output("Step 2 — Raw output", out_inj_s2, "green")
show_thinking(thinking_inj_s2, "Step 2 thinking (continued reasoning)")
show_tool_calls(calls_inj_s2)

# Execute any additional tool calls
for tc in calls_inj_s2:
    result = call_tool(tc.name, tc.args)
    step2_inj = ToolStep(thinking=thinking_inj_s2, tool_calls=calls_inj_s2)
    step2_inj.tool_responses.append(ToolResponse(name=tc.name, result=result))
    turn_inj.steps.append(step2_inj)
    console.print(Panel(
        Syntax(json.dumps(json.loads(result), indent=2), "json", theme="nord"),
        title=f"[cyan]{tc.name}({tc.args}) → result[/cyan]",
        border_style="cyan", padding=(0, 1)))

if calls_inj_s2:
    prompt_inj_final = (
        build_system_block(SYSTEM_PROMPT, TOOLS, enable_thinking=True)
        + build_user_turn(user_inject)
        + build_model_turn_raw(turn_inj, for_history=False)
        + build_model_prompt_start()
    )
    resp_inj_final = completions_api(prompt_inj_final, max_tokens=200, stop=["<turn|>"])
    final_inj = resp_inj_final["choices"][0]["text"]
else:
    final_inj = out_inj_s2

show_output("Injected experiment — Final answer", final_inj, "bright_magenta")

# ── Summary ────────────────────────────────────────────────────

section("SUMMARY — Thinking persistence rules", "bright_white")
table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
table.add_column("Context",                  style="cyan",  width=36)
table.add_column("Thinking blocks",          style="white", width=12)
table.add_column("Why",                      style="dim")
table.add_row(
    "Within a turn (tool-call steps)",
    "✅ KEEP",
    "Model uses prior reasoning as 'save state' to continue CoT"
)
table.add_row(
    "Across turns (in multi-turn history)",
    "❌ STRIP",
    "strip_thinking() in Jinja template — only final answer kept"
)
table.add_row(
    "Injected prefix (our custom CoT)",
    "✅ KEEP",
    "Persists exactly like model-generated thinking within the turn"
)
table.add_row(
    "Via /v1/chat/completions content field",
    "❌ STRIPPED",
    "Template strips it — use /v1/completions or custom template"
)
console.print(table)

console.print()
console.rule("[bold green]✓ Experiments complete[/bold green]", style="green")
console.print()
