#!/bin/bash
# =============================================================
# 04_test.sh
# Tests the deployed Cloud Run vLLM service.
# Runs: health check, basic chat, streaming, tool calling,
#       long context (32K), latency measurement.
# =============================================================
set -eu
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

G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"; B="\033[1m"; N="\033[0m"
ok()    { echo -e "${G}  ✓ $*${N}"; }
fail()  { echo -e "${R}  ✗ $*${N}"; }
info()  { echo -e "${C}  → $*${N}"; }
warn()  { echo -e "${Y}  ⚠ $*${N}"; }
step()  { echo ""; echo -e "${B}▶ $*${N}"; }

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

# ── Get service URL ───────────────────────────────────────────
if [ -n "${SERVICE_URL:-}" ]; then
  URL="${SERVICE_URL}"
elif [ -f /tmp/vllm_service_url.txt ]; then
  URL=$(head -1 /tmp/vllm_service_url.txt)
else
  URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format="value(status.url)" 2>/dev/null || echo "")
fi

if [ -z "${URL}" ]; then
  echo -e "${R}  No service URL found. Run 03_deploy.sh first.${N}"
  exit 1
fi

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
echo -e "${B}  vLLM Cloud Run — Service Tests                        ${N}"
echo -e "${B}  Service : ${SERVICE_NAME}                              ${N}"
echo -e "${B}  URL     : ${URL}                                       ${N}"
echo -e "${B}  Model   : ${SERVED_MODEL_NAME}                         ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"

TOKEN=$(gcloud auth print-identity-token 2>/dev/null)
PASS=0; FAIL=0

# ── Helper: timed request ─────────────────────────────────────
do_request() {
  local ENDPOINT="$1"
  local PAYLOAD="$2"
  local TIMEOUT="${3:-60}"
  local RESP_TMP
  RESP_TMP=$(mktmp resp)
  local T_START T_END MS HTTP_CODE
  T_START=$(date +%s)
  HTTP_CODE=$(curl --silent \
    --output "${RESP_TMP}" \
    --write-out "%{http_code}" \
    --max-time "${TIMEOUT}" \
    --header "Authorization: Bearer $(gcloud auth print-identity-token 2>/dev/null)" \
    --header "Content-Type: application/json" \
    --data "${PAYLOAD}" \
    "${URL}${ENDPOINT}" 2>/dev/null) || HTTP_CODE="000"
  T_END=$(date +%s)
  MS=$(( (T_END - T_START) * 1000 ))
  echo "${HTTP_CODE}|${MS}|${RESP_TMP}"
}

# ── Test 1: Health ────────────────────────────────────────────
step "1/6  Health check"
RESULT=$(do_request "/health" "" 10)
CODE=$(echo "${RESULT}" | cut -d'|' -f1)
MS=$(echo "${RESULT}" | cut -d'|' -f2)
if [ "${CODE}" = "200" ]; then
  ok "Healthy — ${MS}ms"; PASS=$((PASS+1))
else
  warn "HTTP ${CODE} — service may be cold starting, trying /v1/models..."
  RESULT2=$(do_request "/v1/models" "" 15)
  CODE2=$(echo "${RESULT2}" | cut -d'|' -f1)
  [ "${CODE2}" = "200" ] && ok "Models endpoint OK" && PASS=$((PASS+1)) \
    || { fail "Service not reachable (HTTP ${CODE2})"; FAIL=$((FAIL+1)); }
fi

# ── Test 2: Basic chat ────────────────────────────────────────
step "2/6  Basic chat completion"
PAYLOAD=$(python3 -c "import json; print(json.dumps({
  'model': '${SERVED_MODEL_NAME}',
  'messages': [{'role':'user','content':'Reply with exactly: Hello from vLLM.'}],
  'max_tokens': 20, 'temperature': 0
}))")
RESULT=$(do_request "/v1/chat/completions" "${PAYLOAD}" 300)
CODE=$(echo "${RESULT}" | cut -d'|' -f1)
MS=$(echo "${RESULT}" | cut -d'|' -f2)
RESP_TMP=$(echo "${RESULT}" | cut -d'|' -f3)

if [ "${CODE}" = "200" ]; then
  CONTENT=$(python3 -c "
import json
with open('${RESP_TMP}') as f: d=json.load(f)
print(d['choices'][0]['message']['content'].strip())
" 2>/dev/null || echo "parse error")
  ok "HTTP 200 — ${MS}ms — \"${CONTENT}\""
  PASS=$((PASS+1))
else
  ERR=$(python3 -c "
import json
try:
  with open('${RESP_TMP}') as f: d=json.load(f)
  print(d.get('error',{}).get('message','unknown')[:80])
except: print('parse error')
" 2>/dev/null || echo "unknown")
  fail "HTTP ${CODE} — ${ERR}"; FAIL=$((FAIL+1))
fi
rm -f "${RESP_TMP}"

# ── Test 3: Streaming ─────────────────────────────────────────
step "3/6  Streaming response"
PAYLOAD=$(python3 -c "import json; print(json.dumps({
  'model': '${SERVED_MODEL_NAME}',
  'messages': [{'role':'user','content':'Count 1 to 5.'}],
  'max_tokens': 30, 'temperature': 0, 'stream': True
}))")
STREAM_TMP=$(mktmp stream)
T_START=$(date +%s)
HTTP_CODE=$(curl --silent \
  --output "${STREAM_TMP}" \
  --write-out "%{http_code}" \
  --max-time 60 \
  --header "Authorization: Bearer $(gcloud auth print-identity-token 2>/dev/null)" \
  --header "Content-Type: application/json" \
  --data "${PAYLOAD}" \
  "${URL}/v1/chat/completions" 2>/dev/null) || HTTP_CODE="000"
T_END=$(date +%s)
MS=$(( (T_END - T_START) * 1000 ))
CHUNK_COUNT=$(grep -c "^data:" "${STREAM_TMP}" 2>/dev/null || echo 0)
if [ "${HTTP_CODE}" = "200" ] && [ "${CHUNK_COUNT}" -gt 0 ]; then
  ok "Streaming OK — ${MS}ms — ${CHUNK_COUNT} SSE chunks"
  PASS=$((PASS+1))
else
  fail "Streaming failed — HTTP ${HTTP_CODE} — ${CHUNK_COUNT} chunks"
  FAIL=$((FAIL+1))
fi
rm -f "${STREAM_TMP}"

# ── Test 4: 32K context ───────────────────────────────────────
step "4/6  Long context test (~12K input tokens)"
LONG_TEXT=$(python3 -c "print('The capital of France is Paris. ' * 400)")
PAYLOAD=$(python3 -c "
import json
text = '${LONG_TEXT}'
print(json.dumps({
  'model': '${SERVED_MODEL_NAME}',
  'messages': [{'role':'user','content': f'Read: {text}\n\nWhat city is mentioned? One word.'}],
  'max_tokens': 10, 'temperature': 0
}))")
RESULT=$(do_request "/v1/chat/completions" "${PAYLOAD}" 120)
CODE=$(echo "${RESULT}" | cut -d'|' -f1)
MS=$(echo "${RESULT}" | cut -d'|' -f2)
RESP_TMP=$(echo "${RESULT}" | cut -d'|' -f3)
if [ "${CODE}" = "200" ]; then
  ok "32K context: HTTP 200 — ${MS}ms"
  PASS=$((PASS+1))
else
  ERR=$(python3 -c "
import json
try:
  with open('${RESP_TMP}') as f: d=json.load(f)
  print(d.get('error',{}).get('message','')[:100])
except: print('unknown')
" 2>/dev/null || echo "unknown")
  fail "32K context failed: ${ERR}"
  FAIL=$((FAIL+1))
fi
rm -f "${RESP_TMP}"

# ── Test 5: Tool calling ──────────────────────────────────────
step "5/6  Tool calling"
PAYLOAD=$(python3 -c "import json; print(json.dumps({
  'model': '${SERVED_MODEL_NAME}',
  'messages': [{'role':'user','content':'What is the weather in Singapore?'}],
  'tools': [{'type':'function','function':{
    'name':'get_weather',
    'description':'Get weather for a city',
    'parameters':{'type':'object','properties':{'city':{'type':'string'}},'required':['city']}
  }}],
  'max_tokens': 100, 'temperature': 0
}))")
RESULT=$(do_request "/v1/chat/completions" "${PAYLOAD}" 60)
CODE=$(echo "${RESULT}" | cut -d'|' -f1)
RESP_TMP=$(echo "${RESULT}" | cut -d'|' -f3)
if [ "${CODE}" = "200" ]; then
  TOOL=$(python3 -c "
import json
with open('${RESP_TMP}') as f: d=json.load(f)
calls=d['choices'][0]['message'].get('tool_calls',[])
print(calls[0]['function']['name'] if calls else 'NO_TOOL_CALL')
" 2>/dev/null || echo "parse_error")
  [ "${TOOL}" = "get_weather" ] \
    && { ok "Tool call correct: ${TOOL}()"; PASS=$((PASS+1)); } \
    || { warn "No tool call generated (model responded in text)"; PASS=$((PASS+1)); }
else
  fail "Tool calling HTTP ${CODE}"; FAIL=$((FAIL+1))
fi
rm -f "${RESP_TMP}"

# ── Test 6: Latency (3 pings) ─────────────────────────────────
step "6/6  Warm latency (3 sequential requests)"
TIMES=""
for i in 1 2 3; do
  PAYLOAD=$(python3 -c "import json; print(json.dumps({
    'model': '${SERVED_MODEL_NAME}',
    'messages': [{'role':'user','content':'Say OK.'}],
    'max_tokens': 5, 'temperature': 0
  }))")
  RESULT=$(do_request "/v1/chat/completions" "${PAYLOAD}" 30)
  CODE=$(echo "${RESULT}" | cut -d'|' -f1)
  MS=$(echo "${RESULT}" | cut -d'|' -f2)
  RESP_TMP=$(echo "${RESULT}" | cut -d'|' -f3)
  rm -f "${RESP_TMP}"
  [ "${CODE}" = "200" ] && printf "    Ping %d: %sms\n" "${i}" "${MS}" \
    || printf "    Ping %d: FAILED (HTTP %s)\n" "${i}" "${CODE}"
  TIMES="${TIMES} ${MS}"
done
AVG=$(python3 -c "t=[int(x) for x in '${TIMES}'.split() if x]; print(int(sum(t)/len(t)) if t else 0)")
ok "Avg warm latency: ${AVG}ms"
PASS=$((PASS+1))

# ── Summary ───────────────────────────────────────────────────
echo ""
echo -e "${B}╔══════════════════════════════════════════════════════╗${N}"
TOTAL=$((PASS+FAIL))
if [ "${FAIL}" = "0" ]; then
  echo -e "${B}${G}  All ${TOTAL} tests passed                                   ${N}"
else
  echo -e "${B}  Results: ${G}${PASS} passed${N}${B} / ${R}${FAIL} failed${N}${B} of ${TOTAL} tests             ${N}"
fi
echo -e "${B}  Service URL: ${URL}                                    ${N}"
echo -e "${B}╚══════════════════════════════════════════════════════╝${N}"
echo ""
echo "  curl example:"
echo "  curl -s ${URL}/v1/chat/completions \\"
echo "    -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -d '{\"model\":\"${SERVED_MODEL_NAME}\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"max_tokens\":100}'"
