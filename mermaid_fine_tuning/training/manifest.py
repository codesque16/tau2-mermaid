"""Experiment manifest schema.

One YAML per experiment. The manifest fully describes a training + eval run,
so it's self-documenting and reproducible.

See experiments/ for example manifests.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class DataSpec(BaseModel):
    """Where the training and eval data come from."""
    train_path: str
    eval_path: str
    graphs_path: str  # needed by eval_harness to resolve domains
    # Optional subsampling for scaling experiments
    train_subsample: Optional[int] = None
    train_subsample_seed: int = 42


class ModelSpec(BaseModel):
    """Which base model to start from."""
    name: str  # e.g., "google/gemma-4-26b-a4b-it" or "google/gemma-4-e4b-it"
    torch_dtype: Literal["bfloat16", "float16", "float32"] = "bfloat16"
    attn_implementation: Optional[str] = None  # None | "flash_attention_2" | "sdpa"


class QLoRASpec(BaseModel):
    """LoRA + 4-bit quantization config."""
    r: int = 64
    alpha: int = 128
    dropout: float = 0.05
    target_modules: list[str] = Field(
        default_factory=lambda: [
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ]
    )
    bnb_4bit_quant_type: Literal["nf4", "fp4"] = "nf4"
    bnb_4bit_use_double_quant: bool = True


class FullSFTSpec(BaseModel):
    """Full SFT (no LoRA, no quantization). FSDP wrapping handled at launch."""
    activation_checkpointing: bool = True


class DPOSpec(BaseModel):
    """DPO config. Pairs come from a separate JSONL with {prompt, chosen, rejected}."""
    beta: float = 0.1
    preference_data_path: str
    # If starting from an SFT checkpoint:
    sft_checkpoint: Optional[str] = None


class TrainingHParams(BaseModel):
    num_train_epochs: int = 3
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 16
    learning_rate: float = 1.0e-4
    warmup_ratio: float = 0.03
    lr_scheduler_type: str = "cosine"
    max_seq_length: int = 8192
    logging_steps: int = 10
    save_steps: int = 200
    eval_steps: int = 200
    save_total_limit: int = 3
    gradient_checkpointing: bool = True
    group_by_length: bool = True
    optim: str = "paged_adamw_8bit"
    bf16: bool = True
    seed: int = 42
    # If True, skip the per-N-step in-loop eval that HF Trainer does. The
    # final eval we actually care about runs after training via
    # eval_harness.py (see experiments/run.sh). Skipping the in-loop eval
    # also avoids OOM at long context — eval doesn't use grad checkpointing
    # so it allocates the full logits tensor (seq_len * vocab * 4B), which
    # at 8k context * 256k vocab ≈ 8 GB and easily OOMs after training has
    # filled up the rest of VRAM.
    skip_in_loop_eval: bool = False


class EvalSpec(BaseModel):
    """Auto-eval config. Set run_eval=False to skip."""
    run_eval: bool = True
    quantize_for_eval: bool = True  # 4-bit eval to fit larger models per GPU
    max_examples: Optional[int] = None


class ExperimentManifest(BaseModel):
    """Top-level experiment definition. One YAML file = one of these."""
    name: str
    description: str = ""
    mode: Literal["qlora", "full_sft", "dpo"]

    data: DataSpec
    model: ModelSpec
    training: TrainingHParams = Field(default_factory=TrainingHParams)
    eval_spec: EvalSpec = Field(default_factory=EvalSpec)

    # Mode-specific sections. Exactly one is consulted based on `mode`.
    qlora: Optional[QLoRASpec] = None
    full_sft: Optional[FullSFTSpec] = None
    dpo: Optional[DPOSpec] = None

    def validate_mode(self) -> None:
        """Ensure the right sub-config is present for the chosen mode."""
        if self.mode == "qlora" and self.qlora is None:
            raise ValueError("mode=qlora requires `qlora:` section in manifest")
        if self.mode == "full_sft" and self.full_sft is None:
            raise ValueError("mode=full_sft requires `full_sft:` section in manifest")
        if self.mode == "dpo" and self.dpo is None:
            raise ValueError("mode=dpo requires `dpo:` section in manifest")