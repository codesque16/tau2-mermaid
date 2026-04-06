#!/usr/bin/env bash
# =============================================================
# vertex_endpoint_warm.sh
#
# POST :predict (chatCompletions) on a dedicated Vertex endpoint on a fixed
# interval so scale-to-zero endpoints stay warm (e.g. every 120s vs 600s idle).
#
# Config-driven (YAML registry or tau2 runs file — same keys as agent_llm_args):
#   ./vertex_endpoint_warm.sh --config vertex_warm_endpoints.yaml RUN_OR_ENDPOINT_KEY [INTERVAL_SEC]
#   ./vertex_endpoint_warm.sh --config ../../tau3-bench-fork/examples/retail_vertex_text.yaml \
#     retail_solo_gemma4_31b_vertex_endpoint
#
# Legacy positional (no YAML):
#   ./vertex_endpoint_warm.sh ENDPOINT_ID REGION [INTERVAL_SEC]
#
# Registry YAML: see vertex_warm_endpoints.yaml (defaults + endpoints with optional ref:).
#
# Env overrides when not set in YAML: PROJECT_ID, VERTEX_PROJECT_NUMBER, VERTEX_PREDICT_PROJECT,
# PREDICT_TIMEOUT, WARM_INTERVAL_SEC (legacy mode only for interval).
# =============================================================
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
PREDICT_TIMEOUT="${PREDICT_TIMEOUT:-120}"
INTERVAL_SEC="${WARM_INTERVAL_SEC:-120}"
ENDPOINT_ID=""
REGION=""
PREDICT_URL=""
CONFIG_KEY=""

resolve_from_yaml() {
  local CONFIG_PATH="$1"
  local KEY="$2"
  local INTERVAL_ARG="${3:-}"
  local RESOLVER="${SCRIPT_DIR}/vertex_warm_resolve.py"
  [ -f "${RESOLVER}" ] || {
    echo "ERROR: missing ${RESOLVER}" >&2
    exit 1
  }
  # No "${EXTRA[@]}" when empty: with set -u, bash 5.1+ treats empty arrays as unbound.
  if command -v uv >/dev/null 2>&1 && [ -f "${REPO_ROOT}/pyproject.toml" ]; then
    if [ -n "${INTERVAL_ARG}" ]; then
      eval "$(uv run --directory "${REPO_ROOT}" python "${RESOLVER}" "${CONFIG_PATH}" "${KEY}" --interval-override "${INTERVAL_ARG}")"
    else
      eval "$(uv run --directory "${REPO_ROOT}" python "${RESOLVER}" "${CONFIG_PATH}" "${KEY}")"
    fi
  else
    if [ -n "${INTERVAL_ARG}" ]; then
      eval "$(python3 "${RESOLVER}" "${CONFIG_PATH}" "${KEY}" --interval-override "${INTERVAL_ARG}")"
    else
      eval "$(python3 "${RESOLVER}" "${CONFIG_PATH}" "${KEY}")"
    fi
  fi
}

if [ "${1:-}" = "--config" ]; then
  shift
  CONFIG_FILE="${1:-}"
  WARM_KEY="${2:-}"
  INTERVAL_OPT="${3:-}"
  if [ -z "${CONFIG_FILE}" ] || [ -z "${WARM_KEY}" ]; then
    echo "Usage: $0 --config CONFIG.yaml ENDPOINT_KEY [INTERVAL_SEC]" >&2
    echo "  Example: $0 --config vertex_warm_endpoints.yaml retail_solo_gemma4_31b_vertex_endpoint" >&2
    exit 1
  fi
  if [ -n "${INTERVAL_OPT}" ]; then
    case "${INTERVAL_OPT}" in
      *[!0-9]*)
        echo "ERROR: INTERVAL_SEC must be a positive integer, got: ${INTERVAL_OPT}" >&2
        exit 1
        ;;
    esac
    if [ "${INTERVAL_OPT}" -lt 10 ]; then
      echo "ERROR: interval must be >= 10 seconds" >&2
      exit 1
    fi
  fi
  if [[ "${CONFIG_FILE}" != /* ]]; then
    CONFIG_FILE="$(cd "${PWD}" && pwd)/${CONFIG_FILE}"
  fi
  [ -f "${CONFIG_FILE}" ] || {
    echo "ERROR: config not found: ${CONFIG_FILE}" >&2
    exit 1
  }
  resolve_from_yaml "${CONFIG_FILE}" "${WARM_KEY}" "${INTERVAL_OPT}"
  gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true
else
  ENDPOINT_ID="${1:-}"
  REGION="${2:-}"
  INTERVAL_SEC="${3:-${WARM_INTERVAL_SEC:-120}}"

  if [ -z "${ENDPOINT_ID}" ] || [ -z "${REGION}" ]; then
    echo "Usage: $0 --config CONFIG.yaml KEY [INTERVAL_SEC]" >&2
    echo "   or: $0 ENDPOINT_ID REGION [INTERVAL_SEC]" >&2
    exit 1
  fi

  case "${INTERVAL_SEC}" in
    *[!0-9]*) echo "ERROR: INTERVAL_SEC must be a positive integer, got: ${INTERVAL_SEC}" >&2; exit 1 ;;
  esac
  if [ "${INTERVAL_SEC}" -lt 10 ]; then
    echo "ERROR: interval must be >= 10 seconds" >&2
    exit 1
  fi

  gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true

  if echo "${PROJECT_ID}" | grep -Eq '^[0-9]+$'; then
    PROJECT_NUMBER="${VERTEX_PROJECT_NUMBER:-${PROJECT_ID}}"
  else
    PROJECT_NUMBER="${VERTEX_PROJECT_NUMBER:-$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || echo '')}"
  fi
  PREDICT_PROJECT="${VERTEX_PREDICT_PROJECT:-${PROJECT_NUMBER:-$PROJECT_ID}}"

  if [[ "${ENDPOINT_ID}" == mg-endpoint-* ]] && [ -n "${PROJECT_NUMBER}" ]; then
    BASE_HOST="${ENDPOINT_ID}.${REGION}-${PROJECT_NUMBER}.prediction.vertexai.goog"
  elif [ -n "${DEDICATED_PREDICT_HOST:-}" ]; then
    BASE_HOST="${DEDICATED_PREDICT_HOST}"
  else
    echo "ERROR: use an mg-endpoint-* id with resolvable project number, or set DEDICATED_PREDICT_HOST" >&2
    exit 1
  fi

  PREDICT_URL="https://${BASE_HOST}/v1/projects/${PREDICT_PROJECT}/locations/${REGION}/endpoints/${ENDPOINT_ID}:predict"
fi

# Minimal chatCompletions ping (same wire shape as vertex_endpoint_verify.sh / tau2 YAML).
BODY='{"instances":[{"@requestFormat":"chatCompletions","messages":[{"role":"user","content":"."}],"max_tokens":1,"temperature":0}]}'

echo "Warming endpoint every ${INTERVAL_SEC}s (Ctrl-C to stop)"
if [ -n "${CONFIG_KEY}" ]; then
  echo "  config key: ${CONFIG_KEY}"
fi
echo "  URL: ${PREDICT_URL}"
echo "  PREDICT_TIMEOUT=${PREDICT_TIMEOUT}s"
echo ""

trap 'echo ""; echo "Stopped."; exit 0' INT TERM

while true; do
  ERRF=$(mktemp "/tmp/vtx_warm_gcloud_err_XXXXXX")
  if TOKEN=$(gcloud auth print-access-token 2>"${ERRF}"); then
    rm -f "${ERRF}"
  else
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ERROR: gcloud auth print-access-token failed"
    if [ -s "${ERRF}" ]; then
      sed 's/^/  /' "${ERRF}" >&2
    else
      echo "  (no stderr; try: gcloud auth login  or  gcloud auth activate-service-account KEY.json)" >&2
    fi
    rm -f "${ERRF}"
    TOKEN=""
  fi
  if [ -z "${TOKEN}" ]; then
    sleep "${INTERVAL_SEC}"
    continue
  fi

  PR_OUT=$(mktemp "/tmp/vtx_warm_XXXXXX")
  T0=$(python3 -c "import time; print(int(time.time()*1000))")
  CODE=$(curl --silent --output "${PR_OUT}" --write-out "%{http_code}" \
    -X POST "${PREDICT_URL}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${BODY}" \
    --max-time "${PREDICT_TIMEOUT}" 2>/dev/null) || CODE="000"
  T1=$(python3 -c "import time; print(int(time.time()*1000))")
  MS=$((T1 - T0))

  TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  if [ "${CODE}" = "200" ]; then
    echo "${TS}  HTTP ${CODE}  ${MS}ms  ok"
  else
    MSG=$(head -c 500 "${PR_OUT}" | tr '\n' ' ')
    echo "${TS}  HTTP ${CODE}  ${MS}ms  ${MSG}"
  fi
  rm -f "${PR_OUT}"

  sleep "${INTERVAL_SEC}"
done
