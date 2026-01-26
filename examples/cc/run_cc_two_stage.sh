#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: run_cc_two_stage.sh --case_id ID [--dataset_path PATH]

Pipeline:
1) Run cc_agent to produce a reproduction test only.
2) Extract trace from the run.
3) Run verifier.py to generate a new prompt (task + repro).
4) Run cc_agent again with the new prompt to solve the task.

Examples:
  ./run_cc_two_stage.sh --case_id matplotlib__matplotlib-27818
  ./run_cc_two_stage.sh --case_id sympy__sympy-15976 --dataset_path whitelist_cases.jsonl
USAGE
}

CASE_ID=""
DATASET_PATH="whitelist_cases.jsonl"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --case_id)
      CASE_ID="$2"
      shift 2
      ;;
    --dataset_path)
      DATASET_PATH="$2"
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

if [[ -z "$CASE_ID" ]]; then
  echo "Missing --case_id" >&2
  usage
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

tmp_case="$(mktemp -t cc_case.XXXXXX.jsonl)"
tmp_prompt="$(mktemp -t cc_prompt.XXXXXX.txt)"

cleanup() {
  rm -f "$tmp_case" "$tmp_prompt"
}
trap cleanup EXIT

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

cat > "$tmp_prompt" <<'PROMPT'
You are given a code repository in the current directory (/testbed).
The task description is:
{description}
=================================================
Stage 1: Reproduction only.

Your task:
1) Write the minimal reproduction test that demonstrates the bug or missing behavior.
2) Run only that reproduction test and record the failure.
3) Do NOT attempt to fix the bug in this run.
4) Do NOT delete the test yet.

Output requirements:
- Keep the repro test self-contained with clear file paths and test commands.
- Stop after the repro test fails (or confirms missing behavior).
PROMPT

run_ts="$(date +%Y%m%d_%H%M%S)"
log_root="logs_${CASE_ID}_repro_${run_ts}"

echo "[stage1] cc_agent reproduction-only run: case=$CASE_ID ts=$run_ts"
CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
  ./run_cc_official_with_prompt.sh --prompt_path "$tmp_prompt" --dataset_path "$tmp_case"

span_dir="data-${CASE_ID}"
span_file="${span_dir}/${CASE_ID}.json"
if [[ ! -f "$span_file" ]]; then
  echo "Span file not found: $span_file" >&2
  exit 1
fi

trace_dir="trace"
mkdir -p "$trace_dir"
trace_out="${trace_dir}/${CASE_ID}_extracted_${run_ts}.json"
echo "[stage1] extracting trace: $trace_out"
python3 extract_traces.py "$span_file" "$trace_out"

verifier_dir="verifier_prompts"
mkdir -p "$verifier_dir"

echo "[stage2] running verifier to build prompt"
python3 verifier.py \
  --dataset_path "$tmp_case" \
  --instance_id "$CASE_ID" \
  --trace_path "$trace_out" \
  --output_dir "$verifier_dir"

safe_id="$(python3 - <<'PY'
import re, os
case_id = os.environ["CASE_ID"]
safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id)
print(safe)
PY
)"

verifier_prompt_path="$(ls -t "$verifier_dir/${safe_id}_verifier_"*.txt 2>/dev/null | head -n 1)"
if [[ -z "$verifier_prompt_path" || ! -f "$verifier_prompt_path" ]]; then
  echo "Verifier prompt not found in $verifier_dir for $CASE_ID" >&2
  exit 1
fi

run_ts2="$(date +%Y%m%d_%H%M%S)"
log_root2="logs_${CASE_ID}_solve_${run_ts2}"

echo "[stage3] cc_agent solve run: case=$CASE_ID ts=$run_ts2"
CC_RUN_TS="$run_ts2" CC_LOGS_DIR="$log_root2" CC_EVAL_LOG_DIR="$log_root2/run_evaluation" \
  ./run_cc_official_with_prompt.sh --prompt_path "$verifier_prompt_path" --dataset_path "$tmp_case"

echo "Done. Stage1 trace: $trace_out"
echo "Verifier prompt: $verifier_prompt_path"
