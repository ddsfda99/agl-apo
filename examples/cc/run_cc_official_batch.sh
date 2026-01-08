#!/usr/bin/env bash
set -euo pipefail

RUNS_PER_CASE=3
SLEEP_SECONDS=6
CASES_JSON="whitelist_cases.jsonl"
SESSION_NAME="cc_official_batch"
LOG_DIR="logs_tmux"
WORKDIR="$(pwd)"
BASE_CONFIG="agent_config.yaml"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "tmux session already exists: $SESSION_NAME" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${SESSION_NAME}_$(date +%Y%m%d_%H%M%S).log"
SCRIPT_PATH="$(mktemp -t cc_official_batch.XXXXXX)"

cat <<EOF > "$SCRIPT_PATH"
#!/usr/bin/env bash
set -euo pipefail
set -x
WORKDIR="$WORKDIR"
cd "\$WORKDIR"

if [[ "\${WORKDIR##*/}" != "cc" ]]; then
  echo "Please run from examples/cc (current: \$WORKDIR)" >&2
  exit 1
fi

if [[ ! -f "$CASES_JSON" ]]; then
  echo "Cases file not found: $CASES_JSON"
  exit 1
fi

if [[ ! -f "$BASE_CONFIG" ]]; then
  echo "Base config not found: $BASE_CONFIG"
  exit 1
fi

echo "[init] workdir=\$WORKDIR cases=$CASES_JSON runs=$RUNS_PER_CASE sleep=$SLEEP_SECONDS"

mapfile -t CASES < <(python3 - <<'PY'
import json
from pathlib import Path
instances = []
for line in Path("whitelist_cases.jsonl").read_text(encoding="utf-8").splitlines():
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

if [[ "\${#CASES[@]}" -eq 0 ]]; then
  echo "No cases found in $CASES_JSON"
  exit 1
fi

RUNS_PER_CASE=$RUNS_PER_CASE
SLEEP_SECONDS=$SLEEP_SECONDS
BASE_CONFIG=$BASE_CONFIG

for case_id in "\${CASES[@]}"; do
  for run_idx in \$(seq 1 "\$RUNS_PER_CASE"); do
    if [[ -d logs ]]; then
      ts=\$(date +%Y%m%d_%H%M%S)
      mv logs "logs_\$ts"
    fi

    ts=\$(date +%Y%m%d_%H%M%S)
    echo "[run] case=\$case_id run=\$run_idx/\$RUNS_PER_CASE ts=\$ts"

    tmp_case_jsonl=\$(mktemp -t cc_case.XXXXXX.jsonl)
    tmp_cfg=\$(mktemp -t cc_agent_cfg.XXXXXX.yaml)

    CASE_ID="\$case_id" CASES_JSON="$CASES_JSON" TMP_CASE_JSONL="\$tmp_case_jsonl" python3 - <<'PY'
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
        tmp_case.write_text(json.dumps(item, ensure_ascii=False) + "\\n", encoding="utf-8")
        found = True
        break
if not found:
    raise SystemExit(f"case not found in {cases_json}: {case_id}")
PY

    BASE_CONFIG="$BASE_CONFIG" CASE_ID="\$case_id" TMP_PATH="\$tmp_cfg" TMP_CASE_JSONL="\$tmp_case_jsonl" python3 - <<'PY'
import os, yaml
from pathlib import Path
base = Path(os.environ["BASE_CONFIG"]).read_text(encoding="utf-8")
data = yaml.safe_load(base)
case_id = os.environ["CASE_ID"]
tmp_path = os.environ["TMP_PATH"]
tmp_case = os.environ["TMP_CASE_JSONL"]
data.setdefault("dataset", {})["dataset_path"] = tmp_case
data.setdefault("runtime", {})["run_id"] = case_id
Path(tmp_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

    uv run cc_agent.py --official --agent_config "\$tmp_cfg"
    rm -f "\$tmp_cfg" "\$tmp_case_jsonl"

    if [[ -d logs ]]; then
      mv logs "logs_\$ts"
    fi

    sleep "\$SLEEP_SECONDS"
  done
done
EOF

chmod +x "$SCRIPT_PATH"
tmux new-session -d -s "$SESSION_NAME" "bash \"$SCRIPT_PATH\" 2>&1 | tee -a \"$LOG_FILE\""

echo "Started tmux session: $SESSION_NAME"
echo "Attach with: tmux attach -t $SESSION_NAME"
echo "Log: $LOG_FILE"
