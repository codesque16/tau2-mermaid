#!/usr/bin/env bash
# =============================================================
# vertex_endpoint_verify.sh
#
# 1) GET  v1beta1 .../endpoints/{id} — full dedicatedResources
#    (min/max replicas, scaleToZeroSpec) via shared aiplatform host.
# 2) POST inference check — same wire format as tau2 ``build_vertex_predict_url``:
#    https://{endpoint_id}.{region}-{project_number}.prediction.vertexai.goog
#    +  /v1/projects/{project_number}/locations/{region}/endpoints/{id}:predict
#    +  instances[0].@requestFormat=chatCompletions  (see gemini_solo_batch_qwen_vertex.yaml)
#
# Model Garden also returns a *numeric* dedicated hostname in some 400 errors;
# we try that after the mg-endpoint-style URL. Shared *-aiplatform.googleapis.com
# predict is rejected for dedicated endpoints.
#
# Env: DEDICATED_PREDICT_HOST=host  (no https://)
#      VERTEX_PREDICT_PROJECT=53845524870  (path segment; defaults to project number)
#      VERTEX_PROJECT_NUMBER=53845524870   (subdomain; default from gcloud describe)
#
# Usage:
#   ./vertex_endpoint_verify.sh ENDPOINT_ID REGION
#   DEDICATED_PREDICT_HOST='8326....us-central1-53845524870.prediction.vertexai.goog' \
#     ./vertex_endpoint_verify.sh --predict-only ENDPOINT_ID REGION
#
#   ./vertex_endpoint_verify.sh --get-only ENDPOINT_ID REGION
#   ./vertex_endpoint_verify.sh --predict-only ENDPOINT_ID REGION
#
# If rawPredict returns 400 (wrong body shape), try:
#   VERTEX_USE_CHAT_COMPLETIONS=1 ./vertex_endpoint_verify.sh ...
#
# zsh: quote --format values with [brackets]; this script needs no brackets.
# =============================================================
set -eu

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
PREDICT_TIMEOUT="${PREDICT_TIMEOUT:-120}"
# v1 is what tau2 build_vertex_predict_url uses by default for :predict
PREDICT_API_VERSION="${PREDICT_API_VERSION:-v1beta1}"

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

MODE="both"
if [ "${1:-}" = "--get-only" ]; then MODE="get"; shift; fi
if [ "${1:-}" = "--predict-only" ]; then MODE="predict"; shift; fi

ENDPOINT_ID="${1:-}"
REGION="${2:-}"

if [ -z "${ENDPOINT_ID}" ] || [ -z "${REGION}" ]; then
  echo "Usage: $0 [--get-only|--predict-only] ENDPOINT_ID REGION"
  echo "Example (Qwen):  $0 mg-endpoint-b9895d67-2e18-417d-ad70-a68e58f25dd9 us-central1"
  echo "Example (Gemma): $0 mg-endpoint-6cf1c18e-4fec-477e-add1-3022a520a5ab asia-southeast1"
  exit 1
fi

SHARED_BASE="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}"
GET_URL="${SHARED_BASE}/endpoints/${ENDPOINT_ID}"
PREDICT_URL_SHARED="${SHARED_BASE}/endpoints/${ENDPOINT_ID}:rawPredict"

echo ""
echo "Project (REST describe): ${PROJECT_ID}"
echo "Region:                ${REGION}"
echo "Endpoint:              ${ENDPOINT_ID}"

TOKEN=$(gcloud auth print-access-token)
[ -z "${TOKEN}" ] && echo "ERROR: gcloud auth login" && exit 1

gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

# tau2 YAML uses project *number* in :predict paths (e.g. projects/53845524870/...).
if echo "${PROJECT_ID}" | grep -Eq '^[0-9]+$'; then
  PROJECT_NUMBER="${VERTEX_PROJECT_NUMBER:-${PROJECT_ID}}"
else
  PROJECT_NUMBER="${VERTEX_PROJECT_NUMBER:-$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || echo '')}"
fi
PREDICT_PROJECT="${VERTEX_PREDICT_PROJECT:-${PROJECT_NUMBER:-$PROJECT_ID}}"
echo "Predict path project:    ${PREDICT_PROJECT}  (matches vertex_project in YAML / tau2)"
echo ""

DED_HOST_FILE=$(mktmp vtx_dedhost)
: > "${DED_HOST_FILE}"

# ── 1) GET endpoint (v1beta1) ───────────────────────────────
if [ "${MODE}" = "both" ] || [ "${MODE}" = "get" ]; then
  echo "═══════════════════════════════════════════════════════════"
  echo "  GET (v1beta1) — dedicatedResources + scaleToZeroSpec"
  echo "  ${GET_URL}"
  echo "═══════════════════════════════════════════════════════════"
  GET_OUT=$(mktmp vtx_get)
  CODE=$(curl --silent --output "${GET_OUT}" --write-out "%{http_code}" \
    --max-time 30 \
    -H "Authorization: Bearer ${TOKEN}" \
    "${GET_URL}" 2>/dev/null) || CODE="000"
  echo "  HTTP ${CODE}"
  echo ""
  if [ "${CODE}" != "200" ]; then
    head -c 2000 "${GET_OUT}" | tr '\n' ' '
    echo ""
    rm -f "${GET_OUT}"
    [ "${MODE}" = "get" ] && exit 1
  else
    python3 - "${GET_OUT}" "${REGION}" "${DED_HOST_FILE}" << 'PY'
import json, re, sys

path, region, host_out = sys.argv[1], sys.argv[2], sys.argv[3]

def find_dedicated_host(ep) -> str:
    text = json.dumps(ep)
    # Same shape as vertex_http_predict_base in YAML (mg-endpoint-... subdomain).
    m = re.search(
        r"(mg-endpoint-[a-f0-9-]{20,}\.[a-z0-9-]+-\d+\.prediction\.vertexai\.goog)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    cands = re.findall(
        r"(\d+\.[a-z0-9-]+-\d+\.prediction\.vertexai\.goog)",
        text,
        flags=re.IGNORECASE,
    )
    # Reject false positives like "9.us-central1-53845524870..." (need long numeric GW id).
    cands = [c for c in cands if (c.split(".", 1)[0].isdigit() and len(c.split(".", 1)[0]) >= 10)]
    for c in cands:
        if f".{region}-" in c:
            return c
    return cands[0] if cands else ""

with open(path) as f:
    ep = json.load(f)
models = ep.get("deployedModels") or []
if not models:
    print("  (no deployedModels)")
    open(host_out, "w").write(find_dedicated_host(ep))
    sys.exit(0)
m0 = models[0]
dr = m0.get("dedicatedResources") or {}
print("  deployedModels[0].id:", m0.get("id"))
print("  deployedModels[0].displayName:", m0.get("displayName"))
print("  deployedModels[0].state:", m0.get("state"))
st = m0.get("status") or {}
if st:
    print("  deployedModels[0].status:", json.dumps(st, indent=4).replace("\n", "\n  "))
    msg = (st.get("message") or "").lower()
    if "never became ready" in msg:
        print("")
        print("  WARNING: Vertex reports a model-server readiness problem (not just scale-to-zero).")
        print("           Open the Logs URL in the status message above, or redeploy the model.")
print("")
print("  dedicatedResources:")
print(json.dumps(dr, indent=4).replace("\n", "\n  "))
h = find_dedicated_host(ep)
open(host_out, "w").write(h)
PY
    rm -f "${GET_OUT}"
  fi
  echo ""
fi

# ── Resolve dedicated prediction host ──────────────────────
DED_HOST="${DEDICATED_PREDICT_HOST:-}"
if [ -z "${DED_HOST}" ] && [ -s "${DED_HOST_FILE}" ]; then
  DED_HOST=$(tr -d '\n' < "${DED_HOST_FILE}")
fi
rm -f "${DED_HOST_FILE}"

# ── 2) POST predict / rawPredict ───────────────────────────
if [ "${MODE}" = "both" ] || [ "${MODE}" = "predict" ]; then
  # tau2 default: chatCompletions on :predict (vertex_endpoint_chat.build_vertex_predict_body)
  BODY_TAU2='{"instances":[{"@requestFormat":"chatCompletions","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8,"temperature":0}]}'
  if [ "${VERTEX_USE_CHAT_COMPLETIONS:-0}" = "1" ]; then
    BODY="${BODY_TAU2}"
  else
    BODY='{"instances":[{"messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8}]}'
  fi

  extract_ded_host_from_json_file() {
    python3 - "$1" "${REGION}" << 'PYE'
import json, re, sys
raw = open(sys.argv[1]).read()
region = sys.argv[2]
try:
    d = json.loads(raw)
    msg = (d.get("error") or {}).get("message") or ""
except Exception:
    msg = raw
cands = re.findall(
    r"(\d+\.[a-z0-9-]+-\d+\.prediction\.vertexai\.goog)",
    msg,
    flags=re.IGNORECASE,
)
cands = [c for c in cands if (c.split(".", 1)[0].isdigit() and len(c.split(".", 1)[0]) >= 10)]
for c in cands:
    if f".{region}-" in c:
        print(c, end="")
        raise SystemExit(0)
if cands:
    print(cands[0], end="")
PYE
  }

  # Last HTTP code from run_one_predict (bash global; not local).
  LAST_PRED_HTTP="000"

  run_one_predict() {
    local URL="$1"
    local LABEL="$2"
    local PAYLOAD="${3:-$BODY}"
    echo "  → ${LABEL}"
    echo "     ${URL}"
    PR_OUT=$(mktmp vtx_pred)
    T0=$(python3 -c "import time; print(int(time.time()*1000))")
    local PCODE
    PCODE=$(curl --silent --output "${PR_OUT}" --write-out "%{http_code}" \
      -X POST "${URL}" \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      -H "Content-Type: application/json" \
      -d "${PAYLOAD}" \
      --max-time "${PREDICT_TIMEOUT}" 2>/dev/null) || PCODE="000"
    LAST_PRED_HTTP="${PCODE}"
    T1=$(python3 -c "import time; print(int(time.time()*1000))")
    MS=$((T1 - T0))
    echo "     HTTP ${PCODE}  (${MS} ms)"
    if [ "${PCODE}" = "200" ]; then
      echo "  Body (pretty, truncated):"
      python3 -m json.tool < "${PR_OUT}" 2>/dev/null | head -n 50 | sed 's/^/    /' || head -c 1500 "${PR_OUT}" | sed 's/^/    /'
      rm -f "${PR_OUT}"
      return 0
    fi
    head -c 1200 "${PR_OUT}" | tr '\n' ' ' | fold -s -w 72 | sed 's/^/     /'
    echo ""
    rm -f "${PR_OUT}"
    return 1
  }

  echo "═══════════════════════════════════════════════════════════"
  echo "  POST :predict / :rawPredict"
  echo "═══════════════════════════════════════════════════════════"

  OK=1
  # If mg-endpoint hostname returns a real HTTP status (e.g. 429), ignore the
  # numeric *.prediction.vertexai.goog host from shared-domain errors — it often
  # yields HTTP 000 (TLS/SNI) while the mg-endpoint host is the working vhost.
  MG_DEDICATE_RESPONDED=0

  # ── Primary: tau2 / YAML shape (mg-endpoint in subdomain + project number in path)
  if [ "${OK}" != "0" ] && [ -n "${PROJECT_NUMBER}" ] && [[ "${ENDPOINT_ID}" == mg-endpoint-* ]]; then
    TAU2_BASE="https://${ENDPOINT_ID}.${REGION}-${PROJECT_NUMBER}.prediction.vertexai.goog"
    TAU2_URL="${TAU2_BASE}/v1/projects/${PREDICT_PROJECT}/locations/${REGION}/endpoints/${ENDPOINT_ID}:predict"
    echo "  (tau2-style URL — vertex_http_predict_base + :predict)"
    if run_one_predict "${TAU2_URL}" "v1 :predict + chatCompletions" "${BODY_TAU2}"; then OK=0; fi
    case "${LAST_PRED_HTTP}" in 429|200|400|401|403|503) MG_DEDICATE_RESPONDED=1 ;; esac
  fi

  if [ "${OK}" != "0" ] && [ -n "${DED_HOST}" ]; then
    echo "  Dedicated host (from GET JSON or DEDICATED_PREDICT_HOST): ${DED_HOST}"
    PURL="https://${DED_HOST}/v1/projects/${PREDICT_PROJECT}/locations/${REGION}/endpoints/${ENDPOINT_ID}:predict"
    if run_one_predict "${PURL}" "v1 :predict on dedicated host (PREDICT_PROJECT path)" "${BODY_TAU2}"; then OK=0; fi
    case "${LAST_PRED_HTTP}" in 429|200|400|401|403|503) MG_DEDICATE_RESPONDED=1 ;; esac
  fi

  if [ "${OK}" != "0" ] && [ -n "${DED_HOST}" ]; then
    PURL="https://${DED_HOST}/${PREDICT_API_VERSION}/projects/${PREDICT_PROJECT}/locations/${REGION}/endpoints/${ENDPOINT_ID}:rawPredict"
    if run_one_predict "${PURL}" "${PREDICT_API_VERSION} :rawPredict (same dedicated host)"; then OK=0; fi
  fi

  if [ "${OK}" != "0" ]; then
    echo "  → shared regional host (dedicated endpoints often return 400 here)"
    echo "     ${PREDICT_URL_SHARED}"
    SH_OUT=$(mktmp vtx_sh)
    T0=$(python3 -c "import time; print(int(time.time()*1000))")
    SH_CODE=$(curl --silent --output "${SH_OUT}" --write-out "%{http_code}" \
      -X POST "${PREDICT_URL_SHARED}" \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      -H "Content-Type: application/json" \
      -d "${BODY}" \
      --max-time "${PREDICT_TIMEOUT}" 2>/dev/null) || SH_CODE="000"
    T1=$(python3 -c "import time; print(int(time.time()*1000))")
    MS=$((T1 - T0))
    echo "     HTTP ${SH_CODE}  (${MS} ms)"
    if [ "${SH_CODE}" != "200" ]; then
      head -c 1200 "${SH_OUT}" | tr '\n' ' ' | fold -s -w 72 | sed 's/^/     /'
      echo ""
    fi
    ALT=$(extract_ded_host_from_json_file "${SH_OUT}")
    rm -f "${SH_OUT}"
    if [ -n "${ALT}" ]; then
      if [ "${MG_DEDICATE_RESPONDED}" = "1" ] && [[ "${ENDPOINT_ID}" == mg-endpoint-* ]]; then
        echo "  (Skipping numeric host from shared-domain error — mg-endpoint URL already returned HTTP ${LAST_PRED_HTTP}.)"
      else
        echo "  Extracted dedicated host from error: ${ALT}"
        DED_HOST="${ALT}"
      fi
    fi
    if [ "${SH_CODE}" = "200" ]; then OK=0; fi
  fi

  if [ "${OK}" != "0" ] && [ -n "${DED_HOST}" ] && [ "${MG_DEDICATE_RESPONDED}" != "1" ]; then
    PURL_NP="https://${DED_HOST}/v1/projects/${PREDICT_PROJECT}/locations/${REGION}/endpoints/${ENDPOINT_ID}:predict"
    if run_one_predict "${PURL_NP}" "v1 :predict on dedicated host (after shared 400)" "${BODY_TAU2}"; then OK=0; fi
  fi

  if [ "${OK}" != "0" ] && [ -n "${DED_HOST}" ] && [ "${MG_DEDICATE_RESPONDED}" != "1" ]; then
    PURL_V1R="https://${DED_HOST}/v1/projects/${PREDICT_PROJECT}/locations/${REGION}/endpoints/${ENDPOINT_ID}:rawPredict"
    if run_one_predict "${PURL_V1R}" "v1 :rawPredict on dedicated host"; then OK=0; fi
  fi

  if [ "${OK}" != "0" ]; then
    echo ""
    echo "  Still failing — check PREDICT_PROJECT matches YAML vertex_project (often project number)."
    echo "    VERTEX_PREDICT_PROJECT=53845524870 $0 --predict-only ${ENDPOINT_ID} ${REGION}"
    echo "  Optional numeric host (only if mg-endpoint URL never returns HTTP; often gives 000 locally):"
    echo "    DEDICATED_PREDICT_HOST='8326....${REGION}-PROJECT_NUMBER.prediction.vertexai.goog' \\"
    echo "      $0 --predict-only ${ENDPOINT_ID} ${REGION}"
    echo "  If GET showed \"model server never became ready\", fix deploy/logs first; 429 may persist until healthy."
    echo "  HTTP 000: DNS/TLS/firewall to *.prediction.vertexai.goog — try: curl -v \"https://${ENDPOINT_ID}.${REGION}-\${N}.prediction.vertexai.goog/\""
    echo ""
  fi

  echo ""
  echo "  Codes: 200=OK  429=often scaled-to-zero  503=loading  000=TLS/DNS/timeout"
  echo ""
fi
