#!/usr/bin/env bash
# =============================================================
# check_vertex_endpoints.sh
#
# Answer: "Is my Vertex AI endpoint up, configured, and accepting
# traffic?" — scans all regions, prints deploy state, optional
# live :rawPredict probe (warm vs scaled-to-zero vs deploying).
#
# macOS bash 3.2 compatible. BSD mktemp safe (Xs last in template).
#
# Usage:
#   bash check_vertex_endpoints.sh              # config table only
#   bash check_vertex_endpoints.sh --probe      # + live HTTP probe per endpoint
#   PROBE_TIMEOUT=30 bash check_vertex_endpoints.sh --probe
#   PROJECT_ID=my-proj bash check_vertex_endpoints.sh --probe
#
# Reading the probe:
#   HTTP 200 — model served a reply (endpoint warm / replicas > 0)
#   HTTP 429 — common when scaled to zero (request dropped; scale-up queued)
#   HTTP 503 — model loading or deploy in progress
#   HTTP 000 — timeout / network (raise PROBE_TIMEOUT if cold start)
# =============================================================
set -eu

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-120}"
DO_PROBE="0"
case "${1:-}" in --probe) DO_PROBE="1" ;; "") ;; *)
  echo "Usage: $0 [--probe]"
  exit 1
  ;;
esac

ALL_REGIONS="africa-south1 northamerica-northeast1 northamerica-northeast2 southamerica-east1 southamerica-west1 us-central1 us-east1 us-east4 us-east5 us-south1 us-west1 us-west2 us-west3 us-west4 us-west8 asia-east1 asia-east2 asia-northeast1 asia-northeast2 asia-northeast3 asia-south1 asia-south2 asia-southeast1 asia-southeast2 australia-southeast1 australia-southeast2 europe-central2 europe-north1 europe-north2 europe-southwest1 europe-west1 europe-west2 europe-west3 europe-west4 europe-west6 europe-west8 europe-west9 europe-west12 europe-west15 me-central1 me-central2 me-west1"

mktmp() { mktemp "/tmp/${1}_XXXXXX"; }

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "  Vertex AI endpoint check  —  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  Project: ${PROJECT_ID}"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

echo "▶ Access token..."
TOKEN=$(gcloud auth print-access-token)
[ -z "${TOKEN}" ] && echo "ERROR: gcloud auth login first" && exit 1
echo "  OK"
gcloud config set project "${PROJECT_ID}" --quiet 2>/dev/null || true
echo ""

echo "▶ Scanning regions in parallel..."
SCAN_DIR=$(mktemp -d /tmp/vtx_chk_scan_XXXXXX)

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

INDEX_DIR=$(mktemp -d /tmp/vtx_chk_idx_XXXXXX)
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
    dm_id = m0.get("id", "")
    state = m0.get("state", "UNKNOWN")
    dr = m0.get("dedicatedResources", {})
    min_r = str(dr.get("minReplicaCount", "?"))
    max_r = str(dr.get("maxReplicaCount", "?"))
    s2z = dr.get("scaleToZeroSpec", {})
    idle = s2z.get("idleScaledownPeriod", "not-set")
    msup = s2z.get("minScaleupPeriod", "not-set")
    path = ep.get("name", "")
    with open(os.path.join(idx_dir, f"ep_{i}"), "w") as out:
        out.write(ep_id + "\n")
        out.write(region + "\n")
        out.write(dm_id + "\n")
        out.write(name + "\n")
        out.write(model + "\n")
        out.write(state + "\n")
        out.write(min_r + "\n")
        out.write(max_r + "\n")
        out.write(idle + "\n")
        out.write(msup + "\n")
        out.write(path + "\n")
PYEOF
done

rm -rf "${SCAN_DIR}"

# ListEndpoints often omits dedicatedResources / model state. One GET per
# endpoint fills min/max replicas, scale-to-zero, and real deployment state.
echo "▶ Enriching each endpoint (GET detail)..."
export ENRICH_INDEX_DIR="${INDEX_DIR}"
export ENRICH_PROJECT="${PROJECT_ID}"
export ENRICH_TOKEN="${TOKEN}"
python3 << 'PYENRICH'
import glob
import json
import os
import urllib.error
import urllib.request

idx = os.environ["ENRICH_INDEX_DIR"]
project = os.environ["ENRICH_PROJECT"]
token = os.environ["ENRICH_TOKEN"]


def get_json(url: str):
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp)


def deployed_model_state(m0: dict) -> str:
    if not m0:
        return "no-model"
    st = m0.get("state")
    if st:
        return str(st)
    # Some API versions expose status.message or legacy fields
    status = m0.get("status")
    if isinstance(status, dict):
        for k in ("state", "code", "message"):
            v = status.get(k)
            if v:
                return str(v)
    return "UNKNOWN"


for path in sorted(glob.glob(os.path.join(idx, "ep_*"))):
    with open(path) as f:
        lines = [ln.rstrip("\n") for ln in f.readlines()]
    while len(lines) < 11:
        lines.append("")
    ep_id, region = lines[0], lines[1]
    if not ep_id or not region:
        continue
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1beta1/"
        f"projects/{project}/locations/{region}/endpoints/{ep_id}"
    )
    try:
        d = get_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        continue

    models = d.get("deployedModels") or []
    m0 = models[0] if models else {}
    dr = m0.get("dedicatedResources") or {}
    s2z = dr.get("scaleToZeroSpec") or {}

    lines[2] = str(m0.get("id", lines[2] or ""))
    lines[3] = str(d.get("displayName", lines[3] or "unnamed"))
    lines[4] = str(m0.get("displayName", lines[4] or "no-model"))
    lines[5] = deployed_model_state(m0)

    mn = dr.get("minReplicaCount")
    mx = dr.get("maxReplicaCount")
    lines[6] = str(mn) if mn is not None else (lines[6] or "?")
    lines[7] = str(mx) if mx is not None else (lines[7] or "?")

    idle = s2z.get("idleScaledownPeriod")
    msup = s2z.get("minScaleupPeriod")
    lines[8] = str(idle) if idle else (lines[8] or "not-set")
    lines[9] = str(msup) if msup else (lines[9] or "not-set")
    lines[10] = str(d.get("name", lines[10] or ""))

    with open(path, "w") as out:
        out.write("\n".join(lines[:11]) + "\n")
PYENRICH
echo "  Done."
echo ""

TOTAL=$(ls "${INDEX_DIR}" 2>/dev/null | grep -c '^ep_' || true)
TOTAL=$(echo "${TOTAL}" | tr -d '[:space:]')
TOTAL=${TOTAL:-0}

if [ "${TOTAL}" = "0" ]; then
  echo "  No endpoints found in any region."
  rm -rf "${INDEX_DIR}"
  exit 0
fi

echo "  Found ${TOTAL} endpoint(s)."
echo ""
printf "  %-3s  %-38s  %-18s  %-12s  %5s/%5s  %-10s  %s\n" \
  "No." "Display name" "Region" "State" "min" "max" "idle↓" "min↑"
printf "  %-3s  %-38s  %-18s  %-12s  %5s/%5s  %-10s  %s\n" \
  "---" "--------------------------------------" "------------------" "------------" "-----" "-----" "----------" "----"

i=1
while [ "${i}" -le "${TOTAL}" ]; do
  F="${INDEX_DIR}/ep_${i}"
  EP_ID=$(sed -n '1p' "${F}")
  REG=$(sed -n '2p' "${F}")
  DISP=$(sed -n '4p' "${F}")
  ST=$(sed -n '6p' "${F}")
  MN=$(sed -n '7p' "${F}")
  MX=$(sed -n '8p' "${F}")
  IDL=$(sed -n '9p' "${F}")
  MSU=$(sed -n '10p' "${F}")
  SD="${DISP}"
  [ "${#DISP}" -gt 38 ] && SD="${DISP:0:35}..."
  printf "  %-3s  %-38s  %-18s  %-12s  %5s/%5s  %-10s  %s\n" \
    "${i}" "${SD}" "${REG}" "${ST}" "${MN}" "${MX}" "${IDL}" "${MSU}"
  i=$((i + 1))
done

if [ "${DO_PROBE}" = "1" ]; then
  echo ""
  echo "▶ Live probe (:rawPredict, max_tokens=1, timeout=${PROBE_TIMEOUT}s)"
  echo "   (Cold starts can exceed 60s; increase PROBE_TIMEOUT if needed.)"
  echo ""

  probe_one() {
    local IDX="$1"
    local F="${INDEX_DIR}/ep_${IDX}"
    local EP_ID REG DISP
    EP_ID=$(sed -n '1p' "${F}")
    REG=$(sed -n '2p' "${F}")
    DISP=$(sed -n '4p' "${F}")
    local URL="https://${REG}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REG}/endpoints/${EP_ID}:rawPredict"
    local BODY
    if [ "${VERTEX_USE_CHAT_COMPLETIONS:-0}" = "1" ]; then
      BODY='{"instances":[{"@requestFormat":"chatCompletions","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"temperature":0}]}'
    else
      BODY='{"instances":[{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}]}'
    fi
    local T0 T1 MS CODE RESP
    T0=$(python3 -c "import time; print(int(time.time()*1000))")
    RESP=$(mktmp vtx_probe_r)
    CODE=$(curl --silent --output "${RESP}" --write-out "%{http_code}" -X POST "${URL}" \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      -H "Content-Type: application/json" \
      -d "${BODY}" \
      --max-time "${PROBE_TIMEOUT}" 2>/dev/null) || CODE="000"
    T1=$(python3 -c "import time; print(int(time.time()*1000))")
    MS=$((T1 - T0))
    local MEANING="other"
    case "${CODE}" in
      200) MEANING="serving (warm or just became ready)" ;;
      429) MEANING="likely scaled to zero (scale-up may be in progress)" ;;
      503) MEANING="not ready (loading / deploying)" ;;
      400) MEANING="bad request — try VERTEX_USE_CHAT_COMPLETIONS=1 $0 --probe" ;;
      401|403) MEANING="auth / permission" ;;
      000) MEANING="timeout or connection error" ;;
    esac
    local SN="${DISP}"
    [ "${#DISP}" -gt 44 ] && SN="${DISP:0:41}..."
    echo "  [${IDX}] ${SN}"
    echo "       HTTP ${CODE}  ${MS} ms  — ${MEANING}"
    if [ "${CODE}" != "200" ] && [ -s "${RESP}" ]; then
      echo "       Body (first 400 chars):"
      head -c 400 "${RESP}" | tr '\n' ' ' | fold -s -w 76 | sed 's/^/         /'
      echo ""
    fi
    rm -f "${RESP}"
  }

  j=1
  while [ "${j}" -le "${TOTAL}" ]; do
    probe_one "${j}" &
    j=$((j + 1))
  done
  wait
fi

rm -rf "${INDEX_DIR}"
echo ""
echo "Done."
echo ""
echo "Tip: full metrics + replica time series → bash monitor_endpoints.sh"
echo "Tip: load test → bash benchmark_throughput.sh"
echo ""
