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

usage() {
  cat <<'USAGE'
Usage: run_cc_iterative_step.sh --case_id ID --iter 1|2 [--dataset_path PATH] [--skip_textgrad]

Runs a single iterative step for one case:
- iter=1 uses the latest applyedits/<instance_id>_vN.txt (must already exist)
- iter=2 runs textgrad+applyedit first (unless --skip_textgrad), then uses latest applyedit

Examples:
  ./run_cc_iterative_step.sh --case_id sympy__sympy-15976 --iter 1
  ./run_cc_iterative_step.sh --case_id sympy__sympy-15976 --iter 2
  ./run_cc_iterative_step.sh --case_id sympy__sympy-15976 --iter 2 --skip_textgrad
  ./run_cc_iterative_step.sh --case_id sympy__sympy-15976 --iter 1 --dataset_path whitelist_cases.jsonl
USAGE
}

CASE_ID=""
ITER=""
DATASET_PATH="whitelist_cases.jsonl"
SKIP_TEXTGRAD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --case_id)
      CASE_ID="$2"
      shift 2
      ;;
    --iter)
      ITER="$2"
      shift 2
      ;;
    --dataset_path)
      DATASET_PATH="$2"
      shift 2
      ;;
    --skip_textgrad)
      SKIP_TEXTGRAD=1
      shift 1
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

if [[ -z "$CASE_ID" || -z "$ITER" ]]; then
  echo "Missing --case_id or --iter" >&2
  usage
  exit 1
fi

if [[ "$ITER" != "1" && "$ITER" != "2" ]]; then
  echo "--iter must be 1 or 2" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "$DATASET_PATH" != /* ]]; then
  DATASET_PATH="$SCRIPT_DIR/$DATASET_PATH"
fi

if [[ ! -f "$DATASET_PATH" ]]; then
  echo "Dataset file not found: $DATASET_PATH" >&2
  exit 1
fi

if [[ "$ITER" == "2" && "$SKIP_TEXTGRAD" -eq 0 ]]; then
  echo "[textgrad/applyedit] case=$CASE_ID"
  uv run textgrad.py --dataset_path "$DATASET_PATH" --instance_id "$CASE_ID"
  uv run applyedit.py --dataset_path "$DATASET_PATH" --instance_id "$CASE_ID"
fi

prompt_path=$(python3 - "$CASE_ID" <<'PY'
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
    raise SystemExit(f"Applyedit prompt not found for {case_id} in {apply_dir}")
print(latest_path)
PY
)

if [[ -z "$prompt_path" ]]; then
  echo "Applyedit prompt path not found for $CASE_ID" >&2
  exit 1
fi

tmp_case="$(mktemp -t cc_case.XXXXXX.jsonl)"
CASE_ID="$CASE_ID" CASES_JSON="$DATASET_PATH" TMP_CASE_JSONL="$tmp_case" python3 - <<'PY'
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

run_ts="$(date +%Y%m%d_%H%M%S)"
log_root="logs_${CASE_ID}_iter${ITER}_${run_ts}"

echo "[cc_agent+prompt] case=$CASE_ID iter=$ITER ts=$run_ts"
CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
  ./run_cc_official_with_prompt.sh --prompt_path "$prompt_path" --dataset_path "$tmp_case"

rm -f "$tmp_case"
