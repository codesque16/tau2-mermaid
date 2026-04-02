#!/usr/bin/env bash
# Copy new/modified tau3-bench files into another tree, preserving paths.
#
# Usage (from repo root):
#   ./scripts/copy_tau3_bench_to_nech_fork.sh
#   DRY_RUN=1 ./scripts/copy_tau3_bench_to_nech_fork.sh
#   DEST=tau3-bench-fork ./scripts/copy_tau3_bench_to_nech_fork.sh   # if you meant bench-fork
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${SRC:-$ROOT/tau3-bench}"
DEST="${DEST:-$ROOT/tau3-bench-fork}"

# Paths relative to tau3-bench/ (union of the two change lists you shared).
FILES=(
  .env.example
  pyproject.toml
  uv.lock
  docs/getting-started.md
  data/tau2/domains/retail/policy_solo.md
  examples/retail_vertex_text.yaml
  src/tau2/cli.py
  src/tau2/config_cli.py
  src/tau2/config.py
  src/tau2/registry.py
  src/tau2/data_model/simulation.py
  src/tau2/agent/vertex_agent.py
  src/tau2/domains/retail/environment.py
  src/tau2/domains/retail/utils.py
  src/tau2/evaluator/auth_classifier.py
  src/tau2/evaluator/evaluator_nl_assertions.py
  src/tau2/evaluator/evaluator.py
  src/tau2/evaluator/hallucination_reviewer.py
  src/tau2/evaluator/review_llm_judge_user_only.py
  src/tau2/evaluator/review_llm_judge.py
  src/tau2/metrics/agent_metrics.py
  src/tau2/orchestrator/orchestrator.py
  src/tau2/runner/batch.py
  src/tau2/runner/build.py
  src/tau2/runner/simulation.py
  src/tau2/runner/tracing.py
  src/tau2/user/user_simulator_base.py
  src/tau2/user/vertex_user_simulator.py
  src/tau2/utils/__init__.py
  src/tau2/utils/genai_logfire.py
  src/tau2/utils/llm_utils.py
  src/tau2/utils/vertex_content_replay.py
)

if [[ ! -d "$SRC" ]]; then
  echo "error: source directory not found: $SRC" >&2
  exit 1
fi

for rel in "${FILES[@]}"; do
  if [[ ! -e "$SRC/$rel" ]]; then
    echo "warning: missing in source, skipping: $rel" >&2
  fi
done

for rel in "${FILES[@]}"; do
  from="$SRC/$rel"
  to="$DEST/$rel"
  [[ -e "$from" ]] || continue
  if [[ -n "${DRY_RUN:-}" ]]; then
    echo "would copy: $rel"
    continue
  fi
  mkdir -p "$(dirname "$to")"
  cp -p "$from" "$to"
  echo "copied: $rel"
done

if [[ -n "${DRY_RUN:-}" ]]; then
  echo "(dry run; DEST=$DEST)"
fi
