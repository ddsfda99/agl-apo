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

build_temp_config_with_prompt() {
  local tmp_case="$1" tmp_cfg="$2" prompt_text="$3"
  BASE_CONFIG="$BASE_CONFIG" TMP_PATH="$tmp_cfg" TMP_CASE_JSONL="$tmp_case" PROMPT_TEXT="$prompt_text" python3 - <<'PY'
import os
import yaml
from pathlib import Path

base = Path(os.environ["BASE_CONFIG"]).read_text(encoding="utf-8")
data = yaml.safe_load(base)
tmp_path = os.environ["TMP_PATH"]
tmp_case = os.environ["TMP_CASE_JSONL"]
prompt_text = os.environ["PROMPT_TEXT"]

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt_text)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

for case_id in "${CASES[@]}"; do
  tmp_case="$DATA_DIR/${case_id}.jsonl"
  attempts_dir="attempts/${case_id}"

  attempt_jsons=(
    "${attempts_dir}/attempt_run1.json"
    "${attempts_dir}/attempt_run2.json"
    "${attempts_dir}/attempt_run3.json"
    "${attempts_dir}/attempt_run4.json"
    "${attempts_dir}/attempt_run5.json"
  )

  for p in "${attempt_jsons[@]}"; do
    if [[ ! -f "$p" ]]; then
      echo "[followup] Missing attempt file: $p" >&2
      exit 1
    fi
  done

  prompt_path="$(find_latest_prompt "$case_id")"
  if [[ -z "$prompt_path" ]]; then
    echo "[followup] Missing full prompt for $case_id" >&2
    exit 1
  fi

  for actor_idx in 1 2 3; do
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_actor${actor_idx}_${run_ts}"

    guidance=$(uv run actor.py --inputs "${attempt_jsons[@]}")
    base_prompt=$(cat "$prompt_path")
    combined_prompt="${base_prompt}

# Strategic Guidance from Previous Attempts

${guidance}"

    tmp_cfg="$(mktemp -t cc_actor_cfg.XXXXXX.yaml)"
    build_temp_config_with_prompt "$tmp_case" "$tmp_cfg" "$combined_prompt"

    echo "[cc_agent] case=$case_id actor=$actor_idx/3 ts=$run_ts"
    CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
      uv run cc_agent.py --official --agent_config "$tmp_cfg"

    rm -f "$tmp_cfg"
    sleep "$SLEEP_SECONDS"
  done
done
