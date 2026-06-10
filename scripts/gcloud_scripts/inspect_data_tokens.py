# inspect_data_tokens.py
# Tokenises a dataset with the same pipeline used by the SFT / LoRA trainers and
# prints a full size report — token counts, truncation rate, training-token
# estimates — without loading model weights or needing a GPU.
#
# Usage:
#   python inspect_data_tokens.py \
#       --dataset_path /home/shiladitya/my_out/depth_03/split_file_09.jsonl \
#       --max_seq_length 16384

import argparse
import statistics

from transformers import AutoTokenizer
from datasets import load_dataset

MODEL_NAME = "Qwen/Qwen3-8B"


def parse_args():
    p = argparse.ArgumentParser(description="Token-count audit for SFT datasets")
    p.add_argument("--dataset_path", type=str,
                   default="/home/shiladitya/my_out/depth_03/split_file_09.jsonl")
    p.add_argument("--text_field", type=str, default="text")
    p.add_argument("--max_seq_length", type=int, default=16384)
    p.add_argument("--num_epochs", type=int, default=3,
                   help="Used to estimate total training tokens.")
    p.add_argument("--tokenizer_name", type=str, default=MODEL_NAME,
                   help="HF tokenizer to use (default: Qwen/Qwen3-8B).")
    p.add_argument("--num_proc", type=int, default=4)
    return p.parse_args()


def detect_ext(path):
    p = path.lower()
    if p.endswith(".jsonl") or p.endswith(".json"):
        return "json"
    if p.endswith(".csv"):
        return "csv"
    if p.endswith(".parquet"):
        return "parquet"
    return None


def build_to_text(tokenizer, text_field):
    """Return a map-fn that replicates the trainer's chat-template formatting."""
    def to_text(example):
        if text_field in example and isinstance(example[text_field], str):
            return {text_field: example[text_field]}

        msgs = []
        sys_prompt = example.get("system")

        if "messages" in example:
            incoming = example["messages"]
            if sys_prompt and not (incoming and incoming[0].get("role") == "system"):
                msgs.append({"role": "system", "content": sys_prompt})
            msgs.extend(incoming)
        elif "conversations" in example:
            role_map = {"human": "user", "gpt": "assistant", "system": "system",
                        "tool": "tool", "observation": "tool"}
            conv = example["conversations"]
            if sys_prompt and not (bool(conv) and conv[0].get("from") == "system"):
                msgs.append({"role": "system", "content": sys_prompt})
            for t in conv:
                msgs.append({"role": role_map.get(t.get("from"), t.get("from")),
                              "content": t.get("value", "")})
        elif "instruction" in example:
            if sys_prompt:
                msgs.append({"role": "system", "content": sys_prompt})
            user = example["instruction"]
            if example.get("input"):
                user += "\n\n" + example["input"]
            msgs.append({"role": "user", "content": user})
            msgs.append({"role": "assistant", "content": example.get("output", "")})

        if not msgs:
            raise ValueError(f"Couldn't find text/messages in example: {list(example)}")

        text = tokenizer.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=False, enable_thinking=False,
        )
        return {text_field: text}
    return to_text


def load_and_format(path, tokenizer, text_field, num_proc):
    ext = detect_ext(path)
    if ext:
        raw = load_dataset(ext, data_files=path, split="train")
    else:
        raw = load_dataset(path, split="train")
    to_text = build_to_text(tokenizer, text_field)
    return raw.map(
        to_text,
        remove_columns=[c for c in raw.column_names if c != text_field],
        num_proc=num_proc,
    )


def tokenize_dataset(dataset, tokenizer, text_field, max_seq_length, num_proc):
    """Return a list of token counts, one per example (capped at max_seq_length)."""
    def count_tokens(batch):
        encoded = tokenizer(
            batch[text_field],
            truncation=True,
            max_length=max_seq_length,
            add_special_tokens=False,   # chat template already adds them
        )
        return {"token_count": [len(ids) for ids in encoded["input_ids"]]}

    counted = dataset.map(count_tokens, batched=True, num_proc=num_proc,
                          remove_columns=[text_field])
    return counted["token_count"]


def percentile(sorted_vals, p):
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def print_report(token_counts, max_seq_length, num_epochs, dataset_path):
    n = len(token_counts)
    if n == 0:
        print("Dataset is empty.")
        return

    sorted_counts = sorted(token_counts)
    total = sum(token_counts)
    truncated = sum(1 for c in token_counts if c >= max_seq_length)

    mean   = total / n
    median = statistics.median(token_counts)
    stdev  = statistics.stdev(token_counts) if n > 1 else 0

    print()
    print("=" * 60)
    print("  DATASET TOKEN AUDIT")
    print("=" * 60)
    print(f"  File            : {dataset_path}")
    print(f"  max_seq_length  : {max_seq_length:,}")
    print(f"  num_epochs      : {num_epochs}")
    print("-" * 60)
    print(f"  Examples        : {n:,}")
    print(f"  Total tokens    : {total:,}")
    print(f"  Tokens/example  : mean={mean:,.1f}  median={median:,.0f}  stdev={stdev:,.1f}")
    print(f"  Min / Max       : {sorted_counts[0]:,} / {sorted_counts[-1]:,}")
    print("-" * 60)
    print("  Percentiles (token length):")
    for p in [50, 75, 90, 95, 99, 100]:
        print(f"    p{p:3d} : {percentile(sorted_counts, p):,}")
    print("-" * 60)
    print(f"  Truncated (>= max_seq_length) : {truncated:,}  "
          f"({100 * truncated / n:.1f}%)")
    print("-" * 60)
    print("  Training-token estimate:")
    print(f"    Tokens per epoch      : {total:,}")
    print(f"    Tokens over {num_epochs} epochs : {total * num_epochs:,}")
    print("=" * 60)
    print()


def main():
    args = parse_args()

    print(f"Loading tokenizer: {args.tokenizer_name} ...")
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name)

    print(f"Loading & formatting dataset: {args.dataset_path} ...")
    dataset = load_and_format(
        args.dataset_path, tokenizer, args.text_field, args.num_proc
    )
    print(f"  {len(dataset):,} examples loaded.")

    print("Tokenising ...")
    token_counts = tokenize_dataset(
        dataset, tokenizer, args.text_field, args.max_seq_length, args.num_proc
    )

    print_report(token_counts, args.max_seq_length, args.num_epochs, args.dataset_path)


if __name__ == "__main__":
    main()
