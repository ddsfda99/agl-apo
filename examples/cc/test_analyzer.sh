#!/usr/bin/env bash
# Run attempt_analyzer multiple times for astropy__astropy-13236
# Usage: ./test_analyzer.sh <num_runs> [attempt_files...]
# Examples:
#   ./test_analyzer.sh 5                   # 5 runs with all 3 attempts
#   ./test_analyzer.sh 5 attempts/astropy__astropy-13236_attempt_2.json attempts/astropy__astropy-13236_attempt_3.json
set -euo pipefail

INSTANCE_ID="astropy__astropy-13236"
PROMPT="full_prompts/astropy__astropy-13236_v1.txt"

NUM_RUNS="${1:-3}"
shift || true

if [[ $# -gt 0 ]]; then
    ATTEMPTS=("$@")
else
    ATTEMPTS=(
        "attempts/astropy__astropy-13236_attempt_1.json"
        "attempts/astropy__astropy-13236_attempt_2.json"
        "attempts/astropy__astropy-13236_attempt_3.json"
    )
fi

echo "=== Running attempt_analyzer ${NUM_RUNS} times ==="
echo "Instance: ${INSTANCE_ID}"
echo "Prompt:   ${PROMPT}"
echo "Attempts: ${ATTEMPTS[*]}"
echo ""

for i in $(seq 1 "$NUM_RUNS"); do
    echo "--- Run ${i}/${NUM_RUNS} ---"
    uv run attempt_analyzer.py \
        --instance_id "${INSTANCE_ID}" \
        --original_prompt_path "${PROMPT}" \
        --attempts "${ATTEMPTS[@]}"
    echo ""
done

echo "=== All ${NUM_RUNS} runs completed ==="
