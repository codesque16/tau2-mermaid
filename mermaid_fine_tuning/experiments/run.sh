#!/usr/bin/env bash
# Run one experiment end-to-end: train, then eval.
#
# Usage:
#   bash experiments/run.sh <experiment_name>
#
# Example:
#   bash experiments/run.sh qlora_26b_pilot
#
# The script reads experiments/<name>/manifest.yaml, dispatches to the right
# launcher based on the mode field, runs training, then runs eval against
# the resulting checkpoint and writes results.json under the experiment dir.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: bash experiments/run.sh <experiment_name>"
    echo "Available experiments:"
    ls -d experiments/*/  2>/dev/null | sed 's|experiments/||; s|/||' || echo "  (none yet)"
    exit 1
fi

NAME="$1"
EXP_DIR="experiments/${NAME}"
MANIFEST="${EXP_DIR}/manifest.yaml"

if [ ! -f "${MANIFEST}" ]; then
    echo "ERROR: ${MANIFEST} does not exist"
    exit 1
fi

# Extract `mode` from the manifest (use python since we need YAML parsing)
MODE=$(python -c "import yaml; print(yaml.safe_load(open('${MANIFEST}'))['mode'])")

echo "============================================================"
echo "Running experiment: ${NAME}"
echo "Manifest: ${MANIFEST}"
echo "Mode: ${MODE}"
echo "============================================================"

# Pick the right launcher based on mode.
case "${MODE}" in
    qlora)
        # Single-GPU: run on CUDA_VISIBLE_DEVICES if set, else default to GPU 0.
        echo "[launch] single-GPU QLoRA"
        python -m training.train --manifest "${MANIFEST}" 2>&1 | tee "${EXP_DIR}/train.log"
        ;;
    full_sft)
        echo "[launch] multi-GPU FSDP full SFT"
        accelerate launch \
            --config_file accelerate_configs/fsdp_4gpu.yaml \
            -m training.train --manifest "${MANIFEST}" 2>&1 | tee "${EXP_DIR}/train.log"
        ;;
    dpo)
        echo "[launch] DPO"
        # DPO can be single or multi GPU depending on size; default single for safety.
        python -m training.train --manifest "${MANIFEST}" 2>&1 | tee "${EXP_DIR}/train.log"
        ;;
    *)
        echo "ERROR: unknown mode '${MODE}' in manifest"
        exit 1
        ;;
esac

# Pick up the final checkpoint path recorded by train.py
if [ ! -f "${EXP_DIR}/final_checkpoint.txt" ]; then
    echo "ERROR: ${EXP_DIR}/final_checkpoint.txt missing; training may have failed"
    exit 1
fi
CKPT=$(cat "${EXP_DIR}/final_checkpoint.txt")

# Eval, if the manifest asks for it.
RUN_EVAL=$(python -c "import yaml; m=yaml.safe_load(open('${MANIFEST}')); print(m.get('eval_spec', {}).get('run_eval', True))")
if [ "${RUN_EVAL}" = "True" ]; then
    EVAL_PATH=$(python -c "import yaml; print(yaml.safe_load(open('${MANIFEST}'))['data']['eval_path'])")
    GRAPHS_PATH=$(python -c "import yaml; print(yaml.safe_load(open('${MANIFEST}'))['data']['graphs_path'])")
    echo ""
    echo "============================================================"
    echo "Running eval on ${CKPT}"
    echo "============================================================"
    python -m evaluation.eval_harness \
        --model "${CKPT}" \
        --eval_path "${EVAL_PATH}" \
        --graphs_path "${GRAPHS_PATH}" \
        --output "${EXP_DIR}/eval.json" 2>&1 | tee "${EXP_DIR}/eval.log"
    echo ""
    echo "Eval results written to ${EXP_DIR}/eval.json"
fi

echo ""
echo "Experiment '${NAME}' complete."
echo "  checkpoint: ${CKPT}"
echo "  eval:       ${EXP_DIR}/eval.json"
echo "  logs:       ${EXP_DIR}/train.log, ${EXP_DIR}/eval.log"
