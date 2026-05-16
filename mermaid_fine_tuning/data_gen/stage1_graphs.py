"""Stage 1: Generate SOP graphs across domains and topologies."""
from __future__ import annotations
import sys, os, json, uuid, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import SOPGraph, NodePolicy
from config.prompts import stage1_prompt, STAGE1_SYSTEM
from utils.gemini_client import GeminiClient
from utils.validators import validate_graph
from utils.parallel import run_parallel, ParallelFailure


# NOTE: retail, airline, and telecom are deliberately excluded from training.
# They are held out for tau-bench-style evaluation (test set).
# If you reintroduce any of them here, your held-out eval numbers will be
# contaminated by structurally similar training examples.
DOMAINS = [
    "banking customer support",
    "healthcare appointment triage",
    "IT helpdesk",
    "hotel front desk",
    "insurance claims",
    "food delivery support",
    "ride-hailing support",
    "government services helpdesk",
    "SaaS customer success",
    "edtech student support",
    "real estate inquiry",
    "gym membership service",
    "utility billing support",
]

# Mix of sizes and feature combinations
SIZE_VARIANTS = [
    (12, ["intent routing", "single mutation flow"]),
    (20, ["intent routing", "cross-flow jumps", "decision branching"]),
    (28, ["intent routing", "batch processing", "policy violations", "confirmation gates"]),
    (40, ["intent routing", "cross-flow jumps", "deep decision branching", "batch processing", "multiple mutation flows"]),
]


def _generate_one_graph(client, domain, size, features, max_retries_per_slot):
    """Try up to max_retries_per_slot times to produce a valid graph for one (domain, size) slot."""
    for attempt in range(max_retries_per_slot):
        try:
            prompt = stage1_prompt(domain, size, features)
            graph = client.generate_structured(
                prompt=prompt,
                response_schema=SOPGraph,
                system_instruction=STAGE1_SYSTEM,
                temperature=0.8,
                max_output_tokens=16384,
                call_name=f"stage1_graph_{domain[:20].replace(' ','_')}",
            )
        except Exception as e:
            if attempt == max_retries_per_slot - 1:
                return None, f"all attempts failed: {type(e).__name__}: {e}"
            continue

        ok, errs = validate_graph(graph)
        if ok:
            graph.domain = domain
            return graph, None
        if attempt == max_retries_per_slot - 1:
            return None, f"validation: {errs[:3]}"
    return None, "exhausted retries"


def generate_graphs(out_path: str, llm: str, llm_args: dict | None = None,
                    n_per_domain: int = 2, max_retries_per_slot: int = 2,
                    concurrency: int = 8):
    """Generate graphs across the domain × size matrix and write valid ones to out_path."""
    client = GeminiClient(llm=llm, llm_args=llm_args, actor="data_gen")

    # Build the work list: one (domain, size, features) tuple per slot
    slots = [
        (domain, size, features)
        for domain in DOMAINS
        for size, features in SIZE_VARIANTS[:n_per_domain]
    ]

    def work(slot):
        domain, size, features = slot
        return _generate_one_graph(client, domain, size, features, max_retries_per_slot)

    print(f"[stage1] generating {len(slots)} graphs with concurrency={concurrency}")
    results = run_parallel(
        items=slots,
        work_fn=work,
        concurrency=concurrency,
        description="Stage 1: graphs",
    )

    valid_graphs = []
    rejected = []
    for slot, result in zip(slots, results):
        slot_label = f"{slot[0][:20]}_size{slot[1]}"
        if isinstance(result, ParallelFailure):
            rejected.append({"slot": slot_label, "errors": [f"unhandled: {result.exception}"]})
            continue
        graph, error = result
        if graph is not None:
            valid_graphs.append(graph)
        else:
            rejected.append({"slot": slot_label, "errors": [error]})

    with open(out_path, "w") as f:
        for g in valid_graphs:
            f.write(json.dumps(g.model_dump()) + "\n")

    rejected_path = out_path.replace(".jsonl", "_rejected.json")
    with open(rejected_path, "w") as f:
        json.dump(rejected, f, indent=2)

    print(f"[stage1] wrote {len(valid_graphs)} valid graphs to {out_path}")
    print(f"[stage1] rejected {len(rejected)} (see {rejected_path})")


if __name__ == "__main__":
    from config.llm_args import load_llm_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="data/graphs.jsonl")
    ap.add_argument("--n_per_domain", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    cfg = load_llm_config()
    generate_graphs(args.output, llm=cfg.llm, llm_args=cfg.llm_args,
                    n_per_domain=args.n_per_domain, concurrency=args.concurrency)
