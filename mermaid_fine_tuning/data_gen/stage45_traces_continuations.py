"""Stage 4 (deterministic): build structured traces for every assistant turn.
Stage 5 (Gemini): generate optional continuations for turns that warrant them.

We combine these into one file because they share the same loop over conversations.
"""
from __future__ import annotations
import sys, os, json, argparse
from typing import Any
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import (
    SOPGraph, AssistantTurn, ToolCall, StructuredTrace,
    Continuation, TrainingExample
)
from config.prompts import stage5_prompt, STAGE5_SYSTEM
from utils.gemini_client import GeminiClient
from utils.templating import build_structured_trace, render_trace_block
from utils.parallel import run_parallel, ParallelFailure
from data_gen.stage2_plans import render_sop


def _format_history(history: list[dict]) -> str:
    """Render a history list as readable text for the Gemini stage-5 prompt."""
    lines = []
    for h in history:
        role = h.get("role", "?")
        content = h.get("content", "")
        if isinstance(content, dict):
            content = json.dumps(content)
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _observation_from_history(history: list[dict]) -> str:
    """The last meaningful user message or tool result becomes the observation."""
    for h in reversed(history):
        if h["role"] == "tool":
            return f"Tool {h.get('name')} returned: {json.dumps(h.get('content'))[:200]}"
        if h["role"] == "user":
            return f"User said: {h['content'][:200]}"
    return "Conversation start"


def _process_one_conversation(client, conv, graphs, generate_continuations):
    """Worker: walk one conversation turn-by-turn, emit list of TrainingExamples.

    Inside a conversation we go serial because history accumulates and Stage 5
    needs the running history for the continuation prompt. Across conversations
    we parallelize at a higher level.
    """
    graph = graphs.get(conv["graph_domain"])
    if graph is None:
        return []

    sop_rendered = render_sop(graph)
    examples: list[TrainingExample] = []
    history: list[dict] = []

    for t in conv["turns"]:
        if t["kind"] == "user":
            history.append({"role": "user", "content": t["content"]})

        elif t["kind"] == "assistant":
            obs = _observation_from_history(history)
            at = AssistantTurn(
                turn_index=t["turn_index"],
                current_node=t.get("current_node", ""),
                next_node=t.get("next_node", ""),
                edge_taken=t.get("edge_taken"),
                tool_call=ToolCall(**t["tool_call"]) if t.get("tool_call") else None,
                response_to_user=t.get("response_to_user"),
            )
            trace = build_structured_trace(at, graph, observation=obs)

            cont = Continuation(text="", rationale="", needed=False)
            if generate_continuations and client is not None:
                try:
                    turn_with_trace = (
                        f"Structured trace:\n{render_trace_block(trace)}\n\n"
                        f"Assistant will: "
                        f"{'call ' + at.tool_call.name if at.tool_call else 'respond to user with: ' + (at.response_to_user or '')[:200]}"
                    )
                    raw = client.generate_text(
                        stage5_prompt(sop_rendered, _format_history(history), turn_with_trace),
                        system_instruction=STAGE5_SYSTEM,
                        temperature=0.4,
                        max_output_tokens=1024,
                        call_name=f"stage5_cont_{conv['plan_id']}_t{at.turn_index}",
                    )
                    data = json.loads(raw)
                    cont = Continuation(
                        text=data.get("text", "") if data.get("needed") else "",
                        rationale=data.get("rationale", ""),
                        needed=bool(data.get("needed", False)),
                    )
                except Exception as e:
                    cont = Continuation(text="", rationale=f"gen_failed: {e}", needed=False)

            assistant_history_content: Any
            if at.tool_call:
                assistant_history_content = {"tool_call": at.tool_call.model_dump()}
            else:
                assistant_history_content = at.response_to_user or ""

            examples.append(TrainingExample(
                example_id=f"{conv['plan_id']}_t{at.turn_index}",
                conversation_id=conv["plan_id"],
                turn_index=at.turn_index,
                system_prompt=sop_rendered,
                conversation_history=list(history),
                structured_trace=trace,
                continuation=cont,
                tool_call=at.tool_call,
                response_to_user=at.response_to_user,
                gt_current_node=at.current_node,
                gt_next_node=at.next_node,
                gt_edge=at.edge_taken,
            ))

            history.append({"role": "assistant", "content": assistant_history_content})

        elif t["kind"] == "tool_result":
            history.append({
                "role": "tool", "name": t.get("name"),
                "content": t.get("result"),
            })

    return examples


def build_training_examples(
    conversations_path: str,
    graphs_path: str,
    out_path: str,
    llm: str | None = None,
    llm_args: dict | None = None,
    generate_continuations: bool = True,
    max_examples: int | None = None,
    concurrency: int = 8,
):
    # Load graphs
    graphs: dict[str, SOPGraph] = {}
    with open(graphs_path) as f:
        for line in f:
            if not line.strip(): continue
            g = SOPGraph(**json.loads(line))
            graphs[g.domain] = g

    client = (
        GeminiClient(llm=llm, llm_args=llm_args, actor="data_gen")
        if generate_continuations and llm
        else None
    )

    # Load all conversations
    conversations = []
    with open(conversations_path) as f:
        for line in f:
            if not line.strip(): continue
            conversations.append(json.loads(line))

    if generate_continuations:
        print(f"[stage4/5] processing {len(conversations)} conversations, concurrency={concurrency}")
    else:
        print(f"[stage4/5] processing {len(conversations)} conversations (no continuations)")

    def work(conv):
        return _process_one_conversation(client, conv, graphs, generate_continuations)

    # When not generating continuations, work is CPU-light; concurrency=1 is fine.
    # When generating continuations, each conversation makes N Gemini calls
    # serially internally, so concurrency parallelizes across conversations.
    effective_concurrency = concurrency if generate_continuations else 1

    results = run_parallel(
        items=conversations, work_fn=work, concurrency=effective_concurrency,
        description="Stage 4/5: traces + continuations",
    )

    examples: list[TrainingExample] = []
    for result in results:
        if isinstance(result, ParallelFailure):
            print(f"  ✗ unhandled: {result.exception}")
            continue
        examples.extend(result)
        if max_examples and len(examples) >= max_examples:
            examples = examples[:max_examples]
            break

    with open(out_path, "w") as f:
        for ex in examples:
            f.write(ex.model_dump_json() + "\n")
    print(f"[stage4/5] wrote {len(examples)} training examples to {out_path}")


if __name__ == "__main__":
    from config.llm_args import load_llm_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", default="data/conversations.jsonl")
    ap.add_argument("--graphs", default="data/graphs.jsonl")
    ap.add_argument("--output", default="data/examples.jsonl")
    ap.add_argument("--no_continuations", action="store_true")
    ap.add_argument("--max_examples", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    if args.no_continuations:
        llm, llm_args = None, None
    else:
        cfg = load_llm_config()
        llm, llm_args = cfg.llm, cfg.llm_args
    build_training_examples(
        args.conversations, args.graphs, args.output,
        llm=llm, llm_args=llm_args,
        generate_continuations=not args.no_continuations,
        max_examples=args.max_examples,
        concurrency=args.concurrency,
    )
