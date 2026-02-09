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

RUNS_PER_CASE="${RUNS_PER_CASE:-1}"
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

THINK_ONLY_INSTRUCTIONS=$'### Think-Only Mode\\n- Do NOT run tools or shell commands.\\n- Do NOT modify files or propose patches.\\n- Only output a concise solution description and plan.\\n- Keep it implementation-oriented but do not execute.\\n'

for case_id in "${CASES[@]}"; do
  tmp_case="$DATA_DIR/${case_id}.jsonl"

  for run_idx in $(seq 1 "$RUNS_PER_CASE"); do
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_thinkonly_run${run_idx}_${run_ts}"
    tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"
    build_temp_config_with_prompt "$tmp_case" "$tmp_cfg" "$THINK_ONLY_INSTRUCTIONS"

    echo "[cc_agent think-only] case=$case_id run=$run_idx/$RUNS_PER_CASE ts=$run_ts"
    CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
      uv run cc_agent.py --official --agent_config "$tmp_cfg"
    rm -f "$tmp_cfg"
  done
done
