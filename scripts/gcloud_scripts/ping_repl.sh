#!/bin/bash
# =============================================================
# ping_repl.sh — Vertex AI Endpoint Ping REPL
#
# A terminal-based interactive tool that:
#   - Scans all GCP regions for endpoints
#   - Manages named ping schedules (cron-like background jobs)
#   - Shows a live split-pane: left=REPL, right=ping log tail
#   - Persists schedule list across sessions in /tmp/ping_registry
#
# Requires: bash 3.2+, curl, python3, tput, gcloud
# macOS compatible (BSD mktemp, bash 3.2)
#
# Usage:  bash ping_repl.sh
# =============================================================

# ── Terminal capabilities ─────────────────────────────────────
COLS=$(tput cols  2>/dev/null || echo 120)
ROWS=$(tput lines 2>/dev/null || echo 40)
HALF=$(( COLS / 2 - 1 ))

# Colors (safe for most terminals)
BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
LOG_FILE="/tmp/ping_repl_log_${PROJECT_ID}.txt"
REGISTRY_DIR="/tmp/ping_registry_${PROJECT_ID}"
SCAN_CACHE="/tmp/ping_scan_cache_${PROJECT_ID}"
INTERVAL_DEFAULT=240   # 4 min default

mkdir -p "${REGISTRY_DIR}"
touch "${LOG_FILE}"

ALL_REGIONS="africa-south1 northamerica-northeast1 northamerica-northeast2 southamerica-east1 southamerica-west1 us-central1 us-east1 us-east4 us-east5 us-south1 us-west1 us-west2 us-west3 us-west4 us-west8 asia-east1 asia-east2 asia-northeast1 asia-northeast2 asia-northeast3 asia-south1 asia-south2 asia-southeast1 asia-southeast2 australia-southeast1 australia-southeast2 europe-central2 europe-north1 europe-north2 europe-southwest1 europe-west1 europe-west2 europe-west3 europe-west4 europe-west6 europe-west8 europe-west9 europe-west12 europe-west15 me-central1 me-central2 me-west1"

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

# ── Logging ───────────────────────────────────────────────────
log() {
  echo "[$(date '+%H:%M:%S')] $*" >> "${LOG_FILE}"
}

# ── Scan all regions (parallel, cached) ──────────────────────
do_scan() {
  echo -e "${CYAN}  Scanning all regions in parallel...${RESET}"
  local SCAN_DIR
  SCAN_DIR=$(mktemp -d /tmp/ep_scan_XXXXXX)
  local TOKEN
  TOKEN=$(gcloud auth print-access-token 2>/dev/null)

  scan_one() {
    local R="$1" OUT="${SCAN_DIR}/${R}"
    local CODE
    CODE=$(curl --silent --output "${OUT}" --write-out "%{http_code}" \
      --max-time 8 \
      --header "Authorization: Bearer ${TOKEN}" \
      "https://${R}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${R}/endpoints" \
      2>/dev/null) || CODE="000"
    [ "${CODE}" != "200" ] && rm -f "${OUT}" && return
    COUNT=$(python3 -c "
import json
try: print(len(json.load(open('${OUT}')).get('endpoints',[])))
except: print(0)
" 2>/dev/null) || COUNT=0
    [ "${COUNT}" = "0" ] && rm -f "${OUT}"
  }

  for R in $ALL_REGIONS; do scan_one "${R}" & done
  wait

  # Build cache file: one line per endpoint
  # Format: INDEX|EP_ID|REGION|DISPLAY_NAME|DM_ID|MIN|MAX|IDLE
  rm -f "${SCAN_CACHE}"
  local IDX=0
  for R in $ALL_REGIONS; do
    local RFILE="${SCAN_DIR}/${R}"
    [ -f "${RFILE}" ] || continue
    python3 - "${RFILE}" "${R}" << 'PYEOF' >> "${SCAN_CACHE}"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
region = sys.argv[2]
for ep in data.get("endpoints", []):
    ep_id  = ep.get("name","").split("/")[-1]
    name   = ep.get("displayName","unnamed")
    models = ep.get("deployedModels", [])
    dm_id  = models[0].get("id","") if models else ""
    dr     = models[0].get("dedicatedResources",{}) if models else {}
    min_r  = str(dr.get("minReplicaCount","?"))
    max_r  = str(dr.get("maxReplicaCount","?"))
    s2z    = dr.get("scaleToZeroSpec",{})
    idle   = s2z.get("idleScaledownPeriod","not-set")
    # pipe-safe: replace any | in names
    name  = name.replace("|","_")
    dm_id = dm_id.replace("|","_")
    print(f"{ep_id}|{region}|{name}|{dm_id}|{min_r}|{max_r}|{idle}")
PYEOF
  done
  rm -rf "${SCAN_DIR}"

  if [ ! -f "${SCAN_CACHE}" ] || [ ! -s "${SCAN_CACHE}" ]; then
    echo -e "${RED}  No endpoints found in any region.${RESET}"
    return
  fi

  local COUNT
  COUNT=$(wc -l < "${SCAN_CACHE}" | tr -d ' ')
  echo -e "${GREEN}  Found ${COUNT} endpoint(s). Use 'list' to show them.${RESET}"
  log "SCAN completed: ${COUNT} endpoints found"
}

# ── List endpoints from cache ─────────────────────────────────
do_list() {
  if [ ! -f "${SCAN_CACHE}" ] || [ ! -s "${SCAN_CACHE}" ]; then
    echo -e "${YELLOW}  No scan data. Run 'scan' first.${RESET}"
    return
  fi
  echo ""
  printf "  ${BOLD}%-4s  %-38s  %-15s  %-5s  %-5s  %-15s${RESET}\n" \
    "No." "Endpoint name" "Region" "Min" "Max" "Idle scaledown"
  printf "  %-4s  %-38s  %-15s  %-5s  %-5s  %-15s\n" \
    "---" "--------------------------------------" "---------------" "---" "---" "---------------"
  local IDX=0
  while IFS='|' read -r ep_id region name dm_id min_r max_r idle; do
    IDX=$((IDX+1))
    SHORT="${name}"
    [ "${#name}" -gt 37 ] && SHORT="${name:0:34}..."
    # Check if this endpoint has a running ping schedule
    SCHED_MARK="  "
    if ls "${REGISTRY_DIR}/${ep_id}_"*.pid 2>/dev/null | head -1 | grep -q .; then
      SCHED_MARK="${GREEN}▶ ${RESET}"
    fi
    printf "  [%-2s]  ${SCHED_MARK}%-38s  %-15s  %-5s  %-5s  %s\n" \
      "${IDX}" "${SHORT}" "${region}" "${min_r}" "${max_r}" "${idle}"
  done < "${SCAN_CACHE}"
  echo ""
  echo -e "  ${DIM}${GREEN}▶${RESET}${DIM} = active ping schedule running${RESET}"
}

# ── Get endpoint line by index ────────────────────────────────
get_ep_line() {
  local IDX="$1"
  sed -n "${IDX}p" "${SCAN_CACHE}" 2>/dev/null || echo ""
}

# ── Ping worker (runs as background daemon) ───────────────────
ping_worker() {
  local EP_ID="$1"
  local REGION="$2"
  local NAME="$3"
  local INTERVAL="$4"
  local LOG="$5"
  local BASE_URL="https://${REGION}-aiplatform.googleapis.com"

  echo "[$(date '+%H:%M:%S')] STARTED ping schedule for ${NAME} every ${INTERVAL}s" >> "${LOG}"

  while true; do
    local TOKEN HTTP_CODE RESP START END MS
    TOKEN=$(gcloud auth print-access-token 2>/dev/null) || TOKEN=""
    RESP=$(mktmp pwork)
    START=$(date +%s)
    HTTP_CODE=$(curl --silent \
      --output "${RESP}" \
      --write-out "%{http_code}" \
      --request POST \
      --header "Authorization: Bearer ${TOKEN}" \
      --header "Content-Type: application/json" \
      --data '{"instances":[{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}]}' \
      --max-time 15 \
      "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${EP_ID}:rawPredict" \
      2>/dev/null) || HTTP_CODE="000"
    END=$(date +%s)
    MS=$(( (END - START) * 1000 ))
    rm -f "${RESP}"

    case "${HTTP_CODE}" in
      200) echo "[$(date '+%H:%M:%S')] [${NAME}] OK ${MS}ms — warm, idle timer reset" >> "${LOG}" ;;
      429) echo "[$(date '+%H:%M:%S')] [${NAME}] SCALED_TO_ZERO — scale-up triggered" >> "${LOG}" ;;
      000) echo "[$(date '+%H:%M:%S')] [${NAME}] TIMEOUT" >> "${LOG}" ;;
      503) echo "[$(date '+%H:%M:%S')] [${NAME}] SERVICE_UNAVAILABLE" >> "${LOG}" ;;
      *)   echo "[$(date '+%H:%M:%S')] [${NAME}] HTTP ${HTTP_CODE} ${MS}ms" >> "${LOG}" ;;
    esac

    sleep "${INTERVAL}"
  done
}

# ── Start a ping schedule ─────────────────────────────────────
do_start() {
  local EP_NUM="$1"
  local INTERVAL="${2:-${INTERVAL_DEFAULT}}"

  if [ ! -f "${SCAN_CACHE}" ] || [ ! -s "${SCAN_CACHE}" ]; then
    echo -e "${RED}  Run 'scan' first.${RESET}"; return
  fi

  local LINE
  LINE=$(get_ep_line "${EP_NUM}")
  if [ -z "${LINE}" ]; then
    echo -e "${RED}  Invalid endpoint number.${RESET}"; return
  fi

  local EP_ID REGION NAME
  EP_ID=$(echo "${LINE}" | cut -d'|' -f1)
  REGION=$(echo "${LINE}" | cut -d'|' -f2)
  NAME=$(echo "${LINE}"   | cut -d'|' -f3)

  # Check if already running
  local EXISTING
  EXISTING=$(ls "${REGISTRY_DIR}/${EP_ID}_"*.pid 2>/dev/null | head -1)
  if [ -n "${EXISTING}" ]; then
    local OLD_PID
    OLD_PID=$(cat "${EXISTING}" 2>/dev/null)
    if kill -0 "${OLD_PID}" 2>/dev/null; then
      echo -e "${YELLOW}  Already running (PID ${OLD_PID}). Stop it first with: stop ${EP_NUM}${RESET}"
      return
    fi
    rm -f "${EXISTING}"
  fi

  # Launch background worker
  local PID_FILE="${REGISTRY_DIR}/${EP_ID}_${INTERVAL}.pid"
  local META_FILE="${REGISTRY_DIR}/${EP_ID}_${INTERVAL}.meta"

  ping_worker "${EP_ID}" "${REGION}" "${NAME}" "${INTERVAL}" "${LOG_FILE}" &
  local WORKER_PID=$!

  echo "${WORKER_PID}" > "${PID_FILE}"
  echo "${NAME}|${REGION}|${INTERVAL}|$(date '+%Y-%m-%d %H:%M:%S')" > "${META_FILE}"

  echo -e "${GREEN}  Started ping for [${NAME}] every ${INTERVAL}s (PID ${WORKER_PID})${RESET}"
  log "START schedule: ${NAME} ep=${EP_ID} interval=${INTERVAL}s pid=${WORKER_PID}"
}

# ── Stop a ping schedule ──────────────────────────────────────
do_stop() {
  local EP_NUM="$1"

  if [ ! -f "${SCAN_CACHE}" ] || [ ! -s "${SCAN_CACHE}" ]; then
    echo -e "${RED}  Run 'scan' first.${RESET}"; return
  fi

  local LINE
  LINE=$(get_ep_line "${EP_NUM}")
  if [ -z "${LINE}" ]; then echo -e "${RED}  Invalid endpoint number.${RESET}"; return; fi

  local EP_ID NAME
  EP_ID=$(echo "${LINE}" | cut -d'|' -f1)
  NAME=$(echo "${LINE}"  | cut -d'|' -f3)

  local FOUND=0
  for PID_FILE in "${REGISTRY_DIR}/${EP_ID}_"*.pid; do
    [ -f "${PID_FILE}" ] || continue
    local PID
    PID=$(cat "${PID_FILE}")
    if kill "${PID}" 2>/dev/null; then
      echo -e "${GREEN}  Stopped ping for [${NAME}] (PID ${PID})${RESET}"
      log "STOP schedule: ${NAME} ep=${EP_ID} pid=${PID}"
    else
      echo -e "${YELLOW}  Process ${PID} was not running (cleaned up)${RESET}"
    fi
    rm -f "${PID_FILE}" "${PID_FILE%.pid}.meta"
    FOUND=$((FOUND+1))
  done

  [ "${FOUND}" = "0" ] && echo -e "${YELLOW}  No active ping schedule for [${NAME}]${RESET}"
}

# ── Stop all schedules ────────────────────────────────────────
do_stopall() {
  local COUNT=0
  for PID_FILE in "${REGISTRY_DIR}/"*.pid; do
    [ -f "${PID_FILE}" ] || continue
    local PID
    PID=$(cat "${PID_FILE}")
    kill "${PID}" 2>/dev/null && COUNT=$((COUNT+1))
    rm -f "${PID_FILE}" "${PID_FILE%.pid}.meta"
  done
  echo -e "${GREEN}  Stopped ${COUNT} ping schedule(s).${RESET}"
  log "STOPALL: stopped ${COUNT} schedules"
}

# ── List active schedules ─────────────────────────────────────
do_schedules() {
  local COUNT=0
  echo ""
  echo -e "  ${BOLD}Active ping schedules:${RESET}"
  for META_FILE in "${REGISTRY_DIR}/"*.meta; do
    [ -f "${META_FILE}" ] || continue
    local PID_FILE="${META_FILE%.meta}.pid"
    [ -f "${PID_FILE}" ] || continue
    local PID
    PID=$(cat "${PID_FILE}")
    local ALIVE="DEAD"
    kill -0 "${PID}" 2>/dev/null && ALIVE="RUNNING"
    local META
    META=$(cat "${META_FILE}")
    local EP_NAME REGION INTERVAL STARTED
    EP_NAME=$(echo "${META}" | cut -d'|' -f1)
    REGION=$(echo "${META}"  | cut -d'|' -f2)
    INTERVAL=$(echo "${META}"| cut -d'|' -f3)
    STARTED=$(echo "${META}" | cut -d'|' -f4)
    if [ "${ALIVE}" = "RUNNING" ]; then
      echo -e "  ${GREEN}▶ [PID ${PID}]${RESET} ${EP_NAME} (${REGION}) — every ${INTERVAL}s — started ${STARTED}"
    else
      echo -e "  ${RED}✗ [PID ${PID} DEAD]${RESET} ${EP_NAME} — cleaning up"
      rm -f "${PID_FILE}" "${META_FILE}"
    fi
    Count=$((Count+1))
  done
  local TOTAL
  TOTAL=$(ls "${REGISTRY_DIR}/"*.meta 2>/dev/null | wc -l | tr -d ' ')
  [ "${TOTAL}" = "0" ] && echo "  No active schedules."
  echo ""
}

# ── One-shot ping ─────────────────────────────────────────────
do_ping_once() {
  local EP_NUM="$1"
  local LINE
  LINE=$(get_ep_line "${EP_NUM}")
  [ -z "${LINE}" ] && echo -e "${RED}  Invalid endpoint number.${RESET}" && return

  local EP_ID REGION NAME
  EP_ID=$(echo "${LINE}" | cut -d'|' -f1)
  REGION=$(echo "${LINE}" | cut -d'|' -f2)
  NAME=$(echo "${LINE}"   | cut -d'|' -f3)

  echo -e "  Pinging ${NAME}..."
  local TOKEN RESP HTTP_CODE START END MS
  TOKEN=$(gcloud auth print-access-token 2>/dev/null)
  RESP=$(mktmp pingonce)
  START=$(date +%s)
  HTTP_CODE=$(curl --silent \
    --output "${RESP}" \
    --write-out "%{http_code}" \
    --request POST \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Content-Type: application/json" \
    --data '{"instances":[{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}]}' \
    --max-time 15 \
    "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${EP_ID}:rawPredict" \
    2>/dev/null) || HTTP_CODE="000"
  END=$(date +%s)
  MS=$(( (END - START) * 1000 ))
  rm -f "${RESP}"

  case "${HTTP_CODE}" in
    200) echo -e "${GREEN}  HTTP 200 — ${MS}ms — LIVE${RESET}" ;;
    429) echo -e "${YELLOW}  HTTP 429 — SCALED TO ZERO (scale-up triggered)${RESET}" ;;
    000) echo -e "${RED}  TIMEOUT${RESET}" ;;
    *)   echo -e "${RED}  HTTP ${HTTP_CODE} — ${MS}ms${RESET}" ;;
  esac
  log "PING_ONCE [${NAME}] HTTP=${HTTP_CODE} ${MS}ms"
}

# ── Show log tail ─────────────────────────────────────────────
do_log() {
  local N="${1:-20}"
  echo ""
  echo -e "  ${BOLD}Last ${N} log entries:${RESET}"
  tail -n "${N}" "${LOG_FILE}" 2>/dev/null | while IFS= read -r L; do
    case "${L}" in
      *OK*|*STARTED*|*LIVE*) echo -e "  ${GREEN}${L}${RESET}" ;;
      *ZERO*|*TIMEOUT*|*DEAD*|*ERROR*) echo -e "${RED}  ${L}${RESET}" ;;
      *STOP*) echo -e "${YELLOW}  ${L}${RESET}" ;;
      *) echo "  ${L}" ;;
    esac
  done
  echo ""
}

# ── Clear log ─────────────────────────────────────────────────
do_clear_log() {
  > "${LOG_FILE}"
  echo -e "${GREEN}  Log cleared.${RESET}"
}

# ── Help ──────────────────────────────────────────────────────
do_help() {
  echo ""
  echo -e "  ${BOLD}Commands:${RESET}"
  echo "  scan                   — scan all regions for endpoints"
  echo "  list                   — list discovered endpoints"
  echo "  ping   <N>             — one-shot ping endpoint N"
  echo "  start  <N> [interval]  — start keepalive ping (default ${INTERVAL_DEFAULT}s)"
  echo "  stop   <N>             — stop ping schedule for endpoint N"
  echo "  stopall                — stop all running ping schedules"
  echo "  schedules              — show active ping schedules"
  echo "  log    [lines]         — show recent log (default 20 lines)"
  echo "  clearlog               — clear the ping log"
  echo "  help                   — show this help"
  echo "  quit / exit            — exit (schedules keep running in background)"
  echo ""
  echo -e "  ${DIM}Example workflow:${RESET}"
  echo "  > scan"
  echo "  > list"
  echo "  > start 1 240     ← ping endpoint 1 every 4 min"
  echo "  > start 2 300     ← ping endpoint 2 every 5 min"
  echo "  > schedules       ← see what's running"
  echo "  > log 30          ← see last 30 log lines"
  echo "  > stop 1          ← stop endpoint 1 pings"
  echo ""
}

# ── Split-pane log display (background tail) ──────────────────
start_log_pane() {
  # Runs tail -f in background, piping to right side of terminal
  # We simulate a right pane by periodically printing a divider + tail
  # (true split-pane needs tmux; this is pure bash approximation)
  :
}

# ── REPL banner ───────────────────────────────────────────────
print_banner() {
  clear
  echo -e "${BOLD}${CYAN}"
  echo "  ╔══════════════════════════════════════════════════════╗"
  echo "  ║        Vertex AI Endpoint Ping REPL                 ║"
  echo "  ║        Project: ${PROJECT_ID}$(printf '%*s' $((36 - ${#PROJECT_ID})) '')║"
  echo "  ║        Log: ${LOG_FILE}$(printf '%*s' $((41 - ${#LOG_FILE})) '')║"
  echo "  ╚══════════════════════════════════════════════════════╝"
  echo -e "${RESET}"
  echo -e "  Type ${BOLD}help${RESET} for commands. Schedules survive REPL exit."
  echo ""
}

# ── Live log sidebar (runs in background, prints periodically) ─
log_watcher() {
  # Every 15s, print a compact log tail separated by a divider
  while true; do
    sleep 15
    # Only print if log has new lines
    local LINES
    LINES=$(wc -l < "${LOG_FILE}" 2>/dev/null | tr -d ' ')
    if [ "${LINES:-0}" -gt 0 ]; then
      echo ""
      echo -e "${DIM}  ─── ping log (last 5) ───────────────────────────────${RESET}"
      tail -5 "${LOG_FILE}" 2>/dev/null | while IFS= read -r L; do
        case "${L}" in
          *OK*|*LIVE*) echo -e "  ${GREEN}${DIM}${L}${RESET}" ;;
          *ZERO*|*TIMEOUT*) echo -e "  ${YELLOW}${DIM}${L}${RESET}" ;;
          *) echo -e "  ${DIM}${L}${RESET}" ;;
        esac
      done
      echo -e "${DIM}  ────────────────────────────────────────────────────${RESET}"
    fi
  done
}

# ── Main REPL loop ────────────────────────────────────────────
print_banner
do_help

# Start background log watcher
log_watcher &
LOG_WATCHER_PID=$!
trap "kill ${LOG_WATCHER_PID} 2>/dev/null; exit 0" INT TERM

while true; do
  printf "${BOLD}${CYAN}ping>${RESET} "
  read -r CMD ARG1 ARG2 || break

  case "${CMD}" in
    scan)        do_scan ;;
    list|ls)     do_list ;;
    ping)
      [ -z "${ARG1}" ] && echo "  Usage: ping <N>" || do_ping_once "${ARG1}" ;;
    start)
      [ -z "${ARG1}" ] && echo "  Usage: start <N> [interval_seconds]" || \
        do_start "${ARG1}" "${ARG2:-${INTERVAL_DEFAULT}}" ;;
    stop)
      [ -z "${ARG1}" ] && echo "  Usage: stop <N>" || do_stop "${ARG1}" ;;
    stopall)     do_stopall ;;
    schedules|ps) do_schedules ;;
    log)         do_log "${ARG1:-20}" ;;
    clearlog)    do_clear_log ;;
    help|?)      do_help ;;
    quit|exit|q) 
      echo ""
      echo -e "${YELLOW}  Exiting REPL. Background ping schedules continue running.${RESET}"
      ACTIVE=$(ls "${REGISTRY_DIR}/"*.pid 2>/dev/null | wc -l | tr -d ' ')
      [ "${ACTIVE}" -gt 0 ] && echo -e "  ${GREEN}  ${ACTIVE} schedule(s) still active. Use 'stopall' to kill them.${RESET}"
      echo ""
      kill "${LOG_WATCHER_PID}" 2>/dev/null
      exit 0 ;;
    "")          ;; # ignore blank lines
    *)           echo -e "${RED}  Unknown command: ${CMD}. Type 'help'.${RESET}" ;;
  esac
done
