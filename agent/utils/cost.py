"""Token usage and cost tracking for Claude (Anthropic) and Gemini (Google) APIs."""

from typing import Any


# Pricing per million tokens (USD).
# Format: (base_input, output, cache_hits_and_refreshes, cache_write_5m).
# For Gemini we use cache=0; for Claude we use 5m cache write tier only.
# Sources: https://www.anthropic.com/pricing , https://ai.google.dev/gemini-api/docs/pricing ,
# https://openai.com/api/pricing/
PRICING_PER_MILLION: dict[str, tuple[float, float, float, float]] = {
    # --- OpenAI Standard tier (input, output, cache=0); https://developers.openai.com/api/docs/pricing ---
    "gpt-4o": (2.50, 10.0, 0.0, 0.0),
    "gpt-4o-mini": (0.15, 0.60, 0.0, 0.0),
    "gpt-4.1": (2.00, 8.00, 0.0, 0.0),
    "gpt-4.1-mini": (0.40, 1.60, 0.0, 0.0),
    "gpt-4-turbo": (10.0, 30.0, 0.0, 0.0),
    "gpt-3.5-turbo": (0.50, 1.50, 0.0, 0.0),
    # --- Gemini (Standard tier, text; no context cache in this table) ---
    "gemini-3-pro-preview": (2.0, 12.0, 0.0, 0.0),
    "gemini-3-flash-preview": (0.50, 3.0, 0.0, 0.0),
    "gemini-2.5-pro": (1.25, 10.0, 0.0, 0.0),
    "gemini-2.5-flash": (0.30, 2.50, 0.0, 0.0),
    "gemini-2.5-flash-preview-09-2025": (0.30, 2.50, 0.0, 0.0),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.0, 0.0),
    "gemini-2.5-flash-lite-preview-09-2025": (0.10, 0.40, 0.0, 0.0),
    "gemini-2.0-flash": (0.10, 0.40, 0.0, 0.0),
    # --- Claude ---
    # Claude Opus 4.6 / 4.5 — $5 / $6.25 / $0.50 / $25
    "claude-opus-4-6": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4": (5.0, 25.0, 0.50, 6.25),
    # Claude Opus 4.1 / 4 (older) — $15 / $18.75 / $1.50 / $75
    "claude-opus-4-1": (15.0, 75.0, 1.50, 18.75),
    "claude-3-opus-20240229": (15.0, 75.0, 1.50, 18.75),
    # Claude Opus 3 (deprecated) — same as Opus 4.1
    "claude-3-opus": (15.0, 75.0, 1.50, 18.75),
    # Claude Sonnet 4.5 / 4 / 3.7 — $3 / $3.75 / $0.30 / $15
    "claude-sonnet-4-5-20250929": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),
    "claude-3-5-sonnet-20241022": (3.0, 15.0, 0.30, 3.75),
    "claude-3-7-sonnet-20250219": (3.0, 15.0, 0.30, 3.75),
    # Claude Haiku 4.5 — $1 / $1.25 / $0.10 / $5
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "claude-3-5-haiku": (1.0, 5.0, 0.10, 1.25),
    # Claude Haiku 3.5 — $0.80 / $1 / $0.08 / $4
    "claude-3-5-haiku-20241022": (0.80, 4.0, 0.08, 1.0),
    # Claude Haiku 3 — $0.25 / $0.30 / $0.03 / $1.25
    "claude-3-haiku-20240307": (0.25, 1.25, 0.03, 0.30),
    "claude-3-haiku": (0.25, 1.25, 0.03, 0.30),
}
DEFAULT_PRICING = (3.0, 15.0, 0.30, 3.75)  # Claude Sonnet 4.5/4
DEFAULT_PRICING_GEMINI = (0.30, 2.50, 0.0, 0.0)  # Gemini 2.5 Flash
DEFAULT_PRICING_OPENAI = (0.50, 1.50, 0.0, 0.0)  # GPT-3.5


def usage_from_response(usage: Any) -> dict[str, int]:
    """Extract usage dict from Anthropic API response.usage."""
    if usage is None:
        return {}
    out: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = getattr(usage, key, None)
        if val is not None:
            out[key] = int(val)
    # Normalize to zero if missing
    out.setdefault("input_tokens", 0)
    out.setdefault("output_tokens", 0)
    out.setdefault("cache_creation_input_tokens", 0)
    out.setdefault("cache_read_input_tokens", 0)
    return out


def compute_cost(model: str, usage: dict[str, int]) -> float:
    """Compute cost in USD for the given usage. Usage keys: input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens."""
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache_create = usage.get("cache_creation_input_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    # Effective input = non-cached
    effective_input = max(0, inp - cache_create - cache_read)
    model_lower = model.strip().lower()
    pricing = PRICING_PER_MILLION.get(model) or PRICING_PER_MILLION.get(model_lower)
    if pricing is None:
        if model_lower.startswith("gemini-"):
            pricing = DEFAULT_PRICING_GEMINI
        elif model_lower.startswith("gpt-"):
            pricing = DEFAULT_PRICING_OPENAI
        else:
            pricing = DEFAULT_PRICING
    input_per_m, output_per_m, cache_read_per_m, cache_write_per_m = pricing
    cost = (
        (effective_input / 1_000_000) * input_per_m
        + (out / 1_000_000) * output_per_m
        + (cache_read / 1_000_000) * cache_read_per_m
        + (cache_create / 1_000_000) * cache_write_per_m
    )
    return round(cost, 6)


def accumulate_usage(acc: dict[str, int], u: dict[str, int]) -> None:
    """Add usage u into acc in place."""
    for k, v in u.items():
        acc[k] = acc.get(k, 0) + v


def usage_from_openai_response(usage: Any) -> dict[str, int]:
    """Build our standard usage dict from OpenAI completion usage (prompt_tokens, completion_tokens)."""
    if usage is None:
        return {}
    inp = getattr(usage, "prompt_tokens", None)
    out = getattr(usage, "completion_tokens", None)
    if isinstance(usage, dict):
        inp = inp or usage.get("prompt_tokens")
        out = out or usage.get("completion_tokens")
    inp = int(inp) if inp is not None else 0
    out = int(out) if out is not None else 0
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def usage_from_gemini_response(response: Any) -> dict[str, int]:
    """Build our standard usage dict from a Gemini generate_content response (usage_metadata)."""
    if response is None:
        return {}
    meta = getattr(response, "usage_metadata", None) or getattr(response, "usage", None)
    if meta is None:
        return {}
    prompt = getattr(meta, "prompt_token_count", None) or getattr(meta, "promptTokenCount", None) or getattr(meta, "input_tokens", None)
    candidates = getattr(meta, "candidates_token_count", None) or getattr(meta, "candidatesTokenCount", None) or getattr(meta, "output_tokens", None)
    total = getattr(meta, "total_token_count", None) or getattr(meta, "totalTokenCount", None)
    inp = int(prompt) if prompt is not None else 0
    out_val = int(candidates) if candidates is not None else (int(total) - inp if total is not None and prompt is not None else 0)
    out_val = max(0, out_val)
    return {
        "input_tokens": inp,
        "output_tokens": out_val,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
