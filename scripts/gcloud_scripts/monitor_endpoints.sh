#!/bin/bash
# =============================================================
# monitor_endpoints.sh
#
# Scans ALL GCP regions in parallel, finds every Vertex AI
# endpoint, and prints full LLM serving metrics for each:
#   replica state, scale-to-zero config, latency (p50/p95),
#   TTFT, GPU duty cycle, VRAM, request rate, errors,
#   token throughput, idle analysis, live ping.
#
# macOS bash 3.2 compatible — no associative arrays,
# BSD mktemp safe (no extension after X template).
#
# Usage:
#   bash monitor_endpoints.sh           # one-shot
#   watch -n 60 bash monitor_endpoints.sh   # live refresh
# =============================================================

set -eu

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"

ALL_REGIONS="africa-south1 northamerica-northeast1 northamerica-northeast2 southamerica-east1 southamerica-west1 us-central1 us-east1 us-east4 us-east5 us-south1 us-west1 us-west2 us-west3 us-west4 us-west8 asia-east1 asia-east2 asia-northeast1 asia-northeast2 asia-northeast3 asia-south1 asia-south2 asia-southeast1 asia-southeast2 australia-southeast1 australia-southeast2 europe-central2 europe-north1 europe-north2 europe-southwest1 europe-west1 europe-west2 europe-west3 europe-west4 europe-west6 europe-west8 europe-west9 europe-west12 europe-west15 me-central1 me-central2 me-west1"

# BSD mktemp safe helper — no extension after X's
mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

# ── Token + time ──────────────────────────────────────────────
TOKEN=$(gcloud auth print-access-token)
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
# macOS / Linux compatible 1-hour-ago
MINUS_1H=$(date -u -v-1H +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || \
           date -u -d "1 hour ago" +"%Y-%m-%dT%H:%M:%SZ")
MINUS_30M=$(date -u -v-30M +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || \
            date -u -d "30 minutes ago" +"%Y-%m-%dT%H:%M:%SZ")

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "  LLM Endpoint Monitor  —  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  Project: ${PROJECT_ID}"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "▶ Scanning all regions in parallel..."

# ── Parallel scan — same pattern as patch script ──────────────
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

# Count found endpoints
TOTAL_FOUND=0
for R in $ALL_REGIONS; do
  [ -f "${SCAN_DIR}/${R}" ] && TOTAL_FOUND=$((TOTAL_FOUND + 1))
done

if [ "${TOTAL_FOUND}" = "0" ]; then
  echo "  No endpoints found in any region."
  rm -rf "${SCAN_DIR}"
  exit 0
fi
echo "  Found endpoints in ${TOTAL_FOUND} region(s)."

# ── Monitoring helpers ────────────────────────────────────────
# Fetch a scalar metric (mean of last 1h, 5-min alignment)
get_metric() {
  local METRIC="$1"
  local ENDPOINT_ID="$2"
  local TMP
  TMP=$(mktmp metric)
  curl --silent --output "${TMP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?filter=metric.type%3D%22aiplatform.googleapis.com%2F${METRIC}%22+AND+resource.labels.endpoint_id%3D%22${ENDPOINT_ID}%22&interval.startTime=${MINUS_1H}&interval.endTime=${NOW}&aggregation.alignmentPeriod=300s&aggregation.perSeriesAligner=ALIGN_MEAN&aggregation.crossSeriesReducer=REDUCE_MEAN" \
    2>/dev/null || true
  python3 -c "
import json
try:
    d = json.load(open('${TMP}'))
    ts = d.get('timeSeries', [])
    if not ts: print('no_data'); exit()
    pts = ts[0].get('points', [])
    if not pts: print('no_points'); exit()
    val = pts[0]['value']
    v = val.get('doubleValue', val.get('int64Value', None))
    print(round(float(v), 2) if v is not None else 'n/a')
except: print('unavailable')
" 2>/dev/null || echo "unavailable"
  rm -f "${TMP}"
}

# Fetch p95 of a distribution metric
get_metric_p95() {
  local METRIC="$1"
  local ENDPOINT_ID="$2"
  local TMP
  TMP=$(mktmp metric_p95)
  curl --silent --output "${TMP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?filter=metric.type%3D%22aiplatform.googleapis.com%2F${METRIC}%22+AND+resource.labels.endpoint_id%3D%22${ENDPOINT_ID}%22&interval.startTime=${MINUS_1H}&interval.endTime=${NOW}&aggregation.alignmentPeriod=300s&aggregation.perSeriesAligner=ALIGN_PERCENTILE_95" \
    2>/dev/null || true
  python3 -c "
import json
try:
    d = json.load(open('${TMP}'))
    ts = d.get('timeSeries', [])
    if not ts: print('no_data'); exit()
    pts = ts[0].get('points', [])
    if not pts: print('no_points'); exit()
    val = pts[0]['value']
    v = val.get('doubleValue', val.get('int64Value', None))
    # latency metrics come in seconds — convert to ms
    print(str(round(float(v) * 1000, 1)) + ' ms') if v is not None else print('n/a')
except: print('unavailable')
" 2>/dev/null || echo "unavailable"
  rm -f "${TMP}"
}

# Idle windows in last 30 min
get_idle_analysis() {
  local ENDPOINT_ID="$1"
  local TMP
  TMP=$(mktmp idle)
  curl --silent --output "${TMP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?filter=metric.type%3D%22aiplatform.googleapis.com%2Fprediction%2Fonline%2Frequest_count%22+AND+resource.labels.endpoint_id%3D%22${ENDPOINT_ID}%22&interval.startTime=${MINUS_30M}&interval.endTime=${NOW}&aggregation.alignmentPeriod=60s&aggregation.perSeriesAligner=ALIGN_SUM" \
    2>/dev/null || true
  python3 -c "
import json
try:
    d = json.load(open('${TMP}'))
    ts = d.get('timeSeries', [])
    if not ts:
        print('  no traffic data (likely scaled to zero)')
        exit()
    pts = ts[0].get('points', [])
    zeros = sum(1 for p in pts if float(p['value'].get('int64Value', p['value'].get('doubleValue', 1))) == 0)
    total = len(pts)
    print(f'  zero-traffic windows : {zeros}/{total} min in last 30')
    if zeros >= 10:
        print(f'  scale-to-zero status : TRIGGERED (≥10 min idle)')
    else:
        print(f'  scale-to-zero status : not yet ({10 - zeros} more idle min needed)')
except: print('  idle analysis unavailable')
" 2>/dev/null || echo "  unavailable"
  rm -f "${TMP}"
}

# ── Monitor each discovered endpoint ─────────────────────────
for R in $ALL_REGIONS; do
  RFILE="${SCAN_DIR}/${R}"
  [ -f "${RFILE}" ] || continue

  # Parse each endpoint in this region
  python3 - "${RFILE}" "${R}" << 'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
region = sys.argv[2]
for ep in data.get("endpoints", []):
    ep_id  = ep.get("name","").split("/")[-1]
    name   = ep.get("displayName","unnamed")
    models = ep.get("deployedModels", [])
    model  = models[0].get("displayName","no-model") if models else "no-model"
    dm_id  = models[0].get("id","") if models else ""
    dr     = models[0].get("dedicatedResources",{}) if models else {}
    min_r  = str(dr.get("minReplicaCount","?"))
    max_r  = str(dr.get("maxReplicaCount","?"))
    state  = models[0].get("state","UNKNOWN") if models else "UNKNOWN"
    s2z    = dr.get("scaleToZeroSpec",{})
    idle_p = s2z.get("idleScaledownPeriod","not set")
    warm_p = s2z.get("minScaleupPeriod","not set")
    # Print as simple key=value lines, one endpoint block per separator
    print(f"EP_ID={ep_id}")
    print(f"NAME={name}")
    print(f"MODEL={model}")
    print(f"REGION={region}")
    print(f"STATE={state}")
    print(f"MIN={min_r}")
    print(f"MAX={max_r}")
    print(f"IDLE_PERIOD={idle_p}")
    print(f"WARM_PERIOD={warm_p}")
    print("---")
PYEOF
done | while IFS= read -r LINE; do
  # Accumulate fields until we hit --- separator, then process
  case "${LINE}" in
    EP_ID=*)    EP_ID="${LINE#EP_ID=}" ;;
    NAME=*)     EP_NAME="${LINE#NAME=}" ;;
    MODEL=*)    MODEL="${LINE#MODEL=}" ;;
    REGION=*)   REGION="${LINE#REGION=}" ;;
    STATE=*)    STATE="${LINE#STATE=}" ;;
    MIN=*)      MIN_R="${LINE#MIN=}" ;;
    MAX=*)      MAX_R="${LINE#MAX=}" ;;
    IDLE_PERIOD=*) IDLE_P="${LINE#IDLE_PERIOD=}" ;;
    WARM_PERIOD=*) WARM_P="${LINE#WARM_PERIOD=}" ;;
    ---)
      BASE_URL="https://${REGION}-aiplatform.googleapis.com"

      echo ""
      echo "┌──────────────────────────────────────────────────────────"
      echo "│  ${EP_NAME}"
      echo "│  Model: ${MODEL}"
      echo "│  Endpoint: ${EP_ID}  |  Region: ${REGION}"
      echo "└──────────────────────────────────────────────────────────"

      # ── Replica / scale config ──────────────────────────────
      echo ""
      echo "  REPLICA STATE"
      echo "  state              : ${STATE}"
      echo "  min / max replicas : ${MIN_R} / ${MAX_R}"
      echo "  idle_scaledown     : ${IDLE_P}"
      echo "  min_scaleup        : ${WARM_P}"

      REPLICAS=$(get_metric "prediction%2Fonline%2Freplicas" "${EP_ID}")
      echo "  current replicas   : ${REPLICAS}"

      # ── Latency ────────────────────────────────────────────
      echo ""
      echo "  LATENCY (last 1h)"
      P50=$(get_metric    "prediction%2Fonline%2Fpredictions_latency" "${EP_ID}")
      P95=$(get_metric_p95 "prediction%2Fonline%2Fpredictions_latency" "${EP_ID}")
      TTFT=$(get_metric   "prediction%2Fonline%2Ftime_to_first_token"  "${EP_ID}")
      echo "  p50 latency        : ${P50}"
      echo "  p95 latency        : ${P95}"
      echo "  time-to-first-token: ${TTFT}"

      # ── Throughput ─────────────────────────────────────────
      echo ""
      echo "  THROUGHPUT & ERRORS (last 1h)"
      REQS=$(get_metric "prediction%2Fonline%2Frequest_count" "${EP_ID}")
      ERRS=$(get_metric "prediction%2Fonline%2Ferror_count"   "${EP_ID}")
      TOKS=$(get_metric "prediction%2Fonline%2Ftoken_count"   "${EP_ID}")
      echo "  requests (5m avg)  : ${REQS} req/min"
      echo "  errors   (5m avg)  : ${ERRS}"
      echo "  token throughput   : ${TOKS} tok/min"

      # ── GPU ────────────────────────────────────────────────
      echo ""
      echo "  GPU (last 1h)"
      DUTY=$(get_metric "prediction%2Fonline%2Faccelerator_duty_cycle"  "${EP_ID}")
      MEM=$( get_metric "prediction%2Fonline%2Fgpu_memory_utilization"  "${EP_ID}")
      echo "  gpu duty cycle     : ${DUTY}%"
      echo "  gpu memory used    : ${MEM}%"
      # Interpretation
      case "${DUTY}" in
        [0-9]*)
          DUTY_INT="${DUTY%.*}"
          if   [ "${DUTY_INT}" -lt 5  ] 2>/dev/null; then echo "  interpretation     : nearly idle"
          elif [ "${DUTY_INT}" -lt 40 ] 2>/dev/null; then echo "  interpretation     : light load"
          elif [ "${DUTY_INT}" -lt 80 ] 2>/dev/null; then echo "  interpretation     : moderate load (good)"
          else                                              echo "  interpretation     : HIGH — near saturation"
          fi ;;
        *) echo "  interpretation     : no GPU data yet" ;;
      esac

      # ── Idle analysis ───────────────────────────────────────
      echo ""
      echo "  IDLE ANALYSIS (last 30 min)"
      get_idle_analysis "${EP_ID}"

      # ── Live ping ──────────────────────────────────────────
      echo ""
      echo "  LIVE PING"
      PING_TMP=$(mktmp ping)
      PING_START=$(date +%s)
      HTTP_CODE=$(curl --silent \
        --output "${PING_TMP}" \
        --write-out "%{http_code}" \
        --request POST \
        --header "Authorization: Bearer $(gcloud auth print-access-token)" \
        --header "Content-Type: application/json" \
        --data '{"instances":[{"messages":[{"role":"user","content":"hi"}],"max_tokens":1}]}' \
        --max-time 10 \
        "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${EP_ID}:rawPredict" \
        2>/dev/null) || HTTP_CODE="000"
      PING_END=$(date +%s)
      PING_MS=$(( (PING_END - PING_START) * 1000 ))
      rm -f "${PING_TMP}"

      case "${HTTP_CODE}" in
        200) echo "  result             : HTTP 200  ${PING_MS}ms  LIVE" ;;
        429) echo "  result             : HTTP 429  SCALED TO ZERO (scale-up triggered)" ;;
        000) echo "  result             : timeout / network error" ;;
        *)   echo "  result             : HTTP ${HTTP_CODE}  ${PING_MS}ms" ;;
      esac
      ;;
  esac
done

rm -rf "${SCAN_DIR}"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "  Dashboards"
echo "  Endpoints  : https://console.cloud.google.com/vertex-ai/online-prediction/endpoints?project=${PROJECT_ID}"
echo "  Monitoring : https://console.cloud.google.com/monitoring/metrics-explorer?project=${PROJECT_ID}"
echo "  Traces     : https://console.cloud.google.com/traces?project=${PROJECT_ID}"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Key metrics (prefix: aiplatform.googleapis.com/):"
echo "  prediction/online/replicas               — replica count"
echo "  prediction/online/predictions_latency    — e2e latency"
echo "  prediction/online/time_to_first_token    — TTFT"
echo "  prediction/online/accelerator_duty_cycle — GPU %"
echo "  prediction/online/gpu_memory_utilization — VRAM %"
echo "  prediction/online/request_count          — req/min"
echo "  prediction/online/token_count            — tok/min"
