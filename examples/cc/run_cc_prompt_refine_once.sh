#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: run_cc_prompt_refine_once.sh --case_id ID [--dataset_path PATH]

Run cc_agent once with a prompt that:
1) Includes the original/base prompt, and
2) Instructs the agent to scan the codebase and generate a revised, improved prompt.

Examples:
  ./run_cc_prompt_refine_once.sh --case_id reflex-dev__reflex-2617
  ./run_cc_prompt_refine_once.sh --case_id sympy__sympy-15976 --dataset_path whitelist_cases.jsonl
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
The bug description is:
{description}
=================================================
You task is to fix the bug with the following steps:
(1) write test cases to reproduce the bug.
(2) explore the source codes to locate the bug.
(3) edit the source codes to fix the bug.
(4) rerun your written test cases to validate that the bug is fixed. If not, go back to explore the source codes and fix the codes again.
(5) remember to delete the test cases you write at last.
Please do not commit your edits. We will do it later.

=================================================
PROMPT REFINEMENT RUN (do NOT fix the bug in this run)

Your task in this run:
1) Scan the codebase (structure + key files) to understand how this repo is organized.
2) Based on that understanding AND the bug description above, generate a revised, higher-quality prompt
   that would guide a future cc_agent run to solve the task more reliably.

Requirements for the revised prompt:
- It MUST include the {description} placeholder exactly once.
- It MUST be self-contained and actionable (clear steps, constraints, and validation guidance).
- It MUST preserve any critical constraints from the base prompt (no commits, clean up temp files).
- It MUST avoid broken formatting or typos (keep code blocks well-formed).
- It MUST be output as plain text in a single fenced block.

Output requirements:
- Write the revised prompt to /testbed/cc_refined_prompt.txt using the Write tool.
- Also print the revised prompt in your final response inside a single fenced block (```text).
- Do NOT modify any repo files beyond writing /testbed/cc_refined_prompt.txt.
- Do NOT run tests or edit source code in this run.
PROMPT

run_ts="$(date +%Y%m%d_%H%M%S)"
log_root="logs_${CASE_ID}_promptrefine_${run_ts}"

echo "[prompt-refine] case=$CASE_ID ts=$run_ts"
CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
  ./run_cc_official_with_prompt.sh --prompt_path "$tmp_prompt" --dataset_path "$tmp_case"

echo "Done. Logs: $log_root"
echo "Expected refined prompt: /testbed/cc_refined_prompt.txt (inside the container)"
