"""
Debug script: test the endpoint step by step
1. Bare message (no tools) — confirm basic connectivity
2. With tools — confirm tool calling works
3. Print raw response so we can see exactly what the endpoint returns
"""
import json, urllib.request, urllib.error
import google.auth, google.auth.transport.requests

PROJECT_ID  = "53845524870"
REGION      = "us-central1"
ENDPOINT_ID = "mg-endpoint-411386e5-c7ad-40d7-80e2-723bbb793132"
DEDICATED   = f"{ENDPOINT_ID}.{REGION}-{PROJECT_ID}.prediction.vertexai.goog"
PREDICT_URL = f"https://{DEDICATED}/v1/projects/{PROJECT_ID}/locations/{REGION}/endpoints/{ENDPOINT_ID}:predict"

credentials, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
credentials.refresh(google.auth.transport.requests.Request())

def post(instance: dict, label: str):
    payload = {"instances": [instance]}
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"PAYLOAD:\n{json.dumps(payload, indent=2)}")
    print(f"{'='*60}")

    req = urllib.request.Request(
        PREDICT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            print(f"✅ STATUS: {resp.status}")
            print(f"RAW RESPONSE:\n{body[:2000]}")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"❌ HTTP {e.code}")
        print(f"ERROR BODY:\n{body[:2000]}")
    except Exception as ex:
        print(f"💥 EXCEPTION: {type(ex).__name__}: {ex}")

# ── Test 1: Exact format from the console sample request ──────
post({
    "@requestFormat": "chatCompletions",
    "messages": [{"role": "user", "content": "Say hello in one word."}],
    "max_tokens": 10,
    "temperature": 0.3,
}, "Exact console format (no tools)")

# ── Test 2: With a single simple tool ─────────────────────────
post({
    "@requestFormat": "chatCompletions",
    "messages": [{"role": "user", "content": "What is the weather in Goa?"}],
    "max_tokens": 256,
    "temperature": 0.3,
    "tools": [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }],
    "tool_choice": "auto",
}, "With one tool")

