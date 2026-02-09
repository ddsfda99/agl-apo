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

RUNS_PER_CASE=1  # initial run + up to 2 refinement cycles
SLEEP_SECONDS=3
CASES_JSON="whitelist_cases.jsonl"
BASE_CONFIG="agent_config.yaml"

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

# Create temp directory for all case data files
DATA_DIR=$(mktemp -d)
trap 'rm -rf "$DATA_DIR"' EXIT  # Clean up on script exit

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

find_latest_applyedit() {
  local case_id="$1"
  python3 - "$case_id" <<'PY'
import re
import sys
from pathlib import Path

case_id = sys.argv[1]
apply_dir = Path("applyedits")
# Match format: {case_id}_iter{N}_{timestamp}.txt
pattern = re.compile(rf"^{re.escape(case_id)}_iter\d+_[0-9]{{8}}_[0-9]{{6}}\.txt$")
latest_path = None
latest_mtime = -1

if apply_dir.exists():
    for path in apply_dir.iterdir():
        if not path.is_file():
            continue
        if not pattern.match(path.name):
            continue
        mtime = path.stat().st_mtime  # file system timestamp
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_path = path

if latest_path is None:
    print(f"Applyedit prompt not found for {case_id} in {apply_dir}", file=sys.stderr)
    sys.exit(1)
print(latest_path)
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
}

run_cc_with_prompt() {
  local case_id="$1" iter_label="$2"
  local prompt_path
  prompt_path="$(find_latest_applyedit "$case_id")"

  local run_ts
  run_ts="$(date +%Y%m%d_%H%M%S)"
  local log_root="logs_${case_id}_${iter_label}_${run_ts}"
  local tmp_cfg
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"

  # Use pre-generated case file from DATA_DIR
  local tmp_case="$DATA_DIR/${case_id}.jsonl"

  # Build temp config with injected prompt
  BASE_CONFIG="$BASE_CONFIG" TMP_PATH="$tmp_cfg" TMP_CASE_JSONL="$tmp_case" PROMPT_PATH="$prompt_path" python3 - <<'PY'
import os
import sys
import yaml
from pathlib import Path

base = Path(os.environ["BASE_CONFIG"]).read_text(encoding="utf-8")
data = yaml.safe_load(base)
tmp_path = os.environ["TMP_PATH"]
tmp_case = os.environ["TMP_CASE_JSONL"]
prompt_path = os.environ["PROMPT_PATH"]

# Load and escape prompt
prompt = Path(prompt_path).read_text(encoding="utf-8").strip()
if not prompt:
    print(f"Prompt is empty: {prompt_path}", file=sys.stderr)
    sys.exit(1)

# todo
def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

# Inject prompt and dataset path
data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

  echo "[cc_agent+prompt] case=$case_id iter=$iter_label ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_cfg"
}

run_textgrad_applyedit() {
  local case_id="$1"
  echo "[textgrad/applyedit] case=$case_id using $CASES_JSON"
  uv run textgrad.py --dataset_path "$CASES_JSON" --instance_id "$case_id"
  sleep "$SLEEP_SECONDS"
  uv run applyedit.py --dataset_path "$CASES_JSON" --instance_id "$case_id"
  sleep "$SLEEP_SECONDS"
}

# Extract trace from the latest traj file for a given case_id
extract_trace() {
  local case_id="$1"
  echo "[extract_trace] case=$case_id"

  # Find the latest traj file (format: traj/{case_id}_YYYYMMDD_HHMMSS.json)
  local traj_path
  traj_path=$(python3 - "$case_id" <<'PY'
import sys
from pathlib import Path

case_id = sys.argv[1]
traj_dir = Path("traj")
if not traj_dir.exists():
    sys.exit(1)

candidates = []
for path in traj_dir.glob(f"{case_id}_*.json"):
    if path.is_file():
        candidates.append(path)

if candidates:
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    print(latest)
PY
)

  if [[ -z "$traj_path" ]]; then
    echo "[extract_trace] No traj found for $case_id"
    return 1
  fi

  # Use timestamp from traj filename when available
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
    timestamp=$(date +%Y%m%d_%H%M%S)
  fi

  # Extract the trace with timestamp in filename
  uv run extract_outer_content.py "$traj_path" -o "trace/${case_id}_${timestamp}.json"
  echo "[extract_trace] Extracted trace to trace/${case_id}_${timestamp}.json"

  # Clean up the temporary data folder after extraction
  local data_dir
  data_dir="data-${case_id}"
  if [[ -d "$data_dir" ]]; then
    echo "[extract_trace] Removing temporary data folder: $data_dir"
    rm -rf "$data_dir"
  fi
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

# report.json may be either:
# 1) {"resolved": true, ...}
# 2) {"<case_id>": {"resolved": true, ...}}
if isinstance(data, dict) and case_id in data and isinstance(data[case_id], dict):
    resolved = data[case_id].get("resolved", False)
else:
    resolved = data.get("resolved", False) if isinstance(data, dict) else False
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
  # Iteration 0: sleep 3-8s (random)
  run_cc_base "$case_id" "iter0"
  extract_trace "$case_id"
  sleep $((3 + RANDOM % 5))

  if [[ "$(check_passed "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id resolved after iter0"
    sleep $((3 + RANDOM % 5))  # Sleep before next case
    continue
  fi

  # Iteration 1: sleep 3-8s (random)
  run_textgrad_applyedit "$case_id"
  sleep $((3 + RANDOM % 5))
  run_cc_with_prompt "$case_id" "iter1"
  extract_trace "$case_id"
  sleep $((3 + RANDOM % 5))

  if [[ "$(check_passed "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id resolved after iter1"
    sleep $((3 + RANDOM % 5))  # Sleep before next case
    continue
  fi

  # Iteration 2: sleep 3-8s (random)
  run_textgrad_applyedit "$case_id"
  sleep $((3 + RANDOM % 5))
  run_cc_with_prompt "$case_id" "iter2"
  extract_trace "$case_id"
  sleep $((3 + RANDOM % 5))  # Sleep before next case
done
