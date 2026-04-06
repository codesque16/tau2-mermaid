#!/bin/bash
# =============================================================
# redeploy_gemma4_fp8.sh
#
# Redeploys Gemma 4 31B with:
#   --quantization fp8          → on-the-fly FP8 weight quant
#   --kv-cache-dtype fp8        → FP8 KV cache (saves ~40% VRAM)
#   --max-model-len 32768       → 32K context (up from 16K)
#
# Why this works on H100 80GB:
#   BF16 weights:  31B × 2 bytes = ~62 GB
#   FP8 weights:   31B × 1 byte  = ~31 GB
#   VRAM freed: ~31 GB → enough for 32K KV cache headroom
#
# Steps:
#   1. Delete old endpoint (cannot mutate container args in-place)
#   2. Upload model with custom vLLM args
#   3. Create new dedicated endpoint
#   4. Deploy model to endpoint
#   5. Apply scale-to-zero via mutateDeployedModel
#
# Container image: same one Model Garden used (pytorch-vllm-serve:gemma4)
# Model weights:   already in Google's GCS — no HuggingFace needed
# =============================================================
set -eu
trap 'echo ""; echo "FAILED at line ${LINENO}: ${BASH_COMMAND}"; exit 1' ERR

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="asia-southeast1"
OLD_ENDPOINT_ID="${OLD_ENDPOINT_ID:-}"   # set to skip auto-lookup
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"  # override: 16384 / 32768 / 65536

# The same container image Google Model Garden uses for Gemma 4
VLLM_IMAGE="us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:gemma4"

# Model weights path in Google's restricted GCS bucket
# (same path seen in your screenshot's container args)
MODEL_GCS="gs://vertex-model-garden-restricted-us/gemma4/gemma-4-31B-it"

BASE_URL="https://${REGION}-aiplatform.googleapis.com"
mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

echo "======================================================="
echo "  Redeploy Gemma 4 31B — FP8 + ${MAX_MODEL_LEN} context"
echo "  Project: ${PROJECT_ID}  |  Region: ${REGION}"
echo "======================================================="
echo ""

TOKEN=$(gcloud auth print-access-token)
gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

refresh_token() { TOKEN=$(gcloud auth print-access-token); }

api_get() {
  local URL="$1" TMP
  TMP=$(mktmp api_get)
  local CODE
  CODE=$(curl --silent --output "${TMP}" --write-out "%{http_code}" \
    --header "Authorization: Bearer ${TOKEN}" "${URL}")
  if [ "${CODE}" -ge 200 ] && [ "${CODE}" -lt 300 ]; then
    cat "${TMP}"; rm -f "${TMP}"
  else
    echo "HTTP ${CODE}:" >&2; cat "${TMP}" >&2; rm -f "${TMP}"; return 1
  fi
}

api_post() {
  local URL="$1" PAYLOAD_FILE="$2" TMP
  TMP=$(mktmp api_post)
  local CODE
  CODE=$(curl --silent --output "${TMP}" --write-out "%{http_code}" \
    --request POST \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Content-Type: application/json" \
    --data @"${PAYLOAD_FILE}" "${URL}")
  if [ "${CODE}" -ge 200 ] && [ "${CODE}" -lt 300 ]; then
    cat "${TMP}"; rm -f "${TMP}"
  else
    echo "ERROR HTTP ${CODE}:"; cat "${TMP}"; rm -f "${TMP}"; exit 1
  fi
}

poll_op() {
  local OP_NAME="$1" LABEL="${2:-operation}"
  echo "  Polling ${LABEL}..."
  while true; do
    refresh_token
    RESULT=$(api_get "${BASE_URL}/v1/${OP_NAME}" 2>/dev/null || echo "{}")
    DONE=$(python3 -c "import json,sys; d=json.loads('${RESULT}' if '${RESULT}' else '{}'); print('true' if d.get('done') else 'false')" 2>/dev/null || echo "false")
    ERR=$(python3 -c "
import json
d = json.loads('''${RESULT}''')
e = d.get('error')
print(json.dumps(e) if e else '')
" 2>/dev/null || echo "")
    if [ -n "${ERR}" ] && [ "${ERR}" != "null" ] && [ "${ERR}" != "" ]; then
      echo "  FAILED: ${ERR}"; exit 1
    fi
    [ "${DONE}" = "true" ] && echo "  Done." && break
    printf "."
    sleep 30
  done
  echo ""
}

# ── Step 0: Find and delete old endpoint ─────────────────────
echo "▶ 0/5  Finding existing Gemma 4 endpoint to delete..."

if [ -z "${OLD_ENDPOINT_ID}" ]; then
  EP_LIST=$(mktmp ep_list)
  curl --silent --output "${EP_LIST}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints"

  OLD_ENDPOINT_ID=$(python3 -c "
import json
with open('${EP_LIST}') as f:
    d = json.load(f)
for ep in d.get('endpoints', []):
    name = ep.get('displayName','')
    if 'gemma' in name.lower() or 'gemma4' in name.lower():
        print(ep['name'].split('/')[-1])
        break
" 2>/dev/null || echo "")
  rm -f "${EP_LIST}"
fi

if [ -n "${OLD_ENDPOINT_ID}" ]; then
  echo "  Found: ${OLD_ENDPOINT_ID}"
  printf "  Undeploy + delete this endpoint? [y/N]: "
  read -r CONFIRM
  if [ "${CONFIRM}" = "y" ] || [ "${CONFIRM}" = "Y" ]; then

    # Step 0a: get deployed model ID(s) — must undeploy before delete
    echo "  Fetching deployed model IDs..."
    EP_TMP=$(mktmp ep_desc)
    curl --silent --output "${EP_TMP}" \
      --header "Authorization: Bearer ${TOKEN}" \
      "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${OLD_ENDPOINT_ID}"

    DEPLOYED_MODEL_IDS=$(python3 -c "
import json
with open('${EP_TMP}') as f:
    d = json.load(f)
for m in d.get('deployedModels', []):
    print(m['id'])
" 2>/dev/null || echo "")
    rm -f "${EP_TMP}"

    # Step 0b: undeploy each model
    for DM_ID in ${DEPLOYED_MODEL_IDS}; do
      echo "  Undeploying model ${DM_ID}..."
      UNDEP_PAYLOAD=$(mktmp undeploy)
      cat > "${UNDEP_PAYLOAD}" << UNDEP_EOF
{
  "deployedModelId": "${DM_ID}",
  "trafficSplit": {}
}
UNDEP_EOF
      UNDEP_TMP=$(mktmp undep_resp)
      UNDEP_CODE=$(curl --silent --output "${UNDEP_TMP}" --write-out "%{http_code}" \
        --request POST \
        --header "Authorization: Bearer ${TOKEN}" \
        --header "Content-Type: application/json" \
        --data @"${UNDEP_PAYLOAD}" \
        "${BASE_URL}/v1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${OLD_ENDPOINT_ID}:undeployModel")
      echo "  Undeploy HTTP: ${UNDEP_CODE}"

      if [ "${UNDEP_CODE}" = "200" ]; then
        # Poll the undeploy LRO
        UNDEP_OP=$(python3 -c "
import json
with open('${UNDEP_TMP}') as f:
    d = json.load(f)
print(d.get('name',''))
" 2>/dev/null || echo "")
        rm -f "${UNDEP_PAYLOAD}" "${UNDEP_TMP}"

        if [ -n "${UNDEP_OP}" ]; then
          echo "  Waiting for undeploy to complete..."
          while true; do
            refresh_token
            DONE=$(curl --silent \
              --header "Authorization: Bearer ${TOKEN}" \
              "${BASE_URL}/v1/${UNDEP_OP}" \
              | python3 -c "import json,sys; d=json.load(sys.stdin); print('true' if d.get('done') else 'false')" \
              2>/dev/null || echo "false")
            [ "${DONE}" = "true" ] && echo "  Undeployed." && break
            printf "."; sleep 10
          done
          echo ""
        fi
      else
        cat "${UNDEP_TMP}"
        rm -f "${UNDEP_PAYLOAD}" "${UNDEP_TMP}"
        echo "  WARNING: undeploy failed — attempting delete anyway"
      fi
    done

    # Step 0c: now delete the empty endpoint
    echo "  Deleting endpoint..."
    DEL_TMP=$(mktmp del_resp)
    DEL_CODE=$(curl --silent --output "${DEL_TMP}" --write-out "%{http_code}" \
      --request DELETE \
      --header "Authorization: Bearer ${TOKEN}" \
      "${BASE_URL}/v1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${OLD_ENDPOINT_ID}")
    echo "  Delete HTTP: ${DEL_CODE}"
    if [ "${DEL_CODE}" != "200" ]; then
      cat "${DEL_TMP}"
      echo "  WARNING: delete failed — continuing with new deployment anyway"
    fi
    rm -f "${DEL_TMP}"
    echo "  Waiting 30s for cleanup..."
    sleep 30
  else
    echo "  Skipping — will create a new endpoint alongside the old one."
  fi
else
  echo "  No existing Gemma endpoint found — will create fresh."
fi

# ── Step 1: Upload model with custom vLLM args ───────────────
echo ""
echo "▶ 1/5  Uploading model with custom vLLM args..."
echo "  max_model_len : ${MAX_MODEL_LEN}"
echo "  quantization  : fp8 (on-the-fly, no pre-quantized weights needed)"
echo "  kv_cache_dtype: fp8"
echo ""

UPLOAD_PAYLOAD=$(mktmp upload_payload)
cat > "${UPLOAD_PAYLOAD}" << PAYLOAD_EOF
{
  "model": {
    "displayName": "gemma4-31b-fp8-${MAX_MODEL_LEN}",
    "description": "Gemma 4 31B FP8 quantized, ${MAX_MODEL_LEN} context",
    "baseModelSource": {
      "modelGardenSource": {
        "publicModelName": "publishers/google/models/gemma4@gemma-4-31b-it"
      }
    },
    "containerSpec": {
      "imageUri": "${VLLM_IMAGE}",
      "command": ["python", "-m", "vllm.entrypoints.api_server"],
      "args": [
        "--host=0.0.0.0",
        "--port=8080",
        "--model=${MODEL_GCS}",
        "--tensor-parallel-size=1",
        "--gpu-memory-utilization=0.92",
        "--max-model-len=${MAX_MODEL_LEN}",
        "--quantization=fp8",
        "--kv-cache-dtype=fp8",
        "--max-num-seqs=128",
        "--enable-auto-tool-choice",
        "--tool-call-parser=gemma4",
        "--reasoning-parser=gemma4",
        "--swap-space=16",
        "--disable-log-stats"
      ],
      "ports": [{"containerPort": 8080}],
      "predictRoute": "/generate",
      "healthRoute": "/ping"
    }
  }
}
PAYLOAD_EOF

UPLOAD_RESP=$(api_post \
  "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/models:upload" \
  "${UPLOAD_PAYLOAD}")
rm -f "${UPLOAD_PAYLOAD}"

MODEL_ID=$(python3 -c "
import json, sys
d = json.loads('''${UPLOAD_RESP}''')
# Upload returns an LRO — get model from metadata or response
name = d.get('response', {}).get('name', '') or d.get('name', '')
print(name)
" 2>/dev/null || echo "")

echo "  Upload response: ${MODEL_ID}"

# Poll if it's an LRO
if echo "${MODEL_ID}" | grep -q "operations"; then
  poll_op "${MODEL_ID}" "model upload"
  refresh_token
  MODEL_RESOURCE=$(api_get "${BASE_URL}/v1/${MODEL_ID}" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d.get('response',{}).get('name',''))
" 2>/dev/null || echo "")
else
  MODEL_RESOURCE="${MODEL_ID}"
fi

echo "  Model resource: ${MODEL_RESOURCE}"

# ── Step 2: Create dedicated endpoint ────────────────────────
echo ""
echo "▶ 2/5  Creating dedicated endpoint..."

EP_PAYLOAD=$(mktmp ep_payload)
cat > "${EP_PAYLOAD}" << EP_EOF
{
  "displayName": "gemma4-31b-fp8-endpoint",
  "dedicatedEndpointEnabled": true
}
EP_EOF

EP_OP=$(api_post \
  "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints" \
  "${EP_PAYLOAD}" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
rm -f "${EP_PAYLOAD}"

echo "  Endpoint LRO: ${EP_OP}"

# Poll endpoint creation
echo "  Waiting for endpoint..."
while true; do
  refresh_token
  DONE=$(api_get "${BASE_URL}/v1beta1/${EP_OP}" | python3 -c "
import json,sys; d=json.load(sys.stdin); print('true' if d.get('done') else 'false')
" 2>/dev/null || echo "false")
  [ "${DONE}" = "true" ] && break
  printf "."; sleep 10
done
echo " done"

NEW_ENDPOINT_ID=$(api_get "${BASE_URL}/v1beta1/${EP_OP}" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d['response']['name'].split('/')[-1])
" 2>/dev/null || echo "")
echo "  Endpoint ID: ${NEW_ENDPOINT_ID}"

# ── Step 3: Deploy model to endpoint ─────────────────────────
echo ""
echo "▶ 3/5  Deploying model to endpoint (15–25 min)..."

DEPLOY_PAYLOAD=$(mktmp deploy_payload)
cat > "${DEPLOY_PAYLOAD}" << DEPLOY_EOF
{
  "deployedModel": {
    "model": "${MODEL_RESOURCE}",
    "displayName": "gemma4-31b-fp8-deployed",
    "dedicatedResources": {
      "machineSpec": {
        "machineType": "a3-highgpu-1g",
        "acceleratorType": "NVIDIA_H100_80GB",
        "acceleratorCount": 1
      },
      "minReplicaCount": 1,
      "maxReplicaCount": 1
    }
  },
  "trafficSplit": {"0": 100}
}
DEPLOY_EOF

DEPLOY_OP=$(api_post \
  "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${NEW_ENDPOINT_ID}:deployModel" \
  "${DEPLOY_PAYLOAD}" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
rm -f "${DEPLOY_PAYLOAD}"

echo "  Deploy LRO: ${DEPLOY_OP}"

# Poll deployment
DOTS=0
while true; do
  refresh_token
  RESP=$(api_get "${BASE_URL}/v1/${DEPLOY_OP}" 2>/dev/null || echo "{}")
  DONE=$(python3 -c "import json; d=json.loads('${RESP}' if '${RESP}' else '{}'); print('true' if d.get('done') else 'false')" 2>/dev/null || echo "false")
  [ "${DONE}" = "true" ] && echo "" && echo "  Deployment complete!" && break
  printf "."; DOTS=$((DOTS+1))
  [ $((DOTS % 20)) -eq 0 ] && echo " ${DOTS} polls ($((DOTS/2)) min)"
  sleep 30
done

# ── Step 4: Apply scale-to-zero ──────────────────────────────
echo ""
echo "▶ 4/5  Applying scale-to-zero (idle=10 min)..."
refresh_token

DM_TMP=$(mktmp dm)
curl --silent --output "${DM_TMP}" \
  --header "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${NEW_ENDPOINT_ID}"

DM_ID=$(python3 -c "
import json
with open('${DM_TMP}') as f:
    d = json.load(f)
models = d.get('deployedModels', [])
print(models[0]['id'] if models else '')
" 2>/dev/null || echo "")
rm -f "${DM_TMP}"

if [ -n "${DM_ID}" ]; then
  MUTATE_PAYLOAD=$(mktmp mutate)
  cat > "${MUTATE_PAYLOAD}" << MUTATE_EOF
{
  "deployedModel": {
    "id": "${DM_ID}",
    "dedicatedResources": {
      "minReplicaCount": 0,
      "maxReplicaCount": 1,
      "initialReplicaCount": 1,
      "scaleToZeroSpec": {
        "idleScaledownPeriod": "600s",
        "minScaleupPeriod": "300s"
      }
    }
  },
  "updateMask": "dedicated_resources.min_replica_count,dedicated_resources.max_replica_count,dedicated_resources.scale_to_zero_spec"
}
MUTATE_EOF

  refresh_token
  MUTATE_CODE=$(curl --silent --output /dev/null --write-out "%{http_code}" \
    --request POST \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Content-Type: application/json" \
    --data @"${MUTATE_PAYLOAD}" \
    "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${NEW_ENDPOINT_ID}:mutateDeployedModel")
  rm -f "${MUTATE_PAYLOAD}"
  echo "  mutateDeployedModel HTTP: ${MUTATE_CODE}"
fi

# ── Step 5: Save to Secret Manager ───────────────────────────
echo ""
echo "▶ 5/5  Saving endpoint ID to Secret Manager..."
echo -n "${NEW_ENDPOINT_ID}" | gcloud secrets create gemma4-endpoint-id \
  --data-file=- --project="${PROJECT_ID}" 2>/dev/null || \
echo -n "${NEW_ENDPOINT_ID}" | gcloud secrets versions add gemma4-endpoint-id \
  --data-file=- --project="${PROJECT_ID}"

echo ""
echo "======================================================="
echo "  DONE"
echo "  Endpoint : ${NEW_ENDPOINT_ID}"
echo "  Context  : ${MAX_MODEL_LEN} tokens"
echo "  Quant    : FP8 weights + FP8 KV cache"
echo ""
echo "  Memory breakdown on H100 80 GB:"
echo "    BF16 weights (original) : ~62 GB"
echo "    FP8 weights (now)       : ~31 GB"
echo "    VRAM freed              : ~31 GB"
echo "    Available for KV cache  : ~31 GB → supports ~32K+ context"
echo ""
echo "  Console:"
echo "  https://console.cloud.google.com/vertex-ai/online-prediction/endpoints/${NEW_ENDPOINT_ID}?project=${PROJECT_ID}&region=${REGION}"
echo "======================================================="
