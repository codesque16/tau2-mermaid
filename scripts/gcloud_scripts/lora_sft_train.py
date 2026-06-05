# train.py
# Multi-GPU (DDP) LoRA fine-tune of Qwen3-8B with Unsloth + TRL, logging to TensorBoard.
#
# Launch on your 4x A100 node:
#     torchrun --nproc_per_node 4 train.py
#
# (Single GPU also works:  python train.py )
# DDP is auto-enabled by the HF Trainer whenever it sees >1 process.
#
# Watch metrics live:
#     tensorboard --logdir outputs/runs --port 6006
# -----------------------------------------------------------------------------

# IMPORTANT: import unsloth FIRST so its patches apply before transformers/trl load.
from unsloth import FastLanguageModel, is_bfloat16_supported

import os
import math
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
# --- matches your Unsloth Studio "Training Config" panel ---------------------
MODEL_NAME       = "Qwen/Qwen3-8B"     # the model you selected in Studio
MAX_SEQ_LENGTH   = 16384               # covers data max of 10,333 tokens, zero truncation
LOAD_IN_4BIT     = False               # variant = "lora" (16-bit base). True => QLoRA

# LoRA
LORA_RANK        = 32                  # Rank
LORA_ALPHA       = 64                  # Alpha
LORA_DROPOUT     = 0                   # Dropout

# Hyperparams
NUM_EPOCHS       = 3                   # Epochs
LEARNING_RATE    = 2e-4                # 0.0002
OPTIM            = "adamw_8bit"        # AdamW 8-bit
WARMUP_STEPS     = 5                   # Warmup steps
MAX_STEPS        = -1                  # Max steps = 0 in Studio -> use epochs (-1)

# Batch math:
#   global_batch = PER_DEVICE_BATCH * GRAD_ACCUM * num_gpus
# 16 per device * 1 * 4 GPUs = global batch 64 (8x your original Studio batch of 8).
# NOTE: this also drops total steps to ~57 over 3 epochs and may benefit from a
# higher LR. If you OOM at 32k context, lower this (8 -> global 32, 4 -> 16).
PER_DEVICE_BATCH = 16
GRAD_ACCUM       = 1

# Dataset -- point this at the file Studio created (the truncated path was
# /home/shiladitya/.unsloth/studio/...). .jsonl / .json / .csv / .parquet or a
# Hugging Face dataset name all work.
DATASET_PATH     = "/home/shiladitya/split_file_09.jsonl"  # <-- EDIT ME
TEXT_FIELD       = "text"              # column name produced after formatting below

# Eval. If EVAL_DATASET_PATH is set, that file is used directly.
# If it's None/"" and EVAL_FRACTION > 0, a holdout is carved from the train set instead.
DO_EVAL           = True
EVAL_DATASET_PATH = "/home/shiladitya/split_file_22.jsonl"  # set to None to use a holdout
EVAL_FRACTION     = 0.02               # only used when EVAL_DATASET_PATH is None/""
NUM_EVALS         = 9                  # run ~9 evals spread across the whole run
EVAL_BATCH        = 4                  # eval builds full logits over the ~152k vocab; bf16_full_eval
                                       # (below) halves that memory. Drop toward 1 if you OOM.
EVAL_MAX_SAMPLES  = 512                # cap eval set size for speed; None = use all 1270

# Mask loss on user/tool turns so the model only learns its own assistant
# replies + tool_calls. Strongly recommended for multi-turn agent/tool data.
TRAIN_ON_RESPONSES_ONLY = True

OUTPUT_DIR       = "outputs"
RUN_NAME         = None                # None -> auto timestamp. Set a string to name a run.
SEED             = 3407
# =============================================================================

# Pin each torchrun process to its own GPU (the key bit for Unsloth + DDP).
local_rank = int(os.environ.get("LOCAL_RANK", 0))
world_size = int(os.environ.get("WORLD_SIZE", 1))
torch.cuda.set_device(local_rank)
is_main = local_rank == 0


def log(*a):
    if is_main:
        print(*a, flush=True)


# Each launch gets its own TensorBoard subdir so runs don't merge into one.
# (Only rank 0 actually writes events, so a per-process timestamp is harmless.)
RUN_NAME = RUN_NAME or datetime.now().strftime("run-%Y%m%d-%H%M%S")
LOG_DIR  = os.path.join(OUTPUT_DIR, "runs", RUN_NAME)
log(f"TensorBoard run dir: {LOG_DIR}")


# ----------------------------- model -----------------------------------------
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name     = MODEL_NAME,
    max_seq_length = MAX_SEQ_LENGTH,
    dtype          = None,                 # auto: bf16 on H100
    load_in_4bit   = LOAD_IN_4BIT,
    device_map     = {"": local_rank},     # one full copy per GPU = DDP
)

model = FastLanguageModel.get_peft_model(
    model,
    r              = LORA_RANK,
    lora_alpha     = LORA_ALPHA,
    lora_dropout   = LORA_DROPOUT,
    target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                      "gate_proj", "up_proj", "down_proj"],
    bias           = "none",
    use_gradient_checkpointing = "unsloth",  # big VRAM saver at 32k context
    random_state   = SEED,
)


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
    # Prepended unless the conversation already starts with its own system turn.
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

    # NOTE: tools are already embedded inside the `system` text, so we do NOT
    # pass tools= here (that would duplicate them).
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

    # Cap eval size so evaluation stays quick (full 1270 is overkill for monitoring).
    if eval_dataset is not None and EVAL_MAX_SAMPLES and len(eval_dataset) > EVAL_MAX_SAMPLES:
        eval_dataset = eval_dataset.shuffle(seed=SEED).select(range(EVAL_MAX_SAMPLES))

log(f"World size: {world_size} | per-device batch: {PER_DEVICE_BATCH} | "
    f"grad accum: {GRAD_ACCUM} | global batch: "
    f"{PER_DEVICE_BATCH * GRAD_ACCUM * world_size}")
log(f"Train examples: {len(dataset)}"
    + (f" | Eval examples: {len(eval_dataset)}" if eval_dataset else ""))

# Translate "NUM_EVALS evals across the run" into an eval interval (in steps).
# total_steps = ceil(num_examples / global_batch) * epochs   (DDP shards the data)
_global_batch = PER_DEVICE_BATCH * GRAD_ACCUM * world_size
_total_steps  = math.ceil(len(dataset) / _global_batch) * NUM_EPOCHS
EVAL_STEPS    = max(1, _total_steps // NUM_EVALS)
log(f"Total steps: {_total_steps} | eval every {EVAL_STEPS} steps "
    f"(~{NUM_EVALS} evals)")


# ----------------------------- trainer ---------------------------------------
sft_config = SFTConfig(
    # NOTE: newer TRL uses `max_length`; on older TRL rename this to max_seq_length.
    max_length                  = MAX_SEQ_LENGTH,
    dataset_text_field          = TEXT_FIELD,
    packing                     = False,   # set True for up to ~5x speedup (changes loss/step count)

    per_device_train_batch_size = PER_DEVICE_BATCH,
    gradient_accumulation_steps = GRAD_ACCUM,
    num_train_epochs            = NUM_EPOCHS,
    max_steps                   = MAX_STEPS,

    learning_rate               = LEARNING_RATE,
    optim                       = OPTIM,
    warmup_steps                = WARMUP_STEPS,
    lr_scheduler_type           = "linear",
    weight_decay                = 0.0,

    bf16                        = is_bfloat16_supported(),
    fp16                        = not is_bfloat16_supported(),

    # DDP: LoRA has no unused params, so keep this False for speed.
    # If you hit a "marked as not used" error, flip to True.
    ddp_find_unused_parameters  = False,

    # ---- TensorBoard logging ----
    report_to                   = "tensorboard",
    logging_dir                 = LOG_DIR,
    logging_steps               = 1,          # per-step loss, like the Studio chart

    # eval
    eval_strategy               = "steps" if eval_dataset is not None else "no",
    eval_steps                  = EVAL_STEPS if eval_dataset is not None else None,
    per_device_eval_batch_size  = EVAL_BATCH,
    # Run eval in bf16 with no autocast -> skips upcasting the giant logits to
    # fp32 (the exact allocation that OOM'd). Halves eval logit memory.
    bf16_full_eval              = is_bfloat16_supported(),

    # checkpoints
    save_strategy               = "epoch",
    save_total_limit            = 2,

    output_dir                  = OUTPUT_DIR,
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
# Your rows carry a huge system policy + large JSON tool-results in the user/tool
# turns -- without masking, the model spends most of its loss learning to
# reproduce the policy and the tool outputs instead of its OWN replies and
# <tool_call>s. This masks everything up to each assistant turn.
if TRAIN_ON_RESPONSES_ONLY:
    from unsloth.chat_templates import train_on_responses_only
    trainer = train_on_responses_only(
        trainer,
        instruction_part = "<|im_start|>user\n",       # Qwen3 ChatML
        response_part    = "<|im_start|>assistant\n",
    )

trainer.train()

# ----------------------------- save ------------------------------------------
# Trainer already saves only on the main process. Save the LoRA adapter + tokenizer.
if is_main:
    adapter_dir = os.path.join(OUTPUT_DIR, "lora_adapter")
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    log(f"Saved LoRA adapter to: {adapter_dir}")

    # To merge into a standalone 16-bit model for vLLM/llama.cpp export, uncomment:
    # model.save_pretrained_merged(
    #     os.path.join(OUTPUT_DIR, "merged_16bit"), tokenizer, save_method="merged_16bit"
    # )

log("Done.")