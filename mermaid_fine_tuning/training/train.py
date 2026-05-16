"""Unified training script for the SOP-agent project.

Reads an experiment manifest YAML and dispatches to QLoRA, full SFT, or DPO.
Designed to be launched via `accelerate launch` for multi-GPU runs (full SFT,
DPO) or run directly for single-GPU runs (QLoRA).

Usage:
    # QLoRA, single GPU:
    python -m training.train --manifest experiments/qlora_26b_pilot/manifest.yaml

    # Full SFT, 4 GPUs with FSDP:
    accelerate launch --config_file accelerate_configs/fsdp_4gpu.yaml \\
        -m training.train --manifest experiments/full_sft_26b/manifest.yaml

The manifest controls everything; this script just executes it.
"""
from __future__ import annotations
import sys, os, json, argparse, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import torch
from datasets import Dataset
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

from training.manifest import ExperimentManifest


# ============================================================
# Dataset loading
# ============================================================

def load_jsonl_dataset(path: str, max_n: int | None = None, seed: int = 42) -> Dataset:
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    if max_n is not None and max_n < len(rows):
        random.Random(seed).shuffle(rows)
        rows = rows[:max_n]
    return Dataset.from_list(rows)


# ============================================================
# Mode-specific model loaders
# ============================================================

def _unwrap_gemma4_clippable_linear(model):
    """Replace Gemma4ClippableLinear wrappers with their inner Linear4bit.

    Gemma 4 wraps every Linear in a custom Gemma4ClippableLinear class to support
    activation clipping. PEFT's LoRA only recognizes a fixed set of base linear
    classes and refuses to wrap unknown layer types. We swap each wrapper out
    for its inner `.linear` attribute *before* LoRA injection.

    The activation-clipping behavior is lost for these specific layers, which
    is acceptable for QLoRA fine-tuning — the inner Linear4bit handles
    quantized matmul correctly, and we're not relying on FP8 clipping during
    fine-tuning anyway.
    """
    import torch.nn as nn
    replaced = 0
    for name, module in list(model.named_modules()):
        if type(module).__name__ != "Gemma4ClippableLinear":
            continue
        inner = getattr(module, "linear", None)
        if inner is None:
            continue
        # Walk to the parent and swap this child for the inner linear
        parent_path, _, child_name = name.rpartition(".")
        parent = model.get_submodule(parent_path) if parent_path else model
        setattr(parent, child_name, inner)
        replaced += 1
    if replaced:
        print(f"[gemma4_unwrap] replaced {replaced} Gemma4ClippableLinear layers with inner linear")
    return model


def load_qlora_model(model_spec, qlora_spec):
    from peft import LoraConfig, prepare_model_for_kbit_training, get_peft_model

    quant_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=getattr(torch, model_spec.torch_dtype),
        bnb_4bit_quant_type=qlora_spec.bnb_4bit_quant_type,
        bnb_4bit_use_double_quant=qlora_spec.bnb_4bit_use_double_quant,
    )
    kwargs = dict(
        quantization_config=quant_config,
        # `torch_dtype` was renamed to `dtype` in transformers; this avoids the
        # deprecation warning printed on every from_pretrained call.
        dtype=getattr(torch, model_spec.torch_dtype),
        # IMPORTANT: pin QLoRA to a SINGLE GPU. With device_map="auto", HF
        # silently shards the 4-bit weights across all visible GPUs. That
        # "fits" at load time, but during training the optimizer state +
        # activations land on the GPU holding the largest shard and OOM.
        # QLoRA's whole premise is that one 4-bit model fits on one GPU;
        # use device_map="cuda:0" and set CUDA_VISIBLE_DEVICES=N to pick
        # which GPU. The other GPUs are then free for parallel experiments.
        device_map={"": 0},
    )
    if model_spec.attn_implementation:
        kwargs["attn_implementation"] = model_spec.attn_implementation

    model = AutoModelForCausalLM.from_pretrained(model_spec.name, **kwargs)
    model = _unwrap_gemma4_clippable_linear(model)
    model = prepare_model_for_kbit_training(model)
    lora_cfg = LoraConfig(
        r=qlora_spec.r,
        lora_alpha=qlora_spec.alpha,
        lora_dropout=qlora_spec.dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=qlora_spec.target_modules,
    )
    model = get_peft_model(model, lora_cfg)
    return model


def load_full_sft_model(model_spec, full_sft_spec):
    """Full-parameter SFT model. FSDP wrapping is applied by accelerate launch."""
    kwargs = dict(dtype=getattr(torch, model_spec.torch_dtype))
    if model_spec.attn_implementation:
        kwargs["attn_implementation"] = model_spec.attn_implementation
    # Note: NO device_map here — FSDP will handle sharding via accelerate.
    model = AutoModelForCausalLM.from_pretrained(model_spec.name, **kwargs)
    # Unwrap Gemma4ClippableLinear for the same reason as in QLoRA: FSDP's
    # transformer-layer auto-wrap policy walks the module tree and trips on
    # the custom class.
    model = _unwrap_gemma4_clippable_linear(model)
    if full_sft_spec.activation_checkpointing:
        model.gradient_checkpointing_enable()
    return model


# ============================================================
# Training mode dispatchers
# ============================================================

def train_sft(manifest: ExperimentManifest, output_dir: str):
    """Shared path for both QLoRA and full SFT. Mode determines model load."""
    from trl import SFTTrainer, SFTConfig

    print(f"[train] mode={manifest.mode} model={manifest.model.name}")
    tokenizer = AutoTokenizer.from_pretrained(manifest.model.name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if manifest.mode == "qlora":
        model = load_qlora_model(manifest.model, manifest.qlora)
        model.print_trainable_parameters()
    elif manifest.mode == "full_sft":
        model = load_full_sft_model(manifest.model, manifest.full_sft)
    else:
        raise ValueError(f"train_sft does not handle mode={manifest.mode}")

    # Data
    train_ds = load_jsonl_dataset(
        manifest.data.train_path,
        max_n=manifest.data.train_subsample,
        seed=manifest.data.train_subsample_seed,
    )
    eval_ds = load_jsonl_dataset(manifest.data.eval_path) if os.path.exists(manifest.data.eval_path) else None
    print(f"[train] train={len(train_ds)} eval={len(eval_ds) if eval_ds else 0}")

    # Note: we previously passed a `formatting_func` that called
    # `tokenizer.apply_chat_template(example["messages"])`. That's incompatible
    # with `completion_only_loss=True` because the flattened string loses the
    # prompt/completion boundary. TRL's SFTTrainer recognizes the conversational
    # format natively when the dataset has a `messages` column (which
    # format_dataset.py produces), so we just hand it the dataset directly and
    # TRL applies the chat template + identifies the completion span itself.

    t = manifest.training
    sft_config = SFTConfig(
        output_dir=output_dir,
        num_train_epochs=t.num_train_epochs,
        per_device_train_batch_size=t.per_device_train_batch_size,
        gradient_accumulation_steps=t.gradient_accumulation_steps,
        learning_rate=t.learning_rate,
        warmup_ratio=t.warmup_ratio,
        lr_scheduler_type=t.lr_scheduler_type,
        logging_steps=t.logging_steps,
        save_steps=t.save_steps,
        eval_steps=t.eval_steps,
        save_total_limit=t.save_total_limit,
        bf16=t.bf16,
        optim=t.optim if manifest.mode == "qlora" else "adamw_torch",
        max_length=t.max_seq_length,
        gradient_checkpointing=t.gradient_checkpointing,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        # NOTE: `group_by_length` was removed from SFTConfig in TRL v1+.
        # Length-aware batching now happens via `packing=True` (best-fit
        # decreasing). We leave packing off here to keep examples 1:1 with
        # eval — small dataset, modest training time, packing's efficiency
        # win doesn't matter much at this scale.
        report_to="none",
        # In-loop eval can OOM at long context because eval doesn't use grad
        # checkpointing (it allocates full logits = seq_len * vocab * 4B).
        # We disable it via skip_in_loop_eval and rely on the post-training
        # eval_harness pass (see experiments/run.sh) for the real numbers.
        eval_strategy="no" if t.skip_in_loop_eval else ("steps" if eval_ds else "no"),
        # Loss masking:
        # We use the prompt-completion dataset shape (see format_dataset.py),
        # which makes TRL compute loss on completion tokens only by default —
        # no need to set `completion_only_loss=True` explicitly.
        # `assistant_only_loss=True` is the alternative, but it requires the
        # chat template to include `{% generation %}` markers (Gemma 4's
        # template lacks them, and TRL only auto-patches Qwen3-family).
        packing=False,
        seed=t.seed,
    )

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=train_ds,
        eval_dataset=None if t.skip_in_loop_eval else eval_ds,
        processing_class=tokenizer,
    )
    trainer.train()
    final_dir = os.path.join(output_dir, "final")
    trainer.save_model(final_dir)
    tokenizer.save_pretrained(final_dir)
    print(f"[train] saved to {final_dir}")
    return final_dir


def train_dpo(manifest: ExperimentManifest, output_dir: str):
    """DPO over an SFT checkpoint, with preference-pair data."""
    from trl import DPOTrainer, DPOConfig
    from peft import PeftModel

    print(f"[dpo] starting from sft_checkpoint={manifest.dpo.sft_checkpoint}")
    tokenizer = AutoTokenizer.from_pretrained(manifest.model.name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Load base model + SFT adapter (if applicable) for the policy
    base_model = AutoModelForCausalLM.from_pretrained(
        manifest.model.name,
        dtype=getattr(torch, manifest.model.torch_dtype),
        device_map="auto",
    )
    if manifest.dpo.sft_checkpoint:
        # Assume QLoRA SFT checkpoint; load LoRA on top.
        model = PeftModel.from_pretrained(base_model, manifest.dpo.sft_checkpoint, is_trainable=True)
    else:
        model = base_model

    pref_ds = load_jsonl_dataset(manifest.dpo.preference_data_path)
    print(f"[dpo] {len(pref_ds)} preference pairs")

    t = manifest.training
    dpo_config = DPOConfig(
        output_dir=output_dir,
        num_train_epochs=t.num_train_epochs,
        per_device_train_batch_size=t.per_device_train_batch_size,
        gradient_accumulation_steps=t.gradient_accumulation_steps,
        learning_rate=t.learning_rate,
        warmup_ratio=t.warmup_ratio,
        lr_scheduler_type=t.lr_scheduler_type,
        logging_steps=t.logging_steps,
        save_steps=t.save_steps,
        beta=manifest.dpo.beta,
        bf16=t.bf16,
        max_length=t.max_seq_length,
        report_to="none",
        seed=t.seed,
    )

    trainer = DPOTrainer(
        model=model,
        args=dpo_config,
        train_dataset=pref_ds,
        processing_class=tokenizer,
    )
    trainer.train()
    final_dir = os.path.join(output_dir, "final")
    trainer.save_model(final_dir)
    tokenizer.save_pretrained(final_dir)
    print(f"[dpo] saved to {final_dir}")
    return final_dir


# ============================================================
# Entry point
# ============================================================

def main(manifest_path: str):
    with open(manifest_path) as f:
        raw = yaml.safe_load(f)
    manifest = ExperimentManifest(**raw)
    manifest.validate_mode()

    # All outputs go under experiments/<name>/
    experiment_root = os.path.dirname(os.path.abspath(manifest_path))
    checkpoint_dir = os.path.join(experiment_root, "checkpoints")
    os.makedirs(checkpoint_dir, exist_ok=True)

    print(f"\n{'='*60}\nEXPERIMENT: {manifest.name}\n{'='*60}")
    print(f"description: {manifest.description}")
    print(f"output: {experiment_root}\n")

    if manifest.mode in ("qlora", "full_sft"):
        final_dir = train_sft(manifest, checkpoint_dir)
    elif manifest.mode == "dpo":
        final_dir = train_dpo(manifest, checkpoint_dir)
    else:
        raise ValueError(f"Unknown mode: {manifest.mode}")

    # Write a marker so eval / compare know where the final model lives.
    with open(os.path.join(experiment_root, "final_checkpoint.txt"), "w") as f:
        f.write(final_dir + "\n")

    print(f"\n[done] final checkpoint: {final_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="path to experiment manifest YAML")
    args = ap.parse_args()
    main(args.manifest)