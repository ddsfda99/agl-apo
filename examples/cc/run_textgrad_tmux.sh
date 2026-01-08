#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <cases_file> [sleep_seconds=6] [runs_per_case=3] [session_name=textgrad_runs]" >&2
  exit 1
fi

CASES_FILE="$1"
SLEEP_SECONDS="${2:-6}"
RUNS_PER_CASE="${3:-3}"
SESSION_NAME="${4:-textgrad_runs}"

if [[ ! -f "$CASES_FILE" ]]; then
  echo "Cases file not found: $CASES_FILE" >&2
  exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "tmux session already exists: $SESSION_NAME" >&2
  exit 1
fi

WORKDIR="$(pwd)"
CMD=$(cat <<EOF
cd "$WORKDIR" || exit 1
while IFS= read -r case_path; do
  case_path="${case_path%%#*}"
  case_path="$(echo "$case_path" | xargs)"
  if [[ -z "$case_path" ]]; then
    continue
  fi
  for i in $(seq 1 "$RUNS_PER_CASE"); do
    echo "[textgrad] case=$case_path run=$i/$RUNS_PER_CASE"
    CC_SINGLE_CASE_PATH="$case_path" uv run examples/cc/textgrad.py
    sleep "$SLEEP_SECONDS"
  done
done < "$CASES_FILE"
EOF
)

tmux new-session -d -s "$SESSION_NAME"
tmux send-keys -t "$SESSION_NAME" "$CMD" C-m

echo "Started tmux session: $SESSION_NAME"
echo "Attach with: tmux attach -t $SESSION_NAME"
