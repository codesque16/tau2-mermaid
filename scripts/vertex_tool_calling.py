"""
Vertex AI - Qwen3 5-9b Endpoint
OpenAI-style Tool Calling with Agentic Multi-Turn Conversation
================================================================
Endpoint: mg-endpoint-411386e5-c7ad-40d7-80e2-723bbb793132
Region:   us-central1
Project:  53845524870
"""

import json
import re
import textwrap
from datetime import datetime, timedelta
import random

# ─────────────────────────────────────────────
# 0.  Google Auth  (run once, reuse token)
# ─────────────────────────────────────────────
try:
    import google.auth
    import google.auth.transport.requests

    credentials, project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    ACCESS_TOKEN = credentials.token
    print(f"✅  Authenticated as project: {project}")
except Exception as e:
    print(f"⚠️  google-auth not available ({e}). Set ACCESS_TOKEN manually below.")
    ACCESS_TOKEN = "YOUR_ACCESS_TOKEN"   # fallback

# ─────────────────────────────────────────────
# 1.  Endpoint config  (from your screenshot)
# ─────────────────────────────────────────────
ENDPOINT_ID = "mg-endpoint-411386e5-c7ad-40d7-80e2-723bbb793132"
PROJECT_ID  = "53845524870"
REGION      = "us-central1"
MODEL_ID    = "9032977730485420032"   # Deployed model ID

# ✅ Dedicated endpoint domain (required for dedicated endpoints)
# Format: {endpoint_id_numeric}.{region}-{project_id}.prediction.vertexai.goog
DEDICATED_DOMAIN = "3280327520527843328.us-central1-53845524870.prediction.vertexai.goog"
CHAT_URL = f"https://{DEDICATED_DOMAIN}/v1beta1/endpoints/{ENDPOINT_ID}/chat/completions"


# ─────────────────────────────────────────────
# 2.  Tool definitions  (OpenAI format)
# ─────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather for a given city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g. 'Mumbai'"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit"
                    }
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": "Search for available flights between two cities on a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin":      {"type": "string", "description": "Departure city IATA code, e.g. BOM"},
                    "destination": {"type": "string", "description": "Arrival city IATA code, e.g. DEL"},
                    "date":        {"type": "string", "description": "Travel date in YYYY-MM-DD format"},
                    "cabin_class": {
                        "type": "string",
                        "enum": ["economy", "business", "first"],
                        "description": "Cabin class preference"
                    }
                },
                "required": ["origin", "destination", "date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_hotel",
            "description": "Book a hotel room in a city for specified dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city":        {"type": "string"},
                    "check_in":    {"type": "string", "description": "YYYY-MM-DD"},
                    "check_out":   {"type": "string", "description": "YYYY-MM-DD"},
                    "guests":      {"type": "integer", "description": "Number of guests"},
                    "hotel_class": {
                        "type": "string",
                        "enum": ["budget", "3-star", "4-star", "5-star"]
                    }
                },
                "required": ["city", "check_in", "check_out"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": "Convert an amount from one currency to another using live-ish rates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount":        {"type": "number"},
                    "from_currency": {"type": "string", "description": "ISO 4217 code, e.g. USD"},
                    "to_currency":   {"type": "string", "description": "ISO 4217 code, e.g. INR"}
                },
                "required": ["amount", "from_currency", "to_currency"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_local_attractions",
            "description": "Return a list of must-see attractions in a given city, optionally filtered by category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["food", "culture", "nature", "adventure", "shopping"],
                        "description": "Optional category filter"
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "How many attractions to return (default 5)"
                    }
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_itinerary",
            "description": "Compile a day-by-day travel itinerary from collected trip information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination":  {"type": "string"},
                    "duration_days":{"type": "integer"},
                    "interests":    {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of traveller interests"
                    },
                    "budget_usd":   {"type": "number", "description": "Total budget in USD"}
                },
                "required": ["destination", "duration_days"]
            }
        }
    }
]


# ─────────────────────────────────────────────
# 3.  Simulated tool executors
# ─────────────────────────────────────────────
def execute_tool(name: str, args: dict) -> dict:
    """Simulated tool implementations — replace with real APIs."""

    if name == "get_current_weather":
        city = args["city"]
        unit = args.get("unit", "celsius")
        temp = random.randint(22, 38) if unit == "celsius" else random.randint(72, 100)
        return {
            "city": city,
            "temperature": temp,
            "unit": unit,
            "condition": random.choice(["Sunny", "Partly cloudy", "Humid", "Breezy"]),
            "humidity": f"{random.randint(40, 90)}%"
        }

    if name == "search_flights":
        flights = []
        for i in range(3):
            dep_h = 6 + i * 4
            flights.append({
                "flight": f"AI{random.randint(100,999)}",
                "departure": f"{dep_h:02d}:00",
                "arrival":   f"{dep_h+2:02d}:{random.randint(0,59):02d}",
                "price_usd": random.randint(80, 400),
                "airline":   random.choice(["Air India", "IndiGo", "Vistara"])
            })
        return {
            "origin": args["origin"], "destination": args["destination"],
            "date": args["date"], "flights": flights
        }

    if name == "book_hotel":
        return {
            "booking_id":   f"HTL{random.randint(10000,99999)}",
            "hotel":        f"{args.get('hotel_class','4-star').title()} Hotel {args['city']}",
            "city":         args["city"],
            "check_in":     args["check_in"],
            "check_out":    args["check_out"],
            "price_per_night_usd": random.randint(60, 300),
            "status":       "Confirmed"
        }

    if name == "convert_currency":
        rates = {"USD": 1, "INR": 83.5, "EUR": 0.92, "GBP": 0.79, "AED": 3.67}
        frm = args["from_currency"].upper()
        to  = args["to_currency"].upper()
        result = args["amount"] / rates.get(frm, 1) * rates.get(to, 1)
        return {
            "from": f"{args['amount']} {frm}",
            "to":   f"{result:.2f} {to}",
            "rate": rates.get(to, 1) / rates.get(frm, 1)
        }

    if name == "get_local_attractions":
        db = {
            "Delhi":   ["Red Fort", "Qutub Minar", "India Gate", "Chandni Chowk", "Lotus Temple"],
            "Goa":     ["Baga Beach", "Dudhsagar Falls", "Fort Aguada", "Spice Plantations", "Anjuna Flea Market"],
            "Jaipur":  ["Amber Fort", "Hawa Mahal", "City Palace", "Jantar Mantar", "Nahargarh Fort"],
        }
        city = args["city"]
        top  = args.get("top_n", 5)
        places = db.get(city, [f"{city} Museum", f"{city} Park", f"{city} Market"])[:top]
        return {"city": city, "attractions": places, "category": args.get("category")}

    if name == "create_itinerary":
        dest = args["destination"]
        days = args["duration_days"]
        itinerary = []
        for d in range(1, days + 1):
            itinerary.append({
                "day": d,
                "morning":   f"Explore {dest} landmark {d}A",
                "afternoon": f"Lunch at local {dest} restaurant, visit attraction {d}B",
                "evening":   f"Sunset at viewpoint, dinner at {dest} specialty restaurant"
            })
        return {
            "destination": dest,
            "duration":    f"{days} days",
            "itinerary":   itinerary,
            "estimated_cost_usd": args.get("budget_usd", "Not specified")
        }

    return {"error": f"Unknown tool: {name}"}


# ─────────────────────────────────────────────
# 4.  HTTP client wrapper
# ─────────────────────────────────────────────
import urllib.request

def call_endpoint(messages: list, tools: list = None, tool_choice: str = "auto") -> dict:
    """
    Call the Vertex AI OpenAI-compatible endpoint.
    Returns the raw response dict.
    """
    payload = {
        "model":       MODEL_ID,
        "messages":    messages,
        "temperature": 0.3,
        "max_tokens":  1024,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = tool_choice

    data    = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type":  "application/json",
    }

    req  = urllib.request.Request(CHAT_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return {"error": f"HTTP {e.code}: {body}"}


# ─────────────────────────────────────────────
# 5.  Agentic loop  (handles tool calls automatically)
# ─────────────────────────────────────────────
def pretty(label: str, data, color: str = ""):
    COLORS = {"cyan": "\033[96m", "green": "\033[92m",
              "yellow": "\033[93m", "magenta": "\033[95m", "": ""}
    RESET  = "\033[0m"
    c      = COLORS.get(color, "")
    border = "─" * 60
    print(f"\n{c}{border}")
    print(f"  {label}")
    print(border + RESET)
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2))
    else:
        print(textwrap.fill(str(data), width=80))


def agentic_chat(user_query: str, conversation_history: list, max_turns: int = 6):
    """
    Single user turn with automatic tool-call resolution.
    Appends to conversation_history in-place.
    Returns the final assistant text reply.
    """
    conversation_history.append({"role": "user", "content": user_query})
    pretty("👤  USER", user_query, "cyan")

    for turn in range(max_turns):
        response = call_endpoint(conversation_history, tools=TOOLS)

        if "error" in response:
            pretty("❌  API ERROR", response["error"], "yellow")
            return response["error"]

        choice  = response["choices"][0]
        message = choice["message"]
        finish  = choice["finish_reason"]

        # ── Regular text reply ──────────────────────────────
        if finish == "stop" or not message.get("tool_calls"):
            text = message.get("content", "")
            conversation_history.append({"role": "assistant", "content": text})
            pretty("🤖  ASSISTANT (final)", text, "green")
            return text

        # ── Tool calls requested ────────────────────────────
        tool_calls = message["tool_calls"]
        conversation_history.append({
            "role":       "assistant",
            "content":    message.get("content"),
            "tool_calls": tool_calls
        })

        pretty(f"🔧  TOOL CALLS  (turn {turn+1})", tool_calls, "magenta")

        # Execute each tool and feed results back
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"])

            pretty(f"   ▶ Executing: {fn_name}", fn_args, "yellow")
            result  = execute_tool(fn_name, fn_args)
            pretty(f"   ◀ Result:    {fn_name}", result, "green")

            conversation_history.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      json.dumps(result)
            })

    return "Max tool-call turns reached."


# ─────────────────────────────────────────────
# 6.  Demo: 3-4 turn conversation
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are a helpful AI travel assistant.
You have access to tools for weather, flights, hotels, currency conversion,
local attractions, and itinerary planning.
Always use the appropriate tools to get real data before answering.
Be concise but thorough."""

def run_demo():
    print("\n" + "═" * 70)
    print("  VERTEX AI  ·  Qwen3-5-9b  ·  OpenAI-style Tool Calling Demo")
    print("═" * 70)

    # Shared conversation history across turns
    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ── Turn 1: Weather + currency ──────────────────────────
    agentic_chat(
        "I'm planning a trip to Goa next week. What's the weather like there? "
        "Also, I have a budget of $500 — how much is that in Indian Rupees?",
        history
    )

    # ── Turn 2: Flights ────────────────────────────────────
    agentic_chat(
        "Great! Can you find me economy flights from BOM to GOI on 2026-04-05?",
        history
    )

    # ── Turn 3: Hotel + attractions ────────────────────────
    agentic_chat(
        "Book me a 4-star hotel in Goa from April 5th to April 8th for 2 guests, "
        "and show me the top 5 attractions there.",
        history
    )

    # ── Turn 4: Full itinerary ─────────────────────────────
    agentic_chat(
        "Perfect! Now put it all together — create a 3-day itinerary for Goa "
        "focused on beaches and culture, within my $500 budget.",
        history
    )

    print("\n" + "═" * 70)
    print("  Demo complete! Full conversation had", len(history), "messages.")
    print("═" * 70 + "\n")
    return history


# ─────────────────────────────────────────────
# 7.  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    final_history = run_demo()

    # Optional: save full conversation log
    with open("conversation_log.json", "w") as f:
        json.dump(final_history, f, indent=2)
    print("💾  Conversation saved to conversation_log.json")
