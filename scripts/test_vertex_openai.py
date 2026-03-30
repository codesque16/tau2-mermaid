"""
Vertex AI – Qwen3-5-9b Dedicated Endpoint
OpenAI-style Tool Calling · Agentic Multi-Turn Conversation
============================================================
Working URL discovered from console "Sample request":
  POST {dedicated_domain}/v1/projects/{project}/locations/{region}/endpoints/{endpoint}:predict
  Body: {"instances": [{"@requestFormat": "chatCompletions", ...openai_payload...}]}

Install:
    uv add google-cloud-aiplatform
    gcloud auth application-default login
"""

import json
import textwrap
import random
import google.auth
import google.auth.transport.requests
from google.cloud import aiplatform

from agent.vertex_dedicated_http import vertex_predict_post

# ─────────────────────────────────────────────────────────────
# 1.  Config
# ─────────────────────────────────────────────────────────────
PROJECT_ID  = "53845524870"
REGION      = "us-central1"
ENDPOINT_ID = "mg-endpoint-411386e5-c7ad-40d7-80e2-723bbb793132"
DEDICATED   = f"{ENDPOINT_ID}.{REGION}-{PROJECT_ID}.prediction.vertexai.goog"

# ✅ The real working URL (from console Sample Request).
# Keep PROJECT_ID / REGION / ENDPOINT_ID in sync with configs/gemini_simulation_vertex.yaml assistant.vertex_*.
PREDICT_URL = (
    f"https://{DEDICATED}/v1/projects/{PROJECT_ID}"
    f"/locations/{REGION}/endpoints/{ENDPOINT_ID}:predict"
)

credentials, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
credentials.refresh(google.auth.transport.requests.Request())
aiplatform.init(project=PROJECT_ID, location=REGION)
print(f"✅  Authenticated | project={PROJECT_ID} | region={REGION}")
print(f"🌐  Endpoint URL: {PREDICT_URL}\n")


# ─────────────────────────────────────────────────────────────
# 2.  Tool definitions (OpenAI function-call format)
# ─────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": "Search for flights between two cities on a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin":      {"type": "string", "description": "IATA code e.g. BOM"},
                    "destination": {"type": "string", "description": "IATA code e.g. GOI"},
                    "date":        {"type": "string", "description": "YYYY-MM-DD"},
                    "cabin_class": {"type": "string", "enum": ["economy", "business", "first"]},
                },
                "required": ["origin", "destination", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_hotel",
            "description": "Book a hotel in a city for given dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city":        {"type": "string"},
                    "check_in":    {"type": "string", "description": "YYYY-MM-DD"},
                    "check_out":   {"type": "string", "description": "YYYY-MM-DD"},
                    "guests":      {"type": "integer"},
                    "hotel_class": {"type": "string", "enum": ["budget", "3-star", "4-star", "5-star"]},
                },
                "required": ["city", "check_in", "check_out"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": "Convert an amount between two currencies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount":        {"type": "number"},
                    "from_currency": {"type": "string"},
                    "to_currency":   {"type": "string"},
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_local_attractions",
            "description": "Return top attractions in a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city":     {"type": "string"},
                    "category": {"type": "string",
                                 "enum": ["food", "culture", "nature", "adventure", "shopping"]},
                    "top_n":    {"type": "integer"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_itinerary",
            "description": "Build a day-by-day travel itinerary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination":   {"type": "string"},
                    "duration_days": {"type": "integer"},
                    "interests":     {"type": "array", "items": {"type": "string"}},
                    "budget_usd":    {"type": "number"},
                },
                "required": ["destination", "duration_days"],
            },
        },
    },
]


# ─────────────────────────────────────────────────────────────
# 3.  Simulated tool executors (swap for real APIs)
# ─────────────────────────────────────────────────────────────
def execute_tool(name: str, args: dict) -> dict:
    if name == "get_current_weather":
        unit = args.get("unit", "celsius")
        temp = random.randint(28, 36) if unit == "celsius" else random.randint(82, 97)
        return {"city": args["city"], "temperature": temp, "unit": unit,
                "condition": random.choice(["Sunny", "Partly cloudy", "Humid"]),
                "humidity": f"{random.randint(55, 85)}%"}

    if name == "search_flights":
        return {
            "origin": args["origin"], "destination": args["destination"],
            "date": args["date"],
            "flights": [
                {"flight": f"AI{random.randint(100,999)}",
                 "airline": random.choice(["Air India", "IndiGo", "Vistara"]),
                 "departure": f"{6+i*4:02d}:00",
                 "arrival":   f"{8+i*4:02d}:{random.randint(0,59):02d}",
                 "price_usd": random.randint(80, 350)}
                for i in range(3)
            ],
        }

    if name == "book_hotel":
        return {"booking_id": f"HTL{random.randint(10000,99999)}",
                "hotel": f"{args.get('hotel_class','4-star').title()} Hotel {args['city']}",
                "city": args["city"], "check_in": args["check_in"],
                "check_out": args["check_out"],
                "price_per_night_usd": random.randint(60, 250), "status": "Confirmed"}

    if name == "convert_currency":
        rates = {"USD": 1, "INR": 83.5, "EUR": 0.92, "GBP": 0.79}
        frm, to = args["from_currency"].upper(), args["to_currency"].upper()
        converted = args["amount"] / rates.get(frm, 1) * rates.get(to, 1)
        return {"from": f"{args['amount']} {frm}", "to": f"{converted:.2f} {to}"}

    if name == "get_local_attractions":
        db = {
            "Goa":    ["Baga Beach", "Dudhsagar Falls", "Fort Aguada",
                       "Spice Plantations", "Anjuna Flea Market"],
            "Delhi":  ["Red Fort", "Qutub Minar", "India Gate",
                       "Chandni Chowk", "Lotus Temple"],
        }
        places = db.get(args["city"],
                        [f"{args['city']} Museum", f"{args['city']} Park"])
        return {"city": args["city"],
                "attractions": places[:args.get("top_n", 5)]}

    if name == "create_itinerary":
        dest, days = args["destination"], args["duration_days"]
        return {"destination": dest, "duration": f"{days} days",
                "itinerary": [
                    {"day": d,
                     "morning":   f"Visit {dest} landmark {d}A",
                     "afternoon": f"Lunch + attraction {d}B",
                     "evening":   f"Sunset + local dinner"}
                    for d in range(1, days+1)
                ],
                "estimated_cost_usd": args.get("budget_usd", "Not specified")}

    return {"error": f"Unknown tool: {name}"}


# ─────────────────────────────────────────────────────────────
# 4.  API caller — uses :predict with @requestFormat wrapper
# ─────────────────────────────────────────────────────────────
def call_chat(messages: list, tools: list = None, tool_choice=None) -> dict:
    """
    Calls the dedicated endpoint using the :predict path with the
    @requestFormat=chatCompletions wrapper discovered from the console.

    tool_choice: default None → the field is **omitted** from JSON. That is not the same as
    ``"tool_choice":"auto"``. vLLM often returns BadRequestError about --enable-auto-tool-choice
    only when the literal string ``"auto"`` is present. Pass tool_choice="auto" only if your
    deployment enables the matching server flags (same rules as GeminiAgent vertex mode).
    """
    if not credentials.valid:
        credentials.refresh(google.auth.transport.requests.Request())

    # Build the instance — everything OpenAI, plus @requestFormat
    instance = {
        "@requestFormat": "chatCompletions",
        "messages":        messages,
        "max_tokens":      2048,
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature":     0.3,
        "top_p":           0.95,
    }
    if tools:
        instance["tools"] = tools
        if tool_choice is not None:
            instance["tool_choice"] = tool_choice

    payload = {"instances": [instance]}

    try:
        raw = vertex_predict_post(PREDICT_URL, credentials.token, payload, timeout_s=120)
        # Vertex wraps the response: {"predictions": [{...openai chat completion...}]}
        # Unwrap to standard OpenAI shape so the rest of the code stays the same
        if "predictions" in raw:
            prediction = raw["predictions"]
            if isinstance(prediction, str):
                prediction = json.loads(prediction)
            return prediction
        return raw
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# 5.  Pretty printer
# ─────────────────────────────────────────────────────────────
C = {"cyan": "\033[96m", "green": "\033[92m",
     "yellow": "\033[93m", "magenta": "\033[95m", "": ""}
R = "\033[0m"

def pretty(label: str, data, color=""):
    print(f"\n{C.get(color,'')}{'─'*62}\n  {label}\n{'─'*62}{R}")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2))
    elif data:
        print(textwrap.fill(str(data), width=80))


# ─────────────────────────────────────────────────────────────
# 6.  Agentic loop — resolves tool calls automatically
# ─────────────────────────────────────────────────────────────
def agentic_chat(user_query: str, history: list, max_turns: int = 8) -> str:
    history.append({"role": "user", "content": user_query})
    pretty("👤  USER", user_query, "cyan")

    for turn in range(max_turns):
        response = call_chat(history, tools=TOOLS)

        if "error" in response:
            pretty("❌  ERROR", response["error"], "yellow")
            return response["error"]

        # Handle both direct OpenAI response shape and wrapped shape
        choices = response.get("choices") or []
        if not choices:
            pretty("⚠️  UNEXPECTED RESPONSE", response, "yellow")
            return str(response)

        choice     = choices[0]
        message    = choice.get("message", {})
        finish     = choice.get("finish_reason", "")
        tool_calls = message.get("tool_calls") or []

        # ── Final text reply ────────────────────────────────────
        if not tool_calls or finish in ("stop", "length"):
            text = message.get("content", "")
            history.append({"role": "assistant", "content": text})
            pretty("🤖  ASSISTANT", text, "green")
            return text

        # ── Tool call round ─────────────────────────────────────
        history.append({
            "role":       "assistant",
            "content":    message.get("content"),
            "tool_calls": tool_calls,
        })
        pretty(f"🔧  TOOL CALLS (round {turn+1})", tool_calls, "magenta")

        for tc in tool_calls:
            fn   = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"])
            pretty(f"   ▶ {fn}", args, "yellow")
            result = execute_tool(fn, args)
            pretty(f"   ◀ {fn} result", result, "green")
            history.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      json.dumps(result),
            })

    return "Max rounds reached."


# ─────────────────────────────────────────────────────────────
# 7.  Demo: 4-turn Goa trip conversation
# ─────────────────────────────────────────────────────────────
SYSTEM = (
    "You are a helpful AI travel assistant. Use tools to get real data "
    "before answering. Be concise and specific."
)

def run_demo():
    print("\n" + "═"*66)
    print("  VERTEX AI · Qwen3-5-9b · Tool Calling · 4-Turn Demo")
    print("═"*66)

    history = [{"role": "system", "content": SYSTEM}]

    agentic_chat(
        "Planning a trip to Goa next week — what's the weather like? "
        "Also convert $500 to Indian Rupees.",
        history,
    )
    agentic_chat(
        "Find economy flights from BOM to GOI on 2026-04-05.",
        history,
    )
    agentic_chat(
        "Book a 4-star hotel in Goa April 5–8 for 2 guests "
        "and show me the top 5 attractions.",
        history,
    )
    agentic_chat(
        "Create a 3-day Goa itinerary focused on beaches and culture "
        "within my $500 budget.",
        history,
    )

    print("\n" + "═"*66)
    print(f"  Done! {len(history)} messages in conversation.")
    print("═"*66 + "\n")

    with open("conversation_log.json", "w") as f:
        json.dump(history, f, indent=2)
    print("💾  Saved → conversation_log.json")


if __name__ == "__main__":
    run_demo()
