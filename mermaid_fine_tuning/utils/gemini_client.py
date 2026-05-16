"""Gemini client for the SOP-agent data-gen pipeline.

Calls Vertex Gemini directly via google-genai. No logfire dependency.

What this gives you:
  - Direct client.models.generate_content (no wrapper indirection)
  - Truncated exponential backoff for 429 / 5xx with env-tunable knobs
  - Error classification: retryable vs permanent, with named exception types
  - Rich console logging: per-call status, token usage, timing, error context
  - A running counter of calls / tokens / cost (if pricing is in llm_args)

Env knobs (all optional):
  SOP_DATAGEN_MAX_RETRIES   default 8
  SOP_DATAGEN_RETRY_BASE_S  default 1.0  (exponential base)
  SOP_DATAGEN_RETRY_MAX_S   default 60.0 (per-attempt cap)
  SOP_DATAGEN_VERBOSE       default 1    ("0" silences per-call logs)
"""
from __future__ import annotations
import os
import re
import time
import threading
from dataclasses import dataclass, field
from typing import Type, Any
from pydantic import BaseModel, ValidationError

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box


# ============================================================
# Exception hierarchy: makes upstream handling crisp
# ============================================================

class GeminiError(Exception):
    """Base for all client errors."""


class GeminiRetryableError(GeminiError):
    """429 / 5xx / overload / transient network. Caller should retry."""


class GeminiPermanentError(GeminiError):
    """400 / 401 / 403 / 404 / safety block / schema mismatch. Do not retry."""


class GeminiSchemaError(GeminiPermanentError):
    """Returned text could not be validated against the requested Pydantic schema."""


class GeminiBlockedError(GeminiPermanentError):
    """Response was blocked by safety filters or returned no candidates."""


# ============================================================
# Singleton console + call statistics
# ============================================================

_console = Console(stderr=False)


@dataclass
class CallStats:
    total_calls: int = 0
    successful_calls: int = 0
    retried_calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thought_tokens: int = 0
    total_seconds: float = 0.0
    by_call_name: dict = field(default_factory=dict)  # call_name -> count
    _lock: Any = field(default_factory=threading.Lock)

    def record(self, *, call_name: str, prompt_tokens: int, completion_tokens: int,
               thought_tokens: int, elapsed_s: float, retries: int, success: bool):
        with self._lock:
            self.total_calls += 1
            if success:
                self.successful_calls += 1
            else:
                self.failed_calls += 1
            if retries > 0:
                self.retried_calls += 1
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.thought_tokens += thought_tokens
            self.total_seconds += elapsed_s
            self.by_call_name[call_name] = self.by_call_name.get(call_name, 0) + 1

    def render_summary(self) -> Table:
        t = Table(title="Gemini call summary", box=box.SIMPLE, show_header=True)
        t.add_column("metric", style="cyan")
        t.add_column("value", justify="right", style="green")
        t.add_row("total calls", str(self.total_calls))
        t.add_row("successful", str(self.successful_calls))
        t.add_row("retried (eventually OK)", str(self.retried_calls))
        t.add_row("failed (gave up)", str(self.failed_calls))
        t.add_row("prompt tokens", f"{self.prompt_tokens:,}")
        t.add_row("completion tokens", f"{self.completion_tokens:,}")
        t.add_row("thought tokens", f"{self.thought_tokens:,}")
        t.add_row("total time (s)", f"{self.total_seconds:,.1f}")
        if self.total_calls:
            t.add_row("avg latency (s)", f"{self.total_seconds / self.total_calls:,.2f}")
        return t


# One stats object per process; data-gen is sequential so this is fine.
STATS = CallStats()


def print_stats_summary():
    """Call this at the end of the pipeline to print a final report."""
    _console.print(STATS.render_summary())
    if STATS.by_call_name:
        t = Table(title="Calls by stage", box=box.SIMPLE)
        t.add_column("call_name", style="cyan")
        t.add_column("count", justify="right", style="green")
        for name, count in sorted(STATS.by_call_name.items(), key=lambda kv: -kv[1]):
            t.add_row(name, str(count))
        _console.print(t)


# ============================================================
# Vertex client setup
# ============================================================

_data_gen_dotenv_loaded = False


def load_data_gen_dotenv() -> None:
    """Load `.env` file(s) into ``os.environ`` so Vertex and optional DATA_GEN_* vars work.

    Loads every candidate path that exists, in broad-to-narrow order; later files override
    earlier ones for the same variable name. Candidates: repo root ``.env``,
    ``tau3-bench-fork/.env``, cwd ``.env``, ``mermaid_fine_tuning/.env``. If none exist,
    falls back to ``python-dotenv``'s default discovery from the current working directory.
    Safe to call multiple times (loads at most once).
    """
    global _data_gen_dotenv_loaded
    if _data_gen_dotenv_loaded:
        return
    _data_gen_dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    mft_root = os.path.dirname(utils_dir)
    repo_root = os.path.dirname(mft_root)
    paths = (
        os.path.join(repo_root, ".env"),
        os.path.join(repo_root, "tau3-bench-fork", ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(mft_root, ".env"),
    )
    found = False
    for path in paths:
        if os.path.isfile(path):
            load_dotenv(path, override=True)
            found = True
    if not found:
        load_dotenv()


def build_vertex_genai_client() -> genai.Client:
    """Construct the Vertex-backed genai.Client. Mirrors VertexUserSimulator._get_client.

    Note: if both GOOGLE_API_KEY/GEMINI_API_KEY AND VERTEXAI_PROJECT are set, the
    google-genai SDK silently prefers the API-key path. We explicitly null those
    out for this process so vertexai=True actually wins.
    """
    load_data_gen_dotenv()
    project = os.environ.get("VERTEXAI_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("VERTEXAI_LOCATION") or "global"
    if not project:
        raise ValueError(
            "VERTEXAI_PROJECT (or GOOGLE_CLOUD_PROJECT) must be set for the data-gen client."
        )
    # Suppress API-key auth path. We want Vertex creds (ADC).
    for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        if key in os.environ:
            _console.print(
                f"[yellow]ℹ[/yellow] unsetting {key} for this process "
                f"(would otherwise override vertexai=True)"
            )
            del os.environ[key]
    return genai.Client(vertexai=True, project=project, location=location)


def resolve_model_name(llm: str) -> str:
    """Strip provider prefixes the same way VertexUserSimulator does."""
    model = (llm or "").strip()
    if model.startswith("vertex_ai/"):
        return model.removeprefix("vertex_ai/")
    if model.startswith("gemini/"):
        return model.removeprefix("gemini/")
    return model


# ============================================================
# Error classification
# ============================================================

# HTTP / API codes that are worth retrying
_RETRYABLE_CODES = {408, 425, 429, 500, 502, 503, 504}
_RETRYABLE_SUBSTRINGS = (
    "RESOURCE_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "Too Many Requests",
    "Service Unavailable",
    "Internal Server Error",
    "overloaded",
    "TimeoutError",
)


def _classify_exception(exc: BaseException) -> tuple[bool, str]:
    """Return (is_retryable, short_reason) for an exception from the genai SDK.

    Used both for retry decisions and for rich console output.
    """
    # Google genai APIError carries a numeric code
    if isinstance(exc, genai_errors.APIError):
        code = int(getattr(exc, "code", 0) or 0)
        msg = str(exc)
        if code in _RETRYABLE_CODES:
            return True, f"APIError {code}"
        if any(s in msg for s in _RETRYABLE_SUBSTRINGS):
            return True, f"APIError {code} ({msg.split(chr(10))[0][:60]})"
        return False, f"APIError {code} (permanent)"

    # Generic exceptions: substring sniff as a last resort
    msg = str(exc)
    if any(s in msg for s in _RETRYABLE_SUBSTRINGS):
        return True, f"{type(exc).__name__}: transient"
    return False, type(exc).__name__


def _extract_retry_delay_s(exc: BaseException) -> float | None:
    """Some Google errors include a 'Retry after Xs' hint in the message."""
    m = re.search(r"[Rr]etry (?:after|in) ([0-9.]+)\s*s", str(exc))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ============================================================
# Response extraction
# ============================================================

def _usage_tokens(response: Any) -> tuple[int, int, int]:
    """Return (prompt, completion, thought) token counts from a response."""
    u = getattr(response, "usage_metadata", None)
    if u is None:
        return 0, 0, 0
    prompt = int(getattr(u, "prompt_token_count", None) or
                 getattr(u, "input_token_count", None) or 0)
    completion = int(getattr(u, "candidates_token_count", None) or
                     getattr(u, "output_token_count", None) or 0)
    thought = int(getattr(u, "thoughts_token_count", None) or 0)
    return prompt, completion, thought


def _extract_text(response: Any) -> str:
    """Walk candidates[0].content.parts and concatenate non-thought text."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        t = getattr(response, "text", None)
        return t or ""

    parts = getattr(candidates[0].content, "parts", None) or []
    text_parts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if not isinstance(text, str) or not text:
            continue
        if bool(getattr(part, "thought", False)):
            continue
        text_parts.append(text)
    return "".join(text_parts)


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "no_candidates"
    fr = getattr(candidates[0], "finish_reason", None)
    return str(fr) if fr is not None else ""


# ============================================================
# The client
# ============================================================

def _verbose() -> bool:
    return os.environ.get("SOP_DATAGEN_VERBOSE", "1").strip().lower() not in {"0", "false", "no", "off"}


class GeminiClient:
    """Direct-SDK Gemini client with retry + rich logging.

    No logfire. No second wrapper. One construction per pipeline run.
    """

    def __init__(
        self,
        llm: str,
        llm_args: dict | None = None,
        actor: str = "data_gen",
    ):
        self.model = resolve_model_name(llm)
        self.llm_args: dict = dict(llm_args or {})
        self.actor = actor
        self._client = build_vertex_genai_client()

        self._max_retries = max(1, int(os.environ.get("SOP_DATAGEN_MAX_RETRIES", "8")))
        self._retry_base = float(os.environ.get("SOP_DATAGEN_RETRY_BASE_S", "1.0"))
        self._retry_cap = float(os.environ.get("SOP_DATAGEN_RETRY_MAX_S", "60.0"))

        if _verbose():
            _console.print(Panel.fit(
                f"[bold cyan]GeminiClient[/bold cyan] ready\n"
                f"  model: [yellow]{self.model}[/yellow]\n"
                f"  actor: [yellow]{self.actor}[/yellow]\n"
                f"  retries: [yellow]{self._max_retries}[/yellow] "
                f"(base={self._retry_base}s cap={self._retry_cap}s)\n"
                f"  llm_args: {self.llm_args}",
                title="data-gen client", border_style="cyan"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_text(
        self,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        call_name: str = "data_gen_text",
    ) -> str:
        config = self._build_config(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        response = self._call_with_retries(prompt, config, call_name)
        return _extract_text(response)

    def generate_structured(
        self,
        prompt: str,
        response_schema: Type[BaseModel],
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        call_name: str = "data_gen_structured",
    ) -> BaseModel:
        config = self._build_config(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=response_schema,
        )
        response = self._call_with_retries(prompt, config, call_name)
        raw_text = _extract_text(response)
        try:
            return response_schema.model_validate_json(raw_text)
        except ValidationError as e:
            # Schema mismatches are permanent for this call. Caller decides
            # whether to skip the example or retry with a tweaked prompt.
            preview = raw_text[:500].replace("\n", " ")
            _console.print(
                f"[red]✗ schema validation failed[/red] [{call_name}] "
                f"errors={len(e.errors())}; first 500 chars: [dim]{preview}[/dim]"
            )
            raise GeminiSchemaError(
                f"Response did not match {response_schema.__name__}: {e}"
            ) from e

    # ------------------------------------------------------------------
    # Core: retry loop with classification
    # ------------------------------------------------------------------

    def _call_with_retries(self, prompt: str, config: Any, call_name: str) -> Any:
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        start_total = time.perf_counter()
        last_exc: BaseException | None = None
        verbose = _verbose()

        for attempt in range(self._max_retries):
            try:
                t0 = time.perf_counter()
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                elapsed = time.perf_counter() - t0

                # Check for empty / blocked responses up front
                fr = _finish_reason(response)
                if fr in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"}:
                    if verbose:
                        _console.print(
                            f"[red]✗[/red] [{call_name}] blocked by safety filter ({fr})"
                        )
                    STATS.record(call_name=call_name, prompt_tokens=0,
                                 completion_tokens=0, thought_tokens=0,
                                 elapsed_s=elapsed, retries=attempt, success=False)
                    raise GeminiBlockedError(f"Response blocked: finish_reason={fr}")

                prompt_tok, completion_tok, thought_tok = _usage_tokens(response)
                STATS.record(call_name=call_name, prompt_tokens=prompt_tok,
                             completion_tokens=completion_tok, thought_tokens=thought_tok,
                             elapsed_s=elapsed, retries=attempt, success=True)

                if verbose:
                    retry_note = f" [yellow](after {attempt} retr{'y' if attempt == 1 else 'ies'})[/yellow]" if attempt else ""
                    thought_note = f" thought={thought_tok}" if thought_tok else ""
                    _console.print(
                        f"[green]✓[/green] [{call_name}] {elapsed:.2f}s  "
                        f"in={prompt_tok} out={completion_tok}{thought_note}"
                        f"  finish={fr}{retry_note}"
                    )

                return response

            except GeminiBlockedError:
                raise  # already logged and recorded above

            except Exception as e:
                last_exc = e
                retryable, reason = _classify_exception(e)
                if not retryable or attempt >= self._max_retries - 1:
                    # Final failure
                    elapsed_total = time.perf_counter() - start_total
                    STATS.record(call_name=call_name, prompt_tokens=0,
                                 completion_tokens=0, thought_tokens=0,
                                 elapsed_s=elapsed_total, retries=attempt,
                                 success=False)
                    if verbose:
                        _console.print(
                            f"[red]✗ giving up[/red] [{call_name}] "
                            f"after {attempt + 1} attempt(s): "
                            f"[dim]{reason} — {str(e)[:200]}[/dim]"
                        )
                    if retryable:
                        raise GeminiRetryableError(
                            f"Exhausted {self._max_retries} retries: {e}"
                        ) from e
                    raise GeminiPermanentError(f"{reason}: {e}") from e

                # Schedule next attempt
                delay = min(self._retry_cap, self._retry_base * (2 ** attempt))
                suggested = _extract_retry_delay_s(e)
                if suggested is not None:
                    delay = max(delay, min(self._retry_cap, suggested))

                if verbose:
                    _console.print(
                        f"[yellow]⟲[/yellow] [{call_name}] "
                        f"attempt {attempt + 1}/{self._max_retries} failed "
                        f"([dim]{reason}[/dim]); retrying in {delay:.1f}s"
                    )
                time.sleep(delay)

        # Defensive: loop exited without return — shouldn't happen
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Config builder
    # ------------------------------------------------------------------

    def _build_config(
        self,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_mime_type: str | None = None,
        response_schema: Type[BaseModel] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}

        if system_instruction is not None:
            kwargs["system_instruction"] = system_instruction

        temp = temperature
        if temp is None:
            temp = self.llm_args.get("temperature")
        if temp is not None:
            kwargs["temperature"] = float(temp)

        mot = max_output_tokens
        if mot is None and self.llm_args.get("max_tokens") is not None:
            mot = int(self.llm_args["max_tokens"])
        if mot is not None:
            kwargs["max_output_tokens"] = int(mot)

        if response_mime_type is not None:
            kwargs["response_mime_type"] = response_mime_type
        if response_schema is not None:
            kwargs["response_schema"] = response_schema

        thinking = self._resolve_thinking_config()
        if thinking is not None:
            kwargs["thinking_config"] = thinking

        seed = self.llm_args.get("seed")
        if seed is not None:
            kwargs["seed"] = int(seed)

        return types.GenerateContentConfig(**kwargs)

    def _resolve_thinking_config(self) -> Any | None:
        reasoning_level = self.llm_args.get("reasoning_level")
        if reasoning_level is None:
            return None
        level = str(reasoning_level).strip().upper()
        if level not in {"LOW", "MEDIUM", "HIGH"}:
            return None
        include_thoughts = bool(self.llm_args.get("include_thoughts", True))
        return types.ThinkingConfig(
            include_thoughts=include_thoughts,
            thinking_level=level,
        )
