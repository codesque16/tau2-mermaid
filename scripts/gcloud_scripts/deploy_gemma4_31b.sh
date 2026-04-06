#!/usr/bin/env bash
# =============================================================
# deploy_gemma4_31b.sh
#
# Deploys Gemma 4 31B-IT from Vertex AI Model Garden.
# Region: asia-southeast1 | Hardware: NVIDIA_H100_80GB
# =============================================================

# ── Strict mode with visible trap ────────────────────────────
set -euo pipefail
trap 'echo ""; echo "FAILED at line ${LINENO}: ${BASH_COMMAND}"; exit 1' ERR

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="asia-southeast1"
ENDPOINT_DISPLAY_NAME="gemma4-31b-endpoint"

echo "======================================================="
echo "  Deploying Gemma 4 31B-IT"
echo "  Project: ${PROJECT_ID}  |  Region: ${REGION}"
echo "======================================================="
echo ""

# ── Get token once, reuse (avoids subshell failures) ─────────
echo "▶ Getting access token..."
TOKEN=$(gcloud auth print-access-token)
if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: Could not get access token. Run: gcloud auth login"
  exit 1
fi
echo "  Token: OK (${TOKEN:0:20}...)"
echo ""

gcloud config set project "${PROJECT_ID}" --quiet

# ── Step 1: Deploy via Model Garden :deploy API ───────────────
# Uses /v1/projects/.../locations:deploy which is the correct
# endpoint for publisher models (not /endpoints/{id}:deployModel)
echo "▶ 1/3  Calling Model Garden deploy API..."
echo "  URL: https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}:deploy"
echo ""

# Write payload to temp file to avoid quoting/heredoc issues
PAYLOAD_FILE=$(mktemp /tmp/gemma_deploy_XXXXXX.json)
cat > "${PAYLOAD_FILE}" << 'PAYLOAD_EOF'
{
  "publisher_model_name": "publishers/google/models/gemma4@gemma-4-31b-it",
  "model_config": {
    "accept_eula": true
  },
  "deploy_config": {
    "dedicated_resources": {
      "machine_spec": {
        "machine_type": "a3-highgpu-1g",
        "accelerator_type": "NVIDIA_H100_80GB",
        "accelerator_count": 1
      },
      "min_replica_count": 1,
      "max_replica_count": 1
    }
  }
}
PAYLOAD_EOF

echo "  Payload:"
cat "${PAYLOAD_FILE}"
echo ""

RESPONSE_FILE=$(mktemp /tmp/gemma_response_XXXXXX.json)

HTTP_CODE=$(curl \
  --silent \
  --output "${RESPONSE_FILE}" \
  --write-out "%{http_code}" \
  --request POST \
  --header "Authorization: Bearer ${TOKEN}" \
  --header "Content-Type: application/json" \
  --data @"${PAYLOAD_FILE}" \
  "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}:deploy")

echo "  HTTP response code: ${HTTP_CODE}"
echo "  Response body:"
cat "${RESPONSE_FILE}"
echo ""

if [[ "${HTTP_CODE}" -lt 200 || "${HTTP_CODE}" -ge 300 ]]; then
  echo "ERROR: Deploy API call failed (HTTP ${HTTP_CODE})"
  rm -f "${PAYLOAD_FILE}" "${RESPONSE_FILE}"
  exit 1
fi

# ── Extract operation name ────────────────────────────────────
OPERATION=$(python3 -c "
import json, sys
with open('${RESPONSE_FILE}') as f:
    d = json.load(f)
op = d.get('name', '')
if not op:
    print('ERROR: no operation name in response', file=sys.stderr)
    sys.exit(1)
print(op)
")

echo "  Operation: ${OPERATION}"
rm -f "${PAYLOAD_FILE}" "${RESPONSE_FILE}"

# ── Step 2: Poll until done ───────────────────────────────────
echo ""
echo "▶ 2/3  Polling deployment status (takes 15–25 min)..."
echo "  Ctrl+C to stop polling — deployment continues in background."
echo ""

POLL_URL="https://${REGION}-aiplatform.googleapis.com/v1/${OPERATION}"
DOTS=0

while true; do
  POLL_FILE=$(mktemp /tmp/gemma_poll_XXXXXX.json)
  POLL_CODE=$(curl \
    --silent \
    --output "${POLL_FILE}" \
    --write-out "%{http_code}" \
    --header "Authorization: Bearer $(gcloud auth print-access-token)" \
    "${POLL_URL}")

  if [[ "${POLL_CODE}" != "200" ]]; then
    echo "  Poll HTTP ${POLL_CODE} — retrying..."
    rm -f "${POLL_FILE}"
    sleep 30
    continue
  fi

  DONE=$(python3 -c "
import json
with open('${POLL_FILE}') as f:
    d = json.load(f)
print('true' if d.get('done') else 'false')
")

  ERR=$(python3 -c "
import json
with open('${POLL_FILE}') as f:
    d = json.load(f)
e = d.get('error')
print(json.dumps(e) if e else '')
" 2>/dev/null || echo "")

  rm -f "${POLL_FILE}"

  if [[ -n "${ERR}" && "${ERR}" != "null" && "${ERR}" != "" ]]; then
    echo ""
    echo "ERROR: Deployment failed:"
    echo "${ERR}"
    exit 1
  fi

  if [[ "${DONE}" == "true" ]]; then
    echo ""
    echo "  Deployment complete!"
    break
  fi

  printf "."
  DOTS=$((DOTS + 1))
  if (( DOTS % 20 == 0 )); then
    echo " ${DOTS} polls ($(( DOTS / 2 )) min elapsed)"
  fi
  sleep 30
done

# ── Get endpoint ID ───────────────────────────────────────────
echo ""
echo "  Finding endpoint by display name..."
EP_LIST_FILE=$(mktemp /tmp/gemma_ep_XXXXXX.json)

curl \
  --silent \
  --output "${EP_LIST_FILE}" \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/endpoints"

ENDPOINT_ID=$(python3 -c "
import json
with open('${EP_LIST_FILE}') as f:
    d = json.load(f)
for ep in d.get('endpoints', []):
    if '${ENDPOINT_DISPLAY_NAME}' in ep.get('displayName', ''):
        print(ep['name'].split('/')[-1])
        break
" 2>/dev/null || echo "")

rm -f "${EP_LIST_FILE}"

if [[ -z "${ENDPOINT_ID}" ]]; then
  echo "  WARNING: Could not auto-find endpoint ID."
  echo "  Find it manually:"
  echo "  gcloud ai endpoints list --region=${REGION} --project=${PROJECT_ID} --filter=displayName=${ENDPOINT_DISPLAY_NAME}"
  echo ""
  echo "  Then run step 3 manually (see bottom of this script)"
  exit 0
fi

echo "  Endpoint ID: ${ENDPOINT_ID}"

# ── Step 3: Apply scale-to-zero ───────────────────────────────
echo ""
echo "▶ 3/3  Patching scale-to-zero (idle=10 min)..."

# Get deployed model ID
DM_FILE=$(mktemp /tmp/gemma_dm_XXXXXX.json)
curl \
  --silent \
  --output "${DM_FILE}" \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${ENDPOINT_ID}"

DEPLOYED_MODEL_ID=$(python3 -c "
import json
with open('${DM_FILE}') as f:
    d = json.load(f)
models = d.get('deployedModels', [])
print(models[0]['id'] if models else '')
" 2>/dev/null || echo "")

rm -f "${DM_FILE}"

if [[ -z "${DEPLOYED_MODEL_ID}" ]]; then
  echo "  Could not get deployed model ID — skip scale-to-zero patch for now."
  echo "  Run this after deployment is fully ready:"
  echo ""
  echo "  DEPLOYED_MODEL_ID=\$(gcloud ai endpoints describe ${ENDPOINT_ID} \\"
  echo "    --region=${REGION} --project=${PROJECT_ID} \\"
  echo "    --format='value(deployedModels[0].id)')"
  echo ""
  echo "  Then manually call mutateDeployedModel — see script comments."
else
  MUTATE_FILE=$(mktemp /tmp/gemma_mutate_XXXXXX.json)
  cat > "${MUTATE_FILE}" << MUTATE_EOF
{
  "deployedModel": {
    "id": "${DEPLOYED_MODEL_ID}",
    "dedicatedResources": {
      "minReplicaCount": 0,
      "maxReplicaCount": 1,
      "initialReplicaCount": 1,
      "scaleToZeroSpec": {
        "idleScaledownPeriod": "600s",
        "minScaleupPeriod": "180s"
      }
    }
  },
  "updateMask": "dedicated_resources.min_replica_count,dedicated_resources.max_replica_count,dedicated_resources.scale_to_zero_spec"
}
MUTATE_EOF

  MUTATE_RESP=$(mktemp /tmp/gemma_mresp_XXXXXX.json)
  MUTATE_CODE=$(curl \
    --silent \
    --output "${MUTATE_RESP}" \
    --write-out "%{http_code}" \
    --request POST \
    --header "Authorization: Bearer $(gcloud auth print-access-token)" \
    --header "Content-Type: application/json" \
    --data @"${MUTATE_FILE}" \
    "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${ENDPOINT_ID}:mutateDeployedModel")

  echo "  mutateDeployedModel HTTP: ${MUTATE_CODE}"
  if [[ "${MUTATE_CODE}" == "200" ]]; then
    echo "  Scale-to-zero applied:"
    echo "    idleScaledownPeriod : 600s (10 min idle → 0 replicas)"
    echo "    minScaleupPeriod    : 180s (3 min buffer after wake-up)"
  else
    echo "  Response:"
    cat "${MUTATE_RESP}"
  fi
  rm -f "${MUTATE_FILE}" "${MUTATE_RESP}"
fi

# ── Save to Secret Manager ────────────────────────────────────
echo ""
echo "  Saving endpoint ID to Secret Manager..."
echo -n "${ENDPOINT_ID}" | gcloud secrets create gemma4-endpoint-id \
  --data-file=- --project="${PROJECT_ID}" 2>/dev/null || \
echo -n "${ENDPOINT_ID}" | gcloud secrets versions add gemma4-endpoint-id \
  --data-file=- --project="${PROJECT_ID}"

echo ""
echo "======================================================="
echo "  COMPLETE"
echo "  Endpoint ID : ${ENDPOINT_ID}"
echo "  Console     : https://console.cloud.google.com/vertex-ai/online-prediction/endpoints/${ENDPOINT_ID}?project=${PROJECT_ID}&region=${REGION}"
echo "======================================================="
