#!/usr/bin/env bash
set -euo pipefail

RUNS_PER_CASE=1  # initial run + up to 2 refinement cycles
SLEEP_SECONDS=6
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

mapfile -t CASES < <(CASES_JSON="$CASES_JSON" python3 - <<'PY'
import json
import os
from pathlib import Path
instances = []
cases_json = Path(os.environ["CASES_JSON"])
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
        instances.append(instance_id)
for iid in instances:
    print(iid)
PY
)

if [[ "${#CASES[@]}" -eq 0 ]]; then
  echo "No cases found in $CASES_JSON" >&2
  exit 1
fi

build_case_jsonl() {
  local case_id="$1" tmp_case="$2"
  CASE_ID="$case_id" CASES_JSON="$CASES_JSON" TMP_CASE_JSONL="$tmp_case" python3 - <<'PY'
import json
import os
from pathlib import Path

case_id = os.environ["CASE_ID"]
cases_json = Path(os.environ["CASES_JSON"])
tmp_case = Path(os.environ["TMP_CASE_JSONL"])

found = False
for line in cases_json.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        item = json.loads(line)
    except Exception:
        continue
    if item.get("instance_id") == case_id:
        tmp_case.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
        found = True
        break
if not found:
    raise SystemExit(f"case not found in {cases_json}: {case_id}")
PY
}

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
pattern = re.compile(rf"^{re.escape(case_id)}_v(\d+)\.txt$")
latest_path = None
latest_version = -1
if apply_dir.exists():
    for path in apply_dir.iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version > latest_version:
            latest_version = version
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
  local tmp_case
  local tmp_cfg
  tmp_case="$(mktemp -t cc_case.XXXXXX.jsonl)"
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"

  build_case_jsonl "$case_id" "$tmp_case"
  build_temp_config "$tmp_case" "$tmp_cfg"

  echo "[cc_agent] case=$case_id iter=$iter_label ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_cfg" "$tmp_case"
  LAST_LOG_ROOT="$log_root"
}

run_cc_with_prompt() {
  local case_id="$1" iter_label="$2"
  local prompt_path
  prompt_path="$(find_latest_applyedit "$case_id")"

  local run_ts
  run_ts="$(date +%Y%m%d_%H%M%S)"
  local log_root="logs_${case_id}_${iter_label}_${run_ts}"
  local tmp_case
  local tmp_cfg
  tmp_case="$(mktemp -t cc_case.XXXXXX.jsonl)"
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"

  build_case_jsonl "$case_id" "$tmp_case"

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

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

if "{description}" not in prompt and "{{description}}" not in prompt:
    print("[error] prompt is missing required {description} placeholder.", file=sys.stderr)
    sys.exit(1)

# Inject prompt and dataset path
data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

  echo "[cc_agent+prompt] case=$case_id iter=$iter_label ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_case" "$tmp_cfg"
  LAST_LOG_ROOT="$log_root"
}

run_textgrad_applyedit() {
  local case_id="$1"
  echo "[textgrad/applyedit] case=$case_id using $CASES_JSON"
  uv run textgrad.py --dataset_path "$CASES_JSON" --instance_id "$case_id"
  uv run applyedit.py --dataset_path "$CASES_JSON" --instance_id "$case_id"
}

check_passed() {
  local log_root="$1" case_id="$2"
  local report
  report=$(find "$log_root" -type f -name "report.json" -path "*${case_id}*" | head -n1 || true)
  if [[ -z "$report" ]]; then
    echo 0
    return
  fi
  if jq -e --arg id "$case_id" '.[$id].resolved == true' "$report" >/dev/null 2>&1; then
    echo 1
    return
  fi
  echo 0
}

for case_id in "${CASES[@]}"; do
  run_cc_base "$case_id" "iter0"
  sleep "$SLEEP_SECONDS"

  if [[ "$(check_passed "$LAST_LOG_ROOT" "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id passed after iter0"
    continue
  fi

  run_textgrad_applyedit "$case_id"
  run_cc_with_prompt "$case_id" "iter1"
  sleep "$SLEEP_SECONDS"

  if [[ "$(check_passed "$LAST_LOG_ROOT" "$case_id")" -eq 1 ]]; then
    echo "[skip] case=$case_id passed after iter1"
    continue
  fi

  run_textgrad_applyedit "$case_id"
  run_cc_with_prompt "$case_id" "iter2"
  sleep "$SLEEP_SECONDS"
done
