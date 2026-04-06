#!/bin/bash
# =============================================================
# warmup_ping.sh
#
# Keeps a Cloud Run GPU service warm by pinging it regularly.
# Lists all Cloud Run services in your project, lets you pick
# one (or multiple), then pings them on a schedule.
#
# Usage:
#   bash warmup_ping.sh              # interactive menu
#   bash warmup_ping.sh --interval=60  # custom interval (seconds)
#   bash warmup_ping.sh --all        # ping all services
# =============================================================

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="${REGION:-us-central1}"
INTERVAL="${INTERVAL:-60}"   # default: 60 seconds
LOG_FILE="/tmp/warmup_ping_${PROJECT_ID}.log"

# ── Colours ───────────────────────────────────────────────────
G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"
B="\033[1m"; DIM="\033[2m"; N="\033[0m"

# ── Parse CLI ─────────────────────────────────────────────────
PING_ALL=false
for arg in "$@"; do
  case "${arg}" in
    --interval=*) INTERVAL="${arg#--interval=}" ;;
    --all)        PING_ALL=true ;;
    --region=*)   REGION="${arg#--region=}" ;;
  esac
done

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

log() {
  local MSG="[$(date '+%H:%M:%S')] $*"
  echo "${MSG}" >> "${LOG_FILE}"
  echo -e "${MSG}"
}

# ── Fetch all Cloud Run services ──────────────────────────────
fetch_services() {
  gcloud run services list \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(name,status.url,status.conditions[0].status)" \
    2>/dev/null
}

# ── Ping one service ──────────────────────────────────────────
ping_service() {
  local NAME="$1"
  local URL="$2"

  local TOKEN
  TOKEN=$(gcloud auth print-identity-token 2>/dev/null)

  local RESP_TMP
  RESP_TMP=$(mktmp ping)
  local T_START T_END MS HTTP_CODE

  T_START=$(date +%s)
  HTTP_CODE=$(curl --silent \
    --output "${RESP_TMP}" \
    --write-out "%{http_code}" \
    --max-time 30 \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Content-Type: application/json" \
    --data '{"model":"gemma4","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
    "${URL}/v1/chat/completions" 2>/dev/null) || HTTP_CODE="000"
  T_END=$(date +%s)
  MS=$(( (T_END - T_START) * 1000 ))
  rm -f "${RESP_TMP}"

  case "${HTTP_CODE}" in
    200) log "${G}✓${N} [${NAME}] OK ${MS}ms — warm" ;;
    000) log "${Y}⚠${N} [${NAME}] TIMEOUT — may be cold starting" ;;
    403) log "${R}✗${N} [${NAME}] HTTP 403 — auth error" ;;
    *)   log "${Y}⚠${N} [${NAME}] HTTP ${HTTP_CODE} — ${MS}ms" ;;
  esac
}

# ── Banner ────────────────────────────────────────────────────
clear
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  Cloud Run Warmup Pinger                               ${N}"
echo -e "${B}  Project : ${PROJECT_ID}  |  Region : ${REGION}        ${N}"
echo -e "${B}  Interval: ${INTERVAL}s  |  Log: ${LOG_FILE}           ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""

# ── Fetch and display services ────────────────────────────────
echo -e "${C}▶ Fetching Cloud Run services...${N}"
echo ""

SVC_NAMES=()
SVC_URLS=()
IDX=0

while IFS=$'\t' read -r NAME URL STATUS; do
  [ -z "${NAME}" ] && continue
  IDX=$((IDX + 1))
  SVC_NAMES+=("${NAME}")
  SVC_URLS+=("${URL}")

  # Shorten URL for display
  SHORT_URL="${URL}"
  [ "${#URL}" -gt 50 ] && SHORT_URL="${URL:0:47}..."

  printf "  ${B}[%2d]${N}  %-30s  ${DIM}%s${N}\n" \
    "${IDX}" "${NAME}" "${SHORT_URL}"
done < <(fetch_services)

if [ "${IDX}" = "0" ]; then
  echo -e "${R}  No Cloud Run services found in ${REGION}.${N}"
  echo "  Check region with: gcloud run services list --project=${PROJECT_ID}"
  exit 1
fi

echo ""
echo -e "  ${DIM}Total: ${IDX} service(s)${N}"
echo ""

# ── Selection ─────────────────────────────────────────────────
SELECTED_NAMES=()
SELECTED_URLS=()

if [ "${PING_ALL}" = "true" ]; then
  SELECTED_NAMES=("${SVC_NAMES[@]}")
  SELECTED_URLS=("${SVC_URLS[@]}")
  echo -e "${G}  Pinging all ${IDX} service(s)${N}"
else
  echo -e "  Enter service numbers to ping (space-separated, e.g. ${B}1 3${N})"
  echo -e "  Or press ${B}Enter${N} to ping all, ${B}q${N} to quit:"
  echo ""
  printf "  > "
  read -r INPUT

  [ "${INPUT}" = "q" ] || [ "${INPUT}" = "Q" ] && echo "Quit." && exit 0

  if [ -z "${INPUT}" ]; then
    # Ping all
    SELECTED_NAMES=("${SVC_NAMES[@]}")
    SELECTED_URLS=("${SVC_URLS[@]}")
    echo -e "${G}  Pinging all ${IDX} service(s)${N}"
  else
    for NUM in ${INPUT}; do
      if ! echo "${NUM}" | grep -qE '^[0-9]+$'; then
        echo -e "${R}  Invalid: ${NUM}${N}"; continue
      fi
      if [ "${NUM}" -lt 1 ] || [ "${NUM}" -gt "${IDX}" ]; then
        echo -e "${R}  Out of range: ${NUM}${N}"; continue
      fi
      SELECTED_NAMES+=("${SVC_NAMES[$((NUM-1))]}")
      SELECTED_URLS+=("${SVC_URLS[$((NUM-1))]}")
    done
  fi
fi

if [ "${#SELECTED_NAMES[@]}" = "0" ]; then
  echo -e "${R}  No valid services selected.${N}"
  exit 1
fi

echo ""
echo -e "${B}  Selected services:${N}"
for i in $(seq 0 $((${#SELECTED_NAMES[@]} - 1))); do
  echo -e "    ${G}•${N} ${SELECTED_NAMES[$i]}"
done

echo ""
printf "  Ping interval: ${B}${INTERVAL}s${N}. Confirm? [Y/n]: "
read -r CONFIRM
[ "${CONFIRM}" = "n" ] || [ "${CONFIRM}" = "N" ] && echo "Cancelled." && exit 0

# ── Custom interval prompt ────────────────────────────────────
echo ""
printf "  Use ${INTERVAL}s interval or enter custom (seconds): "
read -r CUSTOM
if echo "${CUSTOM}" | grep -qE '^[0-9]+$' && [ "${CUSTOM}" -gt 0 ]; then
  INTERVAL="${CUSTOM}"
fi

# ── Start pinging ─────────────────────────────────────────────
echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  Pinging every ${INTERVAL}s — Ctrl+C to stop           ${N}"
echo -e "${B}  Log: ${LOG_FILE}                                       ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""

log "Starting warmup pinger — interval=${INTERVAL}s — services: ${SELECTED_NAMES[*]}"

trap 'echo ""; log "Stopped."; exit 0' INT TERM

ROUND=0
while true; do
  ROUND=$((ROUND + 1))
  echo -e "${DIM}── Round ${ROUND} — $(date '+%H:%M:%S') ──────────────────────────────${N}"

  for i in $(seq 0 $((${#SELECTED_NAMES[@]} - 1))); do
    ping_service "${SELECTED_NAMES[$i]}" "${SELECTED_URLS[$i]}"
  done

  echo -e "${DIM}  Sleeping ${INTERVAL}s... (Ctrl+C to stop)${N}"
  sleep "${INTERVAL}"
done
