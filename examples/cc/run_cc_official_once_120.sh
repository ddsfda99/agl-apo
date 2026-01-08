#!/usr/bin/env bash
set -euo pipefail

RUNS_PER_CASE=1
SLEEP_SECONDS=6
CASES_JSON="swebench-lite.json"
LOG_DIR="logs_tmux"
WHITELIST=(
  astropy__astropy-14182
  sphinx-doc__sphinx-8801
  sphinx-doc__sphinx-10325
  sympy__sympy-11400
  sympy__sympy-21612
  django__django-15022
  django__django-16816
  scikit-learn__scikit-learn-10508
  scikit-learn__scikit-learn-10949
  matplotlib__matplotlib-22711
  matplotlib__matplotlib-22835
)

WORKDIR="$(pwd)"
if [[ "${WORKDIR##*/}" != "cc" ]]; then
  echo "Please run from examples/cc (current: $WORKDIR)" >&2
  exit 1
fi

if [[ ! -f "$CASES_JSON" ]]; then
  echo "Cases file not found: $CASES_JSON" >&2
  exit 1
fi

# Build whitelist set in Python
mapfile -t CASES < <(python3 - <<'PY'
import json
from pathlib import Path
whitelist = {
    "astropy__astropy-14182",
    "sphinx-doc__sphinx-8801",
    "sphinx-doc__sphinx-10325",
    "sympy__sympy-11400",
    "sympy__sympy-21612",
    "django__django-15022",
    "django__django-16816",
    "scikit-learn__scikit-learn-10508",
    "scikit-learn__scikit-learn-10949",
    "matplotlib__matplotlib-22711",
    "matplotlib__matplotlib-22835",
}
instances = []
for line in Path("swebench-lite.json").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        item = json.loads(line)
    except Exception:
        continue
    instance_id = item.get("instance_id")
    if instance_id and instance_id in whitelist:
        instances.append(instance_id)
for iid in instances:
    print(iid)
PY
)

if [[ "${#CASES[@]}" -eq 0 ]]; then
  echo "No cases found in $CASES_JSON (after whitelist filter)" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

# Backup agent_config.yaml to restore later
CONFIG_FILE="agent_config.yaml"
CONFIG_BACKUP="${CONFIG_FILE}.bak_run_once"
cp "$CONFIG_FILE" "$CONFIG_BACKUP"
trap 'cp "$CONFIG_BACKUP" "$CONFIG_FILE"; rm -f "$CONFIG_BACKUP"' EXIT

for case_id in "${CASES[@]}"; do
  for run_idx in $(seq 1 "$RUNS_PER_CASE"); do
    # rotate existing logs
    if [[ -d logs ]]; then
      ts=$(date +%Y%m%d_%H%M%S)
      mv logs "logs_$ts"
    fi

    ts=$(date +%Y%m%d_%H%M%S)
    echo "[run] case=$case_id run=$run_idx/$RUNS_PER_CASE ts=$ts max_step=120"

    # update config: dataset_path and max_step
    sed -i "s|^  dataset_path: .*|  dataset_path: ${case_id}.jsonl|" "$CONFIG_FILE"
    sed -i "s|^  max_step: .*|  max_step: 120|" "$CONFIG_FILE"

    uv run cc_agent.py --official

    if [[ -d logs ]]; then
      mv logs "logs_${case_id}_${ts}"
    fi

    sleep "$SLEEP_SECONDS"
  done
done

# Restore config
cp "$CONFIG_BACKUP" "$CONFIG_FILE"
rm -f "$CONFIG_BACKUP"
trap - EXIT
