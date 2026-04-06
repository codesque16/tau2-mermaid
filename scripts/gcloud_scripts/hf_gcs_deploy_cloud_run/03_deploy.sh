#!/bin/bash
# =============================================================
# 03_deploy.sh
# Deploys the Cloud Run GPU service with vLLM.
# Reads all settings from config.env.
#
# Handles:
#   - Building correct vLLM args from config
#   - fp8 / fp8kv / bf16 precision selection
#   - Tool calling parser configuration
#   - Startup probe timing based on model size
#   - Saves service URL for use by test + warmup scripts
# =============================================================
set -eu
# ── Config file resolution ────────────────────────────────────
# Priority: --config=path > first positional arg > ./config.env
SCRIPT_DIR="$(dirname "$0")"
CONFIG_FILE=""

# 1. Named flag takes highest priority
for _arg in "$@"; do
  case "${_arg}" in
    --config=*) CONFIG_FILE="${_arg#--config=}"; break ;;
  esac
done

# 2. First positional arg if it looks like a file path (.env or exists)
if [ -z "${CONFIG_FILE}" ] && [ -n "${1:-}" ]; then
  # Accept if it ends in .env OR if the file actually exists
  case "${1}" in
    *.env) CONFIG_FILE="${1}" ;;
    *)     [ -f "${1}" ] && CONFIG_FILE="${1}" ;;
  esac
fi

# 3. Default: config.env next to the script
[ -z "${CONFIG_FILE}" ] && CONFIG_FILE="${SCRIPT_DIR}/config.env"

if [ ! -f "${CONFIG_FILE}" ]; then
  echo "ERROR: Config not found: ${CONFIG_FILE}"
  echo "Usage: bash $0 models/my-model.env"
  echo "       bash $0 --config=models/my-model.env"
  exit 1
fi
echo "  Config: ${CONFIG_FILE}"
source "${CONFIG_FILE}"

G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"; B="\033[1m"; N="\033[0m"
ok()   { echo -e "${G}  ✓ $*${N}"; }
info() { echo -e "${C}  → $*${N}"; }
warn() { echo -e "${Y}  ⚠ $*${N}"; }
step() { echo ""; echo -e "${B}▶ $*${N}"; }

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  vLLM Cloud Run — Deploy Service                       ${N}"
echo -e "${B}  Service: ${SERVICE_NAME}                              ${N}"
echo -e "${B}  Model  : ${HF_MODEL_ID}                               ${N}"
echo -e "${B}  GPU    : ${GPU_TYPE}                                   ${N}"
echo -e "${B}  Quant  : ${QUANTIZATION}                              ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""

gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

# ── Verify weights exist ──────────────────────────────────────
step "1/3  Verifying weights in GCS"
FILE_COUNT=$(gcloud storage ls "${GCS_MODEL_PATH}/" \
  --project="${PROJECT_ID}" 2>/dev/null | wc -l | tr -d ' ') || FILE_COUNT=0

if [ "${FILE_COUNT}" -lt "2" ]; then
  echo -e "${R}  ✗ No weights found at ${GCS_MODEL_PATH}${N}"
  echo "  Run: bash 02_copy_weights.sh first"
  exit 1
fi
ok "Weights found: ${FILE_COUNT} files at ${GCS_MODEL_PATH}"

# ── Build vLLM args ───────────────────────────────────────────
step "2/3  Building vLLM command"

ARGS=(
  "vllm" "serve" "${GCS_MODEL_PATH}"
  "--served-model-name" "${SERVED_MODEL_NAME}"
  "--dtype" "bfloat16"
  "--max-num-seqs" "${MAX_NUM_SEQS}"
  "--gpu-memory-utilization" "${GPU_MEM_UTIL}"
  "--tensor-parallel-size" "${TENSOR_PARALLEL_SIZE}"
  "--load-format" "${LOAD_FORMAT}"
  "--port" "8080"
  "--host" "0.0.0.0"
)

# Context window
[ -n "${MAX_MODEL_LEN}" ] && ARGS+=("--max-model-len" "${MAX_MODEL_LEN}")

# Quantization
case "${QUANTIZATION}" in
  fp8)
    ARGS+=("--quantization" "fp8")
    info "Precision: FP8 weight quantization"
    ;;
  fp8kv)
    ARGS+=("--quantization" "fp8" "--kv-cache-dtype" "fp8")
    info "Precision: FP8 weights + FP8 KV cache"
    ;;
  bf16)
    info "Precision: BF16 (no quantization)"
    ;;
  *)
    warn "Unknown quantization '${QUANTIZATION}' — using bf16"
    ;;
esac

# Tool calling
if [ -n "${TOOL_CALL_PARSER}" ]; then
  ARGS+=(
    "--enable-auto-tool-choice"
    "--tool-call-parser" "${TOOL_CALL_PARSER}"
  )
fi

# Reasoning parser
[ -n "${REASONING_PARSER}" ] && ARGS+=("--reasoning-parser" "${REASONING_PARSER}")

# Extra flags (split on spaces)
for FLAG in ${EXTRA_VLLM_FLAGS}; do
  ARGS+=("${FLAG}")
done

# Join into single string for Cloud Run --args
ARGS_STR="${ARGS[*]}"

info "vLLM command: ${ARGS_STR:0:100}..."
info "Context: ${MAX_MODEL_LEN:-default} tokens | Seqs: ${MAX_NUM_SEQS} | GPU util: ${GPU_MEM_UTIL}"

# ── Startup probe timing ──────────────────────────────────────
# Larger models need more time to load. Estimate based on model name.
# Cloud Run hard cap: initialDelaySeconds max = 240
# Use 240 for all models — runai_streamer loads fast enough
STARTUP_TIMEOUT=240
info "Startup timeout: ${STARTUP_TIMEOUT}s"

# ── Deploy ────────────────────────────────────────────────────
step "3/3  Deploying to Cloud Run"

gcloud beta run deploy "${SERVICE_NAME}" \
  --image="${VLLM_IMAGE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --execution-environment=gen2 \
  --no-allow-unauthenticated \
  --cpu="${CPU_COUNT}" \
  --memory="${MEMORY_GB}Gi" \
  --gpu=1 \
  --gpu-type="${GPU_TYPE}" \
  --no-gpu-zonal-redundancy \
  --no-cpu-throttling \
  --max-instances="${MAX_INSTANCES}" \
  --concurrency="${CONCURRENCY}" \
  --timeout=600 \
  --network="${VPC_NETWORK}" \
  --subnet="${VPC_SUBNET}" \
  --vpc-egress=all-traffic \
  --set-env-vars="HF_MODEL_ID=${HF_MODEL_ID},SERVICE_NAME=${SERVICE_NAME},PROJECT_ID=${PROJECT_ID}" \
  --startup-probe="tcpSocket.port=8080,initialDelaySeconds=${STARTUP_TIMEOUT},failureThreshold=1,timeoutSeconds=${STARTUP_TIMEOUT},periodSeconds=${STARTUP_TIMEOUT}" \
  --command="bash" \
  --args="^;^-c;${ARGS_STR}"

# ── Save URL ──────────────────────────────────────────────────
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)" 2>/dev/null || echo "")

# Save for other scripts to use
echo "${SERVICE_URL}" > /tmp/vllm_service_url.txt
echo "${SERVED_MODEL_NAME}" >> /tmp/vllm_service_url.txt

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  Deployed successfully                                  ${N}"
echo -e "${B}  Service : ${SERVICE_NAME}                              ${N}"
echo -e "${B}  URL     : ${SERVICE_URL}                               ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""
echo "  First request triggers cold start (~3-5 min for large models)"
echo "  Run: bash 04_test.sh"
echo "  Run: bash warmup_ping.sh   (to keep warm)"
