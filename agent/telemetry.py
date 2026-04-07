"""Logfire setup helpers; export of spans to Google Cloud Trace (Trace Explorer).

**Default: on.** Disable with ``TAU2_GCP_TRACE=0`` (or ``false`` / ``off`` / ``no``) after
``load_dotenv()``, or pass ``use_gcp_trace=False`` to :func:`configure_logfire_tau2` (e.g.
``gepa.use_gcp_trace`` in YAML).

Uses ``opentelemetry-exporter-gcp-trace``. If the package is missing, Cloud Trace is skipped
(no crash).

Authentication: Application Default Credentials (e.g. ``gcloud auth application-default login``).
The exporter resolves the GCP project from, in order: explicit ``project_id`` passed here,
``GOOGLE_CLOUD_PROJECT`` / ``GCP_PROJECT``, ``OTEL_EXPORTER_GCP_TRACE_PROJECT_ID``, then
``google.auth.default()`` — if Trace Explorer shows the wrong project or no app spans, set
``GOOGLE_CLOUD_PROJECT`` to the console project (e.g. ``gemini-txn``).

Spans use ``service.name`` = ``LOGFIRE_SERVICE_NAME`` or ``tau2-mermaid`` — filter Trace Explorer
by that service; unrelated spans (e.g. ``/welcome``) are from other workloads.

``TAU2_GCP_TRACE_DEBUG=1`` prints the resolved project ID to stderr and enables exporter logging.

**Live traces:** ``TAU2_GCP_TRACE_BSP_DELAY_MS`` (default ``1000``) controls batch export delay to Cloud Trace.
``TAU2_GCP_TRACE_PERIODIC_FLUSH_SEC`` (default ``5``, set ``0`` to disable) calls ``logfire.force_flush()`` on an interval so spans appear during long runs, not only after exit.

**Export stall:** If ADC is expired, ``google-api-core`` may retry gRPC for ~120s per batch, blocking span export
and slowing Logfire. Exports run in a side thread capped by ``TAU2_GCP_TRACE_EXPORT_TIMEOUT_SEC`` (default ``8``);
after a timeout the Cloud Trace exporter is disabled for the rest of the process; Logfire → logfire.dev keeps working.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

from opentelemetry.sdk.trace.export import ReadableSpan, SpanExporter, SpanExportResult

__all__ = [
    "configure_logfire_tau2",
    "gcp_cloud_trace_enabled",
    "merge_gcp_trace_into_logfire_kwargs",
]


_GCP_TRACE_OFF = frozenset({"0", "false", "no", "off"})
_ATEXIT_FLUSH_REGISTERED = False
_logger = logging.getLogger(__name__)

# Single worker so at most one Cloud Trace export runs at a time (avoids pile-ups when GCP hangs).
_CLOUD_TRACE_EXECUTOR: ThreadPoolExecutor | None = None


def _cloud_trace_executor() -> ThreadPoolExecutor:
    global _CLOUD_TRACE_EXECUTOR
    if _CLOUD_TRACE_EXECUTOR is None:
        _CLOUD_TRACE_EXECUTOR = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tau2-gcp-trace",
        )
    return _CLOUD_TRACE_EXECUTOR


class _CloudTraceExportGuard(SpanExporter):
    """Run Cloud Trace ``export`` off the hot path with a wall-clock limit.

    Without this, failed ADC / gRPC can block inside ``batch_write_spans`` for ~120s per call,
    stalling OpenTelemetry processors and making Logfire traces appear stuck.
    """

    def __init__(self, inner: SpanExporter, *, timeout_sec: float) -> None:
        self._inner = inner
        self._timeout = max(1.0, timeout_sec)
        self._disabled = False
        self._warned = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._disabled or not spans:
            return SpanExportResult.SUCCESS

        fut = _cloud_trace_executor().submit(self._inner.export, spans)
        try:
            return fut.result(timeout=self._timeout)
        except FuturesTimeoutError:
            self._disable_once(
                f"Cloud Trace export timed out after {self._timeout:.0f}s "
                "(GCP client may retry gRPC up to ~120s)."
            )
            return SpanExportResult.SUCCESS
        except Exception:
            if not self._warned:
                self._warned = True
                self._disabled = True
                _logger.exception(
                    "Cloud Trace export failed; disabling Cloud Trace for this process "
                    "(Logfire → logfire.dev unchanged). "
                    "Fix: `gcloud auth application-default login`, or TAU2_GCP_TRACE=0 / gepa.use_gcp_trace: false."
                )
            return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._disabled:
            return True
        inner_flush = getattr(self._inner, "force_flush", None)
        if inner_flush is None:
            return True
        fut = _cloud_trace_executor().submit(inner_flush, timeout_millis)
        try:
            return bool(fut.result(timeout=self._timeout))
        except Exception:
            return True

    def _disable_once(self, reason: str) -> None:
        if self._warned:
            return
        self._warned = True
        self._disabled = True
        _logger.warning(
            "%s Disabling Cloud Trace for this process (Logfire → logfire.dev unchanged). "
            "Fix: `gcloud auth application-default login`, or set TAU2_GCP_TRACE=0 / gepa.use_gcp_trace: false.",
            reason,
        )


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def resolved_gcp_trace_project() -> str | None:
    """Best-effort project ID spans are written to (for matching Trace Explorer)."""
    pid = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
        or os.environ.get("OTEL_EXPORTER_GCP_TRACE_PROJECT_ID")
        or ""
    ).strip()
    if pid:
        return pid
    try:
        from google.auth import default as google_auth_default

        _, project = google_auth_default()
        return str(project) if project else None
    except Exception:
        return None


def _maybe_gcp_trace_debug_logging() -> None:
    if not _env_truthy("TAU2_GCP_TRACE_DEBUG"):
        return
    logging.getLogger("opentelemetry.exporter.cloud_trace").setLevel(logging.DEBUG)
    logging.getLogger("google.cloud.trace_v2").setLevel(logging.DEBUG)
    logging.getLogger("google.api_core").setLevel(logging.INFO)


def _register_logfire_flush_atexit() -> None:
    global _ATEXIT_FLUSH_REGISTERED
    if _ATEXIT_FLUSH_REGISTERED:
        return
    _ATEXIT_FLUSH_REGISTERED = True
    import logfire

    def _flush() -> None:
        try:
            logfire.force_flush()
        except Exception:
            pass

    atexit.register(_flush)


def gcp_cloud_trace_enabled(*, explicit: bool | None = None) -> bool:
    """Whether Cloud Trace export is on. *explicit* overrides env when not None.

    If ``TAU2_GCP_TRACE`` is unset, export is **on** (opt-out). Set to ``0`` / ``false`` /
    ``off`` / ``no`` to disable.
    """
    if explicit is not None:
        return bool(explicit)
    v = os.environ.get("TAU2_GCP_TRACE", "").strip().lower()
    if not v:
        return True
    if v in _GCP_TRACE_OFF:
        return False
    return v in ("1", "true", "yes", "on")


def _gcp_bsp_schedule_delay_ms() -> float:
    raw = os.environ.get("TAU2_GCP_TRACE_BSP_DELAY_MS", "1000").strip()
    try:
        v = float(raw)
        return max(100.0, min(v, 60_000.0))
    except ValueError:
        return 1000.0


def _gcp_trace_immediate_export() -> bool:
    """Export Cloud Trace on every ended span (default on)."""
    raw = os.environ.get("TAU2_GCP_TRACE_IMMEDIATE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _gcp_trace_export_timeout_sec() -> float:
    raw = os.environ.get("TAU2_GCP_TRACE_EXPORT_TIMEOUT_SEC", "8").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 8.0


def _gcp_trace_span_processors() -> list[Any]:
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    # Prefer explicit env; else let CloudTraceSpanExporter use OTEL_EXPORTER_GCP_TRACE_* and
    # google.auth.default() (see opentelemetry.exporter.cloud_trace.CloudTraceSpanExporter).
    pid = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
        or ""
    ).strip()
    inner = CloudTraceSpanExporter(project_id=pid) if pid else CloudTraceSpanExporter()
    exporter: SpanExporter = _CloudTraceExportGuard(inner, timeout_sec=_gcp_trace_export_timeout_sec())
    # Immediate export gives near-live visibility in Trace Explorer.
    if _gcp_trace_immediate_export():
        return [SimpleSpanProcessor(exporter)]
    # Default 1s (not OTEL's 5s) for batch mode.
    delay = _gcp_bsp_schedule_delay_ms()
    return [BatchSpanProcessor(exporter, schedule_delay_millis=delay)]


def _start_periodic_cloud_trace_flush() -> None:
    """Background flush so batches reach GCP during long runs (not only at exit)."""
    raw = os.environ.get("TAU2_GCP_TRACE_PERIODIC_FLUSH_SEC", "5").strip().lower()
    if raw in ("0", "false", "no", "off", ""):
        return
    try:
        interval = float(raw)
    except ValueError:
        interval = 5.0
    if interval <= 0:
        return

    import threading
    import time

    def _loop() -> None:
        import logfire

        while True:
            try:
                time.sleep(interval)
                logfire.force_flush()
            except Exception:
                break

    threading.Thread(target=_loop, name="tau2-gcp-trace-flush", daemon=True).start()


def merge_gcp_trace_into_logfire_kwargs(
    kwargs: dict[str, Any],
    *,
    use_gcp_trace: bool | None = None,
) -> bool:
    """Mutate *kwargs* for ``logfire.configure`` with optional Cloud Trace processors.

    Returns whether a Cloud Trace :class:`BatchSpanProcessor` was added.
    """
    if not gcp_cloud_trace_enabled(explicit=use_gcp_trace):
        return False
    try:
        extra = _gcp_trace_span_processors()
    except ImportError:
        return False
    merged = list(kwargs.get("additional_span_processors") or [])
    merged.extend(extra)
    kwargs["additional_span_processors"] = merged
    return True


def configure_logfire_tau2(
    *,
    use_gcp_trace: bool | None = None,
    **kwargs: Any,
) -> None:
    """``logfire.configure`` plus optional Google Cloud Trace via ``additional_span_processors``."""
    import logfire

    kwargs.setdefault("service_name", os.environ.get("LOGFIRE_SERVICE_NAME", "tau2-mermaid"))
    added_gcp = merge_gcp_trace_into_logfire_kwargs(kwargs, use_gcp_trace=use_gcp_trace)
    if added_gcp:
        _maybe_gcp_trace_debug_logging()
        if _env_truthy("TAU2_GCP_TRACE_DEBUG"):
            rp = resolved_gcp_trace_project()
            print(
                "[tau2-mermaid telemetry] Cloud Trace: "
                f"project_id={rp!r}, "
                f"service.name={kwargs.get('service_name')!r} "
                "(filter in Trace Explorer by this service name).",
                file=sys.stderr,
            )
    logfire.configure(**kwargs)
    _register_logfire_flush_atexit()
    if added_gcp:
        _start_periodic_cloud_trace_flush()
