# Track 3 Experiment Suite

Each subdirectory here is one experiment. Each contains exactly one
`manifest.yaml` that fully describes the run (model, mode, data, hyperparameters).
Running an experiment writes its outputs back into the subdirectory:
- `checkpoints/` — model weights (sharded for FSDP, single dir for QLoRA)
- `final_checkpoint.txt` — pointer to the directory eval should load
- `train.log` — training stdout
- `eval.log` — eval stdout
- `eval.json` — per-skill metrics

So each experiment dir is self-contained — easy to share, easy to compare,
easy to delete if a run is a dud.

## Running

```bash
# One experiment, end-to-end (train + eval)
bash experiments/run.sh qlora_26b_pilot

# Compare results across experiments
python -m evaluation.compare_runs \
    experiments/qlora_e4b_pilot \
    experiments/qlora_26b_pilot \
    experiments/full_sft_26b
```

## The Track 3 sequence

The experiments below are designed to be run in this order. Each answers
one specific question and reuses outputs from the previous.

1. **qlora_e4b_pilot** — Smoke test on small model. Fast (~20 min on one A100).
   Validates the data pipeline → training → eval loop without burning compute
   on the big model first.

2. **qlora_26b_pilot** — Same data, bigger model. Asks: "Does the 26B's extra
   capacity actually help at small data scale?" Compare vs E4B above.

3. **full_sft_26b** — Same data, same model as #2, but full SFT across 4 GPUs.
   Asks: "Does full-parameter SFT lift over LoRA when data is small?" The
   answer matters because if QLoRA is enough, all subsequent experiments stay
   PEFT and you save 6-8x compute per run.

4. **dpo_after_sft** — Builds on #2 with preference pairs. Asks: "Can DPO on
   hard negatives further improve the SFT model?" This is the closest thing
   to RL in the pipeline without standing up GRPO infrastructure.

Once these four are done you have:
- A scaling-vs-size comparison (E4B vs 26B at fixed data)
- A PEFT-vs-full comparison (QLoRA 26B vs Full SFT 26B at fixed data)
- A preference-optimization data point (DPO on top of SFT)

Future experiments to add as the pipeline matures:
- **qlora_data_{1k, 5k, full}** — data-scaling curve (single GPU each, can run
  in parallel on the 4-GPU box with CUDA_VISIBLE_DEVICES)
- **grpo_26b** — GRPO with structural-correctness reward (needs proper rollout
  infrastructure; add after you have the structural reward function)

## Conventions

- Experiment names are lowercase with underscores
- LR for full SFT is ~10x lower than for LoRA
- Eval uses 4-bit quantization for QLoRA/E4B by default to save time;
  uncomment `quantize_for_eval: false` on full-SFT runs for a fairer signal
- All experiments use the same eval set, so numbers are comparable
