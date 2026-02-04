#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

traces=(
  "trace/pylint-dev__pylint-9785_20260128_122339.json"
  "trace/pylint-dev__pylint-9785_20260128_125417.json"
  "trace/pylint-dev__pylint-9785_20260128_131803.json"
)

for trace_path in "${traces[@]}"; do
  if [[ ! -f "$trace_path" ]]; then
    echo "[skip] missing: $trace_path" >&2
    continue
  fi
  echo "[run] trace_to_memory.py $trace_path"
  uv run python trace_to_memory.py "$trace_path"
done
