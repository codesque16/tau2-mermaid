# train.py
# Multi-GPU (DDP) full SFT / LoRA fine-tune of Qwen3-8B with Unsloth + TRL,
# logging to TensorBoard.
#
# Launch on your 8x A100 node:
#     torchrun --nproc_per_node 8 train.py --learning_rate 2e-5 --grad_accum 8
#
# (Single GPU also works:  python train.py )
# DDP is auto-enabled by the HF Trainer whenever it sees >1 process.
#
# Every run now writes into its OWN directory:
#     <output_dir>/<run_name>/            <- checkpoints (checkpoint-N), final_model, tb/
# so separate runs never overwrite each other's checkpoints.
#
# Watch metrics live (shows every run as a separate curve):
#     tensorboard --logdir 1k_run_full_sft --port 6006
# -----------------------------------------------------------------------------

# IMPORTANT: import unsloth FIRST so its patches apply before transformers/trl load.
from unsloth import FastLanguageModel, is_bfloat16_supported

import os
import math
import argparse
from datetime import datetime

# This VM is provisioned for Google's gIB fabric (it sets NCCL_NET=gIB), which
# can't initialize on a plain single-node box and aborts NCCL. Force the built-in
# socket transport for bootstrap; data still moves GPU-to-GPU over NVLink/P2P.
# Direct assignment (not setdefault) so it OVERRIDES the inherited NCCL_NET=gIB.
os.environ["NCCL_NET"] = "Socket"
os.environ["NCCL_IB_DISABLE"] = "1"

# Let the allocator give back/coalesce memory between the training step and the
# eval forward (eval needs one big contiguous fp32-logits block).
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
from datasets import load_dataset
from trl import SFTTrainer, SFTConfig

# =============================== CONFIG ======================================
# Anything below can be overridden on the command line, e.g.:
#   torchrun --nproc_per_node 8 train.py \
#       --learning_rate 1e-5 --per_device_batch 1 --grad_accum 8 \
#       --num_epochs 2 --do_eval --run_name my_full_sft_v1
# CLI flags win; the values here are just the defaults.
def parse_args():
    p = argparse.ArgumentParser(description="Full SFT / LoRA fine-tune of Qwen3-8B")

    # --- hyperparams (the ones you asked to expose) ---
    p.add_argument("--learning_rate", type=float, default=2e-5)
    p.add_argument("--per_device_batch", type=int, default=1)
    p.add_argument("--grad_accum", type=int, default=8)
    p.add_argument("--num_epochs", type=int, default=3)
    p.add_argument("--max_steps", type=int, default=-1)
    p.add_argument("--max_seq_length", type=int, default=16384)
    p.add_argument("--warmup_steps", type=int, default=5)
    p.add_argument("--optim", type=str, default="adamw_8bit")
    p.add_argument("--weight_decay", type=float, default=0.0)
    p.add_argument("--lr_scheduler_type", type=str, default="linear")
    p.add_argument("--seed", type=int, default=3407)

    # --- data ---
    p.add_argument("--dataset_path", type=str,
                   default="/home/shiladitya/my_out/depth_03/split_file_09.jsonl")
    p.add_argument("--text_field", type=str, default="text")

    # --- eval (required for "best checkpoint" tracking) ---
    p.add_argument("--do_eval", action="store_true",
                   help="Turn on eval. REQUIRED for best-checkpoint saving.")
    p.add_argument("--eval_dataset_path", type=str,
                   default="/home/shiladitya/my_out/depth_03/split_file_22.jsonl",
                   help="Explicit eval file. Set to '' to carve a holdout instead.")
    p.add_argument("--eval_fraction", type=float, default=0.02)
    p.add_argument("--num_evals", type=int, default=9)
    p.add_argument("--eval_batch", type=int, default=4)
    p.add_argument("--eval_max_samples", type=int, default=512)

    # --- output / checkpoints ---
    p.add_argument("--output_dir", type=str, default="qwen_training_runs")
    p.add_argument("--run_name", type=str, default=None,
                   help="Name for this run's subfolder. Default = timestamp.")
    p.add_argument("--save_total_limit", type=int, default=2,
                   help="How many step-checkpoints to keep (best is always kept on top).")

    return p.parse_args()


args = parse_args()

MODEL_NAME       = "Qwen/Qwen3-8B"
MAX_SEQ_LENGTH   = args.max_seq_length
LOAD_IN_4BIT     = False               # full SFT / 16-bit LoRA base

# LoRA (only used if you re-enable the get_peft_model block below)
LORA_RANK        = 32
LORA_ALPHA       = 64
LORA_DROPOUT     = 0

# Hyperparams (from CLI)
NUM_EPOCHS       = args.num_epochs
LEARNING_RATE    = args.learning_rate
OPTIM            = args.optim
WARMUP_STEPS     = args.warmup_steps
MAX_STEPS        = args.max_steps
PER_DEVICE_BATCH = args.per_device_batch
GRAD_ACCUM       = args.grad_accum

# Data
DATASET_PATH     = args.dataset_path
TEXT_FIELD       = args.text_field

# Eval
DO_EVAL           = args.do_eval
EVAL_DATASET_PATH = args.eval_dataset_path
EVAL_FRACTION     = args.eval_fraction
NUM_EVALS         = args.num_evals
EVAL_BATCH        = args.eval_batch
EVAL_MAX_SAMPLES  = args.eval_max_samples

# Mask loss on user/tool turns so the model only learns its own assistant
# replies + tool_calls. Strongly recommended for multi-turn agent/tool data.
TRAIN_ON_RESPONSES_ONLY = True

OUTPUT_DIR       = args.output_dir
RUN_NAME         = args.run_name
SEED             = args.seed
SAVE_TOTAL_LIMIT = args.save_total_limit
# =============================================================================

# Pin each torchrun process to its own GPU (the key bit for Unsloth + DDP).
local_rank = int(os.environ.get("LOCAL_RANK", 0))
world_size = int(os.environ.get("WORLD_SIZE", 1))
torch.cuda.set_device(local_rank)
is_main = local_rank == 0


def log(*a):
    if is_main:
        print(*a, flush=True)


# ---- Per-run directory: EVERYTHING for this run lives under here ----
# This is what stops different runs from overwriting each other's checkpoints.
RUN_NAME = RUN_NAME or datetime.now().strftime("run-%Y%m%d-%H%M%S")
RUN_DIR  = os.path.join(OUTPUT_DIR, RUN_NAME)   # checkpoints + final model land here
LOG_DIR  = os.path.join(RUN_DIR, "tb")          # tensorboard events for this run
os.makedirs(RUN_DIR, exist_ok=True)
log(f"Run directory:        {RUN_DIR}")
log(f"TensorBoard log dir:  {LOG_DIR}")


# ----------------------------- model -----------------------------------------
# NOTE: not wrapping with get_peft_model => full fine-tuning (all base params
# trainable). Re-enable the block below for LoRA instead.
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name     = MODEL_NAME,
    max_seq_length = MAX_SEQ_LENGTH,
    dtype          = None,                 # auto: bf16 on Ampere+
    load_in_4bit   = LOAD_IN_4BIT,
    device_map     = {"": local_rank},     # one full copy per GPU = DDP
)

#"""
model = FastLanguageModel.get_peft_model(
    model,
    r              = LORA_RANK,
    lora_alpha     = LORA_ALPHA,
    lora_dropout   = LORA_DROPOUT,
    target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                      "gate_proj", "up_proj", "down_proj"],
    bias           = "none",
    use_gradient_checkpointing = "unsloth",
    random_state   = SEED,
)
#"""

# ----------------------------- data ------------------------------------------
def detect_ext(path):
    p = path.lower()
    if p.endswith(".jsonl") or p.endswith(".json"):
        return "json"
    if p.endswith(".csv"):
        return "csv"
    if p.endswith(".parquet"):
        return "parquet"
    return None  # treat as a HF hub dataset name


def to_text(example):
    """Normalize common dataset shapes into a single `text` field using the
    Qwen3 chat template. Handles ShareGPT `conversations` + a separate top-level
    `system` field (as in your tool-calling dataset)."""
    # already plain text
    if TEXT_FIELD in example and isinstance(example[TEXT_FIELD], str):
        return {TEXT_FIELD: example[TEXT_FIELD]}

    msgs = []

    # Top-level system prompt (your `system` field holds the policy + tools).
    sys_prompt = example.get("system")

    if "messages" in example:                      # [{"role","content"}, ...]
        incoming = example["messages"]
        if sys_prompt and not (incoming and incoming[0].get("role") == "system"):
            msgs.append({"role": "system", "content": sys_prompt})
        msgs.extend(incoming)
    elif "conversations" in example:               # ShareGPT [{"from","value"}, ...]
        role_map = {"human": "user", "gpt": "assistant", "system": "system",
                    "tool": "tool", "observation": "tool"}
        conv = example["conversations"]
        starts_with_system = bool(conv) and conv[0].get("from") == "system"
        if sys_prompt and not starts_with_system:
            msgs.append({"role": "system", "content": sys_prompt})
        for t in conv:
            msgs.append({"role": role_map.get(t.get("from"), t.get("from")),
                         "content": t.get("value", "")})
    elif "instruction" in example:                 # Alpaca-style
        if sys_prompt:
            msgs.append({"role": "system", "content": sys_prompt})
        user = example["instruction"]
        if example.get("input"):
            user += "\n\n" + example["input"]
        msgs.append({"role": "user", "content": user})
        msgs.append({"role": "assistant", "content": example.get("output", "")})

    if not msgs:
        raise ValueError(f"Couldn't find text/messages in example: {list(example)}")

    # tools are already embedded inside the `system` text, so we do NOT pass tools=.
    # enable_thinking=False -> no <think> scaffolding (non-thinking tool agent).
    text = tokenizer.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=False, enable_thinking=False,
    )
    return {TEXT_FIELD: text}


def load_and_format(path):
    """Load a data file (or HF dataset name) and normalize it to a `text` column."""
    ext = detect_ext(path)
    if ext:
        raw = load_dataset(ext, data_files=path, split="train")
    else:
        raw = load_dataset(path, split="train")
    return raw.map(
        to_text,
        remove_columns=[c for c in raw.column_names if c != TEXT_FIELD],
    )


dataset = load_and_format(DATASET_PATH)

eval_dataset = None
if DO_EVAL:
    if EVAL_DATASET_PATH:
        # explicit eval file -> use it directly (no holdout taken from train)
        eval_dataset = load_and_format(EVAL_DATASET_PATH)
    elif EVAL_FRACTION > 0:
        # no eval file given -> carve a holdout from the training set
        split = dataset.train_test_split(test_size=EVAL_FRACTION, seed=SEED)
        dataset, eval_dataset = split["train"], split["test"]

    # Cap eval size so evaluation stays quick.
    if eval_dataset is not None and EVAL_MAX_SAMPLES and len(eval_dataset) > EVAL_MAX_SAMPLES:
        eval_dataset = eval_dataset.shuffle(seed=SEED).select(range(EVAL_MAX_SAMPLES))

log(f"World size: {world_size} | per-device batch: {PER_DEVICE_BATCH} | "
    f"grad accum: {GRAD_ACCUM} | global batch: "
    f"{PER_DEVICE_BATCH * GRAD_ACCUM * world_size}")
log(f"Train examples: {len(dataset)}"
    + (f" | Eval examples: {len(eval_dataset)}" if eval_dataset else ""))

# Translate "NUM_EVALS evals across the run" into an interval (in steps).
_global_batch = PER_DEVICE_BATCH * GRAD_ACCUM * world_size
_total_steps  = math.ceil(len(dataset) / _global_batch) * NUM_EPOCHS
EVAL_STEPS    = max(1, _total_steps // NUM_EVALS)
log(f"Total steps: {_total_steps} | eval/save every {EVAL_STEPS} steps "
    f"(~{NUM_EVALS} evals)")

# ---- checkpoint / best-model strategy ----
# Best-checkpoint tracking REQUIRES eval. With eval on we save on the same cadence
# as eval (steps) so load_best_model_at_end can compare them. Without eval we fall
# back to per-epoch checkpoints and there is no "best".
HAVE_EVAL = eval_dataset is not None
if HAVE_EVAL:
    eval_strategy = "steps"
    save_strategy = "steps"          # must match eval_strategy for best-model
    save_steps    = EVAL_STEPS       # == eval_steps so they line up
else:
    eval_strategy = "no"
    save_strategy = "epoch"
    save_steps    = None
    log("NOTE: --do_eval not set -> no eval, so NO best-checkpoint tracking. "
        "Saving per-epoch checkpoints only.")


# ----------------------------- trainer ---------------------------------------
sft_config = SFTConfig(
    # NOTE: newer TRL uses `max_length`; on older TRL rename this to max_seq_length.
    max_length                  = MAX_SEQ_LENGTH,
    dataset_text_field          = TEXT_FIELD,
    packing                     = False,

    per_device_train_batch_size = PER_DEVICE_BATCH,
    gradient_accumulation_steps = GRAD_ACCUM,
    num_train_epochs            = NUM_EPOCHS,
    max_steps                   = MAX_STEPS,

    learning_rate               = LEARNING_RATE,
    optim                       = OPTIM,
    warmup_steps                = WARMUP_STEPS,
    lr_scheduler_type           = args.lr_scheduler_type,
    weight_decay                = args.weight_decay,

    bf16                        = is_bfloat16_supported(),
    fp16                        = not is_bfloat16_supported(),

    # Full FT has frozen/tied embeddings inside the DDP module -> True avoids the
    # "parameters that were not used in producing loss" reducer error.
    ddp_find_unused_parameters  = False,
    #ddp_find_unused_parameters  = True,

    # ---- TensorBoard logging (per-run dir) ----
    report_to                   = "tensorboard",
    logging_dir                 = LOG_DIR,
    logging_steps               = 1,

    # ---- eval ----
    eval_strategy               = eval_strategy,
    eval_steps                  = EVAL_STEPS if HAVE_EVAL else None,
    per_device_eval_batch_size  = EVAL_BATCH,
    bf16_full_eval              = is_bfloat16_supported(),

    # ---- checkpoints + best-model ----
    output_dir                  = RUN_DIR,          # <-- per-run: no cross-run overwrite
    save_strategy               = save_strategy,
    save_steps                  = save_steps,
    save_total_limit            = SAVE_TOTAL_LIMIT,
    save_only_model             = True, 
    load_best_model_at_end      = HAVE_EVAL,        # keep + restore the best checkpoint
    metric_for_best_model       = "eval_loss",
    greater_is_better           = False,

    seed                        = SEED,
    dataset_num_proc            = 4,
)

trainer = SFTTrainer(
    model         = model,
    tokenizer     = tokenizer,   # newer TRL: rename to processing_class = tokenizer
    train_dataset = dataset,
    eval_dataset  = eval_dataset,
    args          = sft_config,
)

# Train only on the assistant turns (recommended for this dataset).
if TRAIN_ON_RESPONSES_ONLY:
    from unsloth.chat_templates import train_on_responses_only
    trainer = train_on_responses_only(
        trainer,
        instruction_part = "<|im_start|>user\n",       # Qwen3 ChatML
        response_part    = "<|im_start|>assistant\n",
    )

trainer.train()

# ----------------------------- save ------------------------------------------
# With load_best_model_at_end=True the in-memory model is already the BEST one,
# so this final save IS the best checkpoint (full model + tokenizer).
if is_main:
    final_dir = os.path.join(RUN_DIR, "final_model")
    model.save_pretrained(final_dir)
    tokenizer.save_pretrained(final_dir)
    log(f"Saved final{' (best)' if HAVE_EVAL else ''} model to: {final_dir}")

log("Done.")