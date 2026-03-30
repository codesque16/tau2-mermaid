"""
Vertex AI dedicated endpoint HTTP client: POST .../endpoints/{id}:predict with
{"instances": [{"@requestFormat": "chatCompletions", ...}]}.

Same wire format as scripts/test_vertex_openai.py (urllib, JSON body, Bearer ADC).
This is not the OpenAI Python SDK or api.openai.com — only OpenAI-shaped JSON inside the instance.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def vertex_predict_post(
    url: str,
    bearer_token: str,
    body: dict[str, Any],
    *,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """
    POST JSON to the Vertex :predict URL; return the full parsed JSON envelope
    (e.g. {"predictions": ...} or {"error": ...}).

    Mirrors scripts/test_vertex_openai.py call_chat request path (Request + headers + urlopen).
    """
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Vertex endpoint predict failed HTTP {e.code} for {url!r}: {detail[:4000]}"
        ) from e
    except Exception as e:
        # Includes URLError: nodename nor servname provided / not known, TLS failures, etc.
        raise RuntimeError(
            f"Vertex endpoint predict failed for {url!r}: {type(e).__name__}: {e}"
        ) from e
