#!/usr/bin/env bash
set -euo pipefail

# Rate limit retry settings for runner-level retries (0 = unlimited retries).
: "${CC_RATE_LIMIT_MAX_RETRIES:=0}"
: "${CC_RATE_LIMIT_BASE_WAIT:=15}"
: "${CC_RATE_LIMIT_MAX_WAIT:=300}"
export CC_RATE_LIMIT_MAX_RETRIES CC_RATE_LIMIT_BASE_WAIT CC_RATE_LIMIT_MAX_WAIT

RUNS_PER_CASE="${RUNS_PER_CASE:-3}"
SLEEP_SECONDS="${SLEEP_SECONDS:-10}"
CASES_JSON="${CASES_JSON:-whitelist_cases.jsonl}"
BASE_CONFIG="${BASE_CONFIG:-agent_config.yaml}"
TRACE_DIR="${TRACE_DIR:-traj}"
TRACE_OUT_DIR="${TRACE_OUT_DIR:-trace}"
EXTRACT_SCRIPT="${EXTRACT_SCRIPT:-extract_outer_content.py}"
TRACE_TO_MEMORY_SCRIPT="${TRACE_TO_MEMORY_SCRIPT:-trace_to_memory.py}"
RUN_TRACE_TO_MEMORY="${RUN_TRACE_TO_MEMORY:-1}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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

is_last_case() {
  local current="$1"
  [[ "$current" == "${CASES[-1]}" ]]
}

find_latest_traj() {
  local case_id="$1" trace_dir="$2"
  python3 - "$case_id" "$trace_dir" <<'PY'
import re
import sys
from pathlib import Path

case_id = sys.argv[1]
trace_dir = Path(sys.argv[2])
if not trace_dir.exists():
    sys.exit(1)

pattern = re.compile(rf"^{re.escape(case_id)}_(\d{{8}})_(\d{{6}})\.json$")
candidates = []
for path in trace_dir.glob(f"{case_id}_*.json"):
    if not path.is_file():
        continue
    name = path.name
    match = pattern.match(name)
    if match:
        ts = int(match.group(1) + match.group(2))
        candidates.append((1, ts, path))
    else:
        candidates.append((0, int(path.stat().st_mtime), path))

if not candidates:
    sys.exit(1)
candidates.sort(key=lambda x: (x[0], x[1]))
print(candidates[-1][2])
PY
}

extract_trace() {
  local traj_path="$1" out_dir="$2"
  mkdir -p "$out_dir"
  local base
  base="$(basename "$traj_path")"
  local out_path="${out_dir}/${base}"
  uv run python "$EXTRACT_SCRIPT" "$traj_path" -o "$out_path"
  echo "$out_path"
}


for case_id in "${CASES[@]}"; do
  tmp_case="$(mktemp -t cc_case.XXXXXX.jsonl)"
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"
  build_case_jsonl "$case_id" "$tmp_case"
  build_temp_config "$tmp_case" "$tmp_cfg"

  for run_idx in $(seq 1 "$RUNS_PER_CASE"); do
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_run${run_idx}_${run_ts}"
    echo "[run] case=$case_id run=$run_idx/$RUNS_PER_CASE ts=$run_ts"
    CC_SKIP_EVAL=1 CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
      uv run cc_agent.py --official --agent_config "$tmp_cfg"

    traj_path="$(find_latest_traj "$case_id" "$TRACE_DIR" || true)"
    if [[ -n "${traj_path:-}" ]]; then
      trace_path="$(extract_trace "$traj_path" "$TRACE_OUT_DIR")"
      if [[ "$RUN_TRACE_TO_MEMORY" == "1" ]]; then
        if ! uv run python "$TRACE_TO_MEMORY_SCRIPT" "$trace_path"; then
          echo "[trace_to_memory] failed for $case_id run $run_idx (trace: $trace_path)" >&2
        fi
      fi
    else
      echo "[trace] no traj found for $case_id run $run_idx" >&2
    fi

    if [[ "$run_idx" -lt "$RUNS_PER_CASE" ]]; then
      jitter="$((RANDOM % 20 - 10))"  # -10 to +10 percent
      sleep_time="$((SLEEP_SECONDS * (100 + jitter) / 100))"
      echo "[sleep] waiting ${sleep_time}s before next run (jitter: ${jitter}%)"
      sleep "$sleep_time"
    fi
  done

  # Final eval run after memory update
  run_ts="$(date +%Y%m%d_%H%M%S)"
  log_root="logs_${case_id}_run_eval_${run_ts}"
  echo "[run] case=$case_id run=eval ts=$run_ts"
  CC_SKIP_EVAL=0 CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_case" "$tmp_cfg"

  if ! is_last_case "$case_id"; then
    jitter="$((RANDOM % 30 - 15))"  # -15 to +15 percent
    sleep_time="$((SLEEP_SECONDS * (100 + jitter) / 100))"
    echo "[sleep] waiting ${sleep_time}s before next case (jitter: ${jitter}%)"
    sleep "$sleep_time"
  fi
done
