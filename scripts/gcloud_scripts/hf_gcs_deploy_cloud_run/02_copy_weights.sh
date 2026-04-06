#!/bin/bash
# =============================================================
# 02_copy_weights.sh
# Downloads model weights from HuggingFace into your GCS bucket
# via Cloud Build (avoids local storage limits).
#
# - Uses Cloud Build VM (E2_HIGHCPU_32, 500GB disk)
# - No HF token needed for public/Apache-licensed models
# - Idempotent: skips if weights already in GCS
# - Streams build logs directly to terminal
# =============================================================
set -eu
# ── Config file resolution ────────────────────────────────────
# Accepts: --config=path  OR  bash script.sh models/gemma26b.env
SCRIPT_DIR="$(dirname "$0")"
CONFIG_FILE=""
for _arg in "$@"; do
  case "${_arg}" in
    --config=*) CONFIG_FILE="${_arg#--config=}" ;;
  esac
done
# Positional first arg that looks like a file
if [ -z "${CONFIG_FILE}" ] && [ -n "${1:-}" ] && [ -f "${1}" ]; then
  CONFIG_FILE="${1}"
fi
# Default to config.env alongside the script
[ -z "${CONFIG_FILE}" ] && CONFIG_FILE="${SCRIPT_DIR}/config.env"
if [ ! -f "${CONFIG_FILE}" ]; then
  echo "ERROR: Config not found: ${CONFIG_FILE}"
  echo "Usage: bash $0 [path/to/model.env]"
  echo "       bash $0 --config=path/to/model.env"
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
echo -e "${B}  vLLM Cloud Run — Copy Model Weights to GCS            ${N}"
echo -e "${B}  Model  : ${HF_MODEL_ID}                               ${N}"
echo -e "${B}  Dest   : ${GCS_MODEL_PATH}                            ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""

gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

# ── Check if already copied ───────────────────────────────────
step "1/2  Checking if weights already in GCS"
FILE_COUNT=$(gcloud storage ls "${GCS_MODEL_PATH}/" \
  --project="${PROJECT_ID}" 2>/dev/null | wc -l | tr -d ' ') || FILE_COUNT=0

if [ "${FILE_COUNT}" -gt "2" ]; then
  ok "Weights already in GCS (${FILE_COUNT} files) — skipping download"
  echo ""
  echo -e "${G}  Next: bash 03_deploy.sh${N}"
  exit 0
fi

info "No existing weights found — starting download"

# ── Build HF token arg ────────────────────────────────────────
HF_TOKEN_ARG=""
[ -n "${HF_TOKEN}" ] && HF_TOKEN_ARG="--token ${HF_TOKEN}"

step "2/2  Downloading via Cloud Build (~5-30 min depending on model size)"
info "  Source : HuggingFace — ${HF_MODEL_ID}"
info "  Dest   : ${GCS_MODEL_PATH}"
warn "  Leave this running — logs stream below"
echo ""

# Build config as a temp file to handle special chars cleanly
BUILD_CONFIG=$(mktemp /tmp/cloudbuild_XXXXXX.yaml)
cat > "${BUILD_CONFIG}" << YAML_EOF
steps:
- name: 'gcr.io/google.com/cloudsdktool/google-cloud-cli:slim'
  entrypoint: 'bash'
  args:
  - '-c'
  - |
    set -e
    echo "Installing huggingface_hub..."
    pip3 install --root-user-action=ignore --break-system-packages "huggingface_hub[cli]"

    echo "Downloading: ${HF_MODEL_ID}"
    if [ -n "${HF_TOKEN}" ]; then
      hf download "${HF_MODEL_ID}" --token "${HF_TOKEN}" --local-dir "./weights"
    else
      hf download "${HF_MODEL_ID}" --local-dir "./weights"
    fi

    echo "Uploading to GCS: ${GCS_MODEL_PATH}"
    gcloud storage cp -r "./weights" "${GCS_MODEL_PATH}"

    echo "Verifying..."
    gcloud storage ls "${GCS_MODEL_PATH}/"
    echo "Done."
options:
  machineType: 'E2_HIGHCPU_32'
  diskSizeGb: 500
YAML_EOF

gcloud beta builds submit \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --no-source \
  --config="${BUILD_CONFIG}"

rm -f "${BUILD_CONFIG}"

# ── Verify ────────────────────────────────────────────────────
echo ""
FILE_COUNT=$(gcloud storage ls "${GCS_MODEL_PATH}/" \
  --project="${PROJECT_ID}" 2>/dev/null | wc -l | tr -d ' ') || FILE_COUNT=0

if [ "${FILE_COUNT}" -gt "2" ]; then
  ok "Weights copied successfully — ${FILE_COUNT} files in GCS"
  echo ""
  echo -e "${G}  Next: bash 03_deploy.sh${N}"
else
  echo -e "${R}  ✗ Upload may have failed — only ${FILE_COUNT} files found${N}"
  echo "  Check: gcloud storage ls ${GCS_MODEL_PATH}/"
  exit 1
fi
