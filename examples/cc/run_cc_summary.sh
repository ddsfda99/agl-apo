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

RUNS_PER_CASE=1
SLEEP_SECONDS=3
CASES_JSON="whitelist_cases.jsonl"
BASE_CONFIG="agent_config.yaml"
CLAUDE_MD="CLAUDE.md"

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

DATA_DIR=$(mktemp -d)
trap 'rm -rf "$DATA_DIR"' EXIT

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

build_temp_config_with_claude() {
  local tmp_case="$1" tmp_cfg="$2"
  BASE_CONFIG="$BASE_CONFIG" TMP_PATH="$tmp_cfg" TMP_CASE_JSONL="$tmp_case" CLAUDE_MD="$CLAUDE_MD" python3 - <<'PY'
import os
import sys
import yaml
from pathlib import Path

base = Path(os.environ["BASE_CONFIG"]).read_text(encoding="utf-8")
data = yaml.safe_load(base)
tmp_path = os.environ["TMP_PATH"]
tmp_case = os.environ["TMP_CASE_JSONL"]
claude_md = Path(os.environ["CLAUDE_MD"])

if not claude_md.is_file():
    print(f"CLAUDE.md not found: {claude_md}", file=sys.stderr)
    sys.exit(1)

prompt = claude_md.read_text(encoding="utf-8").strip()
if not prompt:
    print(f"CLAUDE.md is empty: {claude_md}", file=sys.stderr)
    sys.exit(1)

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
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

candidates = [p for p in trace_dir.glob(f"{case_id}_extracted_*.json") if p.is_file()]
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

  local tmp_case="$DATA_DIR/${case_id}.jsonl"
  build_temp_config "$tmp_case" "$tmp_cfg"

  echo "[cc_agent] case=$case_id iter=$iter_label ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_cfg"
}

run_cc_with_claude() {
  local case_id="$1" iter_label="$2"
  local run_ts
  run_ts="$(date +%Y%m%d_%H%M%S)"
  local log_root="logs_${case_id}_${iter_label}_${run_ts}"
  local tmp_cfg
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"

  local tmp_case="$DATA_DIR/${case_id}.jsonl"
  build_temp_config_with_claude "$tmp_case" "$tmp_cfg"

  echo "[cc_agent+claude] case=$case_id iter=$iter_label ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_cfg"
}

run_summary() {
  local case_id="$1"
  local trace_path
  trace_path="$(find_latest_trace "$case_id" || true)"
  if [[ -z "$trace_path" ]]; then
    echo "[summary] no trace found for $case_id"
    return 1
  fi
  echo "[summary] case=$case_id trace=$trace_path -> $CLAUDE_MD"
  uv run summarize.py --trace_path "$trace_path"
  sleep "$SLEEP_SECONDS"
}

extract_trace() {
  local case_id="$1"
  echo "[extract_trace] case=$case_id"

  local raw_path
  raw_path=$(python3 - "$case_id" <<'PY'
import sys
from pathlib import Path

case_id = sys.argv[1]
raw_file = Path(".") / f"data-{case_id}" / f"{case_id}.json"
if raw_file.is_file():
    print(raw_file)
PY
)

  if [[ -z "$raw_path" ]]; then
    echo "[extract_trace] No raw trace found for $case_id"
    return 1
  fi

  local timestamp
  timestamp="$(date +%Y%m%d_%H%M%S)"
  uv run extract_traces.py "$raw_path" "trace/${case_id}_extracted_${timestamp}.json"
  echo "[extract_trace] Extracted trace to trace/${case_id}_extracted_${timestamp}.json"

  local data_dir
  data_dir=$(dirname "$raw_path")
  echo "[extract_trace] Removing temporary data folder: $data_dir"
  rm -rf "$data_dir"
}

check_passed() {
  local case_id="$1"
  local trace_file
  trace_file="$(find_latest_trace "$case_id" || true)"
  if [[ -z "$trace_file" ]]; then
    echo 0
    return
  fi
  local reward
  reward=$(jq -r '.terminal_reward // 0' "$trace_file" 2>/dev/null || echo "0")
  if [[ "$reward" == "1.0" ]]; then
    echo 1
    return
  fi
  echo 0
}

for case_id in "${CASES[@]}"; do
  run_cc_base "$case_id" "iter0"
  extract_trace "$case_id"
  run_summary "$case_id"
  sleep $((3 + RANDOM % 5))

  if [[ "$(check_passed "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id passed after iter0"
    sleep $((3 + RANDOM % 5))
    continue
  fi

  run_cc_with_claude "$case_id" "iter1"
  extract_trace "$case_id"
  run_summary "$case_id"
  sleep $((3 + RANDOM % 5))

  if [[ "$(check_passed "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id passed after iter1"
    sleep $((3 + RANDOM % 5))
    continue
  fi

  run_cc_with_claude "$case_id" "iter2"
  extract_trace "$case_id"
  run_summary "$case_id"
  sleep $((3 + RANDOM % 5))
done
