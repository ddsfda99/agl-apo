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

CASES_JSON="${CASES_JSON:-whitelist_cases.jsonl}"
BASE_CONFIG="${BASE_CONFIG:-agent_config.yaml}"
SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
CC_RUNS="${CC_RUNS:-3}"

INSTANCE_ID=""
ATTEMPT_A_JSON=""
ATTEMPT_B_JSON=""
PROMPT_PATH_OVERRIDE=""
TMUX_SESSION=""
TMUX_LOG_DIR="${TMUX_LOG_DIR:-logs_analyzer}"
NO_TMUX_INTERNAL=0

usage() {
  cat <<'EOF'
Usage:
  run_cc_general.sh \
    --instance_id <instance_id> \
    --attempt_a_json <attempt_a.json> \
    --attempt_b_json <attempt_b.json> \
    [--prompt_path <base_prompt.txt>] \
    [--tmux_session <session_name>] \
    [--tmux_log_dir <dir>] \
    [--cases_json <cases.jsonl>] \
    [--base_config <agent_config.yaml>] \
    [--cc_runs <n>]

Notes:
  - This script directly uses the two attempt JSON files.
  - analyzer_general.py is used to produce a generalized prompt (less case-specific detail).
  - cc_agent runs 3 times by default (CC_RUNS=3).
  - If --tmux_session is provided, the full run starts detached in tmux.
EOF
}

RAW_ARGS=("$@")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance_id)
      INSTANCE_ID="$2"
      shift 2
      ;;
    --attempt_a_json)
      ATTEMPT_A_JSON="$2"
      shift 2
      ;;
    --attempt_b_json)
      ATTEMPT_B_JSON="$2"
      shift 2
      ;;
    --prompt_path)
      PROMPT_PATH_OVERRIDE="$2"
      shift 2
      ;;
    --tmux_session)
      TMUX_SESSION="$2"
      shift 2
      ;;
    --tmux_log_dir)
      TMUX_LOG_DIR="$2"
      shift 2
      ;;
    --no_tmux_internal)
      NO_TMUX_INTERNAL=1
      shift 1
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

WORKDIR="$(pwd)"
if [[ "${WORKDIR##*/}" != "cc" ]]; then
  echo "Please run from examples/cc (current: $WORKDIR)" >&2
  exit 1
fi

if [[ -n "$TMUX_SESSION" && "$NO_TMUX_INTERNAL" -eq 0 ]]; then
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found in PATH" >&2
    exit 1
  fi
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "tmux session already exists: $TMUX_SESSION" >&2
    exit 1
  fi
  mkdir -p "$TMUX_LOG_DIR"
  log_ts="$(date +%Y%m%d_%H%M%S)"
  log_file="$TMUX_LOG_DIR/${TMUX_SESSION}_${log_ts}.log"
  script_path="$(realpath "$0")"

  escaped_args=""
  for arg in "${RAW_ARGS[@]}"; do
    printf -v q '%q' "$arg"
    escaped_args+="$q "
  done
  printf -v workdir_q '%q' "$WORKDIR"
  printf -v script_q '%q' "$script_path"
  printf -v log_q '%q' "$log_file"

  tmux_cmd="cd ${workdir_q} && bash ${script_q} ${escaped_args}--no_tmux_internal 2>&1 | tee -a ${log_q}"
  tmux new-session -d -s "$TMUX_SESSION" "$tmux_cmd"

  echo "Started tmux session: $TMUX_SESSION"
  echo "Log file: $log_file"
  echo "Attach with: tmux attach -t $TMUX_SESSION"
  exit 0
fi

for v in INSTANCE_ID ATTEMPT_A_JSON ATTEMPT_B_JSON; do
  if [[ -z "${!v}" ]]; then
    echo "Missing required argument: --${v,,}" >&2
    usage
    exit 1
  fi
done

for path in "$ATTEMPT_A_JSON" "$ATTEMPT_B_JSON"; do
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

analysis_ts="$(date +%Y%m%d_%H%M%S)"
analysis_path="comparisons/${INSTANCE_ID}_general_manual_${analysis_ts}.txt"
rephrased_prompt_path="rephrased_prompts/${INSTANCE_ID}_general_manual_${analysis_ts}.txt"

analyzer_cmd=(
  uv run analyzer_general.py
  --attempt_a_json "$ATTEMPT_A_JSON"
  --attempt_b_json "$ATTEMPT_B_JSON"
  --original_prompt_path "$ORIGINAL_PROMPT_PATH"
  --analysis_output "$analysis_path"
  --prompt_output "$rephrased_prompt_path"
)

echo "[analyzer_general] instance=$INSTANCE_ID"
"${analyzer_cmd[@]}"
sleep "$SLEEP_SECONDS"

if [[ ! -f "$rephrased_prompt_path" ]]; then
  echo "Missing rephrased prompt output: $rephrased_prompt_path" >&2
  exit 1
fi

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
    return text.replace("{", "{{").replace("}", "}}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

tmp_cfg="$(mktemp -t cc_analyzer_cfg.XXXXXX.yaml)"
build_temp_config_with_prompt "$TMP_CASE" "$tmp_cfg" "$rephrased_prompt_path"

for run_idx in $(seq 1 "$CC_RUNS"); do
  cc_ts="$(date +%Y%m%d_%H%M%S)"
  log_root="logs_${INSTANCE_ID}_analyzer_run${run_idx}_${cc_ts}"
  echo "[cc_agent] run=${run_idx}/${CC_RUNS} instance=$INSTANCE_ID ts=$cc_ts prompt=$rephrased_prompt_path"
  CC_RUN_TS="$cc_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"
  if [[ "$run_idx" -lt "$CC_RUNS" ]]; then
    sleep "$SLEEP_SECONDS"
  fi
done

rm -f "$tmp_cfg"
echo "[done] analysis=$analysis_path prompt=$rephrased_prompt_path cc_runs=$CC_RUNS"
