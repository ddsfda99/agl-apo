#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# run_cc_no_binary.sh
#
# Accepts N attempt JSONs, runs attempt_analyzer.py to produce an analysis,
# then optimizer.py to produce a generalized prompt, then runs cc_agent
# multiple times with the improved prompt.
#
# Each attempt JSON should contain: summary, patch.
# ---------------------------------------------------------------------------

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

CASES_JSON="${CASES_JSON:-whitelist_cases.jsonl}"
BASE_CONFIG="${BASE_CONFIG:-agent_config.yaml}"
SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
CC_RUNS="${CC_RUNS:-3}"

INSTANCE_ID=""
PROMPT_PATH_OVERRIDE=""
ATTEMPT_JSONS=()   # variable-length list of attempt JSON paths

usage() {
  cat <<'EOF'
Usage:
  run_cc_no_binary.sh \
    --instance_id <instance_id> \
    --attempts <a1.json> <a2.json> [<a3.json> ...] \
    [--prompt_path <base_prompt.txt>] \
    [--cases_json <cases.jsonl>] \
    [--base_config <agent_config.yaml>] \
    [--cc_runs <n>]

Notes:
  - Accepts any number of attempt JSONs (≥1) via --attempts.
  - Each JSON should have: summary (str), patch (str).
  - attempt_analyzer.py produces a structured analysis.
  - optimizer.py turns the analysis into a generalized prompt.
  - cc_agent runs 3 times by default (CC_RUNS=3).

Examples:
  # 2 attempts
  run_cc_no_binary.sh --instance_id foo --attempts a1.json a2.json

  # 3 attempts
  run_cc_no_binary.sh --instance_id foo --attempts a1.json a2.json a3.json
EOF
}

# ---- Argument parsing -----------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance_id)
      INSTANCE_ID="$2"
      shift 2
      ;;
    --attempts)
      shift
      # Consume all following non-flag arguments as attempt paths.
      while [[ $# -gt 0 && "$1" != --* ]]; do
        ATTEMPT_JSONS+=("$1")
        shift
      done
      ;;
    --prompt_path)
      PROMPT_PATH_OVERRIDE="$2"
      shift 2
      ;;
    --cases_json)
      CASES_JSON="$2"
      shift 2
      ;;
    --base_config)
      BASE_CONFIG="$2"
      shift 2
      ;;
    --cc_runs)
      CC_RUNS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# ---- Validations -----------------------------------------------------------
WORKDIR="$(pwd)"
if [[ "${WORKDIR##*/}" != "cc" ]]; then
  echo "Please run from examples/cc (current: $WORKDIR)" >&2
  exit 1
fi

if [[ -z "$INSTANCE_ID" ]]; then
  echo "Missing required argument: --instance_id" >&2
  usage
  exit 1
fi

if [[ ${#ATTEMPT_JSONS[@]} -eq 0 ]]; then
  echo "Missing required argument: --attempts (need at least 1 attempt JSON)" >&2
  usage
  exit 1
fi

for path in "${ATTEMPT_JSONS[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "Attempt JSON not found: $path" >&2
    exit 1
  fi
done

if [[ ! -f "$CASES_JSON" ]]; then
  echo "Cases file not found: $CASES_JSON" >&2
  exit 1
fi

if [[ ! -f "$BASE_CONFIG" ]]; then
  echo "Base config not found: $BASE_CONFIG" >&2
  exit 1
fi

if ! [[ "$CC_RUNS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Invalid --cc_runs value: $CC_RUNS (must be positive integer)" >&2
  exit 1
fi

# ---- Find latest prompt ----------------------------------------------------
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
latest_mtime = -1.0
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
print(str(latest) if latest else "")
PY
}

if [[ -n "$PROMPT_PATH_OVERRIDE" ]]; then
  if [[ ! -f "$PROMPT_PATH_OVERRIDE" ]]; then
    echo "Prompt path not found: $PROMPT_PATH_OVERRIDE" >&2
    exit 1
  fi
  ORIGINAL_PROMPT_PATH="$PROMPT_PATH_OVERRIDE"
else
  ORIGINAL_PROMPT_PATH="$(find_latest_prompt "$INSTANCE_ID")"
  if [[ -z "$ORIGINAL_PROMPT_PATH" || ! -f "$ORIGINAL_PROMPT_PATH" ]]; then
    echo "Unable to find latest full prompt for $INSTANCE_ID under full_prompts/" >&2
    exit 1
  fi
fi

# ---- Extract single case from cases JSONL ----------------------------------
DATA_DIR=$(mktemp -d)
trap 'rm -rf "$DATA_DIR"' EXIT

TMP_CASE="$DATA_DIR/${INSTANCE_ID}.jsonl"
python3 - "$CASES_JSON" "$INSTANCE_ID" "$TMP_CASE" <<'PY'
import json
import sys
from pathlib import Path

cases_path = Path(sys.argv[1])
instance_id = sys.argv[2]
out_path = Path(sys.argv[3])

found = None
for line in cases_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        item = json.loads(line)
    except Exception:
        continue
    if item.get("instance_id") == instance_id:
        found = item
        break

if found is None:
    print(f"Instance not found in cases file: {instance_id}", file=sys.stderr)
    raise SystemExit(1)

out_path.write_text(json.dumps(found, ensure_ascii=False) + "\n", encoding="utf-8")
PY

# ---- Run attempt_analyzer then optimizer -----------------------------------
echo "[attempt_analyzer] instance=$INSTANCE_ID attempts=${#ATTEMPT_JSONS[@]}"
analysis_path=$(uv run attempt_analyzer.py \
  --attempts "${ATTEMPT_JSONS[@]}" \
  --instance_id "$INSTANCE_ID" \
  --original_prompt_path "$ORIGINAL_PROMPT_PATH")
sleep "$SLEEP_SECONDS"

if [[ -z "$analysis_path" || ! -f "$analysis_path" ]]; then
  echo "Missing analysis output: $analysis_path" >&2
  exit 1
fi
echo "[attempt_analyzer] output=$analysis_path"

echo "[optimizer] instance=$INSTANCE_ID"
prompt_path=$(uv run optimizer.py \
  --analysis "$analysis_path" \
  --instance_id "$INSTANCE_ID")
sleep "$SLEEP_SECONDS"

if [[ -z "$prompt_path" || ! -f "$prompt_path" ]]; then
  echo "Missing prompt output: $prompt_path" >&2
  exit 1
fi
echo "[optimizer] output=$prompt_path"

# ---- Build temp agent config -----------------------------------------------
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
    # Keep the description placeholder functional after global escaping.
    return text.replace("{{description}}", "{description}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

tmp_cfg="$(mktemp -t cc_nb_cfg.XXXXXX.yaml)"
build_temp_config_with_prompt "$TMP_CASE" "$tmp_cfg" "$prompt_path"

# ---- Run cc_agent N times -------------------------------------------------
for run_idx in $(seq 1 "$CC_RUNS"); do
  cc_ts="$(date +%Y%m%d_%H%M%S)"
  log_root="logs_${INSTANCE_ID}_nb_run${run_idx}_${cc_ts}"
  echo "[cc_agent] run=${run_idx}/${CC_RUNS} instance=$INSTANCE_ID ts=$cc_ts prompt=$prompt_path"
  CC_RUN_TS="$cc_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"
  if [[ "$run_idx" -lt "$CC_RUNS" ]]; then
    sleep "$SLEEP_SECONDS"
  fi
done

rm -f "$tmp_cfg"
echo "[done] analysis=$analysis_path prompt=$prompt_path cc_runs=$CC_RUNS"
