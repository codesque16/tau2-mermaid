"""Tiny GEPA + mermaid demo (no retail DB/tools).

Optimizes the section in AGENTS_TOY.md between:
  - '## Role'
  - '## SOP Flowchart'

Metric: exact match of the goto_node node_id sequence vs golden_mermaid_path.

Run:
  uv run python -m domains.toy_mermaid.run_gepa_toy --config configs/gepa_toy_mermaid.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

import logfire
import yaml

from agent import create_agent
from agent.config import AgentConfig as AgentAgentConfig
from chat.config import AgentConfig as ChatAgentConfig, SimulationConfig
from orchestrator.event_bus import EventBus
from orchestrator.orchestrator import Orchestrator, SoloStopMode


_ROLE_HEADER = "## Role"
_FLOWCHART_HEADER = "## SOP Flowchart"
_ITER_RE = re.compile(r"Iteration\s*[:#]?\s*(\d+)", re.I)


class IterationSpanManager:
    """Keeps a Logfire span open per iteration for clean nesting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_it: int | None = None
        self._current_span_cm: Any = None

    def on_iteration(self, iteration: int) -> None:
        with self._lock:
            if self._current_it == iteration:
                return
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
            span = self._current_span_cm.__enter__()
            try:
                span.message = f"Iteration:{iteration}"
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
            self._current_it = None


class ToyGEPALogger:
    """Receives GEPA engine logs and turns them into structured Logfire info."""

    def __init__(self, iter_mgr: IterationSpanManager) -> None:
        self._iter_mgr = iter_mgr

    def log(self, message: str) -> None:
        line = (message or "").strip()
        if not line:
            return
        m = _ITER_RE.search(line)
        if m:
            try:
                self._iter_mgr.on_iteration(int(m.group(1)))
            except Exception:
                pass
        logfire.info("gepa", message=line)


class CandidateStatsTracker:
    """Aggregate scores per candidate so logs show 'improvement' clearly."""

    def __init__(self, train_ids: set[str], val_ids: set[str]) -> None:
        self._lock = threading.Lock()
        self._train_ids = train_ids
        self._val_ids = val_ids
        self._by_cand: dict[str, dict[str, float]] = {}

    @staticmethod
    def cand_id(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def update(self, cand: str, task_id: Any, score: float) -> None:
        cid = self.cand_id(cand)
        tid = str(task_id)
        bucket = "train" if tid in self._train_ids else ("val" if tid in self._val_ids else "other")
        with self._lock:
            row = self._by_cand.setdefault(
                cid, {"train_sum": 0.0, "train_n": 0.0, "val_sum": 0.0, "val_n": 0.0}
            )
            if bucket == "train":
                row["train_sum"] += float(score)
                row["train_n"] += 1.0
            elif bucket == "val":
                row["val_sum"] += float(score)
                row["val_n"] += 1.0

    def snapshot(self, cand: str) -> dict[str, float]:
        cid = self.cand_id(cand)
        with self._lock:
            row = dict(self._by_cand.get(cid) or {})
        train_avg = (row.get("train_sum", 0.0) / row.get("train_n", 1.0)) if row.get("train_n") else 0.0
        val_avg = (row.get("val_sum", 0.0) / row.get("val_n", 1.0)) if row.get("val_n") else 0.0
        return {
            "candidate_id": cid,
            "train_avg": float(train_avg),
            "val_avg": float(val_avg),
            "train_n": float(row.get("train_n", 0.0)),
            "val_n": float(row.get("val_n", 0.0)),
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_simulation_config(raw: dict[str, Any]) -> SimulationConfig:
    assistant_block: Dict[str, Any] = raw.get("assistant") or {}
    assistant_chat_cfg = ChatAgentConfig(
        system_prompt=assistant_block.get("system_prompt", ""),
        temperature=assistant_block.get("temperature", 0.0),
        max_tokens=assistant_block.get("max_tokens"),
        mcps=assistant_block.get("mcps") or None,
        mermaid=assistant_block.get("mermaid") or None,
        reasoning_effort=assistant_block.get("reasoning_effort"),
    )
    user_chat_cfg = ChatAgentConfig(system_prompt="", temperature=0.0, max_tokens=1)
    return SimulationConfig(
        model=raw.get("model") or "",
        max_turns=raw.get("max_turns", 30),
        stop_phrases=raw.get("stop_phrases") or [],
        initial_message=None,
        assistant=assistant_chat_cfg,
        user=user_chat_cfg,
        assistant_model=assistant_block.get("model") or "",
        user_model=assistant_block.get("model") or "",
        assistant_agent_type=(assistant_block.get("agent_type") or "").strip() or None,
        user_agent_type=(assistant_block.get("agent_type") or "").strip() or None,
        assistant_agent_name=None,
        user_agent_name=None,
        mcp_server_url=None,
        graph_id=None,
        mode=(raw.get("mode") or "solo"),
    )


def _extract_optimizable_section(graph_text: str) -> str:
    i = graph_text.find(_ROLE_HEADER)
    j = graph_text.find(_FLOWCHART_HEADER)
    if i == -1 or j == -1 or j <= i:
        raise ValueError("Toy graph missing expected headings")
    body_start = i + len(_ROLE_HEADER)
    return graph_text[body_start:j].strip("\n").strip()


def _apply_optimizable_section(graph_text: str, new_body: str) -> str:
    i = graph_text.find(_ROLE_HEADER)
    j = graph_text.find(_FLOWCHART_HEADER)
    if i == -1 or j == -1 or j <= i:
        raise ValueError("Toy graph missing expected headings")
    body_start = i + len(_ROLE_HEADER)
    prefix = graph_text[:body_start]
    suffix = graph_text[j:]
    replacement = "\n\n" + (new_body.strip() + "\n\n")
    return prefix.rstrip() + replacement + suffix.lstrip()


def _write_candidate_graph(base_graph_path: Path, candidate_body: str, out_dir: Path) -> Path:
    base_text = base_graph_path.read_text(encoding="utf-8")
    new_text = _apply_optimizable_section(base_text, candidate_body)
    digest = hashlib.sha1(candidate_body.encode("utf-8")).hexdigest()[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"AGENTS_TOY.optimized.{digest}.md"
    out_path.write_text(new_text, encoding="utf-8")
    return out_path


def _extract_goto_node_sequence(history: List[dict[str, Any]]) -> List[str]:
    path: List[str] = []
    for m in history:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []) or []:
            if tc.get("name") != "goto_node":
                continue
            args = tc.get("arguments") or {}
            node_id = args.get("node_id")
            if isinstance(node_id, str) and node_id.strip():
                path.append(node_id.strip())
    return path


def _first_mismatch(pred: List[str], gold: List[str]) -> dict[str, Any] | None:
    if not gold:
        return None
    n = min(len(pred), len(gold))
    for i in range(n):
        if pred[i] != gold[i]:
            return {"index": i, "expected": gold[i], "actual": pred[i], "reason": "node_id_mismatch"}
    if len(pred) != len(gold):
        return {"index": n, "expected": gold[n] if n < len(gold) else None, "actual": pred[n] if n < len(pred) else None, "reason": "length_mismatch"}
    return None


async def _run_one(
    *,
    sim_cfg: SimulationConfig,
    mermaid_graph_path: str,
    ticket: str,
    seed: int | None,
) -> List[str]:
    assistant_base_cfg = sim_cfg.assistant
    assistant_model = sim_cfg.assistant_model or sim_cfg.model
    agent_type = sim_cfg.assistant_agent_type or "litellm"

    # In mermaid mode the system prompt comes from load_graph(); we only pass ticket.
    assistant_cfg = AgentAgentConfig(
        system_prompt=f"<ticket>\n{ticket.strip()}\n</ticket>",
        max_tokens=assistant_base_cfg.max_tokens,
        temperature=assistant_base_cfg.temperature,
        reasoning_effort=getattr(assistant_base_cfg, "reasoning_effort", None),
        mcps=getattr(assistant_base_cfg, "mcps", None),
        mermaid=_override_mermaid_graph(getattr(assistant_base_cfg, "mermaid", None), mermaid_graph_path),
        seed=seed,
    )
    assistant = create_agent(agent_type, "assistant", assistant_cfg, assistant_model)
    user_cfg = AgentAgentConfig(system_prompt="", max_tokens=1, temperature=0.0, reasoning_effort="low", seed=seed)
    user = create_agent(agent_type, "user", user_cfg, assistant_model)
    orch = Orchestrator(assistant, user, EventBus(), sim_cfg)

    await orch.run_solo(
        prompt="Complete all the tasks and respond in 1 single reply.",
        stop_mode=SoloStopMode.FIRST_TEXT_ONLY,
        task_complete_tools=["task_complete", "task_done"],
    )
    if hasattr(orch.assistant, "aclose_mcp"):
        await orch.assistant.aclose_mcp()
    return _extract_goto_node_sequence(getattr(orch.assistant, "history", []))


def _override_mermaid_graph(mermaid_cfg: Any, mermaid_graph_path: str) -> Any:
    if isinstance(mermaid_cfg, dict):
        out = dict(mermaid_cfg)
        out["graph"] = mermaid_graph_path
        return out
    if isinstance(mermaid_cfg, list):
        out_list: list[Any] = []
        for entry in mermaid_cfg:
            if isinstance(entry, dict):
                e2 = dict(entry)
                e2["graph"] = mermaid_graph_path
                out_list.append(e2)
            else:
                out_list.append(entry)
        return out_list
    return mermaid_cfg


def main() -> None:
    logfire.configure(scrubbing=False, console=False)
    # Avoid instrument_litellm() span spam; prefer explicit spans.

    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    args = p.parse_args()

    raw = _load_yaml(args.config)
    sim_cfg = _build_simulation_config(raw)
    domain_cfg: Dict[str, Any] = raw.get("domain") or {}
    gepa_cfg: Dict[str, Any] = raw.get("gepa") or {}

    tasks_path = Path(domain_cfg["tasks"])
    tasks: list[dict[str, Any]] = yaml.safe_load(tasks_path.read_text(encoding="utf-8")) if tasks_path.suffix in {".yaml", ".yml"} else __import__("json").loads(tasks_path.read_text(encoding="utf-8"))
    rng = random.Random(int(gepa_cfg.get("seed", 42)))
    rng.shuffle(tasks)
    train = tasks[: max(1, int(len(tasks) * float(gepa_cfg.get("train_ratio", 0.7))))]
    val = tasks[len(train) :] or tasks[-1:]
    train_ids = {str(t.get("id")) for t in train}
    val_ids = {str(t.get("id")) for t in val}

    assistant_base_cfg = sim_cfg.assistant
    mermaid_cfg = getattr(assistant_base_cfg, "mermaid", None)
    if not (isinstance(mermaid_cfg, dict) and mermaid_cfg.get("graph")):
        raise ValueError("assistant.mermaid.graph is required for toy demo")
    base_graph_path = Path(str(mermaid_cfg["graph"]))
    seed_candidate = _extract_optimizable_section(base_graph_path.read_text(encoding="utf-8"))

    from gepa.optimize_anything import GEPAConfig, EngineConfig, ReflectionConfig, TrackingConfig, optimize_anything

    candidate_dir = Path(gepa_cfg.get("mermaid_candidate_dir", ".gepa/toy_mermaid_graph_candidates"))
    max_metric_calls = int(gepa_cfg.get("max_metric_calls", 30))
    stats = CandidateStatsTracker(train_ids=train_ids, val_ids=val_ids)

    def evaluator(candidate: str | dict[str, Any], example: dict[str, Any]):
        if isinstance(candidate, dict):
            cand_text = next((v for v in candidate.values() if isinstance(v, str)), str(candidate))
        else:
            cand_text = candidate
        graph_path = _write_candidate_graph(base_graph_path, cand_text, candidate_dir)
        ticket = example.get("ticket") or ""
        task_id = example.get("id")
        golden = list((example.get("evaluation_criteria") or {}).get("golden_mermaid_path") or [])
        with logfire.span("eval", _span_name=f"eval: Task: {task_id}", task_id=task_id) as s:
            try:
                s.message = f"eval: Task: {task_id}"
            except Exception:
                pass
            pred = asyncio.run(
                _run_one(
                    sim_cfg=sim_cfg,
                    mermaid_graph_path=str(graph_path),
                    ticket=ticket,
                    seed=int(gepa_cfg.get("seed", 42)),
                )
            )
        ok = pred == golden
        score = 1.0 if ok else 0.0
        stats.update(cand_text, task_id, score)
        snap = stats.snapshot(cand_text)
        logfire.info(
            "eval_result",
            task_id=task_id,
            score=score,
            candidate_id=snap["candidate_id"],
            train_avg=snap["train_avg"],
            val_avg=snap["val_avg"],
            sop_file=str(graph_path),
        )
        side_info = {
            "task_id": task_id,
            "score": score,
            "golden": golden,
            "predicted": pred,
            "mismatch": _first_mismatch(pred, golden),
            "sop_file": str(graph_path),
            "candidate_id": snap["candidate_id"],
            "train_avg_so_far": snap["train_avg"],
            "val_avg_so_far": snap["val_avg"],
        }
        return score, side_info

    def _make_reflection_lm(model_name: str):
        import litellm

        def _lm(prompt: str | list[dict[str, Any]]) -> str:
            if isinstance(prompt, str):
                messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            else:
                messages = prompt
            # Best-effort extract of current candidate from the reflection prompt.
            prompt_text = messages[-1].get("content") if messages else ""
            curr_preview = ""
            try:
                m = re.search(r"```\\s*\\n([\\s\\S]*?)\\n```", str(prompt_text))
                if m:
                    curr_preview = m.group(1)[:400]
            except Exception:
                curr_preview = ""
            with logfire.span(
                "optimization_completion",
                _span_name="optimization_completion",
                model=model_name,
                curr_candidate_preview=curr_preview,
            ):
                completion = litellm.completion(model=model_name, messages=messages, temperature=0)
            return completion.choices[0].message.content  # type: ignore[union-attr]

        return _lm

    with logfire.span(
        "gepa_toy_optimization",
        _span_name="GEPA toy mermaid optimization",
        max_metric_calls=max_metric_calls,
        seed=int(gepa_cfg.get("seed", 42)),
        num_train=len(train),
        num_val=len(val),
    ) as top_span:
        iter_mgr = IterationSpanManager()
        iter_mgr.on_iteration(0)
        gepa_logger = ToyGEPALogger(iter_mgr)
        logfire.info(
            "seed_candidate",
            candidate_id=CandidateStatsTracker.cand_id(seed_candidate),
            seed_preview=seed_candidate[:600],
        )
        result = optimize_anything(
            seed_candidate=seed_candidate,
            evaluator=evaluator,
            dataset=train,
            valset=val,
            objective=str(gepa_cfg.get("objective") or "Match the exact goto_node path."),
            background=str(gepa_cfg.get("background") or ""),
            config=GEPAConfig(
                engine=EngineConfig(
                    max_metric_calls=max_metric_calls,
                    seed=int(gepa_cfg.get("seed", 42)),
                    parallel=bool(gepa_cfg.get("parallel", False)),
                    max_workers=int(gepa_cfg.get("max_workers", 1)),
                ),
                reflection=ReflectionConfig(
                    reflection_lm=_make_reflection_lm(str(gepa_cfg.get("reflection_lm", "openai/gpt-4.1-mini"))),
                    reflection_minibatch_size=int(gepa_cfg.get("reflection_minibatch_size", 2)),
                ),
                tracking=TrackingConfig(logger=gepa_logger),
            ),
        )
        try:
            top_span.set_attribute("best_candidate_preview", str(result.best_candidate)[:400])
        except Exception:
            pass
        try:
            iter_mgr.close()
        except Exception:
            pass

    out_section = Path(gepa_cfg.get("output_section_path", "domains/toy_mermaid/role_optimized.md"))
    out_section.write_text(str(result.best_candidate), encoding="utf-8")
    out_graph = Path(gepa_cfg.get("output_mermaid_graph_path", "domains/toy_mermaid/AGENTS_TOY_optimized.md"))
    out_graph.write_text(_apply_optimizable_section(base_graph_path.read_text(encoding="utf-8"), str(result.best_candidate)), encoding="utf-8")
    print(f"Toy optimized section → {out_section}")
    print(f"Toy optimized graph → {out_graph}")


if __name__ == "__main__":
    main()

