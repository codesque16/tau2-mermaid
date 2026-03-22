"""
Raw ``generate_content`` I/O for debugging (native Gemini shape: user / model / function).

Logfire: logs one string attribute ``gemini_io_json`` so the UI does not explode nested
``messages[]`` / ``tool_calls`` into awkward trees. Copy the string or use a JSON viewer.

Custom UI: In Logfire, open an event → attribute actions → **Enhance view** (JSX) if your
workspace offers it. Bind to ``gemini_io_json``; if the value is a string, ``JSON.parse``
it then render ``request.contents``. Example:

  const root = typeof data === "string" ? JSON.parse(data) : data;
  return (
    <pre className="text-xs whitespace-pre-wrap">{JSON.stringify(root.request?.contents, null, 2)}</pre>
  );

Env:
  TAU2_GEMINI_LOGFIRE_IO=0           — disable Logfire raw I/O (default: **on** — ``logfire.info``
                                     plus optional ``gemini_io_json`` on the generate_content span)
  TAU2_GEMINI_DUMP_PATH=/path.jsonl — append one JSON object per call
  TAU2_GEMINI_LOGFIRE_JSON_PRETTY=0  — minify the string (default: indent=2)
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

__all__ = ["to_jsonable", "log_gemini_generate_io", "logfire_raw_io_enabled"]


def env_on(name: str, default: bool = True) -> bool:
    v = os.environ.get(name, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def logfire_raw_io_enabled() -> bool:
    """Full request/response logging to Logfire (and span attribute when small enough). Opt out: ``TAU2_GEMINI_LOGFIRE_IO=0``."""
    return env_on("TAU2_GEMINI_LOGFIRE_IO", True)


def to_jsonable(o: Any, _depth: int = 0) -> Any:
    if _depth > 40:
        return "<max-depth>"
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, bytes):
        return {
            "_encoding": "base64",
            "_data": base64.standard_b64encode(o).decode("ascii"),
        }
    if isinstance(o, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(x, _depth + 1) for x in o]
    md = getattr(o, "model_dump", None)
    if callable(md):
        for kwargs in (
            {"mode": "json", "exclude_none": True},
            {"exclude_none": True},
        ):
            try:
                return to_jsonable(md(**kwargs), _depth + 1)
            except (TypeError, ValueError):
                continue
    return str(o)


def _dumps(payload: Any) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, bytes):
            return {
                "_encoding": "base64",
                "_data": base64.standard_b64encode(o).decode("ascii"),
            }
        raise TypeError(repr(o))

    indent = 2 if env_on("TAU2_GEMINI_LOGFIRE_JSON_PRETTY", True) else None
    try:
        return json.dumps(payload, ensure_ascii=False, indent=indent, default=default)
    except TypeError:
        return json.dumps(payload, ensure_ascii=False, indent=indent, default=str)


def log_gemini_generate_io(
    *,
    model: str,
    tool_round: int,
    contents: Any,
    config: Any,
    response: Any,
) -> None:
    """Log exactly what was sent to and received from ``models.generate_content``."""
    payload = {
        "tool_round": tool_round,
        "request": {
            "model": model,
            "contents": contents,
            "config": config,
        },
        "response": to_jsonable(response),
    }

    path = os.environ.get("TAU2_GEMINI_DUMP_PATH", "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dumps(payload) + "\n")

    if not logfire_raw_io_enabled():
        return
    try:
        import logfire
    except ImportError:
        return
    logfire.info(
        "gemini.generate_content",
        model=model,
        tool_round=tool_round,
        gemini_io_json=_dumps(payload),
    )
