#!/bin/bash
# =============================================================
# patch_scale_to_zero.sh
# Scans all GCP regions, lists endpoints, patches scale-to-zero.
# macOS bash 3.2 compatible. BSD mktemp compatible (no extensions).
# =============================================================

set -eu

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
IDLE_SCALEDOWN="${IDLE_SCALEDOWN:-600s}"
MIN_SCALEUP="${MIN_SCALEUP:-600s}"
MIN_REPLICAS="${MIN_REPLICAS:-0}"
MAX_REPLICAS="${MAX_REPLICAS:-1}"

ALL_REGIONS="africa-south1 northamerica-northeast1 northamerica-northeast2 southamerica-east1 southamerica-west1 us-central1 us-east1 us-east4 us-east5 us-south1 us-west1 us-west2 us-west3 us-west4 us-west8 asia-east1 asia-east2 asia-northeast1 asia-northeast2 asia-northeast3 asia-south1 asia-south2 asia-southeast1 asia-southeast2 australia-southeast1 australia-southeast2 europe-central2 europe-north1 europe-north2 europe-southwest1 europe-west1 europe-west2 europe-west3 europe-west4 europe-west6 europe-west8 europe-west9 europe-west12 europe-west15 me-central1 me-central2 me-west1"

# macOS BSD mktemp does not support extensions after the X template.
# Always use:  mktemp /tmp/prefix_XXXXXX   (no .json or .txt suffix)
mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

echo "======================================================="
echo "  Scale-to-Zero Patcher  —  All GCP Regions"
echo "  Project: ${PROJECT_ID}"
echo "======================================================="
echo ""

echo "▶ Getting access token..."
TOKEN=$(gcloud auth print-access-token)
[ -z "${TOKEN}" ] && echo "ERROR: run gcloud auth login first" && exit 1
echo "  OK"
echo ""

gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

# ── Parallel region scan ──────────────────────────────────────
echo "▶ Scanning all regions in parallel..."

SCAN_DIR=$(mktemp -d /tmp/ep_scan_XXXXXX)

scan_one() {
  local REGION="$1"
  local OUT="${SCAN_DIR}/${REGION}"
  local CODE
  CODE=$(curl --silent --output "${OUT}" --write-out "%{http_code}" \
    --max-time 10 \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints" \
    2>/dev/null) || CODE="000"
  if [ "${CODE}" != "200" ]; then rm -f "${OUT}"; return; fi
  COUNT=$(python3 -c "
import json
try:
    print(len(json.load(open('${OUT}')).get('endpoints',[])))
except: print(0)
" 2>/dev/null) || COUNT=0
  [ "${COUNT}" = "0" ] && rm -f "${OUT}"
}

for R in $ALL_REGIONS; do scan_one "${R}" & done
wait
echo "  Done."
echo ""

# ── Parse results into per-endpoint index files ───────────────
INDEX_DIR=$(mktemp -d /tmp/ep_idx_XXXXXX)

for R in $ALL_REGIONS; do
  RFILE="${SCAN_DIR}/${R}"
  [ -f "${RFILE}" ] || continue
  python3 - "${RFILE}" "${R}" "${INDEX_DIR}" << 'PYEOF'
import json, sys, os

with open(sys.argv[1]) as f:
    data = json.load(f)
region  = sys.argv[2]
idx_dir = sys.argv[3]

existing = [f for f in os.listdir(idx_dir) if f.startswith("ep_")]
start = len(existing)

for i, ep in enumerate(data.get("endpoints", []), start=start+1):
    ep_id  = ep.get("name","").split("/")[-1]
    name   = ep.get("displayName","unnamed")
    models = ep.get("deployedModels", [])
    model  = models[0].get("displayName","no-model") if models else "no-model"
    dm_id  = models[0].get("id","") if models else ""
    dr     = models[0].get("dedicatedResources",{}) if models else {}
    min_r  = str(dr.get("minReplicaCount","?"))
    max_r  = str(dr.get("maxReplicaCount","?"))
    s2z    = dr.get("scaleToZeroSpec",{})
    idle   = s2z.get("idleScaledownPeriod","not-set")
    # One field per line — no delimiter to misparse
    with open(f"{idx_dir}/ep_{i}", "w") as out:
        out.write(ep_id  + "\n")  # line 1
        out.write(region + "\n")  # line 2
        out.write(dm_id  + "\n")  # line 3
        out.write(name   + "\n")  # line 4
        out.write(model  + "\n")  # line 5
        out.write(min_r  + "\n")  # line 6
        out.write(max_r  + "\n")  # line 7
        out.write(idle   + "\n")  # line 8
PYEOF
done

rm -rf "${SCAN_DIR}"

TOTAL=$(ls "${INDEX_DIR}" | grep "^ep_" | wc -l | tr -d ' ')

if [ "${TOTAL}" = "0" ]; then
  echo "  No endpoints found in any region."
  rm -rf "${INDEX_DIR}"
  exit 0
fi

# ── Print menu ────────────────────────────────────────────────
echo "▶ Endpoints found:"
echo ""
printf "  %-4s  %-40s  %-24s  %-5s  %-5s  %-20s  %s\n" \
  "No." "Endpoint display name" "Deployed model" "Min" "Max" "Idle scaledown" "Region"
printf "  %-4s  %-40s  %-24s  %-5s  %-5s  %-20s  %s\n" \
  "---" "----------------------------------------" "------------------------" "---" "---" "--------------------" "------"

i=1
while [ "${i}" -le "${TOTAL}" ]; do
  EP_FILE="${INDEX_DIR}/ep_${i}"
  DISP=$(  sed -n '4p' "${EP_FILE}")
  MODEL=$( sed -n '5p' "${EP_FILE}")
  MIN_R=$( sed -n '6p' "${EP_FILE}")
  MAX_R=$( sed -n '7p' "${EP_FILE}")
  IDLE=$(  sed -n '8p' "${EP_FILE}")
  REGION=$(sed -n '2p' "${EP_FILE}")
  SHORT_D="${DISP}";  [ "${#DISP}"  -gt 39 ] && SHORT_D="${DISP:0:36}..."
  SHORT_M="${MODEL}"; [ "${#MODEL}" -gt 23 ] && SHORT_M="${MODEL:0:20}..."
  printf "  [%-2s]  %-40s  %-24s  %-5s  %-5s  %-20s  %s\n" \
    "${i}" "${SHORT_D}" "${SHORT_M}" "${MIN_R}" "${MAX_R}" "${IDLE}" "${REGION}"
  i=$((i+1))
done

echo ""
echo "  Total: ${TOTAL} endpoint(s)"
echo ""

# ── Selection ─────────────────────────────────────────────────
while true; do
  printf "  Select [1-%s] or q to quit: " "${TOTAL}"
  read -r CHOICE
  [ "${CHOICE}" = "q" ] || [ "${CHOICE}" = "Q" ] && echo "Quit." && rm -rf "${INDEX_DIR}" && exit 0
  case "${CHOICE}" in ''|*[!0-9]*) echo "  Enter a number." && continue ;; esac
  [ "${CHOICE}" -ge 1 ] && [ "${CHOICE}" -le "${TOTAL}" ] && break
  echo "  Enter 1–${TOTAL}."
done

EP_FILE="${INDEX_DIR}/ep_${CHOICE}"
SEL_EP_ID=$(sed -n '1p' "${EP_FILE}")
SEL_REGION=$(sed -n '2p' "${EP_FILE}")
SEL_DM_ID=$(sed -n '3p' "${EP_FILE}")
SEL_DISP=$(sed -n '4p' "${EP_FILE}")
rm -rf "${INDEX_DIR}"

echo ""
echo "  Selected  : ${SEL_DISP}"
echo "  Endpoint  : ${SEL_EP_ID}"
echo "  Region    : ${SEL_REGION}"
echo ""
echo "  Will apply:"
echo "    min_replica_count    : ${MIN_REPLICAS}"
echo "    max_replica_count    : ${MAX_REPLICAS}"
echo "    idle_scaledown_period: ${IDLE_SCALEDOWN}"
echo "    min_scaleup_period   : ${MIN_SCALEUP}"
echo ""
printf "  Confirm? [y/N]: "
read -r CONFIRM
case "${CONFIRM}" in [Yy]) ;; *) echo "  Aborted." && exit 0 ;; esac

# ── Get deployed model ID if missing ─────────────────────────
if [ -z "${SEL_DM_ID}" ]; then
  echo "  Fetching deployed model ID..."
  DM_TMP=$(mktmp ep_dm)
  curl --silent --output "${DM_TMP}" \
    --header "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://${SEL_REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${SEL_REGION}/endpoints/${SEL_EP_ID}"
  SEL_DM_ID=$(python3 -c "
import json
with open('${DM_TMP}') as f:
    d = json.load(f)
models = d.get('deployedModels', [])
print(models[0]['id'] if models else '')
" 2>/dev/null) || SEL_DM_ID=""
  rm -f "${DM_TMP}"
fi

if [ -z "${SEL_DM_ID}" ]; then
  echo "  ERROR: No deployed model found. It may still be deploying."
  exit 1
fi
echo "  Deployed model ID: ${SEL_DM_ID}"

# ── Build payload file ────────────────────────────────────────
echo ""
echo "▶ Applying scale-to-zero patch..."

MUTATE_TMP=$(mktmp mutate)
cat > "${MUTATE_TMP}" << PAYLOAD_EOF
{
  "deployedModel": {
    "id": "${SEL_DM_ID}",
    "dedicatedResources": {
      "minReplicaCount": ${MIN_REPLICAS},
      "maxReplicaCount": ${MAX_REPLICAS},
      "initialReplicaCount": 1,
      "scaleToZeroSpec": {
        "idleScaledownPeriod": "${IDLE_SCALEDOWN}",
        "minScaleupPeriod": "${MIN_SCALEUP}"
      }
    }
  },
  "updateMask": "dedicated_resources.min_replica_count,dedicated_resources.max_replica_count,dedicated_resources.scale_to_zero_spec"
}
PAYLOAD_EOF

RESP_TMP=$(mktmp mutate_resp)
MUTATE_CODE=$(curl \
  --silent \
  --output "${RESP_TMP}" \
  --write-out "%{http_code}" \
  --request POST \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  --header "Content-Type: application/json" \
  --data @"${MUTATE_TMP}" \
  "https://${SEL_REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${SEL_REGION}/endpoints/${SEL_EP_ID}:mutateDeployedModel")

echo "  HTTP: ${MUTATE_CODE}"

if [ "${MUTATE_CODE}" != "200" ]; then
  echo "  ERROR:"
  cat "${RESP_TMP}"
  rm -f "${MUTATE_TMP}" "${RESP_TMP}"
  exit 1
fi

MUTATE_OP=$(python3 -c "
import json
with open('${RESP_TMP}') as f:
    d = json.load(f)
print(d.get('name',''))
" 2>/dev/null) || MUTATE_OP=""
rm -f "${MUTATE_TMP}" "${RESP_TMP}"
echo "  Submitted. Operation: ${MUTATE_OP}"

# ── Poll operation ────────────────────────────────────────────
if [ -n "${MUTATE_OP}" ]; then
  echo "  Waiting..."
  j=0
  while [ "${j}" -lt 12 ]; do
    sleep 5; j=$((j+1))
    POLL_TMP=$(mktmp poll)
    curl --silent --output "${POLL_TMP}" \
      --header "Authorization: Bearer $(gcloud auth print-access-token)" \
      "https://${SEL_REGION}-aiplatform.googleapis.com/v1beta1/${MUTATE_OP}" 2>/dev/null || true
    DONE=$(python3 -c "
import json
with open('${POLL_TMP}') as f:
    d = json.load(f)
print('true' if d.get('done') else 'false')
" 2>/dev/null) || DONE="false"
    rm -f "${POLL_TMP}"
    [ "${DONE}" = "true" ] && echo "  Done." && break
    printf "."
  done
  echo ""
fi

# ── Verify ────────────────────────────────────────────────────
echo ""
echo "▶ Verifying..."
sleep 3

VERIFY_TMP=$(mktmp verify)
curl --silent --output "${VERIFY_TMP}" \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${SEL_REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${SEL_REGION}/endpoints/${SEL_EP_ID}"

python3 - "${VERIFY_TMP}" << 'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
models = d.get("deployedModels", [])
if not models:
    print("  No deployed models found")
    sys.exit(0)
dr  = models[0].get("dedicatedResources", {})
s2z = dr.get("scaleToZeroSpec", {})
print(f"  min_replica_count    : {dr.get('minReplicaCount','?')}")
print(f"  max_replica_count    : {dr.get('maxReplicaCount','?')}")
print(f"  idle_scaledown_period: {s2z.get('idleScaledownPeriod','⚠ not yet visible — wait 30s')}")
print(f"  min_scaleup_period   : {s2z.get('minScaleupPeriod','⚠ not yet visible')}")
PYEOF

rm -f "${VERIFY_TMP}"

echo ""
echo "======================================================="
echo "  DONE — ${SEL_DISP}"
echo "  Endpoint : ${SEL_EP_ID}"
echo "  Region   : ${SEL_REGION}"
echo "  Scales to 0 after ${IDLE_SCALEDOWN} idle"
echo ""
echo "  Verify:"
echo "  gcloud ai endpoints describe ${SEL_EP_ID} \\"
echo "    --region=${SEL_REGION} --project=${PROJECT_ID} \\"
echo "    --format='yaml(deployedModels[0].dedicatedResources)'"
echo "======================================================="
