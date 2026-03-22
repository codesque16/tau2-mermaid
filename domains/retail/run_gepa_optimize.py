"""Run GEPA optimization for the retail domain (config-driven).

This script:
  - Reads a GEPA config YAML (domain + assistant + gepa sections)
  - Optimizes only the content inside <instructions> (config: gepa.optimize)
  - Uses train/val split from domain.tasks (gepa.train_ratio or train_task_ids/val_task_ids)
  - Writes the optimized instruction text to gepa.output_instructions_path

Evaluation metric:
  - Score is binary (0 or 1) per task. We compare the final DB state after
    replaying the agent's tool calls vs the golden action sequence (see
    domains/retail/evaluate.py). PASS = hashes match.

Train vs val:
  - Train tasks = used by GEPA to optimize the instructions.
  - Val tasks = held-out; GEPA may use them for validation/early stopping.

Run from project root:

  uv run python -m domains.retail.run_gepa_optimize --config configs/gepa_retail.yaml

Tmux split (GEPA logs in second pane, no Rich jitter):

  ./scripts/run_gepa_tmux.sh [CONFIG]
  # or: --gepa-log-file /tmp/gepa.log then tail -f /tmp/gepa.log in another pane
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import random
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

import logfire
from dotenv import load_dotenv

# --- Chained LLM seed (optional ``gepa.llm_seed_chain``) ----------------------------

_MAX_LLM_SEED = 2**31 - 1


class LLMSeedChain:
    """Thread-safe per-iteration LLM ``seed`` for retail simulation evals inside GEPA.

    - **Before** the first ``on_iteration_start``: ``read()`` returns the **master**
      (``gepa.seed``), used for the initial seed-candidate valset evaluation.
    - **Each** ``on_iteration_start`` with ``iteration >= 1`` (GEPA's 1-based counter):
      ``generated = Random(current).randint(1, 10**9)``,
      ``current = (master + generated) % (2**31 - 1)`` (non-zero).

    All simulator and qualitative-eval LLM calls in that iteration should use
    ``read()`` so the API sees an explicit integer seed.
    """

    def __init__(self, master: int) -> None:
        m = int(master) % _MAX_LLM_SEED
        self._master = m if m != 0 else 1
        self._current = self._master
        self._lock = threading.Lock()

    def read(self) -> int:
        with self._lock:
            return self._current

    def advance_on_iteration_start(self, iteration: int) -> None:
        """Advance after baseline; GEPA passes ``iteration`` = 1 for the first opt loop."""
        if iteration < 1:
            return
        with self._lock:
            prev = self._current
            gen = random.Random(prev).randint(1, 10**9)
            nxt = (self._master + gen) % _MAX_LLM_SEED
            self._current = nxt if nxt != 0 else 1


class _LLMSeedChainCallback:
    def __init__(self, chain: LLMSeedChain) -> None:
        self._chain = chain

    def on_iteration_start(self, event: Dict[str, Any]) -> None:
        try:
            it = int(event.get("iteration", 0))
        except (TypeError, ValueError):
            return
        self._chain.advance_on_iteration_start(it)
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

def _make_litellm_lm_with_span(model_name: str, span_title: str):
    """Wrap LiteLLM completion calls in a clean Logfire span."""
    import litellm

    def _lm(prompt: str | list[dict[str, Any]]) -> str:
        if isinstance(prompt, str):
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        else:
            messages = prompt
        with logfire.span("optimization_completion", _span_name=span_title, model=model_name):
            completion = litellm.completion(model=model_name, messages=messages, temperature=0)
        return completion.choices[0].message.content  # type: ignore[union-attr]

    return _lm

# --- Rich two-panel view state (thread-safe) ---
GEPA_VIEW_LINES = 200
SIM_VIEW_LINES = 200


class ViewState:
    """Thread-safe state for GEPA and simulation panels."""

    def __init__(self, max_metric_calls: int) -> None:
        self._lock = threading.Lock()
        self.gepa_log: List[str] = []
        self.sim_log: List[str] = []
        self.max_metric_calls = max_metric_calls
        self.eval_count = 0
        self.current_stage = "Initializing"
        self.done = threading.Event()

    def add_gepa(self, message: str) -> None:
        with self._lock:
            self.gepa_log.append(message)
            if len(self.gepa_log) > GEPA_VIEW_LINES:
                self.gepa_log.pop(0)

    def add_sim(self, message: str) -> None:
        with self._lock:
            self.sim_log.append(message)
            if len(self.sim_log) > SIM_VIEW_LINES:
                self.sim_log.pop(0)

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.current_stage = stage

    def inc_eval(self) -> None:
        with self._lock:
            self.eval_count += 1

    def snapshot(self) -> tuple[List[str], List[str], str, int]:
        with self._lock:
            return (
                list(self.gepa_log),
                list(self.sim_log),
                self.current_stage,
                self.eval_count,
            )


class GEPAPanelLogger:
    """Logger that forwards to ViewState for the GEPA panel."""

    _ITER_RE = re.compile(r"Iteration\s*[:#]?\s*(\d+)", re.I)

    def __init__(
        self,
        state: ViewState | None,
        iteration_span_manager: Any = None,
        echo_to_console: bool = False,
    ) -> None:
        self._state = state
        self._iter_span_mgr = iteration_span_manager
        self._echo_to_console = echo_to_console

    def log(self, message: str) -> None:
        line = (message or "").strip()
        if self._state is not None:
            self._state.add_gepa(line or "")
        if line and self._echo_to_console:
            # Stream GEPA log lines to stdout with a clear prefix instead of
            # taking over the whole terminal via a Rich live dashboard.
            print(f"[GEPA] {line}")
        if line and self._iter_span_mgr is not None:
            m = self._ITER_RE.search(line)
            if m:
                try:
                    self._iter_span_mgr.on_iteration(int(m.group(1)))
                except Exception:
                    pass


class GEPAFileLogger:
    """Logger that writes GEPA messages to a file (for tmux tail -f) with optional Logfire."""

    _ITER_RE = re.compile(r"Iteration\s*[:#]?\s*(\d+)", re.I)

    def __init__(
        self,
        path: Path,
        max_metric_calls: int = 0,
        logfire_span: Any = None,
        iteration_span_manager: Any = None,
    ) -> None:
        self._path = Path(path)
        self._file = self._path.open("a", encoding="utf-8")
        self._max_metric_calls = max_metric_calls
        self._logfire_span = logfire_span
        self._iter_span_mgr = iteration_span_manager
        self._metric_calls_so_far = 0
        self._lock = threading.Lock()
        self._file.write("[GEPA] Log started. Optimization will begin after setup.\n")
        self._file.flush()

    def log(self, message: str) -> None:
        line = (message or "").strip()
        if line:
            with self._lock:
                self._file.write(line + "\n")
                self._file.flush()
            if self._logfire_span is not None:
                try:
                    self._logfire_span.set_attribute("gepa_last_message", line[:1000])
                    m = self._ITER_RE.search(line)
                    if m:
                        it = int(m.group(1))
                        self._logfire_span.set_attribute("gepa_iteration", it)
                        if self._iter_span_mgr is not None:
                            try:
                                self._iter_span_mgr.on_iteration(it)
                            except Exception:
                                pass
                except Exception:
                    pass

    def log_progress(
        self,
        eval_count: int,
        task_id: Any,
        score: float,
        db_match: bool,
        path_match: bool,
    ) -> None:
        """Write a structured progress line (eval #, task_id, score, db/path match)."""
        with self._lock:
            self._metric_calls_so_far += 1
            pct = (
                f"{100 * self._metric_calls_so_far / self._max_metric_calls:.0f}%"
                if self._max_metric_calls
                else "—"
            )
            line = (
                f"[Progress] Eval #{eval_count} | metric_calls≈{self._metric_calls_so_far}"
                f" (budget {self._max_metric_calls}, {pct}) | task_id={task_id} | "
                f"score={score} | db_match={db_match} path_match={path_match}"
            )
            self._file.write(line + "\n")
            self._file.flush()


class IterationSpanManager:
    """Keeps a logfire span open per GEPA iteration for clean nesting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_it: int | None = None
        self._current_span_cm: Any = None
        self._current_span: Any = None

    def on_iteration(self, iteration: int) -> None:
        with self._lock:
            if self._current_it == iteration:
                return
            # Close previous
            if self._current_span_cm is not None:
                try:
                    self._current_span_cm.__exit__(None, None, None)
                except Exception:
                    pass
            self._current_it = iteration
            self._current_span_cm = logfire.span(
                "iteration",
                _span_name=f"Iteration:{iteration}",
                iteration=iteration,
            )
            self._current_span = self._current_span_cm.__enter__()
            try:
                self._current_span.message = f"Iteration:{iteration}"
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._current_span_cm is not None:
                try:
                    self._current_span_cm.__exit__(None, None, None)
                except Exception:
                    pass
            self._current_span_cm = None
            self._current_span = None
            self._current_it = None


def _run_live_display(
    state: ViewState,
    refresh_interval: float = 0.2,
) -> None:
    """Run Rich Live two-panel display until state.done is set."""
    custom_theme = Theme(
        {
            "panel.border": "dim blue",
            "panel.title": "bold cyan",
        }
    )

    def make_layout() -> Layout:
        gepa_lines, sim_lines, stage, eval_count = state.snapshot()
        gepa_content = "\n".join(gepa_lines[-50:]) if gepa_lines else "— waiting for GEPA —"
        sim_content = "\n".join(sim_lines[-50:]) if sim_lines else "— waiting for simulations —"
        header = Text()
        header.append("Stage: ", style="bold")
        header.append(stage + "  ", style="cyan")
        header.append("|  Eval count: ", style="bold")
        header.append(str(eval_count), style="yellow")
        header.append(f"  |  Budget: {state.max_metric_calls} metric calls", style="dim")

        left = Panel(
            gepa_content,
            title="[bold cyan] GEPA [/]",
            border_style="blue",
            height=28,
            expand=True,
        )
        right = Panel(
            sim_content,
            title="[bold green] Simulations [/]",
            border_style="green",
            height=28,
            expand=True,
        )
        layout = Layout()
        layout.split_row(
            Layout(left, name="gepa"),
            Layout(right, name="sim"),
        )
        return Group(Panel(header, border_style="dim"), layout)

    with Live(
        make_layout(),
        refresh_per_second=5,
        screen=False,
        transient=False,
        console=Console(theme=custom_theme),
    ) as live:
        while not state.done.wait(timeout=refresh_interval):
            live.update(make_layout())
        live.update(make_layout())


from chat.config import SimulationConfig
from domains.retail.run_solo_tasks import (
    _build_simulation_config,
    _load_retail_solo_config,
    _load_tasks,
    run_one_solo_task,
)
from orchestrator.orchestrator import SoloStopMode


_ROLE_HEADER = "## Role"
_FLOWCHART_HEADER = "## SOP Flowchart"


def _extract_optimizable_section(graph_text: str) -> str:
    """Extract the section body between '## Role' and '## SOP Flowchart'."""
    i = graph_text.find(_ROLE_HEADER)
    j = graph_text.find(_FLOWCHART_HEADER)
    if i == -1 or j == -1 or j <= i:
        raise ValueError("AGENTS_SOLO.md missing expected headings for optimization region")
    body_start = i + len(_ROLE_HEADER)
    body = graph_text[body_start:j]
    return body.strip("\n").strip()


def _apply_optimizable_section(graph_text: str, new_body: str) -> str:
    """Replace the section body between '## Role' and '## SOP Flowchart'."""
    i = graph_text.find(_ROLE_HEADER)
    j = graph_text.find(_FLOWCHART_HEADER)
    if i == -1 or j == -1 or j <= i:
        raise ValueError("AGENTS_SOLO.md missing expected headings for optimization region")
    body_start = i + len(_ROLE_HEADER)
    prefix = graph_text[:body_start]
    suffix = graph_text[j:]
    replacement = "\n\n" + (new_body.strip() + "\n\n")
    return prefix.rstrip() + replacement + suffix.lstrip()


def _write_candidate_graph(
    *,
    base_graph_path: Path,
    candidate_body: str,
    out_dir: Path,
) -> Path:
    """Write a candidate-specific graph file and return its path."""
    base_text = base_graph_path.read_text(encoding="utf-8")
    new_text = _apply_optimizable_section(base_text, candidate_body)
    digest = hashlib.sha1(candidate_body.encode("utf-8")).hexdigest()[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"AGENTS_SOLO.optimized.{digest}.md"
    out_path.write_text(new_text, encoding="utf-8")
    return out_path


def _write_candidate_instructions(candidate_body: str, out_dir: Path) -> Path:
    """Write a candidate instructions text to disk and return its path.

    This mirrors the mermaid candidate graph writer so that every GEPA candidate
    has a stable, digested file on disk for later inspection or replay.
    """
    digest = hashlib.sha1(candidate_body.encode("utf-8")).hexdigest()[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"instructions_candidate.{digest}.md"
    out_path.write_text(candidate_body, encoding="utf-8")
    return out_path


def _short_file_digest(path: Path, max_bytes: int = 200_000) -> str:
    """Short stable digest of a file's content (bounded read)."""
    h = hashlib.sha1()
    with path.open("rb") as f:
        chunk = f.read(max_bytes)
        h.update(chunk)
    return h.hexdigest()[:12]


def _split_train_val(
    tasks: List[dict[str, Any]],
    train_ratio: float,
    train_task_ids: List[int] | None,
    val_task_ids: List[int] | None,
    rng: random.Random,
) -> tuple[List[dict[str, Any]], List[dict[str, Any]]]:
    """Split tasks into train and val. Explicit IDs override train_ratio."""
    if train_task_ids is not None and val_task_ids is not None:
        allowed_train = {str(i) for i in train_task_ids} | set(train_task_ids)
        allowed_val = {str(i) for i in val_task_ids} | set(val_task_ids)
        train = [t for t in tasks if t.get("id") in allowed_train]
        val = [t for t in tasks if t.get("id") in allowed_val]
        return train, val
    # Shuffle and split by ratio
    indices = list(range(len(tasks)))
    rng.shuffle(indices)
    n_train = max(1, int(len(indices) * train_ratio))
    train_idx = set(indices[:n_train])
    val_idx = set(indices[n_train:])
    if not val_idx:
        val_idx = {indices[-1]}
        train_idx = set(indices) - val_idx
    train = [tasks[i] for i in indices if i in train_idx]
    val = [tasks[i] for i in indices if i in val_idx]
    return train, val


async def _run_one_eval(
    *,
    instructions_text: str,
    policy_text: str,
    task: dict[str, Any],
    sim_cfg: SimulationConfig,
    db_path: Path,
    mcp_command: str,
    stop_mode: SoloStopMode,
    seed: int | None = None,
    mermaid_graph_path: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Run a single task via run_solo_tasks path; return (success, eval_result)."""
    return await run_one_solo_task(
        instructions_text=instructions_text,
        policy_text=policy_text,
        task=task,
        sim_cfg=sim_cfg,
        stop_mode=stop_mode,
        db_path=db_path,
        mcp_command=mcp_command,
        seed=seed,
        mermaid_graph_path=mermaid_graph_path,
        quiet=True,
        include_policy=False,
    )


def _make_evaluator(
    *,
    policy_text: str,
    sim_cfg: SimulationConfig,
    db_path: Path,
    mcp_command: str,
    stop_mode: SoloStopMode,
    seed: int | None = None,
    llm_seed_chain: LLMSeedChain | None = None,
    qualitative_eval: bool = False,
    qualitative_eval_lm: str = "openai/gpt-4.1-mini",
    mermaid_base_graph_path: Path | None = None,
    mermaid_candidate_dir: Path | None = None,
    instructions_candidate_dir: Path | None = None,
    view_state: ViewState | None = None,
    progress_logger: GEPAFileLogger | None = None,
    use_logfire: bool = True,
):
    """Return a sync evaluator (candidate, example) -> score for GEPA."""

    def evaluate(candidate: str | dict[str, Any], example: dict[str, Any]):
        eff_seed = llm_seed_chain.read() if llm_seed_chain is not None else seed
        task_id = example.get("id", "?")
        if view_state:
            view_state.inc_eval()
            view_state.set_stage(f"Evaluating task_id={task_id}")
            view_state.add_sim(f"[Eval {view_state.eval_count}] task_id={task_id} started …")
        else:
            # Console-friendly simulation log for this evaluation.
            print(f"[SIM ] task_id={task_id} started …")

        # Set by _run_eval() so we can include it in side_info.
        mermaid_graph_override: str | None = None
        mermaid_graph_digest: str | None = None
        instructions_path: str | None = None

        def _run_eval():
            nonlocal mermaid_graph_override, mermaid_graph_digest, instructions_path
            if isinstance(candidate, str):
                instructions_text = candidate
            elif isinstance(candidate, dict):
                instructions_text = (
                    candidate.get("instructions")
                    or candidate.get("__str_candidate__")
                    or (next((v for v in candidate.values() if isinstance(v, str)), None))
                )
                instructions_text = instructions_text if isinstance(instructions_text, str) else str(candidate)
            else:
                instructions_text = str(candidate)

            # Persist the raw instructions candidate to disk (for inspection / replay)
            # in a stable, digest-based path, similar to how we store mermaid graphs.
            instructions_path = None
            if instructions_candidate_dir is not None:
                try:
                    instr_path = _write_candidate_instructions(
                        instructions_text, instructions_candidate_dir
                    )
                    instructions_path = str(instr_path)
                except Exception:
                    instructions_path = None

            mermaid_graph_override = None
            mermaid_graph_digest = None
            if mermaid_base_graph_path is not None and mermaid_candidate_dir is not None:
                try:
                    graph_path = _write_candidate_graph(
                        base_graph_path=mermaid_base_graph_path,
                        candidate_body=instructions_text,
                        out_dir=mermaid_candidate_dir,
                    )
                    mermaid_graph_override = str(graph_path)
                    mermaid_graph_digest = _short_file_digest(graph_path)
                except Exception:
                    mermaid_graph_override = None
                    mermaid_graph_digest = None

            # Visible, nested trace: which SOP file is used for this eval (when using mermaid).
            if use_logfire and mermaid_graph_override is not None:
                with logfire.span(
                    "mermaid_graph",
                    _span_name="mermaid_graph",
                    task_id=task_id,
                    sop_file=mermaid_graph_override,
                    sop_digest=mermaid_graph_digest,
                ):
                    logfire.info(
                        "mermaid_graph",
                        task_id=task_id,
                        sop_file=mermaid_graph_override,
                        sop_digest=mermaid_graph_digest,
                    )
            return asyncio.run(
                _run_one_eval(
                    instructions_text=instructions_text,
                    policy_text=policy_text,
                    task=example,
                    sim_cfg=sim_cfg,
                    db_path=db_path,
                    mcp_command=mcp_command,
                    stop_mode=stop_mode,
                    seed=eff_seed,
                    mermaid_graph_path=mermaid_graph_override,
                )
            )

        candidate_preview: str
        if isinstance(candidate, str):
            candidate_preview = candidate[:200] + "…" if len(candidate) > 200 else candidate
        else:
            candidate_preview = str(candidate)[:200] + "…" if len(str(candidate)) > 200 else str(candidate)

        if use_logfire:
            with logfire.span(
                "eval",
                _span_name=f"eval: Task: {task_id}",
                task_id=task_id,
                candidate_preview=candidate_preview,
            ) as eval_span:
                _db_ok, eval_result = _run_eval()
                db_match = eval_result.get("db_match", False)
                path_match = eval_result.get("path_match", True)
                # New reward: depend only on DB correctness. Path details are kept
                # for qualitative diagnosis and logging, but do not affect the score.
                score = 1.0 if db_match else 0.0
                outcome_label = "PASS" if score >= 1.0 else "FAIL"
                eval_span.message = f"eval: Task: {task_id} [{outcome_label}]"
                eval_span.set_attribute("score", score)
                eval_span.set_attribute("db_match", db_match)
                eval_span.set_attribute("path_match", path_match)
                eval_span.set_attribute("llm_seed", eff_seed)
        else:
            _db_ok, eval_result = _run_eval()
            db_match = eval_result.get("db_match", False)
            path_match = eval_result.get("path_match", True)
            score = 1.0 if db_match else 0.0

        side_info: dict[str, Any] = {
            "task_id": task_id,
            "score": score,
            "db_match": bool(db_match),
            "path_match": bool(path_match),
            "golden_hash": eval_result.get("golden_hash"),
            "predicted_hash": eval_result.get("predicted_hash"),
            "golden_mermaid_path": eval_result.get("golden_mermaid_path") or [],
            "predicted_goto_sequence": eval_result.get("predicted_goto_sequence") or [],
            "path_mismatch": eval_result.get("path_mismatch"),
            "trace_preview": eval_result.get("trace_preview") or "",
            "mermaid_sop_file": mermaid_graph_override,
            "mermaid_sop_digest": mermaid_graph_digest,
            "instructions_file": instructions_path,
            "llm_seed": eff_seed,
        }

        if qualitative_eval:
            try:
                import litellm

                # Keep this short and diagnostic; it becomes GEPA's ASI.
                # The evaluator sees the full policy, DB/hash results, and trace preview.
                # Scalar reward is based only on DB correctness; qualitative text should
                # explain what went right or wrong with respect to the DB outcome and actions.
                policy_snippet = policy_text or ""
                judge_prompt = (
                    "You are an evaluator. Use the retail policy and the traces below to judge the run.\n\n"
                    "Your primary job is to assess whether the final database state matches the golden outcome.\n"
                    "If db_match is False, explain which actions or missing actions most likely caused the mismatch.\n"
                    "If db_match is True, briefly confirm that the actions are consistent with the policy.\n"
                    "Do NOT propose prompt edits; only describe what went wrong or right.\n\n"
                    "=== Policy (truncated) ===\n"
                    f"{policy_snippet}\n\n"
                    "=== Evaluation context ===\n"
                    f"task_id: {task_id}\n"
                    f"db_match: {db_match}\n"
                    f"golden_hash: {side_info.get('golden_hash')}\n"
                    f"predicted_hash: {side_info.get('predicted_hash')}\n\n"
                    "trace_preview:\n"
                    f"{(side_info['trace_preview'] or '')[:2500]}\n"
                )
                with logfire.span(
                    "evaluator_completion",
                    _span_name="evaluator_completion",
                    task_id=task_id,
                    model=qualitative_eval_lm,
                ):
                    eval_messages = [{"role": "user", "content": judge_prompt}]
                    _q_kw: dict[str, Any] = {
                        "model": qualitative_eval_lm,
                        "messages": eval_messages,
                        "temperature": 0,
                    }
                    if eff_seed is not None:
                        _q_kw["seed"] = eff_seed
                    completion = litellm.completion(**_q_kw)
                    try:
                        from agent.gemini_log import log_litellm_raw_io

                        _extra_q = {"temperature": 0}
                        if eff_seed is not None:
                            _extra_q["seed"] = eff_seed
                        log_litellm_raw_io(
                            phase="gepa_qualitative_eval",
                            model=qualitative_eval_lm,
                            messages=eval_messages,
                            completion=completion,
                            extra_completion_kwargs=_extra_q,
                        )
                    except ImportError:
                        pass
                    judge_text = completion.choices[0].message.content  # type: ignore[union-attr]
                side_info["qualitative_diagnosis"] = (judge_text or "").strip()
            except Exception as e:
                side_info["qualitative_diagnosis_error"] = f"{type(e).__name__}: {e}"

        if progress_logger is not None:
            eval_count = view_state.eval_count if view_state else 0
            progress_logger.log_progress(eval_count, task_id, score, db_match, path_match)

        if view_state:
            status = "PASS" if score >= 1.0 else ("PARTIAL" if score > 0 else "FAIL")
            view_state.add_sim(
                f"[Eval {view_state.eval_count}] task_id={task_id} {status} "
                f"(db={db_match}, path={path_match}, score={score})"
            )
        else:
            status = "PASS" if score >= 1.0 else "FAIL"
            extra = f" instr_file={instructions_path}" if instructions_path else ""
            print(f"[SIM ] task_id={task_id} {status} (db={db_match}, score={score}){extra}")
        try:
            import gepa.optimize_anything as oa
            # Rich ASI for reflection: mismatch details and sequences.
            golden_path = side_info.get("golden_mermaid_path") or []
            predicted_path = side_info.get("predicted_goto_sequence") or []
            mismatch = side_info.get("path_mismatch")
            oa.log(f"# Task {task_id}")
            oa.log(f"score={score} db_match={db_match} path_match={path_match}")
            if mermaid_graph_override:
                oa.log(f"mermaid_sop_file={mermaid_graph_override} digest={mermaid_graph_digest}")
            oa.log(f"golden_mermaid_path={golden_path}")
            oa.log(f"predicted_goto_sequence={predicted_path}")
            if mismatch:
                oa.log(f"first_mismatch={mismatch}")
            if side_info.get('qualitative_diagnosis'):
                oa.log("qualitative_diagnosis:")
                oa.log(side_info['qualitative_diagnosis'])
        except Exception:
            pass
        return score, side_info

    return evaluate




def main() -> None:
    load_dotenv()
    # Google Gen AI OTel integration disabled for now (see agent.gemini_log for I/O).
    # os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    logfire.configure(scrubbing=False, console=False)
    # logfire.instrument_google_genai()
    from agent.logfire_gemini_integration import instrument_logfire_gemini

    instrument_logfire_gemini()
    # Instrument LiteLLM so individual model calls (including agent rollouts)
    # are visible as spans in Logfire, consistent with run_solo_tasks.
    logfire.instrument_litellm()

    parser = argparse.ArgumentParser(
        description="Run GEPA optimization for retail domain (config-driven)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the GEPA retail YAML config (required).",
    )
    parser.add_argument(
        "--gepa-log-file",
        type=Path,
        default=None,
        help="Write GEPA logs to this file (for tmux: other pane runs tail -f). Disables Rich live display.",
    )
    parser.add_argument(
        "--live-display",
        action="store_true",
        help="Enable the Rich live dashboard in this terminal (split view). "
        "By default, normal logs are printed without the live UI; use this flag to opt in.",
    )
    args = parser.parse_args()

    raw_cfg = _load_retail_solo_config(args.config)
    from agent.api_key_rotation import configure_from_simulation_dict

    configure_from_simulation_dict(raw_cfg)
    sim_cfg = _build_simulation_config(raw_cfg)
    domain_cfg: Dict[str, Any] = raw_cfg.get("domain") or {}
    gepa_cfg: Dict[str, Any] = raw_cfg.get("gepa") or {}

    if not gepa_cfg:
        raise ValueError("Config must contain a 'gepa' section.")

    instructions_path = domain_cfg.get("instructions")
    policy_path = domain_cfg.get("policy")
    tasks_path = domain_cfg.get("tasks")
    if not instructions_path or not policy_path or not tasks_path:
        raise ValueError(
            "Domain config must set 'instructions', 'policy', and 'tasks'."
        )

    optimize_target = gepa_cfg.get("optimize", "instructions")
    if optimize_target != "instructions":
        raise ValueError(
            f"gepa.optimize must be 'instructions' for now, got {optimize_target!r}."
        )

    stop_mode_str = str(domain_cfg.get("stop_mode", "first-text")).lower()
    stop_mode = (
        SoloStopMode.FIRST_TEXT_ONLY
        if stop_mode_str == "first-text"
        else SoloStopMode.TASK_COMPLETE_TOOL
    )

    policy_text = Path(policy_path).read_text(encoding="utf-8")
    seed_path = gepa_cfg.get("seed_path") or instructions_path
    # Optimization should start from a minimal instructions seed (no baked-in policy).
    # We always take the seed text from seed_path, and then, if mermaid is enabled,
    # we apply that evolving instructions block into the AGENTS_SOLO.md Role section.
    assistant_base_cfg = sim_cfg.assistant
    mermaid_cfg = getattr(assistant_base_cfg, "mermaid", None)
    mermaid_graph_path: Path | None = None
    if isinstance(mermaid_cfg, dict) and mermaid_cfg.get("graph"):
        mermaid_graph_path = Path(str(mermaid_cfg.get("graph")))

    # Seed candidate is always the bare instructions text (e.g. instructions_seed.md);
    # we no longer extract from the existing AGENTS_SOLO.md Role section.
    seed_candidate = Path(seed_path).read_text(encoding="utf-8")

    db_path = Path(domain_cfg.get("db_path", "domains/retail/db.json"))
    assistant_mcps = getattr(assistant_base_cfg, "mcps", None) or []
    mcp_command: str = ""
    for server_cfg in assistant_mcps:
        if server_cfg.get("name") == "retail-tools" or not mcp_command:
            mcp_command = server_cfg.get("command") or server_cfg.get("commad") or ""

    tasks = _load_tasks(Path(tasks_path))
    solo_tasks = [t for t in tasks if t.get("solo_convertible", True)]
    task_ids_cfg = domain_cfg.get("task_ids")
    if task_ids_cfg is not None and len(task_ids_cfg) > 0:
        allowed_ids = {str(i) for i in task_ids_cfg} | {int(i) for i in task_ids_cfg}
        solo_tasks = [t for t in solo_tasks if t.get("id") in allowed_ids]

    rng = random.Random(gepa_cfg.get("seed", 42))
    train_tasks, val_tasks = _split_train_val(
        solo_tasks,
        train_ratio=float(gepa_cfg.get("train_ratio", 0.7)),
        train_task_ids=gepa_cfg.get("train_task_ids"),
        val_task_ids=gepa_cfg.get("val_task_ids"),
        rng=rng,
    )

    max_metric_calls = gepa_cfg.get("max_metric_calls", 100)
    gepa_log_path: Path | None = args.gepa_log_file or gepa_cfg.get("log_file")
    if gepa_log_path is not None:
        gepa_log_path = Path(gepa_log_path)
    # Only enable the Rich live dashboard when explicitly requested and when we are
    # not already writing GEPA logs to a file. This avoids terminal jitter and lets
    # normal logs stream cleanly by default.
    use_live_display = bool(args.live_display) and gepa_log_path is None
    view_state = ViewState(max_metric_calls=max_metric_calls) if use_live_display else None

    from gepa.optimize_anything import (
        GEPAConfig,
        EngineConfig,
        ReflectionConfig,
        TrackingConfig,
        optimize_anything,
    )

    objective = gepa_cfg.get("objective") or "Improve the agent instructions."
    with logfire.span(
        "gepa_optimization",
        _span_name="GEPA retail instructions optimization",
        max_metric_calls=max_metric_calls,
        seed=gepa_cfg.get("seed", 42),
        objective=objective[:200],
        num_train=len(train_tasks),
        num_val=len(val_tasks),
    ) as top_span:
        if gepa_log_path is not None:
            iter_mgr = IterationSpanManager()
            gepa_logger = GEPAFileLogger(
                gepa_log_path,
                max_metric_calls=max_metric_calls,
                logfire_span=top_span,
                iteration_span_manager=iter_mgr,
            )
            print(f"GEPA logs → {gepa_log_path} (run 'tail -f {gepa_log_path}' in another pane)", flush=True)
        else:
            iter_mgr = IterationSpanManager()
            # When not using the Rich live dashboard, echo GEPA messages directly
            # to stdout with a simple prefix for a low-jitter logging experience.
            gepa_logger = GEPAPanelLogger(
                view_state,
                iteration_span_manager=iter_mgr,
                echo_to_console=not use_live_display,
            )

        # Ensure Iteration:0 exists from the start so eval spans nest under it.
        try:
            iter_mgr.on_iteration(0)
        except Exception:
            pass

        llm_seed_chain: LLMSeedChain | None = None
        gepa_callbacks_list: list[Any] | None = None
        if bool(gepa_cfg.get("llm_seed_chain", False)):
            llm_seed_chain = LLMSeedChain(int(gepa_cfg.get("seed", 42)))
            gepa_callbacks_list = [_LLMSeedChainCallback(llm_seed_chain)]
            try:
                top_span.set_attribute("llm_seed_chain", True)
            except Exception:
                pass

        evaluator = _make_evaluator(
            policy_text=policy_text,
            sim_cfg=sim_cfg,
            db_path=db_path,
            mcp_command=mcp_command,
            stop_mode=stop_mode,
            seed=None if llm_seed_chain is not None else gepa_cfg.get("seed"),
            llm_seed_chain=llm_seed_chain,
            qualitative_eval=bool(gepa_cfg.get("qualitative_eval", False)),
            qualitative_eval_lm=str(gepa_cfg.get("qualitative_eval_lm", "openai/gpt-4.1-mini")),
            mermaid_base_graph_path=mermaid_graph_path,
            mermaid_candidate_dir=Path(gepa_cfg.get("mermaid_candidate_dir", ".gepa/mermaid_graph_candidates")),
            instructions_candidate_dir=Path(gepa_cfg.get("instructions_candidate_dir", ".gepa/instruction_candidates")),
            view_state=view_state,
            progress_logger=gepa_logger if isinstance(gepa_logger, GEPAFileLogger) else None,
            use_logfire=True,
        )

        config = GEPAConfig(
            engine=EngineConfig(
                max_metric_calls=max_metric_calls,
                seed=gepa_cfg.get("seed", 42),
                cache_evaluation=gepa_cfg.get("cache_evaluation", True),
                parallel=gepa_cfg.get("parallel", False),
                max_workers=gepa_cfg.get("max_workers", 1),
            ),
            reflection=ReflectionConfig(
                reflection_lm=_make_litellm_lm_with_span(
                    str(gepa_cfg.get("reflection_lm", "openai/gpt-4.1-mini")),
                    "optimization_completion",
                ),
                reflection_minibatch_size=gepa_cfg.get("reflection_minibatch_size", 3),
            ),
            tracking=TrackingConfig(logger=gepa_logger),
            gepa_callbacks=gepa_callbacks_list,
        )

        if use_live_display and view_state is not None:
            display_thread = threading.Thread(
                target=_run_live_display,
                args=(view_state,),
                kwargs={"refresh_interval": 0.2},
                daemon=False,
            )
            display_thread.start()
        try:
            if view_state:
                view_state.set_stage("Running GEPA optimize_anything")
            if gepa_log_path is not None and isinstance(gepa_logger, GEPAFileLogger):
                gepa_logger.log(
                    f"[GEPA] Plan: budget={max_metric_calls} metric calls. "
                    "Progress % = metric_calls_used / budget. Evals and iteration logs below."
                )
                gepa_logger.log("[GEPA] Starting optimize_anything (evals will appear below).")
            result = optimize_anything(
                seed_candidate=seed_candidate,
                evaluator=evaluator,
                dataset=train_tasks,
                valset=val_tasks,
                objective=objective,
                background=gepa_cfg.get("background") or "",
                config=config,
            )
        finally:
            if view_state:
                view_state.done.set()
                display_thread.join(timeout=2.0)
            if iter_mgr is not None:
                try:
                    iter_mgr.close()
                except Exception:
                    pass

        if result is not None:
            best_preview = (
                result.best_candidate[:300] + "…"
                if isinstance(result.best_candidate, str)
                else str(result.best_candidate)[:300] + "…"
            )
            top_span.set_attribute("best_candidate_preview", best_preview)

    best = result.best_candidate
    if isinstance(best, dict):
        best = (
            best.get("instructions")
            or best.get("__str_candidate__")
            or next((v for v in best.values() if isinstance(v, str)), str(best))
        )
    out_path = Path(gepa_cfg.get("output_instructions_path", "domains/retail/instructions_gepa.md"))
    out_path.write_text(best if isinstance(best, str) else str(best), encoding="utf-8")
    print(f"Optimized section written to {out_path}")

    # Also write a full optimized graph file for direct use by mermaid MCP (drop-in replacement sop_file).
    if mermaid_graph_path is not None and mermaid_graph_path.exists():
        optimized_graph_path = Path(gepa_cfg.get("output_mermaid_graph_path", "domains/retail/AGENTS_SOLO_optimized.md"))
        base_graph_text = mermaid_graph_path.read_text(encoding="utf-8")
        optimized_graph_text = _apply_optimizable_section(base_graph_text, str(best))
        optimized_graph_path.write_text(optimized_graph_text, encoding="utf-8")
        print(f"Optimized mermaid graph written to {optimized_graph_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
