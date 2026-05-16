"""Run a model (HF checkpoint or vLLM endpoint) against the eval set.

Two backends:
  --backend hf      Local HF + bitsandbytes (default). For checkpoints with
                    LoRA adapters: detects adapter_config.json and attaches.
  --backend vllm    OpenAI-compatible vLLM server. Concurrent generations,
                    ~10x faster per token, persistent model load.

Usage:
  # Baseline (HF, base model):
  python -m evaluation.eval_harness --model google/gemma-4-e4b-it \
      --eval_path data/batch1/eval.jsonl --graphs_path data/batch1/graphs.jsonl \
      --output experiments/baseline/eval.json

  # QLoRA checkpoint via HF:
  python -m evaluation.eval_harness \
      --model experiments/qlora_e4b_pilot/checkpoints/final \
      --eval_path data/batch1/eval.jsonl --graphs_path data/batch1/graphs.jsonl \
      --output experiments/qlora_e4b_pilot/eval.json

  # Same QLoRA checkpoint via a vLLM server (you start the server separately):
  python -m evaluation.eval_harness --backend vllm \
      --vllm_url http://localhost:8000/v1 \
      --vllm_model_id sop-agent-qlora \
      --eval_path data/batch1/eval.jsonl --graphs_path data/batch1/graphs.jsonl \
      --output experiments/qlora_e4b_pilot/eval_vllm.json \
      --concurrency 16

Profile flag (--profile) adds per-section timing for both backends.
"""
from __future__ import annotations
import sys, os, json, argparse, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

from config.schemas import SOPGraph, TrainingExample
from training.format_dataset import example_to_prompt_completion
from evaluation.metrics import score_turn, aggregate


def _cuda_sync():
    if torch.cuda.is_available():
        torch.cuda.synchronize()


# ============================================================
# HF backend (in-process model.generate)
# ============================================================

def load_model_hf(model_name_or_path: str, quantize: bool = True, profile: bool = False):
    print(f"[hf] Loading {model_name_or_path}")
    load_sections: dict[str, float] = {}

    adapter_config_path = os.path.join(model_name_or_path, "adapter_config.json")
    is_adapter = os.path.isfile(adapter_config_path)
    t0 = time.perf_counter()
    if is_adapter:
        with open(adapter_config_path) as f:
            adapter_cfg = json.load(f)
        base_model_name = adapter_cfg.get("base_model_name_or_path")
        print(f"  detected PEFT adapter; base model = {base_model_name}")
    else:
        base_model_name = model_name_or_path
    if profile:
        load_sections["adapter_config_read_s"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(base_model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    if profile:
        load_sections["tokenizer_from_pretrained_s"] = time.perf_counter() - t1

    kwargs = {"dtype": torch.bfloat16, "device_map": {"": 0}}
    if quantize:
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
        )
    t2 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(base_model_name, **kwargs)
    if profile:
        load_sections["model_from_pretrained_s"] = time.perf_counter() - t2

    if is_adapter:
        from peft import PeftModel
        from training.train import _unwrap_gemma4_clippable_linear
        t3 = time.perf_counter()
        model = _unwrap_gemma4_clippable_linear(model)
        if profile:
            load_sections["unwrap_gemma4_clippable_linear_s"] = time.perf_counter() - t3
        t4 = time.perf_counter()
        model = PeftModel.from_pretrained(model, model_name_or_path)
        if profile:
            load_sections["peft_from_pretrained_s"] = time.perf_counter() - t4
        print(f"  attached adapter from {model_name_or_path}")

    model.eval()
    return tokenizer, model, load_sections


def generate_once_hf(tokenizer, model, messages, max_new_tokens: int = 1024,
                     profile: bool = False):
    """In-process HF generate. If profile=True, returns (text, sections)."""
    sections: dict[str, float] = {}
    t0 = time.perf_counter()
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    t1 = time.perf_counter()
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    t2 = time.perf_counter()
    n_prompt = int(inputs["input_ids"].shape[1])

    _cuda_sync()
    t_sync0 = time.perf_counter()
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=1.0,
            pad_token_id=tokenizer.pad_token_id,
        )
    _cuda_sync()
    t3 = time.perf_counter()

    new_tokens = out[0, inputs["input_ids"].shape[1]:]
    n_new = int(new_tokens.shape[0])
    text = tokenizer.decode(new_tokens, skip_special_tokens=False)
    t5 = time.perf_counter()

    if profile:
        sections.update({
            "apply_chat_template_s": t1 - t0,
            "tokenize_to_device_s": t2 - t1,
            "cuda_sync_before_generate_s": t_sync0 - t2,
            "generate_s": t3 - t_sync0,
            "decode_s": t5 - t3,
            "prompt_token_count": float(n_prompt),
            "new_token_count": float(n_new),
        })
        return text, sections
    return text


# ============================================================
# vLLM backend (OpenAI-compatible HTTP)
# ============================================================

def make_vllm_client(vllm_url: str):
    """Build an OpenAI-compatible client pointing at the vLLM server.

    vLLM exposes an OpenAI-style /v1/chat/completions endpoint. We use the
    `openai` Python SDK against it; no API key is needed but the SDK requires
    one to be set (any non-empty string works).
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "Install the OpenAI client to use the vLLM backend: pip install openai"
        ) from e
    return OpenAI(base_url=vllm_url, api_key="EMPTY")


def generate_once_vllm(client, vllm_model_id: str, messages, max_new_tokens: int = 1024,
                       profile: bool = False):
    """Single chat completion against a vLLM server. Deterministic by default."""
    sections: dict[str, float] = {}
    t0 = time.perf_counter()
    # We do NOT apply the chat template client-side — vLLM applies the model's
    # template server-side (or whatever --chat-template you passed at server
    # launch). The messages list goes through as-is.
    response = client.chat.completions.create(
        model=vllm_model_id,
        messages=messages,
        max_tokens=max_new_tokens,
        temperature=0.0,
    )
    t1 = time.perf_counter()
    msg = response.choices[0].message
    text = msg.content or ""
    # If vLLM was started with `--reasoning-parser gemma4`, the <think> block
    # is extracted into a separate `reasoning_content` field rather than left
    # inline in `content`. Our scorer looks for <think>...</think> in the
    # text, so we re-inject it.
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning:
        text = f"<think>\n{reasoning}\n</think>\n{text}"
    # Same for tool_calls — vLLM extracts them with --tool-call-parser gemma4
    # and our scorer expects them inline in the text.
    tc = getattr(msg, "tool_calls", None)
    if tc:
        try:
            f = tc[0].function
            text += f"\n<tool_call>{{\"name\": \"{f.name}\", \"arguments\": {f.arguments}}}</tool_call>"
        except Exception:
            pass
    if profile:
        usage = getattr(response, "usage", None)
        sections.update({
            "generate_s": t1 - t0,
            "prompt_token_count": float(getattr(usage, "prompt_tokens", 0) or 0),
            "new_token_count": float(getattr(usage, "completion_tokens", 0) or 0),
        })
        return text, sections
    return text


# ============================================================
# Eval orchestration (shared between backends)
# ============================================================

def load_eval_set(path: str, graphs_path: str | None = None):
    """Load examples and a dict of domain -> SOPGraph."""
    examples = []
    with open(path) as f:
        for line in f:
            if not line.strip(): continue
            examples.append(TrainingExample(**json.loads(line)))

    graphs = {}
    if graphs_path and os.path.exists(graphs_path):
        with open(graphs_path) as f:
            for line in f:
                if not line.strip(): continue
                g = SOPGraph(**json.loads(line))
                graphs[g.domain] = g
    return examples, graphs


def domain_from_system_prompt(sys_prompt: str, graphs: dict) -> SOPGraph | None:
    for domain, g in graphs.items():
        if any(nid in sys_prompt for nid in list(g.node_policies.keys())[:3]):
            return g
    return None


def _process_one(i, ex, generate_fn, graphs, profile):
    graph = domain_from_system_prompt(ex.system_prompt, graphs)
    if graph is None:
        return None
    pc = example_to_prompt_completion(ex, include_thinking=False)
    prompt_messages = pc["prompt"]
    t_gen = time.perf_counter()
    try:
        if profile:
            output_text, sec = generate_fn(prompt_messages, profile=True)
        else:
            output_text = generate_fn(prompt_messages, profile=False)
            sec = {}
    except Exception as e:
        return {"error": str(e), "example_id": ex.example_id}
    dt = time.perf_counter() - t_gen
    gt = {
        "gt_current_node": ex.gt_current_node,
        "gt_next_node": ex.gt_next_node,
        "gt_tool": ex.tool_call.name if ex.tool_call else None,
        "expected_refusal": False,
    }
    result = score_turn(ex.example_id, gt, output_text, graph)
    return {"i": i, "ex": ex, "result": result, "wall": dt,
            "sec": sec, "output_text": output_text}


def run_eval(model_path: str, eval_path: str, graphs_path: str, output_path: str,
             *, backend: str = "hf",
             vllm_url: str | None = None, vllm_model_id: str | None = None,
             concurrency: int = 1,
             max_examples: int | None = None, quantize: bool = True,
             profile: bool = False):
    # ---- backend setup ----
    t0 = time.perf_counter()
    load_sections: dict[str, float] = {}
    if backend == "hf":
        tokenizer, model, load_sections = load_model_hf(model_path, quantize=quantize, profile=profile)
        def gen_fn(messages, profile=False):
            return generate_once_hf(tokenizer, model, messages, profile=profile)
    elif backend == "vllm":
        if not vllm_url:
            raise ValueError("--vllm_url required for backend=vllm")
        if not vllm_model_id:
            raise ValueError("--vllm_model_id required for backend=vllm "
                             "(must match the server's --served-model-name)")
        print(f"[vllm] using server {vllm_url} model_id={vllm_model_id}")
        client = make_vllm_client(vllm_url)
        def gen_fn(messages, profile=False):
            return generate_once_vllm(client, vllm_model_id, messages, profile=profile)
    else:
        raise ValueError(f"Unknown backend {backend!r}")
    load_s = time.perf_counter() - t0
    print(f"[timing] backend setup: {load_s:.2f}s")
    if profile and load_sections:
        for k in sorted(load_sections.keys()):
            print(f"  load[{k}]: {load_sections[k]:.3f}s")

    # ---- dataset ----
    t_ds = time.perf_counter()
    examples, graphs = load_eval_set(eval_path, graphs_path)
    if max_examples:
        examples = examples[:max_examples]
    dataset_s = time.perf_counter() - t_ds
    print(f"[timing] load_eval_set: {dataset_s:.3f}s ({len(examples)} examples, {len(graphs)} graphs)")

    # ---- run ----
    results = []
    gen_wall_times: list[float] = []
    gen_sections: list[dict] = []

    wall_start = time.perf_counter()
    if backend == "vllm" and concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"[run] vLLM concurrent eval (workers={concurrency})")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(_process_one, i, e, gen_fn, graphs, profile)
                    for i, e in enumerate(examples)]
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                if "error" in r:
                    print(f"[err] {r['example_id']}: {r['error']}")
                    continue
                results.append(r["result"])
                gen_wall_times.append(r["wall"])
                if profile:
                    gen_sections.append(r["sec"])
                print(
                    f"[{len(results)}/{len(examples)}] {r['ex'].example_id} "
                    f"wall={r['wall']:.2f}s "
                    f"new_toks={int(r['sec'].get('new_token_count', 0)) if r['sec'] else '-'}",
                    flush=True,
                )
    else:
        print(f"[run] sequential eval (backend={backend})")
        for i, e in enumerate(examples):
            r = _process_one(i, e, gen_fn, graphs, profile)
            if r is None:
                print(f"[{i+1}/{len(examples)}] no matching graph; skipping")
                continue
            if "error" in r:
                print(f"[{i+1}/{len(examples)}] generation failed: {r['error']}")
                continue
            results.append(r["result"])
            gen_wall_times.append(r["wall"])
            if profile:
                gen_sections.append(r["sec"])
            print(
                f"[{i+1}/{len(examples)}] {r['ex'].example_id} "
                f"wall={r['wall']:.2f}s "
                f"new_toks={int(r['sec'].get('new_token_count', 0)) if r['sec'] else '-'}",
                flush=True,
            )
    wall_total = time.perf_counter() - wall_start

    # ---- timing summary ----
    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    timing_extra: dict | None = None
    if gen_wall_times:
        timing_extra = {
            "backend": backend,
            "concurrency": concurrency if backend == "vllm" else 1,
            "model_load_wall_seconds": round(load_s, 3),
            "load_eval_set_seconds": round(dataset_s, 3),
            "n_generations": len(gen_wall_times),
            # wall_clock_total = sum of per-call walls in sequential mode;
            # vs total elapsed in concurrent mode (the more useful number).
            "wall_clock_total_seconds": round(wall_total, 3),
            "per_call_wall_sum_seconds": round(sum(gen_wall_times), 3),
            "per_call_wall_mean_seconds": round(_mean(gen_wall_times), 4),
            "per_call_wall_min_seconds": round(min(gen_wall_times), 4),
            "per_call_wall_max_seconds": round(max(gen_wall_times), 4),
        }
        if gen_sections:
            nt = sum(int(s.get("new_token_count", 0)) for s in gen_sections)
            pt = sum(int(s.get("prompt_token_count", 0)) for s in gen_sections)
            timing_extra["total_prompt_tokens"] = pt
            timing_extra["total_new_tokens"] = nt
            timing_extra["mean_new_tokens_per_example"] = round(nt / len(gen_sections), 2)
            # Throughput. For sequential (HF), wall ≈ generate; tokens/s is just nt / wall.
            # For concurrent vLLM, what matters is end-to-end throughput over wall_total.
            if wall_total > 0:
                timing_extra["throughput_new_tokens_per_second"] = round(nt / wall_total, 2)
                timing_extra["throughput_examples_per_second"] = round(
                    len(gen_wall_times) / wall_total, 2)

        print(
            f"\n[timing] backend={backend} n={len(gen_wall_times)} "
            f"wall_total={wall_total:.2f}s "
            f"per_call_mean={_mean(gen_wall_times):.2f}s "
            f"min={min(gen_wall_times):.2f}s max={max(gen_wall_times):.2f}s"
        )

    summary = aggregate(results)
    print("\n=== AGGREGATE ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "model": model_path if backend == "hf" else vllm_model_id,
            "backend": backend,
            "n": len(results),
            "summary": summary,
            "timing": timing_extra,
            "results": [r.__dict__ for r in results],
        }, f, indent=2)
    print(f"\nWrote eval results to {output_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hf", "vllm"], default="hf")
    # HF args
    ap.add_argument("--model", help="HF backend: path or HF id of model/adapter")
    ap.add_argument("--no_quantize", action="store_true")
    # vLLM args
    ap.add_argument("--vllm_url", default=None,
                    help="vLLM OpenAI-compatible endpoint, e.g. http://localhost:8000/v1")
    ap.add_argument("--vllm_model_id", default=None,
                    help="Model id the server was started with (--served-model-name)")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Concurrent generations (vLLM only)")
    # Shared
    ap.add_argument("--eval_path", default="data/eval.jsonl")
    ap.add_argument("--graphs_path", default="data/graphs.jsonl")
    ap.add_argument("--output", default="results/eval.json")
    ap.add_argument("--max_examples", type=int, default=None)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    if args.backend == "hf" and not args.model:
        ap.error("--model required for backend=hf")
    if args.backend == "vllm" and (not args.vllm_url or not args.vllm_model_id):
        ap.error("--vllm_url and --vllm_model_id required for backend=vllm")

    run_eval(
        model_path=args.model or "",
        eval_path=args.eval_path,
        graphs_path=args.graphs_path,
        output_path=args.output,
        backend=args.backend,
        vllm_url=args.vllm_url,
        vllm_model_id=args.vllm_model_id,
        concurrency=args.concurrency,
        max_examples=args.max_examples,
        quantize=not args.no_quantize,
        profile=args.profile,
    )