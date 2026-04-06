#!/usr/bin/env bash
# =============================================================
# ping_endpoints.sh
#
# Keepalive ping — sends a minimal request to each endpoint
# every N minutes to prevent scale-to-zero during active hours.
#
# How it works:
#   Vertex AI scale-to-zero triggers after idle_scaledown_period
#   (we set 600s = 10 min). A ping every 8 min resets the idle
#   timer, keeping the endpoint live at zero inference cost
#   (max_tokens=1 consumes negligible GPU compute).
#
# Usage:
#   bash ping_endpoints.sh            # runs forever, pings every 8 min
#   INTERVAL=300 bash ping_endpoints.sh  # ping every 5 min
#   bash ping_endpoints.sh --once     # single ping and exit
#   bash ping_endpoints.sh --status   # check current state only
#
# Run as background daemon:
#   nohup bash ping_endpoints.sh >> /tmp/ping.log 2>&1 &
#   echo $! > /tmp/ping.pid
#
# Stop daemon:
#   kill $(cat /tmp/ping.pid)
# =============================================================
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
QWEN_REGION="us-central1"
GEMMA_REGION="asia-southeast1"
INTERVAL="${INTERVAL:-480}"   # 8 min — safely under the 10 min scaledown window
MODE="${1:---loop}"

# Load endpoint IDs
QWEN_ENDPOINT_ID="${QWEN_ENDPOINT_ID:-$(gcloud secrets versions access latest \
  --secret=qwen35-endpoint-id --project="${PROJECT_ID}" 2>/dev/null || echo '')}"
GEMMA_ENDPOINT_ID="${GEMMA_ENDPOINT_ID:-$(gcloud secrets versions access latest \
  --secret=gemma4-endpoint-id --project="${PROJECT_ID}" 2>/dev/null || echo '')}"

# ── Ping one endpoint ─────────────────────────────────────────
ping_endpoint() {
  local NAME="$1"
  local ENDPOINT_ID="$2"
  local REGION="$3"
  local BASE_URL="https://${REGION}-aiplatform.googleapis.com"

  if [[ -z "${ENDPOINT_ID}" ]]; then
    echo "[$(date '+%H:%M:%S')] ${NAME}: endpoint ID not set, skipping"
    return
  fi

  TOKEN=$(gcloud auth print-access-token 2>/dev/null)
  START=$(date +%s%N)

  HTTP_CODE=$(curl -sf -o /tmp/ping_response.json -w "%{http_code}" -X POST \
    "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${ENDPOINT_ID}:rawPredict" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"instances":[{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}]}' \
    --max-time 15 2>/dev/null || echo "000")

  END=$(date +%s%N)
  MS=$(( (END - START) / 1000000 ))

  case "${HTTP_CODE}" in
    200)
      echo "[$(date '+%H:%M:%S')] ${NAME}: OK  ${MS}ms  (warm, idle timer reset)"
      ;;
    429)
      # 429 = scaled to zero, but our ping triggered scale-up
      echo "[$(date '+%H:%M:%S')] ${NAME}: SCALED_TO_ZERO — scale-up triggered (will be ready in ~3–5 min)"
      ;;
    000)
      echo "[$(date '+%H:%M:%S')] ${NAME}: TIMEOUT after ${MS}ms"
      ;;
    503)
      echo "[$(date '+%H:%M:%S')] ${NAME}: SERVICE_UNAVAILABLE (endpoint may be deploying)"
      ;;
    *)
      echo "[$(date '+%H:%M:%S')] ${NAME}: HTTP ${HTTP_CODE}  ${MS}ms"
      ;;
  esac
}

# ── Status check (describe endpoint) ─────────────────────────
check_status() {
  local NAME="$1"
  local ENDPOINT_ID="$2"
  local REGION="$3"
  local BASE_URL="https://${REGION}-aiplatform.googleapis.com"

  if [[ -z "${ENDPOINT_ID}" ]]; then
    echo "  ${NAME}: endpoint ID not configured"
    return
  fi

  TOKEN=$(gcloud auth print-access-token 2>/dev/null)
  curl -sf \
    "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${ENDPOINT_ID}" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
models = d.get('deployedModels', [])
if not models:
    print('  ${NAME}: no deployed models')
else:
    m = models[0]
    state = m.get('state', 'UNKNOWN')
    dr = m.get('dedicatedResources', {})
    s2z = dr.get('scaleToZeroSpec', {})
    print(f'  ${NAME}:')
    print(f'    state             : {state}')
    print(f'    min/max replicas  : {dr.get(\"minReplicaCount\",\"?\")} / {dr.get(\"maxReplicaCount\",\"?\")}')
    print(f'    idle_scaledown    : {s2z.get(\"idleScaledownPeriod\",\"not set\")}')
    print(f'    min_scaleup       : {s2z.get(\"minScaleupPeriod\",\"not set\")}')
" 2>/dev/null || echo "  ${NAME}: could not fetch status"
}

# ── Main logic ────────────────────────────────────────────────
case "${MODE}" in
  --status)
    echo "=== Endpoint Status ==="
    check_status "Qwen 3.5-9B" "${QWEN_ENDPOINT_ID}" "${QWEN_REGION}"
    check_status "Gemma 4 31B" "${GEMMA_ENDPOINT_ID}" "${GEMMA_REGION}"
    exit 0
    ;;

  --once)
    echo "=== Single ping ==="
    ping_endpoint "Qwen 3.5-9B" "${QWEN_ENDPOINT_ID}" "${QWEN_REGION}"
    ping_endpoint "Gemma 4 31B" "${GEMMA_ENDPOINT_ID}" "${GEMMA_REGION}"
    exit 0
    ;;

  --loop|*)
    echo "=== Keepalive loop (interval=${INTERVAL}s) ==="
    echo "    Ctrl+C or kill PID to stop"
    echo "    Endpoints pinged every ${INTERVAL}s to prevent 10-min scale-to-zero"
    echo ""
    while true; do
      ping_endpoint "Qwen 3.5-9B" "${QWEN_ENDPOINT_ID}" "${QWEN_REGION}"
      ping_endpoint "Gemma 4 31B" "${GEMMA_ENDPOINT_ID}" "${GEMMA_REGION}"
      echo "    [sleeping ${INTERVAL}s...]"
      sleep "${INTERVAL}"
    done
    ;;
esac
