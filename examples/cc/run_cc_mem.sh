#!/usr/bin/env bash
set -euo pipefail

# Claude CLI retry settings (can be overridden by env).
: "${CC_CLAUDE_MAX_RETRIES:=8}"
: "${CC_CLAUDE_RETRY_BASE_DELAY:=8}"
: "${CC_CLAUDE_RETRY_MAX_DELAY:=120}"
export CC_CLAUDE_MAX_RETRIES CC_CLAUDE_RETRY_BASE_DELAY CC_CLAUDE_RETRY_MAX_DELAY

# Rate limit retry settings for runner-level retries (0 = unlimited retries).
: "${CC_RATE_LIMIT_MAX_RETRIES:=0}"
: "${CC_RATE_LIMIT_BASE_WAIT:=15}"
: "${CC_RATE_LIMIT_MAX_WAIT:=300}"
export CC_RATE_LIMIT_MAX_RETRIES CC_RATE_LIMIT_BASE_WAIT CC_RATE_LIMIT_MAX_WAIT

SLEEP_SECONDS=3
CASES_JSON="whitelist_cases.jsonl"
BASE_CONFIG="agent_config.yaml"
CLAUDE_MD="CLAUDE.md"
LAST_LOG_ROOT=""

WORKDIR="$(pwd)"
if [[ "${WORKDIR##*/}" != "cc" ]]; then
  echo "Please run from examples/cc (current: $WORKDIR)" >&2
  exit 1
fi

if [[ ! -f "$CASES_JSON" ]]; then
  echo "Cases file not found: $CASES_JSON" >&2
  exit 1
fi

if [[ ! -f "$BASE_CONFIG" ]]; then
  echo "Base config not found: $BASE_CONFIG" >&2
  exit 1
fi

# Overwrite CLAUDE.md with required baseline instructions.
reset_claude_md() {
  cat > "$CLAUDE_MD" <<'EOF'
# Things Must Do
- Write a reproduction test to reproduce the bugs and to verify the implementations later.
- Make Sure you edit the source code to implement the task.
- Make Sure your patch can be applied correctly.
- Make Sure you use the reproduction test you have written before to verify your implementations. If failed, edit the code until you pass your reproduction test.
# Efficiency Tip
- When exploring, testing, or debugging, consider running multiple related commands in one response (e.g., ls + find + grep patterns, or run tests + check logs + verify config) for faster comprehensive analysis.
EOF
}

# Create temp directory for all case data files
DATA_DIR=$(mktemp -d)
trap 'rm -rf "$DATA_DIR"' EXIT

# Generate all case files at once and collect instance_ids
mapfile -t CASES < <(python3 - "$CASES_JSON" "$DATA_DIR" <<'PY'
import json
import sys
from pathlib import Path

cases_json = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
out_dir.mkdir(exist_ok=True)

for line in cases_json.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        item = json.loads(line)
    except Exception:
        continue
    instance_id = item.get("instance_id")
    if instance_id:
        # Write individual case file
        (out_dir / f"{instance_id}.jsonl").write_text(
            json.dumps(item, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )
        print(instance_id)
PY
)

if [[ "${#CASES[@]}" -eq 0 ]]; then
  echo "No cases found in $CASES_JSON" >&2
  exit 1
fi

# write case info into agent config
build_temp_config() {
  local tmp_case="$1" tmp_cfg="$2"
  BASE_CONFIG="$BASE_CONFIG" TMP_PATH="$tmp_cfg" TMP_CASE_JSONL="$tmp_case" python3 - <<'PY'
import os
import yaml
from pathlib import Path

base = Path(os.environ["BASE_CONFIG"]).read_text(encoding="utf-8")
data = yaml.safe_load(base)
tmp_path = os.environ["TMP_PATH"]
tmp_case = os.environ["TMP_CASE_JSONL"]
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

find_latest_trace() {
  local case_id="$1"
  python3 - "$case_id" <<'PY'
import sys
from pathlib import Path

case_id = sys.argv[1]
trace_dir = Path("trace")
if not trace_dir.exists():
    sys.exit(1)

candidates = [p for p in trace_dir.glob(f"{case_id}_outer_*.json") if p.is_file()]
if not candidates:
    sys.exit(1)
latest = max(candidates, key=lambda p: p.stat().st_mtime)
print(latest)
PY
}

find_patch_in_log() {
  local log_root="$1"
  python3 - "$log_root" <<'PY'
import sys
from pathlib import Path

log_root = Path(sys.argv[1])
if not log_root.exists():
    sys.exit(1)

candidates = [p for p in log_root.rglob("patch.diff") if p.is_file()]
if not candidates:
    sys.exit(1)

latest = max(candidates, key=lambda p: p.stat().st_mtime)
print(latest)
PY
}

run_cc_base() {
  local case_id="$1" iter_label="$2"
  local run_ts
  run_ts="$(date +%Y%m%d_%H%M%S)"
  local log_root="logs_${case_id}_${iter_label}_${run_ts}"
  local tmp_cfg
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"

  # Use pre-generated case file from DATA_DIR
  local tmp_case="$DATA_DIR/${case_id}.jsonl"
  build_temp_config "$tmp_case" "$tmp_cfg"

  echo "[cc_agent] case=$case_id iter=$iter_label ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_cfg"
  LAST_LOG_ROOT="$log_root"
}

extract_trace() {
  local case_id="$1"
  echo "[extract_trace] case=$case_id"

  local traj_path
  traj_path=$(python3 - "$case_id" <<'PY'
import sys
from pathlib import Path

case_id = sys.argv[1]
traj_dir = Path("traj")
if not traj_dir.exists():
    sys.exit(1)

candidates = [p for p in traj_dir.glob(f"{case_id}_*.json") if p.is_file()]
if candidates:
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    print(latest)
PY
)

  if [[ -z "$traj_path" ]]; then
    echo "[extract_trace] No traj found for $case_id"
    return 1
  fi

  local timestamp
  timestamp=$(python3 - "$traj_path" <<'PY'
import os
import re
import sys

path = sys.argv[1]
base = os.path.splitext(os.path.basename(path))[0]
match = re.search(r'_(\d{8}_\d{6})$', base)
if match:
    print(match.group(1))
PY
)
  if [[ -z "$timestamp" ]]; then
    timestamp="$(date +%Y%m%d_%H%M%S)"
  fi

  uv run extract_outer_content.py "$traj_path" -o "trace/${case_id}_outer_${timestamp}.json"
  echo "[extract_trace] Extracted trace to trace/${case_id}_outer_${timestamp}.json"

  local data_dir
  data_dir="data-${case_id}"
  if [[ -d "$data_dir" ]]; then
    echo "[extract_trace] Removing temporary data folder: $data_dir"
    rm -rf "$data_dir"
  fi
}

run_summary_to_memory() {
  local case_id="$1" log_root="$2"
  local trace_path
  trace_path="$(find_latest_trace "$case_id" || true)"
  if [[ -z "$trace_path" ]]; then
    echo "[summary_to_memory] no trace found for $case_id"
    return 1
  fi

  local patch_path
  patch_path="$(find_patch_in_log "$log_root" || true)"
  if [[ -z "$patch_path" ]]; then
    echo "[summary_to_memory] no patch found for $case_id in $log_root"
    return 1
  fi

  local summary_dir="summary"
  mkdir -p "$summary_dir"
  local summary_path="${summary_dir}/${case_id}_$(date +%Y%m%d_%H%M%S).txt"

  python3 - "$trace_path" "$summary_path" <<'PY'
import json
import sys
from pathlib import Path

trace_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
lines = trace_path.read_text(encoding="utf-8").splitlines()
if not lines:
    raise SystemExit(f"Empty trace file: {trace_path}")

last_line = ""
for line in reversed(lines):
    if line.strip():
        last_line = line.strip()
        break
if not last_line:
    raise SystemExit(f"No content in trace file: {trace_path}")

summary = ""
try:
    obj = json.loads(last_line)
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str):
            summary = obj["text"]
        elif isinstance(obj.get("content"), str):
            summary = obj["content"]
    elif isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                summary = item["text"]
                break
        if not summary and all(isinstance(item, str) for item in obj):
            summary = "\n".join([item for item in obj if item])
    elif isinstance(obj, str):
        summary = obj
except json.JSONDecodeError:
    summary = last_line

summary = summary.strip()
if not summary:
    raise SystemExit(f"Summary text not found in last line of {trace_path}")

summary_path.write_text(summary + "\n", encoding="utf-8")
PY

  echo "[summary_to_memory] case=$case_id summary=$summary_path patch=$patch_path"
  uv run summary_to_memory.py "$summary_path" "$patch_path"
  sleep "$SLEEP_SECONDS"
}

check_passed() {
  local case_id="$1"
  local resolved
  resolved=$(python3 - "$case_id" <<'PY'
import json
import re
import sys
from pathlib import Path

case_id = sys.argv[1]
latest_report = None
latest_ts = -1
latest_mtime = -1

for logs_dir in Path(".").glob(f"logs_{case_id}_*"):
    if not logs_dir.is_dir():
        continue
    match = re.search(r'_(\d{8}_\d{6})$', logs_dir.name)
    ts_value = -1
    if match:
        ts_value = int(match.group(1).replace("_", ""))
    for report in logs_dir.rglob("report.json"):
        if not report.is_file():
            continue
        if case_id not in str(report):
            continue
        mtime = report.stat().st_mtime
        if ts_value > latest_ts or (ts_value == latest_ts and mtime > latest_mtime):
            latest_ts = ts_value
            latest_mtime = mtime
            latest_report = report

if latest_report is None:
    print("0")
    sys.exit(0)

try:
    data = json.loads(latest_report.read_text(encoding="utf-8"))
except json.JSONDecodeError:
    print("0")
    sys.exit(0)

resolved = data.get("resolved", False)
print("1" if resolved else "0")
PY
)

  if [[ "$resolved" == "1" ]]; then
    echo 1
    return
  fi
  echo 0
}

for case_id in "${CASES[@]}"; do
  reset_claude_md
  # Iteration 0: sleep 3-8s (random)
  run_cc_base "$case_id" "iter0"
  extract_trace "$case_id"
  run_summary_to_memory "$case_id" "$LAST_LOG_ROOT"
  sleep $((3 + RANDOM % 5))

  if [[ "$(check_passed "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id resolved after iter0"
    sleep $((3 + RANDOM % 5))  # Sleep before next case
    continue
  fi

  # Iteration 1: sleep 3-8s (random)
  run_cc_base "$case_id" "iter1"
  extract_trace "$case_id"
  run_summary_to_memory "$case_id" "$LAST_LOG_ROOT"
  sleep $((3 + RANDOM % 5))

  if [[ "$(check_passed "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id resolved after iter1"
    sleep $((3 + RANDOM % 5))  # Sleep before next case
    continue
  fi

  # Iteration 2: sleep 3-8s (random)
  run_cc_base "$case_id" "iter2"
  extract_trace "$case_id"
  run_summary_to_memory "$case_id" "$LAST_LOG_ROOT"
  sleep $((3 + RANDOM % 5))  # Sleep before next case
done
