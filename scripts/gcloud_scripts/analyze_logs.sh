#!/bin/bash
# =============================================================
# analyze_logs.sh
# Reads Cloud Run logs + vLLM /metrics for a service and
# produces a full performance report with recommendations.
#
# Calculates:
#   - Real request volume + error rates
#   - p50/p95/p99 latency from actual traffic
#   - GPU KV cache utilisation (from vLLM /metrics)
#   - Queue depth (waiting vs running requests)
#   - Token throughput (prompt + generation)
#   - Cold start frequency + duration
#   - Timeout rate
#   - Recommended MAX_NUM_SEQS, MAX_MODEL_LEN, MAX_INSTANCES
#   - Whether to upgrade GPU or switch quantization
#
# Usage:
#   bash analyze_logs.sh                          # picks first service
#   bash analyze_logs.sh --service=gemma4-26b-bf16
#   bash analyze_logs.sh --service=gemma4-31b-fp8 --window=60
#   bash analyze_logs.sh --all                   # analyze all services
# =============================================================

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="${REGION:-us-central1}"
WINDOW="${WINDOW:-60}"        # lookback minutes
SERVICE_NAME="${SERVICE:-}"
ANALYZE_ALL=false

for arg in "$@"; do
  case "${arg}" in
    --project=*)  PROJECT_ID="${arg#--project=}" ;;
    --region=*)   REGION="${arg#--region=}" ;;
    --window=*)   WINDOW="${arg#--window=}" ;;
    --service=*)  SERVICE_NAME="${arg#--service=}" ;;
    --all)        ANALYZE_ALL=true ;;
  esac
done

# ── Colours ───────────────────────────────────────────────────
G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"
B="\033[1m"; DIM="\033[2m"; N="\033[0m"
BGRN="\033[92m"; BYLW="\033[93m"; BRED="\033[91m"; BCYN="\033[96m"

ok()   { echo -e "${G}  ✓ $*${N}"; }
warn() { echo -e "${Y}  ⚠ $*${N}"; }
err()  { echo -e "${R}  ✗ $*${N}"; }
info() { echo -e "${C}  → $*${N}"; }
rec()  { echo -e "${BGRN}  ► $*${N}"; }
head() { echo ""; echo -e "${B}▶ $*${N}"; }
sep()  { echo -e "${DIM}$(printf '%0.s─' $(seq 1 72))${N}"; }

mktmp() { mktemp "/tmp/analyze_${1}_XXXXXX"; }

# ── Time helpers ──────────────────────────────────────────────
now_epoch()  { python3 -c "import time; print(int(time.time()))"; }
epoch_to_ts() {
  python3 -c "
import datetime
print(datetime.datetime.utcfromtimestamp($1).strftime('%Y-%m-%dT%H:%M:%SZ'))
"
}

window_start_ts() {
  local NOW
  NOW=$(now_epoch)
  epoch_to_ts $(( NOW - WINDOW * 60 ))
}

window_end_ts() {
  epoch_to_ts "$(now_epoch)"
}

# ── Cloud Monitoring fetch ────────────────────────────────────
fetch_monitoring() {
  local SVC="$1" METRIC="$2" ALIGNER="$3" REDUCER="$4" PERIOD="$5"
  local TOKEN START END
  TOKEN=$(gcloud auth print-access-token 2>/dev/null)
  START=$(window_start_ts)
  END=$(window_end_ts)
  PERIOD="${PERIOD:-$(( WINDOW * 60 ))}"

  local RESP
  RESP=$(mktmp mon)

  curl --silent --output "${RESP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22${METRIC}%22\
+AND+resource.labels.service_name%3D%22${SVC}%22\
+AND+resource.labels.location%3D%22${REGION}%22\
&interval.startTime=${START}\
&interval.endTime=${END}\
&aggregation.alignmentPeriod=${PERIOD}s\
&aggregation.perSeriesAligner=${ALIGNER}\
&aggregation.crossSeriesReducer=${REDUCER}" 2>/dev/null || true

  python3 -c "
import json, sys
try:
    d = json.load(open('${RESP}'))
    ts = d.get('timeSeries', [])
    if not ts:
        print('0')
        sys.exit()
    # Sum all points across all time series
    total = 0
    for series in ts:
        for pt in series.get('points', []):
            v = pt.get('value', {})
            val = float(v.get('int64Value', 0) or v.get('doubleValue', 0))
            total += val
    print(round(total, 2))
except Exception as e:
    print('0')
" 2>/dev/null || echo "0"

  rm -f "${RESP}"
}

fetch_percentile() {
  local SVC="$1" METRIC="$2" PCT="$3"
  local TOKEN START END
  TOKEN=$(gcloud auth print-access-token 2>/dev/null)
  START=$(window_start_ts)
  END=$(window_end_ts)
  local PERIOD=$(( WINDOW * 60 ))

  local RESP
  RESP=$(mktmp pct)

  curl --silent --output "${RESP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?\
filter=metric.type%3D%22${METRIC}%22\
+AND+resource.labels.service_name%3D%22${SVC}%22\
+AND+resource.labels.location%3D%22${REGION}%22\
&interval.startTime=${START}\
&interval.endTime=${END}\
&aggregation.alignmentPeriod=${PERIOD}s\
&aggregation.perSeriesAligner=ALIGN_PERCENTILE_${PCT}\
&aggregation.crossSeriesReducer=REDUCE_MEAN" 2>/dev/null || true

  python3 -c "
import json, sys
try:
    d = json.load(open('${RESP}'))
    ts = d.get('timeSeries', [])
    if not ts:
        print('-1')
        sys.exit()
    pts = ts[0].get('points', [])
    if not pts:
        print('-1')
        sys.exit()
    v = pts[0]['value']
    val = float(v.get('doubleValue', 0) or v.get('int64Value', 0))
    print(round(val, 1))
except:
    print('-1')
" 2>/dev/null || echo "-1"

  rm -f "${RESP}"
}

# ── Cloud Logging fetch ───────────────────────────────────────
fetch_logs() {
  local SVC="$1"
  local RESP
  RESP=$(mktmp logs)

  gcloud logging read \
    "resource.type=\"cloud_run_revision\" \
     AND resource.labels.service_name=\"${SVC}\" \
     AND timestamp>=\"$(window_start_ts)\"" \
    --project="${PROJECT_ID}" \
    --limit=1000 \
    --format="value(timestamp,textPayload,jsonPayload.message)" \
    2>/dev/null > "${RESP}" || true

  echo "${RESP}"
}

# ── vLLM /metrics fetch ───────────────────────────────────────
fetch_vllm_metrics() {
  local URL="$1"
  local TOKEN
  TOKEN=$(gcloud auth print-identity-token 2>/dev/null)
  local RESP
  RESP=$(mktmp vllm)

  curl --silent --output "${RESP}" \
    --max-time 15 \
    --header "Authorization: Bearer ${TOKEN}" \
    "${URL}/metrics" 2>/dev/null || true

  echo "${RESP}"
}

get_vllm_val() {
  local FILE="$1" METRIC="$2"
  grep "^${METRIC}" "${FILE}" 2>/dev/null | \
    grep -v "^#" | awk '{print $2}' | head -1 || echo "N/A"
}

# ── Get service URL ───────────────────────────────────────────
get_url() {
  gcloud run services describe "$1" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(status.url)" 2>/dev/null || echo ""
}

get_service_config() {
  local SVC="$1"
  gcloud run services describe "${SVC}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(\
spec.template.spec.containers[0].resources.limits.cpu,\
spec.template.spec.containers[0].resources.limits.memory,\
spec.template.spec.containerConcurrency,\
spec.template.metadata.annotations[autoscaling.knative.dev/maxScale],\
spec.template.metadata.annotations[autoscaling.knative.dev/minScale]\
)" 2>/dev/null | tr '\t' '|' || echo "||||"
}

# ── Parse log file for patterns ───────────────────────────────
parse_logs() {
  local LOG_FILE="$1"

  # Cold start events
  COLD_STARTS=$(grep -c "Instance started\|Starting new instance\|DEPLOYMENT_ROLLOUT" \
    "${LOG_FILE}" 2>/dev/null || echo 0)

  # Timeout events
  TIMEOUTS=$(grep -ci "timeout\|deadline exceeded\|connection timed out" \
    "${LOG_FILE}" 2>/dev/null || echo 0)

  # OOM events
  OOMS=$(grep -ci "out of memory\|CUDA out of memory\|OOM\|killed" \
    "${LOG_FILE}" 2>/dev/null || echo 0)

  # vLLM cache full warnings
  CACHE_FULL=$(grep -ci "cache is full\|KV cache\|preempt\|abort" \
    "${LOG_FILE}" 2>/dev/null || echo 0)

  # Error lines
  ERRORS=$(grep -c "ERROR\|Exception\|Traceback" \
    "${LOG_FILE}" 2>/dev/null || echo 0)

  # Extract avg prompt/generation lengths from vLLM logs
  AVG_PROMPT=$(grep -o "prompt_tokens=[0-9]*" "${LOG_FILE}" 2>/dev/null | \
    awk -F= '{sum+=$2; n++} END {if(n>0) print int(sum/n); else print "N/A"}')

  AVG_GEN=$(grep -o "output_tokens=[0-9]*" "${LOG_FILE}" 2>/dev/null | \
    awk -F= '{sum+=$2; n++} END {if(n>0) print int(sum/n); else print "N/A"}')

  echo "COLD_STARTS=${COLD_STARTS}"
  echo "TIMEOUTS=${TIMEOUTS}"
  echo "OOMS=${OOMS}"
  echo "CACHE_FULL=${CACHE_FULL}"
  echo "ERRORS=${ERRORS}"
  echo "AVG_PROMPT=${AVG_PROMPT:-N/A}"
  echo "AVG_GEN=${AVG_GEN:-N/A}"
}

# ── Score and recommend ───────────────────────────────────────
make_recommendations() {
  local SVC="$1"
  local REQ_COUNT="$2"
  local ERR_RATE="$3"       # percent
  local P50="$4"
  local P95="$5"
  local P99="$6"
  local GPU_CACHE="$7"      # 0-1 float
  local REQS_RUNNING="$8"
  local REQS_WAITING="$9"
  local COLD_STARTS="${10}"
  local TIMEOUTS="${11}"
  local OOMS="${12}"
  local CACHE_FULL="${13}"
  local CURRENT_SEQS="${14}"
  local CURRENT_CTX="${15}"
  local CURRENT_INST="${16}"

  echo ""
  echo -e "${B}  RECOMMENDATIONS${N}"
  sep

  local ISSUES=0

  # ── GPU Cache ───────────────────────────────────────────────
  local CACHE_PCT
  CACHE_PCT=$(python3 -c "print(round(float('${GPU_CACHE:-0}')*100,1))" 2>/dev/null || echo "0")
  if python3 -c "exit(0 if float('${GPU_CACHE:-0}') > 0.85 else 1)" 2>/dev/null; then
    err "GPU KV cache at ${CACHE_PCT}% — near saturation"
    rec "Switch to fp8kv quantization (halves KV cache memory)"
    rec "Or reduce MAX_MODEL_LEN to free KV cache headroom"
    rec "Or reduce MAX_NUM_SEQS to limit simultaneous sequences"
    ISSUES=$((ISSUES+1))
  elif python3 -c "exit(0 if float('${GPU_CACHE:-0}') > 0.70 else 1)" 2>/dev/null; then
    warn "GPU KV cache at ${CACHE_PCT}% — getting high under load"
    rec "Consider reducing MAX_MODEL_LEN if contexts are shorter than max"
  else
    ok "GPU KV cache at ${CACHE_PCT}% — healthy"
  fi

  # ── Request queue ────────────────────────────────────────────
  local WAIT_INT
  WAIT_INT=$(echo "${REQS_WAITING:-0}" | cut -d. -f1)
  if [ "${WAIT_INT:-0}" -gt 5 ] 2>/dev/null; then
    err "Request queue depth: ${REQS_WAITING} waiting — vLLM is backlogged"
    rec "Increase MAX_NUM_SEQS if GPU cache allows"
    rec "Or increase MAX_INSTANCES for horizontal scaling"
    ISSUES=$((ISSUES+1))
  elif [ "${WAIT_INT:-0}" -gt 0 ] 2>/dev/null; then
    warn "Some requests queuing (${REQS_WAITING} waiting) — watch under sustained load"
  else
    ok "No request queuing — vLLM keeping up"
  fi

  # ── Timeouts ─────────────────────────────────────────────────
  local TO_INT
  TO_INT=$(echo "${TIMEOUTS:-0}" | cut -d. -f1)
  if [ "${TO_INT:-0}" -gt 10 ] 2>/dev/null; then
    err "${TIMEOUTS} timeout events in last ${WINDOW} min"
    rec "Root cause is likely KV cache exhaustion or insufficient instances"
    rec "Quick fix: reduce CONCURRENCY in config to match actual GPU capacity"
    rec "Better fix: switch to fp8kv to free KV cache memory"
    ISSUES=$((ISSUES+1))
  elif [ "${TO_INT:-0}" -gt 0 ] 2>/dev/null; then
    warn "${TIMEOUTS} timeout events — monitor closely"
  else
    ok "No timeouts detected"
  fi

  # ── OOM ───────────────────────────────────────────────────────
  local OOM_INT
  OOM_INT=$(echo "${OOMS:-0}" | cut -d. -f1)
  if [ "${OOM_INT:-0}" -gt 0 ] 2>/dev/null; then
    err "OOM events detected (${OOMS}) — GPU memory exhausted"
    rec "Immediately reduce MAX_NUM_SEQS by 50%"
    rec "Switch quantization: bf16→fp8, or fp8→fp8kv"
    rec "Reduce MAX_MODEL_LEN if your actual prompts are shorter"
    ISSUES=$((ISSUES+1))
  else
    ok "No OOM events"
  fi

  # ── Cold starts ───────────────────────────────────────────────
  local CS_INT
  CS_INT=$(echo "${COLD_STARTS:-0}" | cut -d. -f1)
  if [ "${CS_INT:-0}" -gt 5 ] 2>/dev/null; then
    warn "${COLD_STARTS} cold starts in last ${WINDOW} min — users experiencing 3-5 min waits"
    rec "Set MIN_INSTANCES=1 to keep always warm (costs ~\$3.50/hr for RTX Pro 6000)"
    rec "Or run warmup_ping.sh during peak hours to prevent scale-to-zero"
  elif [ "${CS_INT:-0}" -gt 0 ] 2>/dev/null; then
    info "${COLD_STARTS} cold start(s) — normal if traffic is sporadic"
  else
    ok "No cold starts in window"
  fi

  # ── Latency ───────────────────────────────────────────────────
  local P95_INT
  P95_INT=$(echo "${P95:-0}" | cut -d. -f1)
  if [ "${P95_INT:-0}" -gt 30000 ] 2>/dev/null; then
    err "p95 latency ${P95}ms — very high, likely queuing or cold starts"
    rec "Increase MAX_INSTANCES to distribute load"
    rec "Enable chunked prefill: add --enable-chunked-prefill to EXTRA_VLLM_FLAGS"
    ISSUES=$((ISSUES+1))
  elif [ "${P95_INT:-0}" -gt 10000 ] 2>/dev/null; then
    warn "p95 latency ${P95}ms — elevated, monitor under sustained load"
    rec "Enable prefix caching if prompts share common prefixes"
  elif [ "${P95_INT:-0}" -gt 0 ] 2>/dev/null; then
    ok "p95 latency ${P95}ms — acceptable"
  fi

  # ── Error rate ────────────────────────────────────────────────
  local ER_INT
  ER_INT=$(echo "${ERR_RATE:-0}" | cut -d. -f1)
  if [ "${ER_INT:-0}" -gt 5 ] 2>/dev/null; then
    err "Error rate ${ERR_RATE}% — high"
    rec "Check logs for specific error patterns"
    rec "bash analyze_logs.sh --service=${SVC} for detailed log analysis"
    ISSUES=$((ISSUES+1))
  elif [ "${ER_INT:-0}" -gt 1 ] 2>/dev/null; then
    warn "Error rate ${ERR_RATE}% — slightly elevated"
  else
    ok "Error rate ${ERR_RATE}% — healthy"
  fi

  # ── Overall verdict ───────────────────────────────────────────
  echo ""
  sep
  if [ "${ISSUES}" = "0" ]; then
    echo -e "${BGRN}${B}  ✓ Service is healthy — no action required${N}"
  else
    echo -e "${BYLW}${B}  ⚠ ${ISSUES} issue(s) found — see recommendations above${N}"
  fi

  # ── Suggested config.env values ──────────────────────────────
  echo ""
  echo -e "${B}  SUGGESTED CONFIG VALUES (based on analysis)${N}"
  sep

  # Recommend MAX_NUM_SEQS
  local REC_SEQS="${CURRENT_SEQS:-8}"
  if [ "${OOM_INT:-0}" -gt 0 ] 2>/dev/null; then
    REC_SEQS=$(python3 -c "print(max(1, int(${CURRENT_SEQS:-8} * 0.5)))" 2>/dev/null || echo 4)
    echo -e "  ${BYLW}MAX_NUM_SEQS=${REC_SEQS}${N}  ${DIM}(reduced 50% due to OOM events)${N}"
  elif python3 -c "exit(0 if float('${GPU_CACHE:-0}') > 0.85 else 1)" 2>/dev/null; then
    REC_SEQS=$(python3 -c "print(max(1, int(${CURRENT_SEQS:-8} * 0.75)))" 2>/dev/null || echo 6)
    echo -e "  ${BYLW}MAX_NUM_SEQS=${REC_SEQS}${N}  ${DIM}(reduced 25% due to high KV cache usage)${N}"
  elif [ "${WAIT_INT:-0}" -gt 5 ] 2>/dev/null; then
    REC_SEQS=$(python3 -c "print(int(${CURRENT_SEQS:-8} * 1.5))" 2>/dev/null || echo 12)
    echo -e "  ${BGRN}MAX_NUM_SEQS=${REC_SEQS}${N}  ${DIM}(increased due to request queuing — verify GPU cache first)${N}"
  else
    echo -e "  ${GRN}MAX_NUM_SEQS=${REC_SEQS}${N}  ${DIM}(current value looks good)${N}"
  fi

  # Recommend MAX_INSTANCES
  local REC_INST="${CURRENT_INST:-1}"
  if [ "${TO_INT:-0}" -gt 10 ] || [ "${WAIT_INT:-0}" -gt 10 ] 2>/dev/null; then
    REC_INST=$(python3 -c "print(int(${CURRENT_INST:-1}) + 1)" 2>/dev/null || echo 2)
    echo -e "  ${BYLW}MAX_INSTANCES=${REC_INST}${N}  ${DIM}(scale out — current instance saturated)${N}"
  else
    echo -e "  ${GRN}MAX_INSTANCES=${REC_INST}${N}  ${DIM}(current value sufficient)${N}"
  fi

  # Recommend quantization
  if python3 -c "exit(0 if float('${GPU_CACHE:-0}') > 0.80 else 1)" 2>/dev/null || \
     [ "${OOM_INT:-0}" -gt 0 ] 2>/dev/null || [ "${TO_INT:-0}" -gt 10 ] 2>/dev/null; then
    echo -e "  ${BYLW}QUANTIZATION=fp8kv${N}  ${DIM}(switch to FP8 KV cache to free ~50% KV memory)${N}"
  fi

  # Min instances
  if [ "${CS_INT:-0}" -gt 5 ] 2>/dev/null; then
    echo -e "  ${BYLW}MIN_INSTANCES=1${N}   ${DIM}(prevent cold starts — add to deploy command)${N}"
  fi
}

# ── Analyze one service ───────────────────────────────────────
analyze_service() {
  local SVC="$1"

  echo ""
  echo -e "${B}╔══════════════════════════════════════════════════════════════════════╗${N}"
  echo -e "${B}  Analyzing: ${SVC}${N}"
  echo -e "${B}  Window: last ${WINDOW} minutes | Project: ${PROJECT_ID}${N}"
  echo -e "${B}╚══════════════════════════════════════════════════════════════════════╝${N}"

  # Get service URL and config
  head "Fetching service metadata"
  local URL
  URL=$(get_url "${SVC}")
  if [ -z "${URL}" ]; then
    err "Service not found: ${SVC}"
    return 1
  fi
  info "URL: ${URL}"

  local CONFIG
  CONFIG=$(get_service_config "${SVC}")
  local CPU MEM CONCURRENCY MAX_INST MIN_INST
  CPU=$(echo "${CONFIG}" | cut -d'|' -f1)
  MEM=$(echo "${CONFIG}" | cut -d'|' -f2)
  CONCURRENCY=$(echo "${CONFIG}" | cut -d'|' -f3)
  MAX_INST=$(echo "${CONFIG}" | cut -d'|' -f4)
  MIN_INST=$(echo "${CONFIG}" | cut -d'|' -f5)
  info "CPU: ${CPU} | Memory: ${MEM} | Concurrency: ${CONCURRENCY} | Max instances: ${MAX_INST:-1} | Min: ${MIN_INST:-0}"

  # ── Cloud Monitoring metrics ──────────────────────────────
  head "Fetching Cloud Monitoring metrics (last ${WINDOW} min)"

  info "Fetching request count..."
  local REQ_TOTAL
  REQ_TOTAL=$(fetch_monitoring "${SVC}" \
    "run.googleapis.com%2Frequest_count" ALIGN_SUM REDUCE_SUM)

  info "Fetching error count..."
  local ERR_TOTAL
  ERR_TOTAL=$(fetch_monitoring "${SVC}" \
    "run.googleapis.com%2Frequest_count" ALIGN_SUM REDUCE_SUM \
    "$(( WINDOW * 60 ))")

  info "Fetching latency percentiles..."
  local P50 P95 P99
  P50=$(fetch_percentile "${SVC}" "run.googleapis.com%2Frequest_latencies" 50)
  P95=$(fetch_percentile "${SVC}" "run.googleapis.com%2Frequest_latencies" 95)
  P99=$(fetch_percentile "${SVC}" "run.googleapis.com%2Frequest_latencies" 99)

  info "Fetching instance count..."
  local INSTANCES
  INSTANCES=$(fetch_monitoring "${SVC}" \
    "run.googleapis.com%2Fcontainer%2Finstance_count" ALIGN_MEAN REDUCE_MEAN)

  # ── vLLM /metrics ────────────────────────────────────────
  head "Fetching vLLM live metrics"
  local VLLM_FILE
  VLLM_FILE=$(fetch_vllm_metrics "${URL}")

  local GPU_CACHE REQS_RUNNING REQS_WAITING
  local PROMPT_TOKS GEN_TOKS GEN_THROUGHPUT PROMPT_THROUGHPUT
  GPU_CACHE=$(get_vllm_val "${VLLM_FILE}" "vllm:gpu_cache_usage_perc")
  REQS_RUNNING=$(get_vllm_val "${VLLM_FILE}" "vllm:num_requests_running")
  REQS_WAITING=$(get_vllm_val "${VLLM_FILE}" "vllm:num_requests_waiting")
  PROMPT_TOKS=$(get_vllm_val "${VLLM_FILE}" "vllm:prompt_tokens_total")
  GEN_TOKS=$(get_vllm_val "${VLLM_FILE}" "vllm:generation_tokens_total")
  GEN_THROUGHPUT=$(get_vllm_val "${VLLM_FILE}" "vllm:avg_generation_throughput_toks_per_s")
  PROMPT_THROUGHPUT=$(get_vllm_val "${VLLM_FILE}" "vllm:avg_prompt_throughput_toks_per_s")

  # Extract MAX_NUM_SEQS from vLLM metrics labels
  local MAX_SEQS
  MAX_SEQS=$(grep "max_num_seqs" "${VLLM_FILE}" 2>/dev/null | \
    grep -o 'max_num_seqs="[0-9]*"' | grep -o '[0-9]*' | head -1 || echo "N/A")
  local MAX_CTX
  MAX_CTX=$(grep "max_model_len" "${VLLM_FILE}" 2>/dev/null | \
    grep -o 'max_model_len="[0-9]*"' | grep -o '[0-9]*' | head -1 || echo "N/A")
  local GPU_NAME
  GPU_NAME=$(grep "gpu_hardware_type" "${VLLM_FILE}" 2>/dev/null | \
    grep -o 'gpu_hardware_type="[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' | head -1 || echo "N/A")

  rm -f "${VLLM_FILE}"

  # ── Cloud Logging ────────────────────────────────────────
  head "Fetching Cloud Run logs (last ${WINDOW} min, up to 1000 lines)"
  local LOG_FILE
  LOG_FILE=$(fetch_logs "${SVC}")
  local LOG_LINES
  LOG_LINES=$(wc -l < "${LOG_FILE}" | tr -d ' ')
  info "Log lines fetched: ${LOG_LINES}"

  local PARSED
  PARSED=$(parse_logs "${LOG_FILE}")
  local COLD_STARTS TIMEOUTS OOMS CACHE_FULL ERRORS AVG_PROMPT AVG_GEN
  COLD_STARTS=$(echo "${PARSED}" | grep "^COLD_STARTS=" | cut -d= -f2)
  TIMEOUTS=$(echo "${PARSED}" | grep "^TIMEOUTS=" | cut -d= -f2)
  OOMS=$(echo "${PARSED}" | grep "^OOMS=" | cut -d= -f2)
  CACHE_FULL=$(echo "${PARSED}" | grep "^CACHE_FULL=" | cut -d= -f2)
  ERRORS=$(echo "${PARSED}" | grep "^ERRORS=" | cut -d= -f2)
  AVG_PROMPT=$(echo "${PARSED}" | grep "^AVG_PROMPT=" | cut -d= -f2)
  AVG_GEN=$(echo "${PARSED}" | grep "^AVG_GEN=" | cut -d= -f2)
  rm -f "${LOG_FILE}"

  # ── Error rate ────────────────────────────────────────────
  local ERR_RATE="0"
  ERR_RATE=$(python3 -c "
total = float('${REQ_TOTAL:-0}')
errors = float('${ERRORS:-0}')
if total > 0:
    print(round(errors/total*100, 1))
else:
    print(0)
" 2>/dev/null || echo "0")

  # ── Print full report ─────────────────────────────────────
  echo ""
  echo -e "${B}╔══════════════════════════════════════════════════════════════════════╗${N}"
  echo -e "${B}  PERFORMANCE REPORT — ${SVC}${N}"
  echo -e "${B}╚══════════════════════════════════════════════════════════════════════╝${N}"

  head "Infrastructure"
  printf "  %-22s %s\n" "GPU type:"       "${GPU_NAME:-nvidia-rtx-pro-6000}"
  printf "  %-22s %s\n" "CPU / Memory:"   "${CPU} / ${MEM}"
  printf "  %-22s %s\n" "Max instances:"  "${MAX_INST:-1}"
  printf "  %-22s %s\n" "Min instances:"  "${MIN_INST:-0}"
  printf "  %-22s %s\n" "Concurrency:"    "${CONCURRENCY}"
  printf "  %-22s %s\n" "MAX_NUM_SEQS:"   "${MAX_SEQS}"
  printf "  %-22s %s\n" "MAX_MODEL_LEN:"  "${MAX_CTX}"

  head "Traffic (last ${WINDOW} min)"
  printf "  %-22s %s\n" "Total requests:"   "${REQ_TOTAL}"
  printf "  %-22s %s\n" "Error rate:"       "${ERR_RATE}%"
  printf "  %-22s %s\n" "Active instances:" "${INSTANCES}"

  head "Latency (Cloud Monitoring — real traffic)"
  P50_STR="$([ "${P50}" = "-1" ] && echo "no data" || echo "${P50}ms")"
  P95_STR="$([ "${P95}" = "-1" ] && echo "no data" || echo "${P95}ms")"
  P99_STR="$([ "${P99}" = "-1" ] && echo "no data" || echo "${P99}ms")"
  printf "  %-22s %s\n" "p50 latency:"  "${P50_STR}"
  printf "  %-22s %s\n" "p95 latency:"  "${P95_STR}"
  printf "  %-22s %s\n" "p99 latency:"  "${P99_STR}"

  head "GPU & vLLM (live snapshot)"
  printf "  %-22s %s\n" "GPU KV cache:"      "${GPU_CACHE} ($(python3 -c "print(str(round(float('${GPU_CACHE:-0}')*100,1))+'%')" 2>/dev/null || echo "N/A"))"
  printf "  %-22s %s\n" "Requests running:"  "${REQS_RUNNING}"
  printf "  %-22s %s\n" "Requests waiting:"  "${REQS_WAITING}"
  printf "  %-22s %s\n" "Prompt throughput:" "${PROMPT_THROUGHPUT} tok/s"
  printf "  %-22s %s\n" "Gen throughput:"    "${GEN_THROUGHPUT} tok/s"
  printf "  %-22s %s\n" "Total prompt toks:" "${PROMPT_TOKS}"
  printf "  %-22s %s\n" "Total gen toks:"    "${GEN_TOKS}"

  head "Log analysis (last ${WINDOW} min)"
  printf "  %-22s %s\n" "Cold starts:"    "${COLD_STARTS}"
  printf "  %-22s %s\n" "Timeouts:"       "${TIMEOUTS}"
  printf "  %-22s %s\n" "OOM events:"     "${OOMS}"
  printf "  %-22s %s\n" "Cache pressure:" "${CACHE_FULL} events"
  printf "  %-22s %s\n" "Error lines:"    "${ERRORS}"
  printf "  %-22s %s\n" "Avg prompt len:" "${AVG_PROMPT} tokens"
  printf "  %-22s %s\n" "Avg gen len:"    "${AVG_GEN} tokens"

  # ── Recommendations ───────────────────────────────────────
  make_recommendations \
    "${SVC}" \
    "${REQ_TOTAL}" \
    "${ERR_RATE}" \
    "${P50}" \
    "${P95}" \
    "${P99}" \
    "${GPU_CACHE}" \
    "${REQS_RUNNING}" \
    "${REQS_WAITING}" \
    "${COLD_STARTS}" \
    "${TIMEOUTS}" \
    "${OOMS}" \
    "${CACHE_FULL}" \
    "${MAX_SEQS}" \
    "${MAX_CTX}" \
    "${MAX_INST:-1}"

  echo ""
  sep
  echo ""
}

# ── Main ──────────────────────────────────────────────────────
echo ""
echo -e "${B}╔══════════════════════════════════════════════════════════════════════╗${N}"
echo -e "${B}  vLLM Cloud Run — Log & Metrics Analyzer                             ${N}"
echo -e "${B}  Project: ${PROJECT_ID} | Region: ${REGION} | Window: ${WINDOW} min  ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════════════════════╝${N}"

if [ "${ANALYZE_ALL}" = "true" ]; then
  SERVICES=$(gcloud run services list \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(name)" 2>/dev/null)
  for SVC in ${SERVICES}; do
    analyze_service "${SVC}"
  done
elif [ -n "${SERVICE_NAME}" ]; then
  analyze_service "${SERVICE_NAME}"
else
  # Auto-pick: show menu
  echo ""
  echo -e "${C}  Available services:${N}"
  IDX=0
  NAMES=""
  while IFS= read -r NAME; do
    [ -z "${NAME}" ] && continue
    IDX=$((IDX+1))
    printf "  ${B}[%2d]${N}  %s\n" "${IDX}" "${NAME}"
    NAMES="${NAMES} ${NAME}"
  done < <(gcloud run services list \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(name)" 2>/dev/null)

  echo ""
  printf "  Select service number (or 'a' for all): "
  read -r INPUT

  if [ "${INPUT}" = "a" ] || [ "${INPUT}" = "A" ]; then
    for N in ${NAMES}; do
      analyze_service "${N}"
    done
  else
    IDX2=0
    for N in ${NAMES}; do
      IDX2=$((IDX2+1))
      [ "${IDX2}" = "${INPUT}" ] && analyze_service "${N}" && break
    done
  fi
fi
