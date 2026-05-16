"""Stage 3: Realize plans as full conversations with turn-by-turn labels."""
from __future__ import annotations
import sys, os, json, argparse
from typing import Any
from pydantic import BaseModel
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import SOPGraph, TrajectoryPlan, AssistantTurn, UserTurn, ToolCall
from config.prompts import stage3_prompt, STAGE3_SYSTEM
from utils.gemini_client import GeminiClient
from utils.validators import validate_assistant_turn
from utils.parallel import run_parallel, ParallelFailure
from data_gen.stage2_plans import render_sop


class TurnDict(BaseModel):
    """Flexible turn shape — kind discriminates."""
    kind: str
    turn_index: int
    # user
    content: str | None = None
    # assistant
    current_node: str | None = None
    next_node: str | None = None
    edge_taken: str | None = None
    tool_call: ToolCall | None = None
    response_to_user: str | None = None
    # tool_result
    name: str | None = None
    # result is an arbitrary JSON object from a tool call. Using `Any` here
    # produces an empty schema that Vertex's structured-output mode rejects
    # ("schema didn't specify the schema type field"). dict[str, Any] becomes
    # {"type": "object", "additionalProperties": true}, which Vertex accepts.
    result: dict[str, Any] | None = None


class ConversationOut(BaseModel):
    turns: list[TurnDict]


def _generate_one_conversation(client, plan_record, graphs):
    """Worker: generate one conversation for one plan. Returns (plan_id, conv_dict, error)."""
    plan = TrajectoryPlan(**plan_record["plan"])
    graph = graphs.get(plan_record["graph_domain"])
    if graph is None:
        return plan.plan_id, None, f"no graph for domain {plan_record['graph_domain']}"

    try:
        sop_rendered = render_sop(graph)
        conv_out = client.generate_structured(
            prompt=stage3_prompt(sop_rendered, plan.model_dump_json()),
            response_schema=ConversationOut,
            system_instruction=STAGE3_SYSTEM,
            temperature=0.7,
            max_output_tokens=32768,
            call_name=f"stage3_conv_{plan.plan_id}",
        )
    except Exception as e:
        return plan.plan_id, None, f"{type(e).__name__}: {e}"

    # Validate every assistant turn
    turn_errors = []
    for t in conv_out.turns:
        if t.kind == "assistant":
            at = AssistantTurn(
                turn_index=t.turn_index,
                current_node=t.current_node or "",
                next_node=t.next_node or "",
                edge_taken=t.edge_taken,
                tool_call=t.tool_call,
                response_to_user=t.response_to_user,
            )
            ok, errs = validate_assistant_turn(at, graph)
            if not ok:
                turn_errors.extend(errs)

    if turn_errors:
        return plan.plan_id, None, "; ".join(turn_errors[:5])

    return plan.plan_id, {
        "plan_id": plan.plan_id,
        "graph_domain": graph.domain,
        "turns": [t.model_dump() for t in conv_out.turns],
    }, None


def generate_conversations(plans_path: str, graphs_path: str, out_path: str,
                           llm: str, llm_args: dict | None = None,
                           max_convs: int | None = None,
                           concurrency: int = 8):
    client = GeminiClient(llm=llm, llm_args=llm_args, actor="data_gen")

    graphs = {}
    with open(graphs_path) as f:
        for line in f:
            if not line.strip():
                continue
            g = SOPGraph(**json.loads(line))
            graphs[g.domain] = g

    with open(plans_path) as f:
        plan_records = [json.loads(line) for line in f if line.strip()]
    if max_convs:
        plan_records = plan_records[:max_convs]

    print(f"[stage3] {len(plan_records)} conversations, concurrency={concurrency}")

    def work(rec):
        return _generate_one_conversation(client, rec, graphs)

    results = run_parallel(
        items=plan_records, work_fn=work, concurrency=concurrency,
        description="Stage 3: conversations",
    )

    valid_convs = []
    rejected = []
    for result in results:
        if isinstance(result, ParallelFailure):
            rejected.append({"plan_id": "?", "errors": [f"unhandled: {result.exception}"]})
            continue
        plan_id, conv, err = result
        if conv is not None:
            valid_convs.append(conv)
        else:
            rejected.append({"plan_id": plan_id, "errors": [err]})

    with open(out_path, "w") as f:
        for c in valid_convs:
            f.write(json.dumps(c) + "\n")
    with open(out_path.replace(".jsonl", "_rejected.json"), "w") as f:
        json.dump(rejected, f, indent=2)

    print(f"[stage3] wrote {len(valid_convs)} valid conversations; rejected {len(rejected)}")


if __name__ == "__main__":
    from config.llm_args import load_llm_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--graphs", default="data/graphs.jsonl")
    ap.add_argument("--plans", default="data/plans.jsonl")
    ap.add_argument("--output", default="data/conversations.jsonl")
    ap.add_argument("--max_convs", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    cfg = load_llm_config()
    generate_conversations(args.plans, args.graphs, args.output,
                           llm=cfg.llm, llm_args=cfg.llm_args,
                           max_convs=args.max_convs,
                           concurrency=args.concurrency)
