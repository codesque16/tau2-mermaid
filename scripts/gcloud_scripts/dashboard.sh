#!/bin/bash
# =============================================================
# dashboard.sh — Live vLLM Cloud Run Dashboard
#
# TWO data sources combined:
#
#  1. Cloud Monitoring API (real traffic metrics, rolling windows)
#       - request_count        : total real requests (not just pings)
#       - request_latencies    : p50/p95 from actual workload
#       - instance_count       : how many instances running
#       - container/billable_instance_time
#
#  2. Live probe (our own tiny request every refresh cycle)
#       - current HTTP status (LIVE / COLD / ERROR)
#       - probe latency + tokens/s
#       - history sparkline
#
# Rolling windows: 5min, 1hr, 24hr
#
# bash 3.2 + zsh compatible (macOS). No declare -A, no local outside
# functions, no process substitution.
#
# Usage:
#   bash dashboard.sh
#   bash dashboard.sh --refresh=15 --window=60
#   bash dashboard.sh --project=gemini-1xn --region=us-central1
# =============================================================

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="${REGION:-us-central1}"
REFRESH="${REFRESH:-30}"      # probe interval seconds
WINDOW="${WINDOW:-60}"        # Cloud Monitoring lookback minutes

for arg in "$@"; do
  case "${arg}" in
    --project=*)  PROJECT_ID="${arg#--project=}" ;;
    --region=*)   REGION="${arg#--region=}" ;;
    --refresh=*)  REFRESH="${arg#--refresh=}" ;;
    --window=*)   WINDOW="${arg#--window=}" ;;
  esac
done

# ── ANSI ──────────────────────────────────────────────────────
R="\033[0m"; B="\033[1m"; DIM="\033[2m"
GRN="\033[32m"; YLW="\033[33m"; RED="\033[31m"; CYN="\033[36m"
BGRN="\033[92m"; BYLW="\033[93m"; BRED="\033[91m"; BCYN="\033[96m"; BWHT="\033[97m"
BG1="\033[100m"; BGGN="\033[42m"; BGRD="\033[41m"; BGYW="\033[43m"

STATE_DIR=$(mktemp -d /tmp/vllm_dash_XXXXXX)

cleanup() {
  rm -rf "${STATE_DIR}"
  tput rmcup 2>/dev/null || true
  printf "\033[?25h\033[0m\n"
  echo "Dashboard stopped."
  exit 0
}
trap cleanup INT TERM

mktmp() { mktemp "/tmp/dash_${1}_XXXXXX"; }

# ── Cloud Monitoring: fetch one metric scalar ─────────────────
# Returns latest value for a Cloud Run metric over last $WINDOW min
fetch_metric() {
  local SERVICE_NAME="$1"   # Cloud Run service name
  local METRIC="$2"          # e.g. run.googleapis.com/request_count
  local ALIGNER="${3:-ALIGN_SUM}"
  local REDUCER="${4:-REDUCE_SUM}"

  local NOW_EPOCH
  NOW_EPOCH=$(python3 -c "import time; print(int(time.time()))")
  local START_EPOCH=$(( NOW_EPOCH - WINDOW * 60 ))
  local START_TS
  START_TS=$(python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp(${START_EPOCH}).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  local END_TS
  END_TS=$(python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp(${NOW_EPOCH}).strftime('%Y-%m-%dT%H:%M:%SZ'))")

  local TOKEN
  TOKEN=$(gcloud auth print-access-token 2>/dev/null || echo "")

  local FILTER
  FILTER="metric.type%3D%22${METRIC}%22+AND+resource.labels.service_name%3D%22${SERVICE_NAME}%22+AND+resource.labels.location%3D%22${REGION}%22"
  local ALIGN_PERIOD=$(( WINDOW * 60 ))

  local RESP_TMP
  RESP_TMP=$(mktmp metric)

  curl --silent --output "${RESP_TMP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?filter=${FILTER}&interval.startTime=${START_TS}&interval.endTime=${END_TS}&aggregation.alignmentPeriod=${ALIGN_PERIOD}s&aggregation.perSeriesAligner=${ALIGNER}&aggregation.crossSeriesReducer=${REDUCER}" \
    2>/dev/null || true

  python3 -c "
import json, sys
try:
    d = json.load(open('${RESP_TMP}'))
    ts = d.get('timeSeries', [])
    if not ts:
        print('0')
        sys.exit()
    pts = ts[0].get('points', [])
    if not pts:
        print('0')
        sys.exit()
    v = pts[0].get('value', {})
    val = v.get('int64Value') or v.get('doubleValue') or v.get('distributionValue', {})
    if isinstance(val, dict):
        print('0')
    else:
        print(round(float(val), 1))
except Exception as e:
    print('0')
" 2>/dev/null || echo "0"

  rm -f "${RESP_TMP}"
}

# Fetch p95 latency from distribution metric
fetch_p95() {
  local SERVICE_NAME="$1"
  local METRIC="$2"

  local NOW_EPOCH
  NOW_EPOCH=$(python3 -c "import time; print(int(time.time()))")
  local START_EPOCH=$(( NOW_EPOCH - WINDOW * 60 ))
  local START_TS
  START_TS=$(python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp(${START_EPOCH}).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  local END_TS
  END_TS=$(python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp(${NOW_EPOCH}).strftime('%Y-%m-%dT%H:%M:%SZ'))")

  local TOKEN
  TOKEN=$(gcloud auth print-access-token 2>/dev/null || echo "")
  local FILTER
  FILTER="metric.type%3D%22${METRIC}%22+AND+resource.labels.service_name%3D%22${SERVICE_NAME}%22+AND+resource.labels.location%3D%22${REGION}%22"
  local ALIGN_PERIOD=$(( WINDOW * 60 ))
  local RESP_TMP
  RESP_TMP=$(mktmp p95)

  curl --silent --output "${RESP_TMP}" \
    --header "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/timeSeries?filter=${FILTER}&interval.startTime=${START_TS}&interval.endTime=${END_TS}&aggregation.alignmentPeriod=${ALIGN_PERIOD}s&aggregation.perSeriesAligner=ALIGN_PERCENTILE_95&aggregation.crossSeriesReducer=REDUCE_MEAN" \
    2>/dev/null || true

  python3 -c "
import json, sys
try:
    d = json.load(open('${RESP_TMP}'))
    ts = d.get('timeSeries', [])
    if not ts:
        print('—')
        sys.exit()
    pts = ts[0].get('points', [])
    if not pts:
        print('—')
        sys.exit()
    v = pts[0].get('value', {})
    val = v.get('doubleValue') or v.get('int64Value')
    if val is None:
        print('—')
    else:
        # Cloud Run latency is in ms
        print(str(int(float(val))) + 'ms')
except:
    print('—')
" 2>/dev/null || echo "—"

  rm -f "${RESP_TMP}"
}

# ── Probe service (live ping) ──────────────────────────────────
probe_service() {
  local NAME="$1"
  local URL="$2"
  local SF="${STATE_DIR}/${NAME}"
  local TOKEN RESP HTTP_CODE T_START T_END MS TOKENS CONTENT

  TOKEN=$(gcloud auth print-identity-token 2>/dev/null || echo "")
  RESP=$(mktmp resp)
  T_START=$(python3 -c "import time; print(int(time.time()*1000))")

  HTTP_CODE=$(curl --silent \
    --output "${RESP}" \
    --write-out "%{http_code}" \
    --max-time 15 \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Content-Type: application/json" \
    --data '{"model":"gemma4","messages":[{"role":"user","content":"hi"}],"max_tokens":5,"temperature":0}' \
    "${URL}/v1/chat/completions" 2>/dev/null) || HTTP_CODE="000"

  T_END=$(python3 -c "import time; print(int(time.time()*1000))")
  MS=$(( T_END - T_START ))

  TOKENS=$(python3 -c "
import json
try:
    d=json.load(open('${RESP}'))
    print(d.get('usage',{}).get('completion_tokens',0))
except: print(0)
" 2>/dev/null || echo 0)

  CONTENT=$(python3 -c "
import json
try:
    d=json.load(open('${RESP}'))
    c=d.get('choices',[{}])[0].get('message',{}).get('content','')
    print(c.strip()[:35].replace(chr(10),' '))
except:
    try:
        d=json.load(open('${RESP}'))
        print(str(d.get('error',{}).get('message',''))[:35])
    except: print('')
" 2>/dev/null || echo "")

  rm -f "${RESP}"

  # Rolling probe history (last 15)
  PREV_HIST=""
  [ -f "${SF}" ] && PREV_HIST=$(grep "^HIST=" "${SF}" 2>/dev/null | cut -d= -f2-)
  NEW_HIST=$(printf "%s %s" "${PREV_HIST}" "${HTTP_CODE}" | tr ' ' '\n' | grep -v '^$' | tail -15 | tr '\n' ' ')
  TOTAL=$(echo "${NEW_HIST}" | tr ' ' '\n' | grep -v '^$' | wc -l | tr -d ' ')
  OK_CNT=$(echo "${NEW_HIST}" | tr ' ' '\n' | grep -c '^200$' 2>/dev/null || echo 0)

  TOKS_S="0"
  [ "${MS}" -gt 0 ] && [ "${TOKENS}" -gt 0 ] && \
    TOKS_S=$(python3 -c "print(round(${TOKENS}/(${MS}/1000.0),1))" 2>/dev/null || echo "0")

  # Fetch Cloud Monitoring metrics in parallel
  REQ_COUNT=$(fetch_metric "${NAME}" "run.googleapis.com%2Frequest_count" ALIGN_SUM REDUCE_SUM)
  P95=$(fetch_p95 "${NAME}" "run.googleapis.com%2Frequest_latencies")
  INSTANCES=$(fetch_metric "${NAME}" "run.googleapis.com%2Fcontainer%2Finstance_count" ALIGN_MEAN REDUCE_MEAN)

  # Write state
  TMP_SF=$(mktmp state)
  cat > "${TMP_SF}" << STEOF
CODE=${HTTP_CODE}
MS=${MS}
TOKS_S=${TOKS_S}
TOKENS=${TOKENS}
HIST=${NEW_HIST}
TOTAL=${TOTAL}
OK=${OK_CNT}
CONTENT=${CONTENT}
TS=$(date '+%H:%M:%S')
REQ_COUNT=${REQ_COUNT}
P95=${P95}
INSTANCES=${INSTANCES}
STEOF
  mv "${TMP_SF}" "${SF}"
}

# ── Drawing helpers ────────────────────────────────────────────
draw_bar() {
  local VAL="$1" MAX="$2" W="$3" COLOR="$4"
  local FILLED=0
  [ "${MAX}" -gt 0 ] && FILLED=$(( VAL * W / MAX ))
  [ "${FILLED}" -gt "${W}" ] && FILLED="${W}"
  local EMPTY=$(( W - FILLED ))
  printf "${COLOR}"; printf "%${FILLED}s" | tr ' ' '█'
  printf "${DIM}"; printf "%${EMPTY}s" | tr ' ' '░'
  printf "${R}"
}

draw_badge() {
  local CODE="$1"
  case "${CODE}" in
    200) printf "${BGGN}\033[30m LIVE  ${R}" ;;
    429) printf "${BGYW}\033[30m WARM↑ ${R}" ;;
    500) printf "${BGRD}\033[97m ERROR ${R}" ;;
    000) printf "${BG1}\033[97m T/OUT ${R}" ;;
    -)   printf "${BG1}\033[97m  ...  ${R}" ;;
    *)   printf "${BG1}\033[97m ${CODE}  ${R}" ;;
  esac
}

draw_spark() {
  local HIST="$1"
  for C in ${HIST}; do
    case "${C}" in
      200) printf "${GRN}▪${R}" ;;
      429) printf "${YLW}▪${R}" ;;
      500) printf "${RED}▪${R}" ;;
      000) printf "${DIM}·${R}" ;;
      *)   printf "${DIM}?${R}" ;;
    esac
  done
}

# ── Render ────────────────────────────────────────────────────
render() {
  local NAMES_STR="$1"
  local COLS NOW
  COLS=$(tput cols 2>/dev/null || echo 90)
  NOW=$(date '+%H:%M:%S')

  printf "\033[H"   # top-left, no clear (reduces flicker)

  # Header
  printf "${BG1}${B}${BWHT} ◈  vLLM Cloud Run Dashboard"
  local RPAD=$(( COLS - 30 - ${#PROJECT_ID} - ${#REGION} - ${#NOW} - 4 ))
  [ "${RPAD}" -lt 1 ] && RPAD=1
  printf "%${RPAD}s${DIM}${PROJECT_ID} │ ${REGION} │ ${NOW} ${R}\n"
  printf "${DIM}%${COLS}s${R}\n" | tr ' ' '─'
  printf "  ${DIM}Metrics window: last ${WINDOW} min  │  Probe interval: ${REFRESH}s  │  Source: Cloud Monitoring + live probe${R}\n"
  printf "${DIM}%${COLS}s${R}\n" | tr ' ' '─'

  for NAME in ${NAMES_STR}; do
    local SF="${STATE_DIR}/${NAME}"
    local CODE="-" MS="0" TOKS_S="0" HIST="" TOTAL="0" OK="0"
    local CONTENT="" TS="-" REQ_COUNT="0" P95="—" INSTANCES="0"

    if [ -f "${SF}" ]; then
      CODE=$(grep "^CODE=" "${SF}" | cut -d= -f2-)
      MS=$(grep "^MS=" "${SF}" | cut -d= -f2-)
      TOKS_S=$(grep "^TOKS_S=" "${SF}" | cut -d= -f2-)
      HIST=$(grep "^HIST=" "${SF}" | cut -d= -f2-)
      TOTAL=$(grep "^TOTAL=" "${SF}" | cut -d= -f2-)
      OK=$(grep "^OK=" "${SF}" | cut -d= -f2-)
      CONTENT=$(grep "^CONTENT=" "${SF}" | cut -d= -f2-)
      TS=$(grep "^TS=" "${SF}" | cut -d= -f2-)
      REQ_COUNT=$(grep "^REQ_COUNT=" "${SF}" | cut -d= -f2-)
      P95=$(grep "^P95=" "${SF}" | cut -d= -f2-)
      INSTANCES=$(grep "^INSTANCES=" "${SF}" | cut -d= -f2-)
    fi

    # ── Service header ───────────────────────────────────────
    printf "\n  ${B}${BCYN}%-30s${R}  " "${NAME}"
    draw_badge "${CODE}"
    local INST_COLOR="${GRN}"
    [ "${INSTANCES}" = "0" ] && INST_COLOR="${DIM}"
    printf "  ${DIM}instances:${R}${INST_COLOR}${B}%s${R}\n" "${INSTANCES:-0}"
    printf "  ${DIM}%${COLS}s${R}\n" | tr ' ' '·'

    # ── Cloud Monitoring metrics ─────────────────────────────
    printf "  ${B}${DIM}── Cloud Monitoring (last ${WINDOW} min) ──────────────────────${R}\n"

    # Request count
    local RC_COLOR="${BGRN}"
    [ "${REQ_COUNT}" = "0" ] && RC_COLOR="${DIM}"
    printf "  ${DIM}Requests  ${R}  ${RC_COLOR}${B}%8s${R}${DIM} total req${R}  " "${REQ_COUNT}"
    draw_bar "$(python3 -c "print(int(float('0'.replace(chr(39),'0')))  )" 2>/dev/null || echo 0)" 1000 18 "${BGRN}"
    printf "\n"

    # p95 latency from real traffic
    printf "  ${DIM}p95 lat   ${R}  ${BYLW}${B}%8s${R}${DIM} (real traffic p95)${R}\n" "${P95}"

    # ── Live probe metrics ───────────────────────────────────
    printf "  ${B}${DIM}── Live probe ─────────────────────────────────────────────${R}\n"

    local MS_INT="$(echo "${MS:-0}" | cut -d. -f1)"
    local MS_COLOR="${GRN}"
    [ "${MS_INT}" -gt 3000 ] && MS_COLOR="${YLW}"
    [ "${MS_INT}" -gt 8000 ] && MS_COLOR="${RED}"
    printf "  ${DIM}Latency   ${R}  ${MS_COLOR}${B}%6sms${R}  " "${MS_INT}"
    draw_bar "${MS_INT}" 12000 18 "${MS_COLOR}"
    printf "\n"

    printf "  ${DIM}Tokens/s  ${R}  "
    if [ "${TOKS_S}" != "0" ] && [ -n "${TOKS_S}" ]; then
      printf "${BGRN}${B}%6s${R}${DIM} tok/s (probe only)${R}"
    else
      printf "${DIM}       — ${R}"
    fi
    printf "\n"

    # Probe success rate
    if [ "${TOTAL:-0}" -gt 0 ]; then
      local OK_INT="$(echo "${OK:-0}" | cut -d. -f1)"
      local TOTAL_INT="$(echo "${TOTAL:-1}" | cut -d. -f1)"
      local PCT=$(( OK_INT * 100 / (TOTAL_INT > 0 ? TOTAL_INT : 1) ))
      local PCT_COLOR="${GRN}"
      [ "${PCT}" -lt 90 ] && PCT_COLOR="${YLW}"
      [ "${PCT}" -lt 60 ] && PCT_COLOR="${RED}"
      printf "  ${DIM}Probe OK  ${R}  ${PCT_COLOR}${B}%5s%%${R}  ${DIM}(%s/%s probes)${R}\n" \
        "${PCT}" "${OK_INT}" "${TOTAL_INT}"
    fi

    # Sparkline
    if [ -n "${HIST}" ]; then
      printf "  ${DIM}History   ${R}  "
      draw_spark "${HIST}"
      printf "  ${DIM}▪=200  ▪=429  ▪=5xx  ·=timeout${R}\n"
    fi

    # Last response
    [ -n "${CONTENT}" ] && \
      printf "  ${DIM}Response  ${R}  ${DIM}\"%.40s\"${R}\n" "${CONTENT}"

    printf "  ${DIM}Probed at ${R}  ${DIM}%s${R}\n" "${TS}"
  done

  printf "\n${DIM}%${COLS}s${R}\n" | tr ' ' '─'
  printf " ${DIM}Ctrl+C exit  ·  window=--window=N  ·  refresh=--refresh=N${R}"
  printf "\033[J"
}

# ── Discover services ─────────────────────────────────────────
discover() {
  gcloud run services list \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(name)" 2>/dev/null
}

get_url() {
  gcloud run services describe "$1" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(status.url)" 2>/dev/null
}

# ── Start ─────────────────────────────────────────────────────
printf "\033[?25h"
echo "Discovering services in ${PROJECT_ID} / ${REGION}..."
NAMES_STR=$(discover)

if [ -z "${NAMES_STR}" ]; then
  echo "No services found."
  exit 1
fi

echo "Found: $(echo ${NAMES_STR} | tr '\n' ' ')"
echo "Fetching URLs..."
for N in ${NAMES_STR}; do
  URL=$(get_url "${N}")
  echo "${URL}" > "${STATE_DIR}/${N}.url"
done

echo "Starting dashboard... (first probe takes ~15s)"
sleep 1

tput smcup 2>/dev/null || true
printf "\033[?25l\033[2J"

ROUND=0
while true; do
  ROUND=$((ROUND + 1))

  # Probe all services in parallel (probe + Cloud Monitoring fetch)
  for N in ${NAMES_STR}; do
    URL=""
    [ -f "${STATE_DIR}/${N}.url" ] && URL=$(cat "${STATE_DIR}/${N}.url")
    [ -n "${URL}" ] && probe_service "${N}" "${URL}" &
  done
  wait

  render "${NAMES_STR}"

  # Countdown ticker
  ROWS=$(tput lines 2>/dev/null || echo 40)
  for s in $(seq "${REFRESH}" -1 1); do
    printf "\033[${ROWS};1H\033[2K ${DIM}Next refresh in ${s}s  ·  Round ${ROUND}${R}"
    sleep 1
  done

  # Re-discover (picks up new deployments)
  NAMES_STR=$(discover)
  for N in ${NAMES_STR}; do
    if [ ! -f "${STATE_DIR}/${N}.url" ]; then
      URL=$(get_url "${N}")
      echo "${URL}" > "${STATE_DIR}/${N}.url"
    fi
  done
done
