#!/bin/bash
# =============================================================
# warmup_ping.sh
# Keeps Cloud Run GPU services warm by pinging them regularly.
# Lists all services, lets you pick which to keep warm.
#
# Usage:
#   bash warmup_ping.sh                  # interactive
#   bash warmup_ping.sh --all            # ping all services
#   bash warmup_ping.sh --interval=120   # custom interval
#   bash warmup_ping.sh --region=europe-west4
#
# Background daemon:
#   nohup bash warmup_ping.sh --all >> /tmp/warmup.log 2>&1 &
#   echo $! > /tmp/warmup.pid
#   kill $(cat /tmp/warmup.pid)   # to stop
# =============================================================

# Load config if present
if [ -f "$(dirname "$0")/config.env" ]; then
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
fi

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
REGION="${REGION:-us-central1}"
INTERVAL="${INTERVAL:-60}"
LOG_FILE="/tmp/warmup_ping_${PROJECT_ID}.log"

G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"
B="\033[1m"; DIM="\033[2m"; N="\033[0m"

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

log() {
  local MSG="[$(date '+%H:%M:%S')] $*"
  echo "${MSG}" >> "${LOG_FILE}"
  echo -e "${MSG}"
}

# ── Parse CLI ─────────────────────────────────────────────────
PING_ALL=false
for arg in "$@"; do
  case "${arg}" in
    --interval=*) INTERVAL="${arg#--interval=}" ;;
    --all)        PING_ALL=true ;;
    --region=*)   REGION="${arg#--region=}" ;;
    --project=*)  PROJECT_ID="${arg#--project=}" ;;
  esac
done

# ── Ping one service ──────────────────────────────────────────
ping_service() {
  local NAME="$1" URL="$2"
  local TOKEN RESP_TMP HTTP_CODE T_START T_END MS
  TOKEN=$(gcloud auth print-identity-token 2>/dev/null)
  RESP_TMP=$(mktmp ping)
  T_START=$(date +%s)
  HTTP_CODE=$(curl --silent \
    --output "${RESP_TMP}" \
    --write-out "%{http_code}" \
    --max-time 30 \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Content-Type: application/json" \
    --data "{\"model\":\"${SERVED_MODEL_NAME:-gemma4}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}" \
    "${URL}/v1/chat/completions" 2>/dev/null) || HTTP_CODE="000"
  T_END=$(date +%s)
  MS=$(( (T_END - T_START) * 1000 ))
  rm -f "${RESP_TMP}"

  case "${HTTP_CODE}" in
    200) log "${G}✓${N} [${NAME}] OK ${MS}ms — warm" ;;
    000) log "${Y}⚠${N} [${NAME}] TIMEOUT — cold starting" ;;
    403) log "${R}✗${N} [${NAME}] 403 auth error — re-run gcloud auth login" ;;
    429) log "${Y}⚠${N} [${NAME}] 429 rate limited" ;;
    *)   log "${Y}⚠${N} [${NAME}] HTTP ${HTTP_CODE} — ${MS}ms" ;;
  esac
}

# ── Banner ────────────────────────────────────────────────────
clear
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  Cloud Run Warmup Pinger                               ${N}"
echo -e "${B}  Project : ${PROJECT_ID}  |  Region : ${REGION}        ${N}"
echo -e "${B}  Interval: ${INTERVAL}s   |  Log: ${LOG_FILE}          ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""
echo -e "${C}▶ Fetching Cloud Run services...${N}"
echo ""

# ── List services ─────────────────────────────────────────────
SVC_NAMES=()
SVC_URLS=()
IDX=0

while IFS=$'\t' read -r NAME URL _REST; do
  [ -z "${NAME}" ] && continue
  IDX=$((IDX+1))
  SVC_NAMES+=("${NAME}")
  SVC_URLS+=("${URL}")
  SHORT="${URL}"
  [ "${#URL}" -gt 52 ] && SHORT="${URL:0:49}..."
  printf "  ${B}[%2d]${N}  %-32s  ${DIM}%s${N}\n" "${IDX}" "${NAME}" "${SHORT}"
done < <(gcloud run services list \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(name,status.url)" 2>/dev/null)

if [ "${IDX}" = "0" ]; then
  echo -e "${R}  No services found in ${REGION}.${N}"
  echo "  Try: gcloud run services list --project=${PROJECT_ID}"
  exit 1
fi

echo ""
echo -e "  ${DIM}${IDX} service(s) found${N}"
echo ""

# ── Selection ─────────────────────────────────────────────────
SELECTED_NAMES=()
SELECTED_URLS=()

if [ "${PING_ALL}" = "true" ]; then
  SELECTED_NAMES=("${SVC_NAMES[@]}")
  SELECTED_URLS=("${SVC_URLS[@]}")
else
  echo -e "  Enter numbers to ping (e.g. ${B}1${N} or ${B}1 2${N})"
  echo -e "  Press ${B}Enter${N} = all  |  ${B}q${N} = quit"
  echo ""
  printf "  > "
  read -r INPUT

  [ "${INPUT}" = "q" ] || [ "${INPUT}" = "Q" ] && echo "Quit." && exit 0

  if [ -z "${INPUT}" ]; then
    SELECTED_NAMES=("${SVC_NAMES[@]}")
    SELECTED_URLS=("${SVC_URLS[@]}")
  else
    for NUM in ${INPUT}; do
      echo "${NUM}" | grep -qE '^[0-9]+$' || { echo -e "${R}  Invalid: ${NUM}${N}"; continue; }
      [ "${NUM}" -ge 1 ] && [ "${NUM}" -le "${IDX}" ] || { echo -e "${R}  Out of range: ${NUM}${N}"; continue; }
      SELECTED_NAMES+=("${SVC_NAMES[$((NUM-1))]}")
      SELECTED_URLS+=("${SVC_URLS[$((NUM-1))]}")
    done
  fi
fi

[ "${#SELECTED_NAMES[@]}" = "0" ] && echo -e "${R}  Nothing selected.${N}" && exit 1

echo ""
echo -e "${B}  Pinging:${N}"
for NAME in "${SELECTED_NAMES[@]}"; do
  echo -e "    ${G}•${N} ${NAME}"
done

echo ""
printf "  Interval [${INTERVAL}s] — press Enter to confirm or type new value: "
read -r CUSTOM
echo "${CUSTOM}" | grep -qE '^[0-9]+$' && [ "${CUSTOM}" -gt 0 ] && INTERVAL="${CUSTOM}"

# ── Loop ──────────────────────────────────────────────────────
echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  Pinging every ${INTERVAL}s — Ctrl+C to stop           ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""

trap 'echo ""; log "Warmup pinger stopped."; exit 0' INT TERM

log "Started — services: ${SELECTED_NAMES[*]} — interval: ${INTERVAL}s"

ROUND=0
while true; do
  ROUND=$((ROUND+1))
  echo -e "${DIM}── Round ${ROUND} ─ $(date '+%Y-%m-%d %H:%M:%S') ────────────────${N}"
  for i in $(seq 0 $((${#SELECTED_NAMES[@]}-1))); do
    ping_service "${SELECTED_NAMES[$i]}" "${SELECTED_URLS[$i]}"
  done
  echo -e "${DIM}  Next ping in ${INTERVAL}s${N}"
  sleep "${INTERVAL}"
done
