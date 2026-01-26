#!/usr/bin/env bash
set -euo pipefail

# Rate limit retry settings for runner-level retries (0 = unlimited retries).
: "${CC_RATE_LIMIT_MAX_RETRIES:=0}"
: "${CC_RATE_LIMIT_BASE_WAIT:=15}"
: "${CC_RATE_LIMIT_MAX_WAIT:=300}"
export CC_RATE_LIMIT_MAX_RETRIES CC_RATE_LIMIT_BASE_WAIT CC_RATE_LIMIT_MAX_WAIT

SLEEP_SECONDS="${SLEEP_SECONDS:-10}"
CASES_JSON="${CASES_JSON:-whitelist_cases.jsonl}"
BASE_CONFIG="${BASE_CONFIG:-agent_config.yaml}"

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

build_temp_config_with_prompt() {
  local tmp_case="$1" tmp_cfg="$2" prompt_file="$3"
  BASE_CONFIG="$BASE_CONFIG" TMP_PATH="$tmp_cfg" TMP_CASE_JSONL="$tmp_case" PROMPT_FILE="$prompt_file" python3 - <<'PY'
import os
import sys
import yaml
from pathlib import Path

base = Path(os.environ["BASE_CONFIG"]).read_text(encoding="utf-8")
data = yaml.safe_load(base)
tmp_path = os.environ["TMP_PATH"]
tmp_case = os.environ["TMP_CASE_JSONL"]
prompt_file = Path(os.environ["PROMPT_FILE"])

if not prompt_file.is_file():
    print(f"Prompt file not found: {prompt_file}", file=sys.stderr)
    sys.exit(1)

prompt = prompt_file.read_text(encoding="utf-8").strip()
if not prompt:
    print(f"Prompt is empty: {prompt_file}", file=sys.stderr)
    sys.exit(1)

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

data.setdefault("agent", {})["user_prompt"] = escape_braces(prompt)
data.setdefault("dataset", {})["dataset_path"] = tmp_case
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
}

is_last_case() {
  local current="$1"
  [[ "$current" == "${CASES[-1]}" ]]
}

PROMPT_FILE="$(mktemp -t cc_prompt.XXXXXX.txt)"
cat > "$PROMPT_FILE" <<'CC_SURVEY_PROMPT'
I need you to act as a Senior Technical Architect and perform a comprehensive survey of the current codebase.

Your goal is to generate the content for a `.claude/CLAUDE.md` file (Project Memory) that will help you (and other AI sessions) understand and work with this repository efficiently in the future.

Please execute the following steps:

1.  **Scan & Analyze**:
    * Read the file structure (root directories).
    * Read key configuration files (e.g., `package.json`, `requirements.txt`, `Makefile`, `Dockerfile`, `cargo.toml`, etc.) to identify dependencies and scripts.
    * Read the `README.md` (if available) for high-level context.
    * Sample source code files from major directories to identify architectural patterns and coding styles.

2.  **Synthesize**:
    Based on your analysis, compile a structured summary that includes:
    * **Project Overview**: A high-level description of what this repo does and its main purpose.
    * **Tech Stack**: Key languages, frameworks, and libraries used.
    * **Architecture**: A brief explanation of the directory structure and design patterns (e.g., MVC, Microservices, Clean Architecture).
    * **Commands**: Crucial commands for the workflow (Build, Run, Test, Lint, Deploy). *Extract these directly from config files.*
    * **Coding Standards**: Observed style guidelines (naming conventions, formatting, typing, error handling).

3.  **Output**:
    Generate a single Markdown block containing the content for `.claude/CLAUDE.md`. Follow these best practices from the Claude Code documentation:
    * **Be specific**: Avoid vague statements.
    * **Use Structure**: Use clear Headers (`#`, `##`) and Bullet Points (`-`).
    * **Format**:
        * Start with a generic overview.
        * Group commands logically.
        * Keep it scannable.

If the project is complex enough to warrant splitting into modular rules (e.g., `.claude/rules/testing.md` vs `.claude/rules/style.md`), please mention that after generating the main file, but prioritize creating a comprehensive single `CLAUDE.md` first.
CC_SURVEY_PROMPT

for case_id in "${CASES[@]}"; do
  tmp_case="$(mktemp -t cc_case.XXXXXX.jsonl)"
  tmp_cfg="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"
  build_case_jsonl "$case_id" "$tmp_case"
  build_temp_config_with_prompt "$tmp_case" "$tmp_cfg" "$PROMPT_FILE"

  run_ts="$(date +%Y%m%d_%H%M%S)"
  log_root="logs_${case_id}_survey_${run_ts}"
  echo "[run] case=$case_id survey ts=$run_ts"
  CC_RUN_TS="$run_ts" CC_LOGS_DIR="$log_root" CC_EVAL_LOG_DIR="$log_root/run_evaluation" \
    uv run cc_agent.py --official --agent_config "$tmp_cfg"

  rm -f "$tmp_case" "$tmp_cfg"

  if ! is_last_case "$case_id"; then
    jitter="$((RANDOM % 30 - 15))"  # -15 to +15 percent
    sleep_time="$((SLEEP_SECONDS * (100 + jitter) / 100))"
    echo "[sleep] waiting ${sleep_time}s before next case (jitter: ${jitter}%)"
    sleep "$sleep_time"
  fi
done

rm -f "$PROMPT_FILE"
