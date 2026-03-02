#!/usr/bin/env bash
set -euo pipefail

# Claude CLI retry settings (can be overridden by env).
: "${CC_CLAUDE_MAX_RETRIES:=8}"
: "${CC_CLAUDE_RETRY_BASE_DELAY:=8}"
: "${CC_CLAUDE_RETRY_MAX_DELAY:=120}"
export CC_CLAUDE_MAX_RETRIES CC_CLAUDE_RETRY_BASE_DELAY CC_CLAUDE_RETRY_MAX_DELAY

# Runner-level retry settings for rate limits (0 = unlimited retries).
: "${CC_RATE_LIMIT_MAX_RETRIES:=0}"
: "${CC_RATE_LIMIT_BASE_WAIT:=15}"
: "${CC_RATE_LIMIT_MAX_WAIT:=300}"
export CC_RATE_LIMIT_MAX_RETRIES CC_RATE_LIMIT_BASE_WAIT CC_RATE_LIMIT_MAX_WAIT

CASES_JSON="${CASES_JSON:-whitelist_cases.jsonl}"
BASE_CONFIG="${BASE_CONFIG:-agent_config.yaml}"
SLEEP_SECONDS="${SLEEP_SECONDS:-3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cases_json)
      CASES_JSON="$2"
      shift 2
      ;;
    --base_config)
      BASE_CONFIG="$2"
      shift 2
      ;;
    --sleep_seconds)
      SLEEP_SECONDS="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if ! [[ "$SLEEP_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Invalid --sleep_seconds value: $SLEEP_SECONDS (must be a non-negative integer)" >&2
  exit 1
fi

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

TMP_DIR="$(mktemp -d -t cc_cases_eval.XXXXXX)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

export CC_SKIP_EVAL=0
echo "[info] Evaluation enabled (CC_SKIP_EVAL=0)."
echo "[info] cases=$CASES_JSON"

run_idx=0
line_idx=0

while IFS= read -r raw || [[ -n "$raw" ]]; do
  ((++line_idx))
  if [[ -z "${raw//[[:space:]]/}" ]]; then
    continue
  fi

  tmp_case="$(mktemp -p "$TMP_DIR" cc_case_data.XXXXXX.jsonl)"
  tmp_cfg="$(mktemp -p "$TMP_DIR" cc_case_cfg.XXXXXX.yaml)"

  parsed="$(
    python3 - "$tmp_case" "$line_idx" 3<<<"$raw" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

tmp_case = Path(sys.argv[1])
line_idx = int(sys.argv[2])
with os.fdopen(3, "r", encoding="utf-8") as raw_stream:
    line = raw_stream.read().strip()

try:
    case = json.loads(line)
except Exception as exc:
    raise SystemExit(f"Failed to parse JSON on line {line_idx}: {exc}") from exc

if not isinstance(case, dict):
    raise SystemExit(f"Line {line_idx} is not a JSON object.")

instance_id = case.get("instance_id")
if not instance_id:
    raise SystemExit(f"Line {line_idx} is missing required field: instance_id")

safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(instance_id))
tmp_case.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"{instance_id}\t{safe_id}")
PY
  )"
  IFS=$'\t' read -r case_id safe_case_id <<<"$parsed"

  BASE_CONFIG_PATH="$BASE_CONFIG" CASE_JSONL="$tmp_case" CASE_ID="$case_id" TMP_CFG="$tmp_cfg" python3 - <<'PY'
import os
from pathlib import Path

import yaml

base_config_path = Path(os.environ["BASE_CONFIG_PATH"])
case_jsonl = os.environ["CASE_JSONL"]
case_id = os.environ["CASE_ID"]
tmp_cfg = Path(os.environ["TMP_CFG"])

cfg = yaml.safe_load(base_config_path.read_text(encoding="utf-8")) or {}
cfg.setdefault("dataset", {})["dataset_path"] = case_jsonl
cfg.setdefault("runtime", {})["run_id"] = case_id
tmp_cfg.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
PY

  run_ts="$(date +%Y%m%d_%H%M%S_%N)"
  log_root="logs_${safe_case_id}_eval_${run_ts}"

  current=$((run_idx + 1))
  echo "[run] ${current} case=${case_id}"

  cmd=(
    uv run cc_agent.py --official
    --agent_config "$tmp_cfg"
  )

  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    "${cmd[@]}"

  rm -f "$tmp_case" "$tmp_cfg"

  ((++run_idx))
  if (( SLEEP_SECONDS > 0 )); then
    sleep "$SLEEP_SECONDS"
  fi
done < "$CASES_JSON"

if (( run_idx == 0 )); then
  echo "No runnable cases found in $CASES_JSON" >&2
  exit 1
fi

echo "[done] completed=$run_idx"
