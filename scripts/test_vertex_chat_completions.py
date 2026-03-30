"""Simple sanity test for dedicated Vertex endpoint chatCompletions payload.

This mirrors the known-working curl:
POST https://{DEDICATED_ENDPOINT_DOMAIN}/v1beta1/projects/{project}/locations/{location}/endpoints/{endpoint_id}:predict
with body.instances[0].@requestFormat = "chatCompletions".
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import google.auth
from google.auth.transport.requests import Request
import urllib.error
import urllib.request


def _extract_text(payload: dict[str, Any]) -> str:
    preds = payload.get("predictions")
    if isinstance(preds, dict):
        choices = preds.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0] if isinstance(choices[0], dict) else {}
            msg = c0.get("message") if isinstance(c0, dict) else {}
            if isinstance(msg, dict):
                txt = msg.get("content")
                if isinstance(txt, str):
                    return txt
    if isinstance(preds, list) and preds:
        p0 = preds[0]
        if isinstance(p0, str):
            return p0
        if isinstance(p0, dict):
            for k in ("content", "text", "generated_text", "output_text"):
                v = p0.get(k)
                if isinstance(v, str):
                    return v
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Test dedicated endpoint chatCompletions request.")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.getenv("VERTEX_LOCATION", "us-central1"))
    parser.add_argument("--endpoint-id", default=os.getenv("VERTEX_ENDPOINT_ID", ""))
    parser.add_argument("--domain", default=os.getenv("DEDICATED_ENDPOINT_DOMAIN", ""))
    parser.add_argument("--prompt", default="What is 15 minus 7?")
    parser.add_argument("--max-tokens", type=int, default=100)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--api-version", default="v1beta1")
    parser.add_argument(
        "--with-calculator-tool",
        action="store_true",
        help="Attach a simple calculate(expr) tool schema and require tool use.",
    )
    parser.add_argument(
        "--with-large-tool-list",
        action="store_true",
        help="Attach a larger retail-like function tool list and require tool use.",
    )
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Missing --project (or set GOOGLE_CLOUD_PROJECT).")
    if not args.endpoint_id:
        raise SystemExit("Missing --endpoint-id (or set VERTEX_ENDPOINT_ID).")
    if not args.domain:
        raise SystemExit("Missing --domain (or set DEDICATED_ENDPOINT_DOMAIN).")

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    token = getattr(credentials, "token", None) or ""
    if not token:
        raise SystemExit("Failed to fetch ADC token.")

    url = (
        f"https://{args.domain}/{args.api_version}/projects/{args.project}/locations/"
        f"{args.location}/endpoints/{args.endpoint_id}:predict"
    )
    request_payload: dict[str, Any] = {
        "instances": [
            {
                "@requestFormat": "chatCompletions",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": args.prompt}],
                    }
                ],
                "max_tokens": args.max_tokens,
                "temperature": args.temperature,
            }
        ]
    }
    if args.with_calculator_tool or args.with_large_tool_list:
        # OpenAI-style tool schema in chatCompletions request format.
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": "Evaluate a basic arithmetic expression and return the numeric result.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "description": "Math expression, e.g. '15 - 7' or '(12*8)/3'",
                            }
                        },
                        "required": ["expression"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        if args.with_large_tool_list:
            tools.extend(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "find_user_id_by_email",
                            "description": "Find user id by email.",
                            "parameters": {
                                "type": "object",
                                "properties": {"email": {"type": "string"}},
                                "required": ["email"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "find_user_id_by_name_zip",
                            "description": "Find user id by first/last name and zip.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "first_name": {"type": "string"},
                                    "last_name": {"type": "string"},
                                    "zip": {"type": "string"},
                                },
                                "required": ["first_name", "last_name", "zip"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_user_details",
                            "description": "Get profile and order summary for user.",
                            "parameters": {
                                "type": "object",
                                "properties": {"user_id": {"type": "string"}},
                                "required": ["user_id"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_order_details",
                            "description": "Get status/details for an order.",
                            "parameters": {
                                "type": "object",
                                "properties": {"order_id": {"type": "string"}},
                                "required": ["order_id"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "return_delivered_order_items",
                            "description": "Return delivered items from an order.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "item_ids": {"type": "array", "items": {"type": "string"}},
                                    "payment_method_id": {"type": "string"},
                                },
                                "required": ["order_id", "item_ids", "payment_method_id"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "exchange_delivered_order_items",
                            "description": "Exchange delivered items for new variants.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "item_ids": {"type": "array", "items": {"type": "string"}},
                                    "new_item_ids": {"type": "array", "items": {"type": "string"}},
                                    "payment_method_id": {"type": "string"},
                                },
                                "required": [
                                    "order_id",
                                    "item_ids",
                                    "new_item_ids",
                                    "payment_method_id",
                                ],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "modify_pending_order_address",
                            "description": "Modify shipping address for pending order.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "address1": {"type": "string"},
                                    "address2": {"type": "string"},
                                    "city": {"type": "string"},
                                    "state": {"type": "string"},
                                    "country": {"type": "string"},
                                    "zip": {"type": "string"},
                                },
                                "required": [
                                    "order_id",
                                    "address1",
                                    "address2",
                                    "city",
                                    "state",
                                    "country",
                                    "zip",
                                ],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "modify_pending_order_items",
                            "description": "Modify item variants for pending order.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "item_ids": {"type": "array", "items": {"type": "string"}},
                                    "new_item_ids": {"type": "array", "items": {"type": "string"}},
                                    "payment_method_id": {"type": "string"},
                                },
                                "required": [
                                    "order_id",
                                    "item_ids",
                                    "new_item_ids",
                                    "payment_method_id",
                                ],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "modify_pending_order_payment",
                            "description": "Change pending order payment method.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "payment_method_id": {"type": "string"},
                                },
                                "required": ["order_id", "payment_method_id"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "modify_user_address",
                            "description": "Change user's default address.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "user_id": {"type": "string"},
                                    "address1": {"type": "string"},
                                    "address2": {"type": "string"},
                                    "city": {"type": "string"},
                                    "state": {"type": "string"},
                                    "country": {"type": "string"},
                                    "zip": {"type": "string"},
                                },
                                "required": [
                                    "user_id",
                                    "address1",
                                    "address2",
                                    "city",
                                    "state",
                                    "country",
                                    "zip",
                                ],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "cancel_pending_order",
                            "description": "Cancel pending order with valid reason.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["order_id", "reason"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "transfer_to_human_agents",
                            "description": "Transfer to human support queue.",
                            "parameters": {
                                "type": "object",
                                "properties": {"summary": {"type": "string"}},
                                "required": ["summary"],
                            },
                        },
                    },
                ]
            )
        request_payload["instances"][0]["tools"] = tools
        # Force tool invocation if supported.
        request_payload["instances"][0]["tool_choice"] = {
            "type": "function",
            "function": {"name": "calculate"},
        }

    print("=== Request ===")
    print(json.dumps({"url": url, "body": request_payload}, indent=2))

    raw = json.dumps(request_payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code}: {detail}")

    print("\n=== Response (raw) ===")
    print(json.dumps(payload, indent=2))

    text = _extract_text(payload)
    print("\n=== Assistant text ===")
    print(text)
    preds = payload.get("predictions")
    tool_calls = None
    if isinstance(preds, dict):
        choices = preds.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0] if isinstance(choices[0], dict) else {}
            msg = c0.get("message") if isinstance(c0, dict) else {}
            if isinstance(msg, dict):
                tool_calls = msg.get("tool_calls")
    print("\n=== Tool calls (if any) ===")
    print(json.dumps(tool_calls, indent=2))


if __name__ == "__main__":
    main()
