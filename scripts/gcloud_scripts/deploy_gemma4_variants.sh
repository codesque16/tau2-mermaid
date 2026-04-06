#!/bin/bash
# =============================================================
# cloudrun/deploy_gemma4_variants.sh
#
# Deploys all Gemma 4 variants to Cloud Run GPU using weights
# directly from Vertex AI Model Garden's GCS bucket.
#
# NO HuggingFace token required.
# NO weight download step.
# NO intermediate GCS bucket needed.
#
# How it works:
#   The same bucket Model Garden uses for Vertex AI endpoints
#   (gs://vertex-model-garden-restricted-us/gemma4/) is readable
#   by your project service account via Workload Identity.
#   vLLM's --load-format=runai_streamer streams weights in
#   parallel directly from that bucket at startup.
#
# GPU: NVIDIA RTX Pro 6000 Blackwell (96 GB VRAM) — us-central1
# Image: pytorch-vllm-serve:gemma4 (official Google image)
#
# Usage:
#   bash deploy_gemma4_variants.sh              # show menu
#   bash deploy_gemma4_variants.sh --all        # full matrix
#   bash deploy_gemma4_variants.sh --model=e4b  # one model, all precisions
#   MODEL=31b PRECISION=fp8 bash deploy_gemma4_variants.sh
#
# Model Garden GCS paths (confirmed from Vertex AI logs):
#   gs://vertex-model-garden-restricted-us/gemma4/gemma-4-E2B-it
#   gs://vertex-model-garden-restricted-us/gemma4/gemma-4-E4B-it
#   gs://vertex-model-garden-restricted-us/gemma4/gemma-4-27B-it   (26B MoE)
#   gs://vertex-model-garden-restricted-us/gemma4/gemma-4-31B-it
# =============================================================
set -eu

# ── Config ───────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="${REGION:-us-central1}"
IMAGE="us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:gemma4"

# Model Garden weights bucket — no credentials needed,
# your project's default service account has read access
# via the Model Garden EULA acceptance
MG_BUCKET="gs://vertex-model-garden-restricted-us/gemma4"

# Networking for Direct VPC Egress (fast GCS reads, no public internet)
VPC_NETWORK="${VPC_NETWORK:-vllm-cr-net}"
VPC_SUBNET="${VPC_SUBNET:-vllm-cr-subnet}"
SUBNET_RANGE="${SUBNET_RANGE:-10.8.0.0/26}"

# Service account for Cloud Run
SA_NAME="${SA_NAME:-gemma4-cr-sa}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

MAX_INSTANCES="${MAX_INSTANCES:-1}"

# CLI / env overrides
DEPLOY_MODEL="${MODEL:-}"
DEPLOY_PRECISION="${PRECISION:-}"

# ── Colours ───────────────────────────────────────────────────
G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"
B="\033[1m"; DIM="\033[2m"; N="\033[0m"
ok()   { echo -e "${G}  ✓ $*${N}"; }
info() { echo -e "${C}  → $*${N}"; }
warn() { echo -e "${Y}  ⚠ $*${N}"; }
err()  { echo -e "${R}  ✗ $*${N}"; }
step() { echo ""; echo -e "${B}▶ $*${N}"; }

# ── Parse CLI ─────────────────────────────────────────────────
DEPLOY_ALL=false
for arg in "$@"; do
  case "${arg}" in
    --all)         DEPLOY_ALL=true ;;
    --model=*)     DEPLOY_MODEL="${arg#--model=}" ;;
    --precision=*) DEPLOY_PRECISION="${arg#--precision=}" ;;
    --help|-h)
      sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
  esac
done

echo ""
echo -e "${B}╔═══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  Gemma 4 Cloud Run GPU — Model Garden Weights           ${N}"
echo -e "${B}  Project : ${PROJECT_ID}  |  Region : ${REGION}         ${N}"
echo -e "${B}  Weights : ${MG_BUCKET}   ${N}"
echo -e "${B}  No HuggingFace token required                          ${N}"
echo -e "${B}╚═══════════════════════════════════════════════════════╝${N}"
echo ""

gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

# ── One-time infrastructure ───────────────────────────────────
setup_once() {
  step "Infrastructure setup (idempotent)"

  info "Enabling APIs..."
  gcloud services enable \
    run.googleapis.com \
    storage.googleapis.com \
    iam.googleapis.com \
    compute.googleapis.com \
    --project="${PROJECT_ID}" --quiet
  ok "APIs enabled"

  info "Creating service account: ${SA_NAME}..."
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Gemma4 Cloud Run SA" \
    --project="${PROJECT_ID}" 2>/dev/null \
    || info "Service account already exists"

  # READ access to Model Garden restricted bucket
  # (same bucket your Vertex AI endpoint was using)
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectViewer" \
    --condition=None --quiet 2>/dev/null || true

  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/logging.logWriter" \
    --condition=None --quiet 2>/dev/null || true

  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/monitoring.metricWriter" \
    --condition=None --quiet 2>/dev/null || true
  ok "Service account IAM ready"

  info "Creating VPC for Direct VPC Egress..."
  # Direct VPC Egress routes GCS traffic through private Google
  # APIs (no internet hop), dramatically improving model load speed
  gcloud compute networks create "${VPC_NETWORK}" \
    --subnet-mode=custom --bgp-routing-mode=regional \
    --project="${PROJECT_ID}" 2>/dev/null \
    || info "VPC already exists"

  gcloud compute networks subnets create "${VPC_SUBNET}" \
    --network="${VPC_NETWORK}" \
    --region="${REGION}" \
    --range="${SUBNET_RANGE}" \
    --enable-private-ip-google-access \
    --project="${PROJECT_ID}" 2>/dev/null \
    || info "Subnet already exists"
  ok "VPC ready: ${VPC_NETWORK}/${VPC_SUBNET}"
}

# ── Verify Model Garden GCS access ───────────────────────────
verify_mg_access() {
  local GCS_PATH="$1"
  info "Verifying Model Garden GCS access: ${GCS_PATH}..."
  if gcloud storage ls "${GCS_PATH}" --project="${PROJECT_ID}" 2>/dev/null | grep -q "config.json\|tokenizer\|model"; then
    ok "GCS accessible — weights found"
    return 0
  else
    warn "Cannot verify GCS access from this machine."
    warn "The Cloud Run service account will access it at runtime."
    warn "If deployment fails with permission errors, you may need to:"
    warn "  1. Accept Gemma 4 EULA at https://console.cloud.google.com/vertex-ai/publishers/google/model-garden"
    warn "  2. Grant storage.objectViewer on the restricted bucket to ${SA_EMAIL}"
    return 0  # Don't block — access may work from Cloud Run even if not from local
  fi
}

# ── Build vLLM args ───────────────────────────────────────────
build_vllm_args() {
  local GCS_MODEL_PATH="$1"   # full gs:// path
  local MODEL_SERVED_NAME="$2" # name clients use in model= field
  local PRECISION="$3"
  local MAX_MODEL_LEN="$4"
  local MAX_SEQS="$5"
  local GPU_MEM_UTIL="$6"

  # Base args — matches the working codelab pattern exactly
  local ARGS=(
    "vllm" "serve" "${GCS_MODEL_PATH}"
    "--served-model-name" "${MODEL_SERVED_NAME}"
    "--enable-chunked-prefill"
    "--enable-prefix-caching"
    "--generation-config" "auto"
    "--enable-auto-tool-choice"
    "--tool-call-parser" "gemma4"
    "--reasoning-parser" "gemma4"
    "--dtype" "bfloat16"
    "--max-num-seqs" "${MAX_SEQS}"
    "--gpu-memory-utilization" "${GPU_MEM_UTIL}"
    "--tensor-parallel-size" "1"
    "--load-format" "runai_streamer"  # Run:ai parallel GCS streamer
    "--port" "8080"
    "--host" "0.0.0.0"
  )  # limit-mm-per-prompt removed — JSON braces mangled by Cloud Run ^;^ separator

  # Precision
  case "${PRECISION}" in
    fp8)   ARGS+=("--quantization" "fp8") ;;
    fp8kv) ARGS+=("--quantization" "fp8" "--kv-cache-dtype" "fp8") ;;
    bf16)  ;;  # default — no extra flags
  esac

  # Context window cap
  if [ -n "${MAX_MODEL_LEN}" ] && [ "${MAX_MODEL_LEN}" != "default" ]; then
    ARGS+=("--max-model-len" "${MAX_MODEL_LEN}")
  fi

  # Print as space-joined string for Cloud Run --args
  local JOINED=""
  for a in "${ARGS[@]}"; do JOINED="${JOINED} ${a}"; done
  echo "${JOINED# }"
}

# ── Deploy one Cloud Run service ──────────────────────────────
deploy_service() {
  local SERVICE_NAME="$1"
  local GCS_MODEL_PATH="$2"
  local MODEL_SERVED_NAME="$3"
  local PRECISION="$4"
  local MAX_MODEL_LEN="$5"
  local MAX_SEQS="$6"
  local GPU_MEM_UTIL="$7"
  local CPU="$8"
  local MEM_GB="$9"
  local CTX_LABEL="${10}"

  step "Deploying: ${SERVICE_NAME}"
  info "  Weights  : ${GCS_MODEL_PATH}"
  info "  Precision: ${PRECISION}"
  info "  Context  : ${CTX_LABEL}"
  info "  Max seqs : ${MAX_SEQS}"

  local CONTAINER_ARGS
  CONTAINER_ARGS=$(build_vllm_args \
    "${GCS_MODEL_PATH}" "${MODEL_SERVED_NAME}" "${PRECISION}" \
    "${MAX_MODEL_LEN}" "${MAX_SEQS}" "${GPU_MEM_UTIL}")

  info "  vLLM cmd : ${CONTAINER_ARGS:0:120}..."

  # Cloud Run deploy — pattern exactly from the Google codelab
  gcloud beta run deploy "${SERVICE_NAME}" \
    --image="${IMAGE}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --execution-environment=gen2 \
    --no-allow-unauthenticated \
    --cpu="${CPU}" \
    --memory="${MEM_GB}Gi" \
    --gpu=1 \
    --gpu-type=nvidia-rtx-pro-6000 \
    --no-gpu-zonal-redundancy \
    --no-cpu-throttling \
    --max-instances="${MAX_INSTANCES}" \
    --concurrency="${MAX_SEQS}" \
    --timeout=600 \
    --network="${VPC_NETWORK}" \
    --subnet="${VPC_SUBNET}" \
    --vpc-egress=all-traffic \
    --startup-probe="tcpSocket.port=8080,initialDelaySeconds=240,failureThreshold=1,timeoutSeconds=240,periodSeconds=240" \
    --set-env-vars="MODEL_NAME=${MODEL_SERVED_NAME},PRECISION=${PRECISION},PROJECT_ID=${PROJECT_ID}" \
    --command="bash" \
    --args="^;^-c;${CONTAINER_ARGS}" \
    --quiet

  local SVC_URL
  SVC_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(status.url)" 2>/dev/null || echo "")

  ok "Deployed: ${SERVICE_NAME}"
  info "  URL: ${SVC_URL}"

  # Register for test script
  mkdir -p "$(dirname "${REGISTRY_FILE}")"
  echo "${SERVICE_NAME}|${SVC_URL}|${MODEL_SERVED_NAME}|${PRECISION}|${CTX_LABEL}|${GCS_MODEL_PATH}" \
    >> "${REGISTRY_FILE}"
}

# ── Deployment matrix ─────────────────────────────────────────
REGISTRY_FILE="${REGISTRY_FILE:-/tmp/gemma4_services_registry.txt}"

run_matrix() {
  # Write registry header
  echo "# Gemma 4 Cloud Run services — $(date)" > "${REGISTRY_FILE}"

  # ── Model Garden GCS paths — bash 3.2 compatible (no declare -A)
  mg_path() {
    case "$1" in
      e2b) echo "${MG_BUCKET}/gemma-4-E2B-it" ;;
      e4b) echo "${MG_BUCKET}/gemma-4-E4B-it" ;;
      26b) echo "${MG_BUCKET}/gemma-4-27B-it" ;;  # 26B MoE stored as 27B
      31b) echo "${MG_BUCKET}/gemma-4-31B-it" ;;
    esac
  }

  local ALL_MODELS="e2b e4b 26b 31b"
  local ALL_PRECISIONS="bf16 fp8 fp8kv"

  for MODEL_KEY in ${ALL_MODELS}; do
    [ -n "${DEPLOY_MODEL}" ] && [ "${MODEL_KEY}" != "${DEPLOY_MODEL}" ] && continue

    local GCS_PATH
    GCS_PATH=$(mg_path "${MODEL_KEY}")

    # Per-model config
    case "${MODEL_KEY}" in
      e2b)
        CTX="128000"; CTX_LABEL="128K"
        MAX_SEQS_BF16="64"; MAX_SEQS_FP8="128"
        CPU="8";  MEM_GB="40"; GPU_UTIL="0.92"
        ;;
      e4b)
        CTX="128000"; CTX_LABEL="128K"
        MAX_SEQS_BF16="64"; MAX_SEQS_FP8="128"
        CPU="10"; MEM_GB="40"; GPU_UTIL="0.92"
        ;;
      26b)
        # MoE: 25.2B total but 3.8B active — cap context at 32K for test
        # (256K possible with FP8 but requires careful tuning)
        CTX="32767"; CTX_LABEL="32K"
        MAX_SEQS_BF16="16"; MAX_SEQS_FP8="32"
        CPU="20"; MEM_GB="80"; GPU_UTIL="0.95"
        ;;
      31b)
        # Dense 31B: BF16 needs ~62GB weights + KV cache → OOM on 96GB at 32K
        # FP8: ~31GB weights → ~65GB free for KV cache → 32K ctx comfortable
        CTX="32767"; CTX_LABEL="32K"
        MAX_SEQS_BF16="SKIP"; MAX_SEQS_FP8="8"
        CPU="20"; MEM_GB="80"; GPU_UTIL="0.95"
        ;;
    esac

    verify_mg_access "${GCS_PATH}"

    for PRECISION in ${ALL_PRECISIONS}; do
      [ -n "${DEPLOY_PRECISION}" ] && [ "${PRECISION}" != "${DEPLOY_PRECISION}" ] && continue

      # Skip known OOM combo
      if [ "${MODEL_KEY}" = "31b" ] && [ "${PRECISION}" = "bf16" ]; then
        warn "Skipping gemma4-31b-bf16: BF16 31B (~62GB) + 32K KV cache → OOM on 96GB"
        warn "  H100 80GB is the right GPU for 31B BF16 (use GKE stack for that)"
        continue
      fi

      local MAX_SEQS
      if [ "${PRECISION}" = "bf16" ]; then
        MAX_SEQS="${MAX_SEQS_BF16}"
      else
        MAX_SEQS="${MAX_SEQS_FP8}"
      fi

      [ "${MAX_SEQS}" = "SKIP" ] && continue

      local SERVICE_NAME="gemma4-${MODEL_KEY}-${PRECISION}"

      deploy_service \
        "${SERVICE_NAME}" \
        "${GCS_PATH}" \
        "${MODEL_KEY}" \
        "${PRECISION}" \
        "${CTX}" \
        "${MAX_SEQS}" \
        "${GPU_UTIL}" \
        "${CPU}" \
        "${MEM_GB}" \
        "${CTX_LABEL}"

      echo ""
      [ -n "${DEPLOY_MODEL}" ] || { info "Pausing 15s between deploys..."; sleep 15; }
    done
  done
}

# ── Main ──────────────────────────────────────────────────────
setup_once

if [ "${DEPLOY_ALL}" = "true" ] || [ -n "${DEPLOY_MODEL}" ] || [ -n "${DEPLOY_PRECISION}" ]; then
  run_matrix
else
  echo -e "  ${B}No target specified. Options:${N}"
  echo ""
  echo "  bash deploy_gemma4_variants.sh --all"
  echo "  bash deploy_gemma4_variants.sh --model=e4b"
  echo "  bash deploy_gemma4_variants.sh --model=31b --precision=fp8"
  echo "  MODEL=e4b PRECISION=bf16 bash deploy_gemma4_variants.sh"
  echo ""
  echo -e "  ${B}Model Garden GCS paths (no download needed):${N}"
  echo "  E2B : ${MG_BUCKET}/gemma-4-E2B-it"
  echo "  E4B : ${MG_BUCKET}/gemma-4-E4B-it"
  echo "  26B : ${MG_BUCKET}/gemma-4-27B-it"
  echo "  31B : ${MG_BUCKET}/gemma-4-31B-it"
  echo ""
  echo -e "  ${B}What gets skipped (known OOM):${N}"
  echo "  31B + bf16 on RTX Pro 6000: 62GB weights + 32K KV → OOM"
  echo "  Use GKE + H100 80GB for 31B BF16 (see gke/ scripts)"
  echo ""
  echo "  Registry written to: ${REGISTRY_FILE:-/tmp/gemma4_services_registry.txt}"
  echo "  Run tests with: bash test_gemma4_variants.sh"
fi

if [ -f "${REGISTRY_FILE:-}" ] && [ -s "${REGISTRY_FILE}" ]; then
  echo ""
  echo -e "${B}╔═══════════════════════════════════════════════════════╗${N}"
  echo -e "${B}  Deployed services                                      ${N}"
  echo -e "${B}╚═══════════════════════════════════════════════════════╝${N}"
  grep -v "^#" "${REGISTRY_FILE}" | while IFS='|' read -r name url model prec ctx path; do
    echo -e "  ${G}${name}${N}"
    echo -e "    URL      : ${url}"
    echo -e "    Weights  : ${path}"
    echo ""
  done
  echo "  Test with: bash test_gemma4_variants.sh"
fi
