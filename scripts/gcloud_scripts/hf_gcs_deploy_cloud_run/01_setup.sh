#!/bin/bash
# =============================================================
# 01_setup.sh
# One-time infrastructure setup. Safe to re-run (idempotent).
#
# Creates:
#   - Required GCP APIs enabled
#   - Service account with correct permissions
#   - VPC network + subnet for Direct VPC Egress
#   - GCS bucket for model weights
#   - Cloud Build SA permissions
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

# ── Colours ───────────────────────────────────────────────────
G="\033[32m"; Y="\033[33m"; C="\033[36m"; B="\033[1m"; N="\033[0m"
ok()   { echo -e "${G}  ✓ $*${N}"; }
info() { echo -e "${C}  → $*${N}"; }
warn() { echo -e "${Y}  ⚠ $*${N}"; }
step() { echo ""; echo -e "${B}▶ $*${N}"; }

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  vLLM Cloud Run — Infrastructure Setup                ${N}"
echo -e "${B}  Project : ${PROJECT_ID}  |  Region : ${REGION}        ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"

gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

# ── APIs ──────────────────────────────────────────────────────
step "1/5  Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  compute.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  --project="${PROJECT_ID}" --quiet
ok "APIs enabled"

# ── Service account ───────────────────────────────────────────
step "2/5  Service account: ${SA_NAME}"
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="vLLM Cloud Run SA" \
  --project="${PROJECT_ID}" 2>/dev/null \
  || info "Already exists"

# Roles needed for Cloud Run + GCS + logging
for ROLE in \
  roles/storage.objectViewer \
  roles/logging.logWriter \
  roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None --quiet 2>/dev/null || true
done
ok "Service account ready: ${SA_EMAIL}"

# ── GCS bucket ────────────────────────────────────────────────
step "3/5  GCS bucket: ${MODEL_BUCKET}"
gcloud storage buckets create "gs://${MODEL_BUCKET}" \
  --uniform-bucket-level-access \
  --public-access-prevention \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null \
  || info "Already exists"

# SA needs full access to read weights at runtime
gcloud storage buckets add-iam-policy-binding "gs://${MODEL_BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.admin" \
  --project="${PROJECT_ID}" --quiet 2>/dev/null || true

# Cloud Build SA needs write access to upload weights
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" \
  --format="value(projectNumber)" 2>/dev/null)
CB_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud storage buckets add-iam-policy-binding "gs://${MODEL_BUCKET}" \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/storage.admin" \
  --project="${PROJECT_ID}" --quiet 2>/dev/null || true

# Grant Cloud Build SA logging
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/logging.logWriter" \
  --condition=None --quiet 2>/dev/null || true

ok "Bucket ready: gs://${MODEL_BUCKET}"
ok "Cloud Build SA granted: ${CB_SA}"

# ── VPC ───────────────────────────────────────────────────────
step "4/5  VPC: ${VPC_NETWORK}/${VPC_SUBNET}"
gcloud compute networks create "${VPC_NETWORK}" \
  --subnet-mode=custom \
  --bgp-routing-mode=regional \
  --project="${PROJECT_ID}" 2>/dev/null \
  || info "Network already exists"

gcloud compute networks subnets create "${VPC_SUBNET}" \
  --network="${VPC_NETWORK}" \
  --region="${REGION}" \
  --range="${SUBNET_RANGE}" \
  --enable-private-ip-google-access \
  --project="${PROJECT_ID}" 2>/dev/null \
  || info "Subnet already exists"
ok "VPC ready (Direct VPC Egress enabled)"

# ── Summary ───────────────────────────────────────────────────
step "5/5  Summary"
echo ""
echo "  Model bucket : gs://${MODEL_BUCKET}"
echo "  Service acct : ${SA_EMAIL}"
echo "  VPC network  : ${VPC_NETWORK}/${VPC_SUBNET}"
echo ""
echo -e "${G}  Setup complete. Next: bash 02_copy_weights.sh${N}"
