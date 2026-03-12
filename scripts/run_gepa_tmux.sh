#!/usr/bin/env bash
# Run GEPA optimization with a tmux split: left = simulation run, right = GEPA logs only.
# Usage: ./scripts/run_gepa_tmux.sh [CONFIG]
#   CONFIG defaults to configs/gepa_retail.yaml
# Requires: tmux (install with: brew install tmux), run from project root

set -e
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${1:-configs/gepa_retail.yaml}"
LOG_FILE="${GEPA_LOG_FILE:-/tmp/gepa_retail_$$.log}"

cd "$REPO_ROOT"

if ! command -v tmux &>/dev/null; then
  echo "tmux not found. Install with: brew install tmux"
  echo ""
  echo "Or run without tmux (use two terminals):"
  echo "  Terminal 1: uv run python -m domains.retail.run_gepa_optimize --config $CONFIG --gepa-log-file /tmp/gepa_retail.log"
  echo "  Terminal 2: tail -f /tmp/gepa_retail.log"
  exit 1
fi

export GEPA_LOG_FILE="$LOG_FILE"

# Create log file so tail -f in the right pane has something to attach to immediately
touch "$LOG_FILE"

# Left pane: run optimizer (GEPA logs go to file, so stdout is mainly simulation)
# Right pane: tail -f GEPA log file
tmux new-session -d -s gepa-retail \
  "uv run python -m domains.retail.run_gepa_optimize --config $CONFIG --gepa-log-file $LOG_FILE; exec bash"
tmux split-window -h -t gepa-retail "tail -f $LOG_FILE; exec bash"
tmux select-pane -t gepa-retail:0.0
tmux attach-session -t gepa-retail
