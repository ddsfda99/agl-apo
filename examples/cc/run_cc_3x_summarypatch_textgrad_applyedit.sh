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

RUNS_PER_CASE="${RUNS_PER_CASE:-3}"
EVAL_RUNS="${EVAL_RUNS:-3}"
FOLLOWUP_RUNS="${FOLLOWUP_RUNS:-3}"
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

if [[ "$RUNS_PER_CASE" -ne 3 ]]; then
  echo "This script expects RUNS_PER_CASE=3 (current: $RUNS_PER_CASE)" >&2
  exit 1
fi

if [[ "$EVAL_RUNS" -ne 3 ]]; then
  echo "This script expects EVAL_RUNS=3 (current: $EVAL_RUNS)" >&2
  exit 1
fi

if [[ "$FOLLOWUP_RUNS" -ne 3 ]]; then
  echo "This script expects FOLLOWUP_RUNS=3 (current: $FOLLOWUP_RUNS)" >&2
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
            encoding="utf-8",
        )
        print(instance_id)
PY
)

if [[ "${#CASES[@]}" -eq 0 ]]; then
  echo "No cases found in $CASES_JSON" >&2
  exit 1
fi

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

if "{description}" not in prompt and "{{description}}" not in prompt:
    print(
        f"[warn] Prompt is missing {{description}}, prepending a minimal bug-description header: {prompt_path}",
        file=sys.stderr,
    )
    prompt = "The bug description is:\n{description}\n\n" + prompt

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

run_cc_once() {
  local case_id="$1" phase_label="$2" run_ts="$3" log_root="$4" tmp_case="$5" prompt_path="${6:-}"
  local tmp_cfg
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"

  if [[ -n "$prompt_path" ]]; then
    build_temp_config_with_prompt "$tmp_case" "$tmp_cfg" "$prompt_path"
    echo "[cc_agent+prompt] case=$case_id phase=$phase_label ts=$run_ts prompt=$prompt_path"
  else
    build_temp_config "$tmp_case" "$tmp_cfg"
    echo "[cc_agent] case=$case_id phase=$phase_label ts=$run_ts"
  fi

  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"
  rm -f "$tmp_cfg"
}

extract_trace_path_from_log() {
  local run_log="$1"
  python3 - "$run_log" <<'PY'
import re
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
text = log_path.read_text(encoding="utf-8", errors="ignore")
matches = re.findall(r"Trajectory saved to (.+)", text)
if not matches:
    print("")
    sys.exit(1)
print(matches[-1].strip())
PY
}

extract_summary_from_trace() {
  local trace_path="$1"
  python3 - "$trace_path" <<'PY'
import json
import sys
from pathlib import Path

trace_path = Path(sys.argv[1])

def iter_text_chunks(obj):
    if isinstance(obj, str):
        if obj.strip():
            yield obj
        return
    if isinstance(obj, list):
        for item in obj:
            yield from iter_text_chunks(item)
        return
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            text = obj.get("text", "").strip()
            if text:
                yield text
        if "content" in obj:
            yield from iter_text_chunks(obj["content"])

last_text = ""
for line in trace_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
    for chunk in iter_text_chunks(record):
        last_text = chunk

print(last_text)
PY
}

write_attempt_json() {
  local summary="$1" patch_path="$2" out_path="$3"
  python3 - "$summary" "$patch_path" "$out_path" <<'PY'
import json
import sys
from pathlib import Path

summary = sys.argv[1].strip()
patch = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
out_path = Path(sys.argv[3])
payload = {"summary": summary, "patch": patch}
out_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

build_summary_patch_bundle() {
  local out_path="$1"
  shift
  python3 - "$out_path" "$@" <<'PY'
import json
import sys
from pathlib import Path

out_path = Path(sys.argv[1])
attempts = [Path(p) for p in sys.argv[2:]]

lines = [
    "You are reviewing three coding-agent attempts for the same SWE-bench task.",
    "Each attempt includes a final summary and patch diff.",
    "Analyze quality, correctness risks, missing checks, and likely root causes.",
    "",
]

for idx, path in enumerate(attempts, start=1):
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = (data.get("summary") or "").strip()
    patch = (data.get("patch") or "").strip()
    lines.append(f"=== Attempt {idx} Summary ===")
    lines.append(summary if summary else "(empty)")
    lines.append("")
    lines.append(f"=== Attempt {idx} Patch ===")
    lines.append("```diff")
    lines.append(patch if patch else "")
    lines.append("```")
    lines.append("")

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
PY
}

find_latest_eval() {
  local case_id="$1"
  python3 - "$case_id" <<'PY'
import re
import sys
from pathlib import Path

case_id = sys.argv[1]
eval_dir = Path("evals")
safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(case_id))
pattern = re.compile(rf"^{re.escape(safe_id)}_iter\d+_[0-9]{{8}}_[0-9]{{6}}\.txt$")
latest = None
latest_mtime = -1
if eval_dir.exists():
    for path in eval_dir.iterdir():
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

find_latest_applyedit() {
  local case_id="$1"
  python3 - "$case_id" <<'PY'
import re
import sys
from pathlib import Path

case_id = sys.argv[1]
applyedit_dir = Path("applyedits")
safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(case_id))
pattern = re.compile(rf"^{re.escape(safe_id)}_iter\d+_[0-9]{{8}}_[0-9]{{6}}\.txt$")
latest = None
latest_mtime = -1
if applyedit_dir.exists():
    for path in applyedit_dir.iterdir():
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

for case_id in "${CASES[@]}"; do
  tmp_case="$DATA_DIR/${case_id}.jsonl"
  case_ts="$(date +%Y%m%d_%H%M%S)"
  case_work_dir="attempts/${case_id}/summarypatch_eval_applyedit_${case_ts}"
  mkdir -p "$case_work_dir"

  attempt_jsons=()

  for run_idx in $(seq 1 "$RUNS_PER_CASE"); do
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_seed${run_idx}_${run_ts}"
    run_cc_once "$case_id" "seed${run_idx}/${RUNS_PER_CASE}" "$run_ts" "$log_root" "$tmp_case"
    sleep "$SLEEP_SECONDS"

    run_log="${log_root}/epoch_0_${run_ts}/${case_id}"
    traj_path="$(extract_trace_path_from_log "$run_log")"
    if [[ -z "$traj_path" || ! -f "$traj_path" ]]; then
      echo "[extract] Missing traj from log: $run_log" >&2
      exit 1
    fi

    trace_path="${case_work_dir}/trace_seed${run_idx}_${run_ts}.json"
    uv run extract_outer_content.py "$traj_path" -o "$trace_path"

    patch_path="${log_root}/run_evaluation/epoch_0_${run_ts}/cc/${case_id}/patch.diff"
    if [[ ! -f "$patch_path" ]]; then
      echo "[extract] Missing patch: $patch_path" >&2
      exit 1
    fi

    summary="$(extract_summary_from_trace "$trace_path")"
    attempt_json="${case_work_dir}/attempt_seed${run_idx}.json"
    write_attempt_json "$summary" "$patch_path" "$attempt_json"
    attempt_jsons+=("$attempt_json")
  done

  bundle_trace="${case_work_dir}/summary_patch_bundle.txt"
  build_summary_patch_bundle "$bundle_trace" "${attempt_jsons[@]}"
  echo "[bundle] case=$case_id path=$bundle_trace"

  eval_paths=()
  applyedit_paths=()

  for eval_idx in $(seq 1 "$EVAL_RUNS"); do
    before_eval="$(find_latest_eval "$case_id" || true)"
    echo "[eval] case=$case_id run=$eval_idx/$EVAL_RUNS bundle=$bundle_trace"
    uv run eval.py \
      --dataset_path "$CASES_JSON" \
      --instance_id "$case_id" \
      --summary_patch_path "$bundle_trace"
    after_eval="$(find_latest_eval "$case_id" || true)"

    if [[ -z "$after_eval" || "$after_eval" == "$before_eval" ]]; then
      echo "[eval] Failed to detect newly generated eval output for $case_id" >&2
      exit 1
    fi
    eval_paths+=("$after_eval")
    sleep "$SLEEP_SECONDS"

    before_applyedit="$(find_latest_applyedit "$case_id" || true)"
    echo "[applyedit] case=$case_id source_eval=$after_eval"
    uv run applyedit.py \
      --dataset_path "$CASES_JSON" \
      --instance_id "$case_id"
    after_applyedit="$(find_latest_applyedit "$case_id" || true)"

    if [[ -z "$after_applyedit" || "$after_applyedit" == "$before_applyedit" ]]; then
      echo "[applyedit] Failed to detect newly generated applyedit output for $case_id" >&2
      exit 1
    fi
    applyedit_paths+=("$after_applyedit")
    sleep "$SLEEP_SECONDS"
  done

  if [[ "${#applyedit_paths[@]}" -lt "$FOLLOWUP_RUNS" ]]; then
    echo "[followup] Need $FOLLOWUP_RUNS applyedit prompts, got ${#applyedit_paths[@]}" >&2
    exit 1
  fi

  for follow_idx in $(seq 1 "$FOLLOWUP_RUNS"); do
    prompt_path="${applyedit_paths[$((follow_idx - 1))]}"
    run_ts="$(date +%Y%m%d_%H%M%S)"
    log_root="logs_${case_id}_round2_prompt${follow_idx}_${run_ts}"
    run_cc_once \
      "$case_id" \
      "round2-${follow_idx}/${FOLLOWUP_RUNS}" \
      "$run_ts" \
      "$log_root" \
      "$tmp_case" \
      "$prompt_path"
    sleep "$SLEEP_SECONDS"
  done

  echo "[done] case=$case_id"
done
