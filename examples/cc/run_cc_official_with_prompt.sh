#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: run_cc_official_with_prompt.sh --prompt_path PATH [--agent_config PATH] [--dataset_path PATH]

Runs: uv run cc_agent.py --official --agent_config <temp>
with agent.user_prompt loaded from the specified prompt file.

Examples:
  ./run_cc_official_with_prompt.sh --prompt_path examples/cc/applyedits/django__django-11815_v0.txt
  ./run_cc_official_with_prompt.sh --prompt_path prompts/custom.txt --dataset_path swebench-lite.json
EOF
}

PROMPT_PATH=""
AGENT_CONFIG="agent_config.yaml"
DATASET_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt_path)
      PROMPT_PATH="$2"
      shift 2
      ;;
    --agent_config)
      AGENT_CONFIG="$2"
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

if [[ -z "$PROMPT_PATH" ]]; then
  echo "Missing --prompt_path" >&2
  usage
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f "$PROMPT_PATH" ]]; then
  echo "Prompt file not found: $PROMPT_PATH" >&2
  exit 1
fi

if [[ ! -f "$AGENT_CONFIG" ]]; then
  echo "Config file not found: $AGENT_CONFIG" >&2
  exit 1
fi

TMP_CFG="$(mktemp -t cc_agent_cfg.XXXXXX.yaml)"
cleanup() { rm -f "$TMP_CFG"; }
trap cleanup EXIT

export PROMPT_PATH AGENT_CONFIG DATASET_PATH TMP_CFG

python3 - <<'PY'
import os
import sys
import yaml

agent_config = os.environ["AGENT_CONFIG"]
prompt_path = os.environ["PROMPT_PATH"]
dataset_path = os.environ.get("DATASET_PATH") or ""
tmp_cfg = os.environ["TMP_CFG"]

with open(agent_config, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

with open(prompt_path, "r", encoding="utf-8") as f:
    prompt = f.read().strip()

if not prompt:
    print(f"Prompt is empty: {prompt_path}", file=sys.stderr)
    sys.exit(1)

def escape_braces(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    return text.replace("{{description}}", "{description}")

if "{description}" not in prompt and "{{description}}" not in prompt:
    print("[error] prompt is missing required {description} placeholder.", file=sys.stderr)
    sys.exit(1)

agent_cfg = config.setdefault("agent", {})
agent_cfg["user_prompt"] = escape_braces(prompt)
if dataset_path:
    config.setdefault("dataset", {})["dataset_path"] = dataset_path

with open(tmp_cfg, "w", encoding="utf-8") as f:
    yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)

print(f"[info] temp agent_config: {tmp_cfg}")
PY

uv run cc_agent.py --official --agent_config "$TMP_CFG"
