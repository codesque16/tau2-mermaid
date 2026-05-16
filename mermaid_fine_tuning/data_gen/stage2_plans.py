"""Stage 2: Generate trajectory plans per graph."""
from __future__ import annotations
import sys, os, json, argparse
from pydantic import BaseModel, RootModel
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import SOPGraph, TrajectoryPlan, NodePolicy
from config.prompts import stage2_prompt, STAGE2_SYSTEM
from config.retail_sop import render_retail_system_prompt
from utils.gemini_client import GeminiClient
from utils.validators import validate_plan
from utils.parallel import run_parallel, ParallelFailure


class PlanList(RootModel):
    root: list[TrajectoryPlan]


def render_sop(graph: SOPGraph) -> str:
    """Render a SOPGraph as a system-prompt-style markdown block."""
    lines = ["## SOP Global Policies", ""]
    for p in graph.global_policies:
        lines.append(f"- {p}")
    lines.extend(["", "## SOP Node Policies", "```yaml"])
    for nid, pol in graph.node_policies.items():
        lines.append(f"{nid}:")
        lines.append(f"  tool_hints: {', '.join(pol.tool_hints) if pol.tool_hints else 'null'}")
        lines.append(f"  policy: |\n    {pol.policy}")
    lines.extend(["```", "", "## SOP Flowchart", "```mermaid", graph.mermaid, "```"])
    return "\n".join(lines)


def _generate_plans_for_graph(client, graph, plans_per_graph):
    """Worker: generate plans for one graph. Returns (graph, plans_obj, error)."""
    sop_rendered = render_sop(graph)
    try:
        plans_obj = client.generate_structured(
            prompt=stage2_prompt(graph.domain, sop_rendered, plans_per_graph),
            response_schema=PlanList,
            system_instruction=STAGE2_SYSTEM,
            temperature=0.9,
            max_output_tokens=32768,
            call_name=f"stage2_plans_{graph.domain[:20].replace(' ','_')}",
        )
        return graph, plans_obj.root, None
    except Exception as e:
        return graph, None, f"{type(e).__name__}: {e}"


def generate_plans(graphs_path: str, out_path: str, llm: str,
                   llm_args: dict | None = None, plans_per_graph: int = 30,
                   concurrency: int = 8):
    client = GeminiClient(llm=llm, llm_args=llm_args, actor="data_gen")
    all_plans = []
    rejected = []

    with open(graphs_path) as f:
        graphs = [SOPGraph(**json.loads(line)) for line in f if line.strip()]

    print(f"[stage2] {len(graphs)} graphs × {plans_per_graph} plans, concurrency={concurrency}")

    def work(graph):
        return _generate_plans_for_graph(client, graph, plans_per_graph)

    results = run_parallel(
        items=graphs, work_fn=work, concurrency=concurrency,
        description="Stage 2: plans",
    )

    for result in results:
        if isinstance(result, ParallelFailure):
            rejected.append({"plan_id": "?", "errors": [f"unhandled: {result.exception}"]})
            continue
        graph, plans, err = result
        if err:
            rejected.append({"plan_id": f"{graph.domain}_batch", "errors": [err]})
            continue
        for plan in plans:
            plan.graph_domain = graph.domain
            ok, errs = validate_plan(plan, graph)
            if ok:
                all_plans.append({"graph_domain": graph.domain, "plan": plan.model_dump()})
            else:
                rejected.append({"plan_id": plan.plan_id, "errors": errs[:5]})

    with open(out_path, "w") as f:
        for p in all_plans:
            f.write(json.dumps(p) + "\n")

    rejected_path = out_path.replace(".jsonl", "_rejected.json")
    with open(rejected_path, "w") as f:
        json.dump(rejected, f, indent=2)

    print(f"[stage2] wrote {len(all_plans)} valid plans; rejected {len(rejected)}")


if __name__ == "__main__":
    from config.llm_args import load_llm_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--graphs", default="data/graphs.jsonl")
    ap.add_argument("--output", default="data/plans.jsonl")
    ap.add_argument("--plans_per_graph", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    cfg = load_llm_config()
    generate_plans(args.graphs, args.output, llm=cfg.llm, llm_args=cfg.llm_args,
                   plans_per_graph=args.plans_per_graph, concurrency=args.concurrency)
