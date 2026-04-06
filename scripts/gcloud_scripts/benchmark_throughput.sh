#!/usr/bin/env bash
# =============================================================
# benchmark_throughput.sh
#
# Measures real throughput for your Vertex AI endpoints:
#   - tokens/sec (output throughput)
#   - TTFT (time-to-first-token)
#   - e2e latency at 1, 4, 8 concurrent requests
#   - how to read and interpret results
#
# Requires: curl, python3, GNU parallel (optional for concurrency)
# Install parallel: sudo apt-get install parallel
# =============================================================
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-gemini-1xn}"
QWEN_REGION="us-central1"
GEMMA_REGION="asia-southeast1"

QWEN_ENDPOINT_ID="${QWEN_ENDPOINT_ID:-$(gcloud secrets versions access latest \
  --secret=qwen35-endpoint-id --project="${PROJECT_ID}" 2>/dev/null || echo '')}"
GEMMA_ENDPOINT_ID="${GEMMA_ENDPOINT_ID:-$(gcloud secrets versions access latest \
  --secret=gemma4-endpoint-id --project="${PROJECT_ID}" 2>/dev/null || echo '')}"

TOKEN=$(gcloud auth print-access-token)

# ── Single-request benchmark ──────────────────────────────────
# Measures TTFT and total latency for a single streaming request
benchmark_single() {
  local NAME="$1"
  local ENDPOINT_ID="$2"
  local REGION="$3"
  local OUTPUT_TOKENS="${4:-200}"
  local BASE_URL="https://${REGION}-aiplatform.googleapis.com"

  echo ""
  echo "── ${NAME} — single request (${OUTPUT_TOKENS} output tokens) ──"

  TMPFILE=$(mktemp)
  START_NS=$(date +%s%N)
  FIRST_TOKEN_NS=""

  # Stream and capture timing of first chunk
  curl -sf -X POST \
    "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${ENDPOINT_ID}:streamRawPredict" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{
      \"model\": \"benchmark\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Write a detailed explanation of how transformers work in machine learning. Be thorough.\"}],
      \"max_tokens\": ${OUTPUT_TOKENS},
      \"temperature\": 0,
      \"stream\": true
    }" \
    --no-buffer 2>/dev/null | while IFS= read -r line; do
      # Capture time of first non-empty SSE data line as TTFT proxy
      if [[ -z "${FIRST_TOKEN_NS:-}" ]] && [[ "${line}" == data:* ]] && [[ "${line}" != "data: [DONE]" ]]; then
        FIRST_TOKEN_NS=$(date +%s%N)
        echo "${FIRST_TOKEN_NS}" > /tmp/ttft_ns
      fi
      echo "${line}" >> "${TMPFILE}"
    done

  END_NS=$(date +%s%N)

  TTFT_NS=$(cat /tmp/ttft_ns 2>/dev/null || echo "${START_NS}")
  TTFT_MS=$(( (TTFT_NS - START_NS) / 1000000 ))
  E2E_MS=$(( (END_NS - START_NS) / 1000000 ))

  # Count output tokens from SSE chunks
  OUTPUT_COUNT=$(grep -c '^data:' "${TMPFILE}" 2>/dev/null || echo "0")
  # Rough token count from content length
  CONTENT_LEN=$(wc -c < "${TMPFILE}" 2>/dev/null || echo "0")
  EST_TOKENS=$(( CONTENT_LEN / 4 ))  # ~4 chars per token estimate

  TOKS_PER_SEC=0
  if (( E2E_MS > 0 && EST_TOKENS > 0 )); then
    TOKS_PER_SEC=$(python3 -c "print(round(${EST_TOKENS} / (${E2E_MS}/1000), 1))")
  fi

  echo "  TTFT              : ${TTFT_MS} ms"
  echo "  E2E latency       : ${E2E_MS} ms"
  echo "  Est. output tokens: ~${EST_TOKENS}"
  echo "  Throughput        : ~${TOKS_PER_SEC} tok/s"

  rm -f "${TMPFILE}" /tmp/ttft_ns
}

# ── Concurrent request benchmark ─────────────────────────────
benchmark_concurrent() {
  local NAME="$1"
  local ENDPOINT_ID="$2"
  local REGION="$3"
  local CONCURRENCY="$4"
  local BASE_URL="https://${REGION}-aiplatform.googleapis.com"

  echo ""
  echo "── ${NAME} — ${CONCURRENCY} concurrent requests ──"

  TMPDIR=$(mktemp -d)

  # Fire N requests simultaneously using background jobs
  for i in $(seq 1 "${CONCURRENCY}"); do
    (
      START=$(date +%s%N)
      HTTP=$(curl -sf -o "${TMPDIR}/resp_${i}.json" -w "%{http_code}" -X POST \
        "${BASE_URL}/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/${ENDPOINT_ID}:rawPredict" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{"instances":[{"messages":[{"role":"user","content":"Count from 1 to 50."}],"max_tokens":150}]}' \
        --max-time 120 2>/dev/null || echo "000")
      END=$(date +%s%N)
      MS=$(( (END - START) / 1000000 ))
      echo "${i} ${HTTP} ${MS}" > "${TMPDIR}/timing_${i}.txt"
    ) &
  done
  wait

  # Aggregate results
  echo "${TMPDIR}"/timing_*.txt | xargs cat 2>/dev/null | python3 -c "
import sys
lines = [l.strip().split() for l in sys.stdin if l.strip()]
latencies = [int(l[2]) for l in lines if len(l)==3 and l[1]=='200']
errors = [l for l in lines if len(l)==3 and l[1]!='200']
if latencies:
    latencies.sort()
    print(f'  successful        : {len(latencies)}/{len(lines)}')
    print(f'  p50 latency       : {latencies[len(latencies)//2]} ms')
    print(f'  p95 latency       : {latencies[min(int(len(latencies)*0.95),len(latencies)-1)]} ms')
    print(f'  max latency       : {latencies[-1]} ms')
    if errors:
        print(f'  errors (HTTP)     : {[(e[1]) for e in errors]}')
else:
    print(f'  all requests failed: {[l[1] for l in lines]}')
"
  rm -rf "${TMPDIR}"
}

# ── Run benchmarks ────────────────────────────────────────────
echo "======================================================="
echo "  LLM Throughput Benchmark"
echo "  Project: ${PROJECT_ID}"
echo "  $(date)"
echo "======================================================="
echo ""
echo "NOTE: endpoint must be warm (not scaled-to-zero) for accurate"
echo "results. Run ping_endpoints.sh --once first if needed."

if [[ -n "${QWEN_ENDPOINT_ID}" ]]; then
  echo ""
  echo "══ Qwen 3.5-9B (RTX Pro 6000, us-central1) ══"
  benchmark_single  "Qwen 3.5-9B" "${QWEN_ENDPOINT_ID}" "${QWEN_REGION}" 200
  benchmark_concurrent "Qwen 3.5-9B" "${QWEN_ENDPOINT_ID}" "${QWEN_REGION}" 4
  benchmark_concurrent "Qwen 3.5-9B" "${QWEN_ENDPOINT_ID}" "${QWEN_REGION}" 8
else
  echo "Qwen endpoint not configured (set QWEN_ENDPOINT_ID or ensure secret exists)"
fi

if [[ -n "${GEMMA_ENDPOINT_ID}" ]]; then
  echo ""
  echo "══ Gemma 4 31B (H100 80GB, asia-southeast1) ══"
  benchmark_single  "Gemma 4 31B" "${GEMMA_ENDPOINT_ID}" "${GEMMA_REGION}" 200
  benchmark_concurrent "Gemma 4 31B" "${GEMMA_ENDPOINT_ID}" "${GEMMA_REGION}" 4
  benchmark_concurrent "Gemma 4 31B" "${GEMMA_ENDPOINT_ID}" "${GEMMA_REGION}" 8
else
  echo "Gemma endpoint not configured (set GEMMA_ENDPOINT_ID or ensure secret exists)"
fi

echo ""
echo "======================================================="
echo "  HOW TO READ RESULTS"
echo "======================================================="
echo ""
echo "  TTFT (time-to-first-token)"
echo "    The latency users 'feel' in streaming apps."
echo "    Target: <500ms warm. Cold start (from zero): 3–6 min."
echo ""
echo "  Throughput (tok/s)"
echo "    Higher = more tokens generated per second per replica."
echo "    Qwen 3.5-9B on RTX Pro 6000 : expect 500–900 tok/s"
echo "    Gemma 4 31B on H100 80 GB   : expect 800–1200 tok/s"
echo ""
echo "  Concurrent p95 latency"
echo "    If p95 at concurrency=8 is <2× p95 at concurrency=1,"
echo "    the GPU is handling the batch well."
echo "    If p95 grows 5×+, you're hitting queuing — reduce"
echo "    concurrent users or increase max_replica_count."
echo ""
echo "  Max safe concurrency (vLLM continuous batching):"
echo "    Qwen 3.5-9B  : 30–60 sequences  (24 GB VRAM limit)"
echo "    Gemma 4 31B  : 40–80 sequences  (80 GB VRAM limit)"
echo "    Beyond these, vLLM will queue requests rather than OOM."
