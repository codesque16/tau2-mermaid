"""Run the full data generation pipeline end-to-end.

Recommended: run in batches and review between. Don't generate 30k turns up front.
"""
from __future__ import annotations
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_gen.stage1_graphs import generate_graphs
from data_gen.stage2_plans import generate_plans
from data_gen.stage3_conversations import generate_conversations
from data_gen.stage45_traces_continuations import build_training_examples
from config.llm_args import load_llm_config
from utils.gemini_client import load_data_gen_dotenv, print_stats_summary

load_data_gen_dotenv()


def main(out_dir: str, n_per_domain: int, plans_per_graph: int,
         skip_stage1: bool = False, skip_continuations: bool = False,
         concurrency: int = 8):
    os.makedirs(out_dir, exist_ok=True)
    graphs_path = os.path.join(out_dir, "graphs.jsonl")
    plans_path = os.path.join(out_dir, "plans.jsonl")
    convs_path = os.path.join(out_dir, "conversations.jsonl")
    examples_path = os.path.join(out_dir, "examples.jsonl")

    # Load once, thread through every stage. Logfire spans for the whole
    # pipeline share the same model + llm_args.
    cfg = load_llm_config()
    print(f"Loaded LLMConfig: llm={cfg.llm}, llm_args={cfg.llm_args}")
    print(f"Pipeline concurrency: {concurrency}")

    if not skip_stage1:
        print("=" * 60); print("STAGE 1: GRAPHS"); print("=" * 60)
        if os.path.exists(graphs_path):
            os.remove(graphs_path)
        generate_graphs(graphs_path, llm=cfg.llm, llm_args=cfg.llm_args,
                        n_per_domain=n_per_domain, concurrency=concurrency)

    print("=" * 60); print("STAGE 2: PLANS"); print("=" * 60)
    generate_plans(graphs_path, plans_path, llm=cfg.llm, llm_args=cfg.llm_args,
                   plans_per_graph=plans_per_graph, concurrency=concurrency)

    print("=" * 60); print("STAGE 3: CONVERSATIONS"); print("=" * 60)
    generate_conversations(plans_path, graphs_path, convs_path,
                           llm=cfg.llm, llm_args=cfg.llm_args,
                           concurrency=concurrency)

    print("=" * 60); print("STAGE 4 + 5: TRACES + CONTINUATIONS"); print("=" * 60)
    build_training_examples(
        convs_path, graphs_path, examples_path,
        llm=cfg.llm if not skip_continuations else None,
        llm_args=cfg.llm_args if not skip_continuations else None,
        generate_continuations=not skip_continuations,
        concurrency=concurrency,
    )

    print("\n=== PIPELINE COMPLETE ===")
    print(f"Final dataset: {examples_path}")
    print()
    print_stats_summary()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", default="data/batch1")
    ap.add_argument("--n_per_domain", type=int, default=2)
    ap.add_argument("--plans_per_graph", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Parallel Gemini calls per stage. 8 is safe for default Vertex quotas; "
                         "raise to 16-24 if you have higher quota, lower if you hit 429s often.")
    ap.add_argument("--skip_stage1", action="store_true",
                    help="Skip graph generation if graphs.jsonl already exists")
    ap.add_argument("--skip_continuations", action="store_true",
                    help="Skip Gemini continuation generation (Stage 5)")
    args = ap.parse_args()
    main(args.output_dir, args.n_per_domain, args.plans_per_graph,
         skip_stage1=args.skip_stage1,
         skip_continuations=args.skip_continuations,
         concurrency=args.concurrency)
