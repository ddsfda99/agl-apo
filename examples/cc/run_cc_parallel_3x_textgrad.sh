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

RUNS_PER_CASE="${RUNS_PER_CASE:-3}"
SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
CASES_JSON="${CASES_JSON:-whitelist_cases.jsonl}"
BASE_CONFIG="${BASE_CONFIG:-agent_config.yaml}"

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

if [[ "$RUNS_PER_CASE" -ne 3 ]]; then
  echo "This script expects RUNS_PER_CASE=3 (current: $RUNS_PER_CASE)" >&2
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

build_temp_config_with_prompt() {
  local tmp_case="$1" tmp_cfg="$2" prompt_path="$3"
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

prompt = Path(prompt_path).read_text(encoding="utf-8").strip()
if not prompt:
    print(f"Prompt is empty: {prompt_path}", file=sys.stderr)
    sys.exit(1)

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

find_latest_prompt() {
  local case_id="$1"
  python3 - "$case_id" <<'PY'
import re
import sys
from pathlib import Path

case_id = sys.argv[1]
prompt_dir = Path("full_prompts")
safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(case_id))
pattern = re.compile(rf"^{re.escape(safe_id)}_v\d+\.txt$")
latest = None
latest_mtime = -1
if prompt_dir.exists():
    for path in prompt_dir.iterdir():
        if not path.is_file():
            continue
        if not pattern.match(path.name):
            continue
        mtime = path.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = path
if latest is None:
    print("")
    sys.exit(1)
print(latest)
PY
}

run_cc_once_bg() {
  local case_id="$1" run_idx="$2" run_ts="$3" log_root="$4" tmp_case="$5"
  (
    local tmp_cfg
    tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"
    build_temp_config "$tmp_case" "$tmp_cfg"
    echo "[cc_agent] case=$case_id run=$run_idx/$RUNS_PER_CASE ts=$run_ts"
    CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
      uv run cc_agent.py --official --agent_config "$tmp_cfg"
    rm -f "$tmp_cfg"
  ) &
}

extract_trace_for_run() {
  local case_id="$1" run_idx="$2" run_ts="$3" log_root="$4"
  local trace_path="trace/${case_id}_run${run_idx}_${run_ts}.json"
  local run_log="${log_root}/epoch_0_${run_ts}/${case_id}"

  if [[ ! -f "$run_log" ]]; then
    echo "[extract_trace] Missing run log: $run_log" >&2
    return 1
  fi

  local traj_path
  traj_path=$(python3 - "$run_log" <<'PY'
import re
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
text = log_path.read_text(encoding="utf-8", errors="ignore")
matches = re.findall(r"Trajectory saved to (.+)", text)
if not matches:
    print("")
    sys.exit(1)
print(matches[-1].strip())
PY
)

  if [[ -z "$traj_path" || ! -f "$traj_path" ]]; then
    echo "[extract_trace] Missing traj: $traj_path" >&2
    return 1
  fi

  uv run extract_outer_content.py "$traj_path" -o "$trace_path"
  echo "[extract_trace] Extracted trace to $trace_path"
}

run_textgrad_bg() {
  local case_id="$1" run_idx="$2" run_ts="$3" trace_path="$4"
  (
    echo "[critic] case=$case_id run=$run_idx"
    uv run critic.py \
      --dataset_path "$CASES_JSON" \
      --instance_id "$case_id" \
      --trace_path "$trace_path"
  ) &
}

run_cc_textgrad_pipeline() {
  local case_id="$1" run_idx="$2" run_ts="$3" log_root="$4" tmp_case="$5"
  run_cc_once_bg "$case_id" "$run_idx" "$run_ts" "$log_root" "$tmp_case"
  wait
  extract_trace_for_run "$case_id" "$run_idx" "$run_ts" "$log_root"
  local trace_path="trace/${case_id}_run${run_idx}_${run_ts}.json"
  run_textgrad_bg "$case_id" "$run_idx" "$run_ts" "$trace_path"
  wait
}

for case_id in "${CASES[@]}"; do
  tmp_case="$DATA_DIR/${case_id}.jsonl"

  cat > "CLAUDE.md" <<'EOF'
# Things Must Do
- Write a reproduction test to reproduce the bugs and to verify the implementations later.
- Make Sure you edit the source code to implement the task.
- Make Sure your patch can be applied correctly.
- Make Sure you use the reproduction test you have written before to verify your implementations. If failed, edit the code until you pass your reproduction test.
# Efficiency Tip
- When exploring, testing, or debugging, consider running multiple related commands in one response (e.g., ls + find + grep patterns, or run tests + check logs + verify config) for faster comprehensive analysis.
EOF

  run_ts_list=()
  log_root_list=()

  for run_idx in $(seq 1 "$RUNS_PER_CASE"); do
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_iter0_run${run_idx}_${run_ts}"
    run_ts_list+=("$run_ts")
    log_root_list+=("$log_root")
    run_cc_textgrad_pipeline "$case_id" "$run_idx" "$run_ts" "$log_root" "$tmp_case"
    sleep "$SLEEP_SECONDS"
  done

  printf "\n# Learned Memory\n" >> "CLAUDE.md"
  uv run learner.py --instance_id "$case_id" --max 3 --output_path "-" >> "CLAUDE.md"

  prompt_path="$(find_latest_prompt "$case_id")"
  if [[ -z "$prompt_path" ]]; then
    echo "[prompt] Missing full prompt for $case_id" >&2
    exit 1
  fi

  for final_idx in $(seq 1 3); do
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_iter1_run${final_idx}_${run_ts}"
    tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"
    build_temp_config_with_prompt "$tmp_case" "$tmp_cfg" "CLAUDE.md"
    echo "[cc_agent+combined_prompt] case=$case_id iter=1 run=$final_idx/3 ts=$run_ts"
    CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
      uv run cc_agent.py --official --agent_config "$tmp_cfg"
    rm -f "$tmp_cfg"
    sleep "$SLEEP_SECONDS"
  done
done
