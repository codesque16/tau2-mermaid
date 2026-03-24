"""Optional per-process API key rotation from simulation YAML.

First list entry is primary. On HTTP 429 / rate-limit errors, advance to the next key for
subsequent calls. If a backup key hits a non-rate-limit error, switch back to primary and set
a flag so the next 429 on primary skips straight to index 2 when available, else index 1.

YAML entries may be literal keys or env var names (``ALL_CAPS_WITH_UNDERSCORES`` only).
"""

from __future__ import annotations

import logging
import re
import os
import threading
from typing import Any, Callable

from dotenv import dotenv_values

_logger = logging.getLogger(__name__)

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

_locks: dict[str, threading.Lock] = {
    "gemini": threading.Lock(),
    "openai": threading.Lock(),
    "anthropic": threading.Lock(),
}

_gemini: "_KeyRotator | None" = None
_openai: "_KeyRotator | None" = None
_anthropic: "_KeyRotator | None" = None
_strict_dotenv_only = False
_dotenv_path: str | None = None
_dotenv_cache: dict[str, str] = {}
_dotenv_cache_path: str | None = None


def _resolve_entry(entry: str) -> str:
    s = str(entry).strip()
    if not s:
        return ""
    if _ENV_NAME_RE.fullmatch(s):
        return (os.environ.get(s) or "").strip()
    return s


def set_strict_dotenv_only(enabled: bool, dotenv_path: str | None = None) -> None:
    """When enabled, read API keys only from ``.env`` (ignore inherited process env)."""
    global _strict_dotenv_only, _dotenv_path, _dotenv_cache, _dotenv_cache_path
    _strict_dotenv_only = bool(enabled)
    _dotenv_path = str(dotenv_path).strip() if dotenv_path else None
    _dotenv_cache = {}
    _dotenv_cache_path = None


def _dotenv_values_cached() -> dict[str, str]:
    """Cached ``dotenv_values`` lookup for strict dotenv-only mode."""
    global _dotenv_cache, _dotenv_cache_path
    path = _dotenv_path or ".env"
    if _dotenv_cache and _dotenv_cache_path == path:
        return _dotenv_cache
    raw = dotenv_values(path)
    out: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            continue
        out[str(k)] = str(v).strip()
    _dotenv_cache = out
    _dotenv_cache_path = path
    return out


def _read_env_value(name: str) -> str:
    """Read from process env, or strictly from .env when configured."""
    if _strict_dotenv_only:
        return (_dotenv_values_cached().get(name) or "").strip()
    return (os.environ.get(name) or "").strip()


def _first_non_empty(names: list[str]) -> str:
    for n in names:
        v = _read_env_value(n)
        if v:
            return v
    return ""


class _KeyRotator:
    __slots__ = ("_keys", "_idx", "_returned_from_backup")

    def __init__(self, entries: list[Any]) -> None:
        keys: list[str] = []
        for e in entries:
            if e is None:
                continue
            v = _resolve_entry(str(e))
            if v:
                keys.append(v)
        self._keys = keys
        self._idx = 0
        self._returned_from_backup = False

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    def current(self) -> str:
        if not self._keys:
            return ""
        return self._keys[min(self._idx, len(self._keys) - 1)]

    def _on_rate_limit_unlocked(self) -> None:
        n = len(self._keys)
        if n <= 1:
            return
        if self._idx == 0 and self._returned_from_backup:
            self._idx = 2 if n > 2 else 1
            self._returned_from_backup = False
        else:
            self._idx = min(self._idx + 1, n - 1)


def configure_from_simulation_dict(cfg: dict[str, Any] | None) -> None:
    """Load ``api_key_rotation`` from a merged simulation YAML dict. Safe to call repeatedly."""
    global _gemini, _openai, _anthropic
    root_cfg = cfg or {}
    strict = bool(
        root_cfg.get("strict_dotenv_only")
        or root_cfg.get("dotenv_only")
        or ((root_cfg.get("gepa") or {}).get("strict_dotenv_only") if isinstance(root_cfg.get("gepa"), dict) else False)
        or ((root_cfg.get("domain") or {}).get("strict_dotenv_only") if isinstance(root_cfg.get("domain"), dict) else False)
    )
    dotenv_path = root_cfg.get("dotenv_path")
    if dotenv_path is None and isinstance(root_cfg.get("gepa"), dict):
        dotenv_path = (root_cfg.get("gepa") or {}).get("dotenv_path")
    if dotenv_path is None and isinstance(root_cfg.get("domain"), dict):
        dotenv_path = (root_cfg.get("domain") or {}).get("dotenv_path")
    set_strict_dotenv_only(strict, str(dotenv_path) if dotenv_path else None)

    block = root_cfg.get("api_key_rotation")
    if not isinstance(block, dict):
        _gemini = _openai = _anthropic = None
        return

    def _list_for(key: str) -> list[Any]:
        v = block.get(key)
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return list(v)
        return [v]

    g = _list_for("gemini")
    o = _list_for("openai")
    a = _list_for("anthropic")
    _gemini = _KeyRotator(g) if g else None
    _openai = _KeyRotator(o) if o else None
    _anthropic = _KeyRotator(a) if a else None
    for label, rot in ("gemini", _gemini), ("openai", _openai), ("anthropic", _anthropic):
        if rot and not rot.configured:
            _logger.warning(
                "api_key_rotation.%s: no resolved keys (check env vars or literals).",
                label,
            )


def is_rate_limit_error(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    code = getattr(exc, "code", None)
    if code == 429 or str(code) == "429":
        return True
    name = type(exc).__name__
    if "RateLimit" in name or "ResourceExhausted" in name:
        return True
    low = str(exc).lower()
    if "429" in low and any(
        x in low for x in ("rate", "quota", "limit", "resource", "exhausted")
    ):
        return True
    return False


def mask_secret(value: str, *, head: int = 4, tail: int = 4) -> str:
    """Return a non-reversible fingerprint for logs (first ``head`` + last ``tail`` chars, never the full secret)."""
    s = (value or "").strip()
    if not s:
        return ""
    n = len(s)
    need = head + tail + 1
    if n <= need:
        return f"<{n} chars>"
    return f"{s[:head]}…{s[-tail:]} (len={n})"


def get_gemini_api_key(scope: str | None = None) -> str:
    if _gemini and _gemini.configured:
        with _locks["gemini"]:
            return _gemini.current()
    s = (scope or "").strip().lower()
    if s == "assistant":
        return _first_non_empty(
            [
                "GOOGLE_API_KEY_ASSISTANT",
                "GEMINI_API_KEY_ASSISTANT",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
            ]
        )
    if s in ("gepa", "optimizer", "reflection", "diagnosis"):
        return _first_non_empty(
            [
                "GOOGLE_API_KEY_GEPA",
                "GEMINI_API_KEY_GEPA",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
            ]
        )
    return _first_non_empty(["GOOGLE_API_KEY", "GEMINI_API_KEY"])


def get_gemini_api_key_masked() -> str:
    """``mask_secret(get_gemini_api_key())`` — which key rotation slot is active for the next Gemini call."""
    return mask_secret(get_gemini_api_key())


def get_openai_api_key_masked() -> str:
    return mask_secret(get_openai_api_key())


def get_anthropic_api_key_masked() -> str:
    return mask_secret(get_anthropic_api_key())


def api_key_masked_for_litellm_model(model: str) -> str:
    """Best-effort masked API key for a LiteLLM ``model`` id (rotation-aware)."""
    m = (model or "").strip().lower()
    if not m:
        return ""
    if m.startswith("anthropic/") or m.startswith("claude-"):
        return get_anthropic_api_key_masked()
    if m.startswith("gemini/") or m.startswith("google/") or m.startswith("vertex_ai/"):
        return get_gemini_api_key_masked()
    if m.startswith("openai/") or m.startswith("azure") or m.startswith("text-completion-openai/"):
        return get_openai_api_key_masked()
    tail = m.split("/")[-1]
    if "claude" in tail or tail.startswith("claude"):
        return get_anthropic_api_key_masked()
    if "gemini" in tail:
        return get_gemini_api_key_masked()
    return get_openai_api_key_masked()


def get_openai_api_key(scope: str | None = None) -> str:
    if _openai and _openai.configured:
        with _locks["openai"]:
            return _openai.current()
    s = (scope or "").strip().lower()
    if s == "assistant":
        return _first_non_empty(["OPENAI_API_KEY_ASSISTANT", "OPENAI_API_KEY"])
    if s in ("gepa", "optimizer", "reflection", "diagnosis"):
        return _first_non_empty(["OPENAI_API_KEY_GEPA", "OPENAI_API_KEY"])
    return _first_non_empty(["OPENAI_API_KEY"])


def get_anthropic_api_key(scope: str | None = None) -> str:
    if _anthropic and _anthropic.configured:
        with _locks["anthropic"]:
            return _anthropic.current()
    s = (scope or "").strip().lower()
    if s == "assistant":
        return _first_non_empty(["ANTHROPIC_API_KEY_ASSISTANT", "ANTHROPIC_API_KEY"])
    if s in ("gepa", "optimizer", "reflection", "diagnosis"):
        return _first_non_empty(["ANTHROPIC_API_KEY_GEPA", "ANTHROPIC_API_KEY"])
    return _first_non_empty(["ANTHROPIC_API_KEY"])


def maybe_rotate_after_provider_error(
    provider: str,
    exc: BaseException,
    *,
    invalidate_client: Callable[[], None] | None = None,
) -> bool:
    """Update rotation state from ``exc``. Return True if the caller should retry the request."""
    rot: _KeyRotator | None
    if provider == "gemini":
        rot = _gemini
    elif provider == "openai":
        rot = _openai
    elif provider == "anthropic":
        rot = _anthropic
    else:
        return False
    if rot is None or not rot.configured:
        return False

    lock = _locks[provider]
    if is_rate_limit_error(exc):
        with lock:
            if len(rot._keys) <= 1:
                return False
            rot._on_rate_limit_unlocked()
        if invalidate_client:
            invalidate_client()
        return True

    with lock:
        if rot._idx <= 0:
            return False
        rot._idx = 0
        rot._returned_from_backup = True
    if invalidate_client:
        invalidate_client()
    return True
