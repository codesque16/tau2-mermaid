#!/usr/bin/env bash
# =============================================================
# test_vertex_endpoint.sh
#
# End-to-end tests against Vertex AI dedicated endpoints via
# :rawPredict and :streamRawPredict (same shapes as ping_endpoints.sh
# and benchmark_throughput.sh).
#
# Interactive: scan all regions → pick endpoint → pick test.
# Non-interactive: set ENDPOINT_ID + REGION (+ optional PROJECT_ID).
#
# Usage:
#   bash test_vertex_endpoint.sh
#   ENDPOINT_ID=... REGION=us-central1 bash test_vertex_endpoint.sh --menu
#   ENDPOINT_ID=... REGION=us-central1 bash test_vertex_endpoint.sh --quick
#   ENDPOINT_ID=... REGION=us-central1 bash test_vertex_endpoint.sh --generate
#   ENDPOINT_ID=... REGION=us-central1 bash test_vertex_endpoint.sh --stream
#
# If --quick returns HTTP 400, retry with:
#   VERTEX_USE_CHAT_COMPLETIONS=1 bash test_vertex_endpoint.sh --quick
# =============================================================
set -eu

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
ALL_REGIONS="africa-south1 northamerica-northeast1 northamerica-northeast2 southamerica-east1 southamerica-west1 us-central1 us-east1 us-east4 us-east5 us-south1 us-west1 us-west2 us-west3 us-west4 us-west8 asia-east1 asia-east2 asia-northeast1 asia-northeast2 asia-northeast3 asia-south1 asia-south2 asia-southeast1 asia-southeast2 australia-southeast1 australia-southeast2 europe-central2 europe-north1 europe-north2 europe-southwest1 europe-west1 europe-west2 europe-west3 europe-west4 europe-west6 europe-west8 europe-west9 europe-west12 europe-west15 me-central1 me-central2 me-west1"

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

predict_body_quick() {
  if [ "${VERTEX_USE_CHAT_COMPLETIONS:-0}" = "1" ]; then
    printf '%s' '{"instances":[{"@requestFormat":"chatCompletions","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":8,"temperature":0}]}'
  else
    printf '%s' '{"instances":[{"messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":8}]}'
  fi
}

predict_body_generate() {
  if [ "${VERTEX_USE_CHAT_COMPLETIONS:-0}" = "1" ]; then
    printf '%s' '{"instances":[{"@requestFormat":"chatCompletions","messages":[{"role":"user","content":"Name three colors in one short sentence."}],"max_tokens":64,"temperature":0}]}'
  else
    printf '%s' '{"instances":[{"messages":[{"role":"user","content":"Name three colors in one short sentence."}],"max_tokens":64}]}'
  fi
}

run_raw_predict() {
  local EP_ID="$1"
  local REGION="$2"
  local LABEL="$3"
  local BODY="$4"
  local TIMEOUT="${5:-120}"
  local URL="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${EP_ID}:rawPredict"
  local OUT
  OUT=$(mktmp vtx_test_out)
  echo ""
  echo "── ${LABEL} ──"
  echo "  POST ${URL}"
  local T0 T1 MS
  T0=$(python3 -c "import time; print(int(time.time()*1000))")
  local CODE
  CODE=$(curl --silent --output "${OUT}" --write-out "%{http_code}" -X POST "${URL}" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -d "${BODY}" \
    --max-time "${TIMEOUT}" 2>/dev/null) || CODE="000"
  T1=$(python3 -c "import time; print(int(time.time()*1000))")
  MS=$((T1 - T0))
  echo "  HTTP ${CODE}  (${MS} ms)"
  case "${CODE}" in
    200)
      echo "  Response (pretty):"
      python3 -m json.tool < "${OUT}" 2>/dev/null | head -n 40 | sed 's/^/    /' || head -c 2000 "${OUT}" | sed 's/^/    /'
      ;;
    *)
      echo "  Response body (trimmed):"
      head -c 1200 "${OUT}" | tr '\n' ' ' | fold -s -w 72 | sed 's/^/    /'
      echo ""
      ;;
  esac
  rm -f "${OUT}"
}

run_stream_predict() {
  local EP_ID="$1"
  local REGION="$2"
  local MAX_TOK="${3:-48}"
  local TIMEOUT="${4:-180}"
  local URL="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${EP_ID}:streamRawPredict"
  echo ""
  echo "── streamRawPredict (max_tokens=${MAX_TOK}) ──"
  echo "  POST ${URL}"
  local T0 T1 MS
  T0=$(python3 -c "import time; print(int(time.time()*1000))")
  local OUT
  OUT=$(mktmp vtx_stream_out)
  local CODE
  CODE=$(curl --silent --output "${OUT}" --write-out "%{http_code}" -X POST "${URL}" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"test\",\"messages\":[{\"role\":\"user\",\"content\":\"Count from 1 to 5 slowly, one number per phrase.\"}],\"max_tokens\":${MAX_TOK},\"temperature\":0,\"stream\":true}" \
    --no-buffer \
    --max-time "${TIMEOUT}" 2>/dev/null) || CODE="000"
  T1=$(python3 -c "import time; print(int(time.time()*1000))")
  MS=$((T1 - T0))
  echo "  HTTP ${CODE}  (${MS} ms wall)"
  if [ "${CODE}" = "200" ]; then
    local LINES
    LINES=$(grep -c '^data:' "${OUT}" 2>/dev/null || echo "0")
    echo "  SSE data lines: ${LINES}"
    echo "  First ~15 lines:"
    head -n 15 "${OUT}" | sed 's/^/    /'
  else
    head -c 800 "${OUT}" | tr '\n' ' ' | fold -s -w 72 | sed 's/^/    /'
    echo ""
  fi
  rm -f "${OUT}"
}

scan_and_index() {
  local TOKEN="$1"
  local SCAN_DIR
  SCAN_DIR=$(mktemp -d /tmp/vtx_test_scan_XXXXXX)
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
except Exception:
    print(0)
" 2>/dev/null) || COUNT=0
    [ "${COUNT}" = "0" ] && rm -f "${OUT}"
  }
  for R in $ALL_REGIONS; do scan_one "${R}" & done
  wait

  local INDEX_DIR
  INDEX_DIR=$(mktemp -d /tmp/vtx_test_idx_XXXXXX)
  for R in $ALL_REGIONS; do
    RFILE="${SCAN_DIR}/${R}"
    [ -f "${RFILE}" ] || continue
    python3 - "${RFILE}" "${R}" "${INDEX_DIR}" << 'PYEOF'
import json, sys, os
with open(sys.argv[1]) as f:
    data = json.load(f)
region = sys.argv[2]
idx_dir = sys.argv[3]
existing = [f for f in os.listdir(idx_dir) if f.startswith("ep_")]
start = len(existing)
for i, ep in enumerate(data.get("endpoints", []), start=start + 1):
    ep_id = ep.get("name", "").split("/")[-1]
    name = ep.get("displayName", "unnamed")
    models = ep.get("deployedModels", [])
    m0 = models[0] if models else {}
    model = m0.get("displayName", "no-model")
    state = m0.get("state", "UNKNOWN")
    dr = m0.get("dedicatedResources", {})
    min_r = str(dr.get("minReplicaCount", "?"))
    max_r = str(dr.get("maxReplicaCount", "?"))
    with open(os.path.join(idx_dir, f"ep_{i}"), "w") as out:
        out.write(ep_id + "\n")
        out.write(region + "\n")
        out.write(name + "\n")
        out.write(model + "\n")
        out.write(state + "\n")
        out.write(min_r + "\n")
        out.write(max_r + "\n")
PYEOF
  done
  rm -rf "${SCAN_DIR}"
  echo "${INDEX_DIR}"
}

MODE="${1:---menu}"

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "  Vertex AI endpoint tester"
echo "  Project: ${PROJECT_ID}"
echo "╚════════════════════════════════════════════════════════════╝"

TOKEN=$(gcloud auth print-access-token)
[ -z "${TOKEN}" ] && echo "ERROR: gcloud auth login first" && exit 1
gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

ENDPOINT_ID="${ENDPOINT_ID:-}"
REGION="${REGION:-}"

pick_from_scan() {
  local INDEX_DIR
  INDEX_DIR=$(scan_and_index "${TOKEN}")
  TOTAL=$(ls "${INDEX_DIR}" 2>/dev/null | grep -c '^ep_' || true)
  TOTAL=$(echo "${TOTAL}" | tr -d '[:space:]')
  TOTAL=${TOTAL:-0}
  if [ "${TOTAL}" = "0" ]; then
    echo "No endpoints found."
    rm -rf "${INDEX_DIR}"
    exit 1
  fi
  echo ""
  printf "  %-3s  %-40s  %-18s  %-12s  %s\n" "No." "Display name" "Region" "State" "min/max"
  printf "  %-3s  %-40s  %-18s  %-12s  %s\n" "---" "----------------------------------------" "------------------" "------------" "-------"
  i=1
  while [ "${i}" -le "${TOTAL}" ]; do
    F="${INDEX_DIR}/ep_${i}"
    printf "  %-3s  %-40s  %-18s  %-12s  %s/%s\n" \
      "${i}" "$(sed -n '3p' "${F}" | cut -c1-40)" "$(sed -n '2p' "${F}")" "$(sed -n '5p' "${F}")" "$(sed -n '6p' "${F}")" "$(sed -n '7p' "${F}")"
    i=$((i + 1))
  done
  echo ""
  printf "Select endpoint [1-%s] (q quit): " "${TOTAL}"
  read -r CHOICE
  case "${CHOICE}" in q|Q) rm -rf "${INDEX_DIR}"; exit 0 ;; esac
  case "${CHOICE}" in ''|*[!0-9]*) echo "Invalid."; rm -rf "${INDEX_DIR}"; exit 1 ;; esac
  [ "${CHOICE}" -ge 1 ] && [ "${CHOICE}" -le "${TOTAL}" ] || { echo "Out of range."; rm -rf "${INDEX_DIR}"; exit 1; }
  F="${INDEX_DIR}/ep_${CHOICE}"
  ENDPOINT_ID=$(sed -n '1p' "${F}")
  REGION=$(sed -n '2p' "${F}")
  DISP=$(sed -n '3p' "${F}")
  rm -rf "${INDEX_DIR}"
  echo ""
  echo "Selected: ${DISP}"
  echo "  endpoint_id: ${ENDPOINT_ID}"
  echo "  region:      ${REGION}"
}

case "${MODE}" in
  --quick)
    [ -z "${ENDPOINT_ID}" ] || [ -z "${REGION}" ] && echo "Set ENDPOINT_ID and REGION." && exit 1
    run_raw_predict "${ENDPOINT_ID}" "${REGION}" "rawPredict (quick)" "$(predict_body_quick)" "${TEST_TIMEOUT:-120}"
    ;;
  --generate)
    [ -z "${ENDPOINT_ID}" ] || [ -z "${REGION}" ] && echo "Set ENDPOINT_ID and REGION." && exit 1
    run_raw_predict "${ENDPOINT_ID}" "${REGION}" "rawPredict (short generation)" "$(predict_body_generate)" "${TEST_TIMEOUT:-180}"
    ;;
  --stream)
    [ -z "${ENDPOINT_ID}" ] || [ -z "${REGION}" ] && echo "Set ENDPOINT_ID and REGION." && exit 1
    run_stream_predict "${ENDPOINT_ID}" "${REGION}" "${STREAM_MAX_TOKENS:-48}" "${TEST_TIMEOUT:-240}"
    ;;
  --menu|--interactive|*)
    pick_from_scan
    echo ""
    echo "Pick test:"
    echo "  1) quick rawPredict (low tokens)"
    echo "  2) short generation rawPredict"
    echo "  3) streamRawPredict"
    printf "Choice [1-3]: "
    read -r TC
    case "${TC}" in
      1) run_raw_predict "${ENDPOINT_ID}" "${REGION}" "rawPredict (quick)" "$(predict_body_quick)" "${TEST_TIMEOUT:-120}" ;;
      2) run_raw_predict "${ENDPOINT_ID}" "${REGION}" "rawPredict (short generation)" "$(predict_body_generate)" "${TEST_TIMEOUT:-180}" ;;
      3) run_stream_predict "${ENDPOINT_ID}" "${REGION}" "${STREAM_MAX_TOKENS:-48}" "${TEST_TIMEOUT:-240}" ;;
      *) echo "Cancelled."; exit 1 ;;
    esac
    ;;
esac

echo ""
echo "Done."
