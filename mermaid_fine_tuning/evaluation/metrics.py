"""Per-skill metrics for SOP-agent evaluation.

Inputs are model outputs paired with ground truth.
"""
from __future__ import annotations
import sys, os, re, json
from dataclasses import dataclass, asdict
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import SOPGraph, StructuredTrace
from utils.mermaid_parser import parse_mermaid


@dataclass
class TurnEvalResult:
    example_id: str
    gt_current_node: str
    pred_current_node: str | None
    gt_next_node: str
    pred_next_node: str | None

    node_match: bool        # pred current_node == gt
    next_node_match: bool   # pred next_node == gt
    edge_valid: bool        # pred next is a valid neighbor of pred current
    off_graph: bool         # pred current or next not in graph

    pred_tool: str | None
    gt_tool: str | None
    tool_match: bool        # pred tool == gt tool (None == None counts)
    tool_in_hints: bool     # if tool called, was it in current_node tool_hints

    has_continuation: bool
    continuation_consistent: bool  # heuristic check vs trace

    confirmation_phrase_present: bool | None  # None if N/A
    refusal_correct: bool | None              # None if N/A


# ---------- Parsing model output ----------

# Thinking wrappers vary by model / tokenizer decode settings.
THINK_BLOCK_RES = [
    re.compile(r"<think>(.*?)</think>", flags=re.DOTALL | re.IGNORECASE),
    re.compile(r"<thought>(.*?)</thought>", flags=re.DOTALL | re.IGNORECASE),
]
TRACE_FIELD_RE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.+)$", flags=re.MULTILINE)


def _merge_trace_fields(target: dict, text: str, max_chars: int = 12000) -> None:
    """Fill missing trace keys from key: value lines (works without XML wrappers)."""
    chunk = text[:max_chars]
    for fm in TRACE_FIELD_RE.finditer(chunk):
        key, val = fm.group(1).strip(), fm.group(2).strip()
        if key not in target or not (target.get(key) or "").strip():
            target[key] = val


def parse_model_output(output_text: str) -> dict:
    """Parse a model output into structured fields.

    Returns:
      {trace: dict (parsed trace fields), continuation: str, response: str, tool_call: dict | None}
    """
    out = {"trace": {}, "continuation": "", "response": "", "tool_call": None}

    m = None
    for pat in THINK_BLOCK_RES:
        m = pat.search(output_text)
        if m:
            break

    if m:
        thinking = m.group(1).strip()
        # Split structured trace from continuation by "---"
        if "---" in thinking:
            trace_part, cont_part = thinking.split("---", 1)
            out["continuation"] = cont_part.strip()
        else:
            trace_part = thinking

        for fm in TRACE_FIELD_RE.finditer(trace_part):
            key, val = fm.group(1).strip(), fm.group(2).strip()
            out["trace"][key] = val

        # The model's response is what follows the thinking block
        after = output_text[m.end():].strip()
    else:
        after = output_text.strip()

    # Baseline models often omit XML wrappers but still emit `current_node: ...` lines.
    if not out["trace"].get("current_node") or not out["trace"].get("next_node"):
        _merge_trace_fields(out["trace"], after)
    if not out["trace"].get("current_node") or not out["trace"].get("next_node"):
        _merge_trace_fields(out["trace"], output_text)

    # Detect tool call markup. Gemma 4 has native function call format —
    # this is a simple fallback heuristic. Adjust if your inference wraps tool
    # calls differently.
    tc_match = re.search(r"```tool_code\s*(.+?)\s*```", after, flags=re.DOTALL)
    if tc_match:
        try:
            out["tool_call"] = json.loads(tc_match.group(1))
        except Exception:
            pass
        out["response"] = (after[:tc_match.start()] + after[tc_match.end():]).strip()
    else:
        out["response"] = after

    # Fallback: first JSON object with "name" + "arguments" (flat OpenAI-style tool call)
    if out["tool_call"] is None:
        jm = re.search(
            r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{[\s\S]*?\})\s*\}',
            after,
        )
        if jm:
            try:
                args = json.loads(jm.group(2))
                out["tool_call"] = {"name": jm.group(1), "arguments": args if isinstance(args, dict) else {}}
            except Exception:
                out["tool_call"] = {"name": jm.group(1), "arguments": {}}

    return out


# ---------- Per-turn scoring ----------

CONFIRMATION_PHRASE = "Please confirm so I can process this for you. Please note that the action is not yet complete, and I will notify you once it is successfully processed."
MUTATION_NODES = {"CANCEL", "MOD_ADDRESS", "MOD_PAYMENT", "MOD_ITEMS", "RETURN", "EXCHANGE"}


def score_turn(example_id: str, gt: dict, model_output: str, graph: SOPGraph) -> TurnEvalResult:
    parsed = parse_model_output(model_output)
    trace = parsed["trace"]

    pred_cur = trace.get("current_node")
    pred_next = trace.get("next_node")
    # Strip common trailing prose / punctuation from parsed fields
    if isinstance(pred_cur, str):
        pred_cur = pred_cur.split()[0].strip(".,;:\"'") if pred_cur.strip() else None
    if isinstance(pred_next, str):
        pred_next = pred_next.split()[0].strip(".,;:\"'") if pred_next.strip() else None

    parsed_graph = parse_mermaid(graph.mermaid)
    # IMPORTANT: `None not in parsed_graph.nodes` is True in Python — missing parses
    # must not count as "off graph" (that was inflating off_graph_rate to 100% for baselines).
    off_graph = False
    if pred_cur is not None and pred_cur not in parsed_graph.nodes:
        off_graph = True
    if pred_next is not None and pred_next not in parsed_graph.nodes:
        off_graph = True
    edge_valid = False
    if pred_cur and pred_next:
        if pred_cur == pred_next:
            edge_valid = True  # stay at node
        else:
            edge_valid = parsed_graph.is_valid_edge(pred_cur, pred_next)

    pred_tool = parsed["tool_call"]["name"] if parsed["tool_call"] else None
    gt_tool = gt.get("gt_tool")
    tool_match = pred_tool == gt_tool

    tool_in_hints = True
    if pred_tool and pred_cur is not None and pred_cur in graph.node_policies:
        tool_in_hints = pred_tool in graph.node_policies[pred_cur].tool_hints
    elif pred_tool and pred_cur is None:
        tool_in_hints = False

    cont = parsed["continuation"]
    cont_consistent = True
    if cont:
        # Heuristic checks for contradiction
        if pred_next and f"next_node: {pred_next}" not in cont:
            # OK, just no mention. Look for explicit contradictions instead:
            contradiction_terms = []
            for nid in parsed_graph.nodes:
                if nid != pred_next and nid in cont and "instead" in cont.lower():
                    contradiction_terms.append(nid)
            cont_consistent = len(contradiction_terms) == 0

    confirmation_present = None
    if pred_cur in MUTATION_NODES and parsed["response"] and "confirm" in parsed["response"].lower():
        confirmation_present = CONFIRMATION_PHRASE in parsed["response"]

    refusal_correct = None
    if gt.get("expected_refusal"):
        refusal_correct = any(
            kw in parsed["response"].lower()
            for kw in ["cannot", "not eligible", "ineligible", "unable", "not allowed", "policy"]
        )

    return TurnEvalResult(
        example_id=example_id,
        gt_current_node=gt["gt_current_node"],
        pred_current_node=pred_cur,
        gt_next_node=gt["gt_next_node"],
        pred_next_node=pred_next,
        node_match=(pred_cur == gt["gt_current_node"]),
        next_node_match=(pred_next == gt["gt_next_node"]),
        edge_valid=edge_valid,
        off_graph=off_graph,
        pred_tool=pred_tool,
        gt_tool=gt_tool,
        tool_match=tool_match,
        tool_in_hints=tool_in_hints,
        has_continuation=bool(cont),
        continuation_consistent=cont_consistent,
        confirmation_phrase_present=confirmation_present,
        refusal_correct=refusal_correct,
    )


def aggregate(results: list[TurnEvalResult]) -> dict:
    n = len(results)
    if n == 0:
        return {}

    def pct(predicate):
        return round(100 * sum(1 for r in results if predicate(r)) / n, 2)

    def pct_nonnull(predicate, condition):
        subset = [r for r in results if condition(r)]
        if not subset:
            return None
        return round(100 * sum(1 for r in subset if predicate(r)) / len(subset), 2)

    return {
        "n": n,
        "trace_parse_rate_pct": pct(
            lambda r: r.pred_current_node is not None and r.pred_next_node is not None
        ),
        "node_accuracy_pct": pct(lambda r: r.node_match),
        "next_node_accuracy_pct": pct(lambda r: r.next_node_match),
        "edge_validity_pct": pct(lambda r: r.edge_valid),
        "off_graph_rate_pct": pct(lambda r: r.off_graph),
        "tool_match_pct": pct(lambda r: r.tool_match),
        "tool_in_hints_pct": pct_nonnull(
            lambda r: r.tool_in_hints, lambda r: r.pred_tool is not None
        ),
        "continuation_rate_pct": pct(lambda r: r.has_continuation),
        "continuation_consistency_pct": pct_nonnull(
            lambda r: r.continuation_consistent, lambda r: r.has_continuation
        ),
        "confirmation_phrase_correct_pct": pct_nonnull(
            lambda r: bool(r.confirmation_phrase_present),
            lambda r: r.confirmation_phrase_present is not None,
        ),
        "refusal_correct_pct": pct_nonnull(
            lambda r: bool(r.refusal_correct),
            lambda r: r.refusal_correct is not None,
        ),
    }
