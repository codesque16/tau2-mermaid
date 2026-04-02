#!/usr/bin/env bash
# Compare git status and working-tree bytes between upstream τ²-bench and tau3-bench-fork.
# Clone upstream separately (this repo no longer vendors it as a submodule), then:
#   A=/path/to/tau2-bench ./scripts/compare_tau3_bench_fork.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
A="${A:-}"
B="${B:-$ROOT/tau3-bench-fork}"

if [[ -z "$A" ]]; then
  echo "error: set A to a checkout of github.com/sierra-research/tau2-bench (submodule removed from tau2-mermaid)." >&2
  echo "  example: git clone git@github.com:sierra-research/tau2-bench.git /tmp/tau2-bench && A=/tmp/tau2-bench $0" >&2
  exit 1
fi

for d in "$A" "$B"; do
  if ! git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
    echo "error: not a git repo: $d" >&2
    exit 1
  fi
done

tmpa=$(mktemp)
tmpb=$(mktemp)
trap 'rm -f "$tmpa" "$tmpb"' EXIT

(cd "$A" && git status --porcelain=v1 | sort) >"$tmpa"
(cd "$B" && git status --porcelain=v1 | sort) >"$tmpb"

echo "=== 1. git status --porcelain (sorted) ==="
if diff -q "$tmpa" "$tmpb" >/dev/null; then
  echo "MATCH"
else
  echo "DIFFER:"
  diff "$tmpa" "$tmpb" || true
fi

echo
echo "=== 2. Working-tree bytes for paths in either status ==="
paths=$(
  { (cd "$A" && git status --porcelain=v1); (cd "$B" && git status --porcelain=v1); } |
    sed 's/^...//' |
    awk '{ print $NF }' |
    sort -u
)
bad=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if [[ ! -e "$A/$f" || ! -e "$B/$f" ]]; then
    echo "MISSING on one side: $f"
    bad=1
    continue
  fi
  if ! cmp -s "$A/$f" "$B/$f"; then
    echo "DIFFERS: $f"
    bad=1
  fi
done <<<"$paths"

if [[ "$bad" -eq 0 ]]; then
  echo "All $(grep -c . <<<"$paths" || echo 0) paths: byte-identical (and both exist)."
fi
