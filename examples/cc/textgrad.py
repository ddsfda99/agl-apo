import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

# Configuration
BASE_DIR = Path(__file__).resolve().parent
TRACE_DIR = BASE_DIR / "trace"

# Prompt for optimizing the task description based on failure logs
TASK_OPTIMIZATION_PROMPT = """You are a senior software engineer and an expert code reviewer.
Your goal is to review a coding agent's trace to solve a problem.
You are given the coding agent's execution trace (which includes the original task description as the first user message).
Your task is to review the given information and find out why the coding agent failed.
You must carefully analyze and reason about the trace and find all the places where the coding agent did not do well and how it can improve.

the coding agent's execution trace:
{trace}

Return your analysis and insights so that your output can be combined with the original prompt to make the coding agent pass all tests.
"""


def load_single_case(dataset_path: Path) -> Optional[Dict[str, Any]]:
    """Load the single case from JSONL file."""
    if not dataset_path.exists():
        print(f"Dataset file not found: {dataset_path}")
        return None

    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                return json.loads(line)
    return None


def load_instance(dataset_path: Path, instance_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if instance_id is None:
        print("Error: instance_id is required.")
        return None
    if not dataset_path.exists():
        print(f"Dataset file not found: {dataset_path}")
        return None
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("instance_id") == instance_id:
                return item
    return None


def find_latest_raw_trace(instance_id: str) -> Optional[Path]:
    base_dir = BASE_DIR
    candidates = []
    for path in base_dir.glob(f"data_*-{instance_id}/{instance_id}.json"):
        if path.is_file():
            candidates.append(path)
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    return None


def next_trace_path(trace_dir: Path, instance_id: str) -> Path:
    prefix = f"{instance_id}_extracted_v"
    next_version = 0
    if trace_dir.exists():
        for path in trace_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            suffix = name[len(prefix):-5]
            if suffix.isdigit():
                next_version = max(next_version, int(suffix) + 1)
    return trace_dir / f"{instance_id}_extracted_v{next_version}.json"


def next_textgrad_path(output_dir: Path, instance_id: str) -> Path:
    pattern_iter = re.compile(
        rf"^{re.escape(instance_id)}_iter(\d+)_\d{{8}}_\d{{6}}\.txt$"
    )
    next_iter = 0
    if output_dir.exists():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            match = pattern_iter.match(name)
            if match:
                next_iter = max(next_iter, int(match.group(1)) + 1)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{instance_id}_iter{next_iter}_{timestamp}.txt"


def _load_agent_log_from_data_dir(instance_id: str) -> Optional[str]:
    raw_path = find_latest_raw_trace(instance_id)
    if raw_path is None:
        return None

    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    extracted_tmp = TRACE_DIR / f"{instance_id}_extracted.json"
    if extracted_tmp.exists():
        extracted_tmp.unlink()

    subprocess.run(
        [sys.executable, str(BASE_DIR / "extract_traces.py"), str(raw_path), str(TRACE_DIR)],
        check=False,
    )

    if not extracted_tmp.exists():
        return None

    versioned_path = next_trace_path(TRACE_DIR, instance_id)
    extracted_tmp.rename(versioned_path)

    try:
        data = json.loads(versioned_path.read_text(encoding="utf-8"))
        trace_items = data.get("trace", [])
        if isinstance(trace_items, list):
            lines = []
            for msg in trace_items:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                lines.append(f"{role}: {content}")
            rendered = "\n".join(lines)
            if rendered:
                return rendered
    except Exception:
        pass
    return None


async def main(dataset_path: Path, instance_id: Optional[str]) -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    # 1. Load the data
    instance = load_instance(dataset_path, instance_id)
    if instance is None:
        if instance_id:
            print(f"Failed to load instance {instance_id} from {dataset_path}")
        else:
            print(f"Failed to load instance from {dataset_path}")
        return

    instance_id = instance.get("instance_id", "unknown")
    print(f"Loaded instance: {instance_id}")

    # 2. Read the agent's actual execution trace (includes original prompt as first user message)
    agent_log = _load_agent_log_from_data_dir(instance_id)
    if agent_log:
        print(f"Loaded agent trace from extracted file ({len(agent_log)} chars)")
    else:
        print("Error: Agent trace not found in extracted trace file.")
        return

    # 3. Construct the Optimization Prompt
    optimization_payload = TASK_OPTIMIZATION_PROMPT.format(
        trace=agent_log,
    )

    print(f"\n=== Computing Task Optimization for {instance_id} ===")

    # 4. Call LLM to optimize
    resp = await client.chat.completions.create(
        model="gpt-5-20250807",
        messages=[{"role": "user", "content": optimization_payload}],
    )

    optimized_task_description = resp.choices[0].message.content or ""

    print("\n\n>>> OPTIMIZED TASK DESCRIPTION PREVIEW (First 500 chars) >>>")
    print("-" * 60)
    if len(optimized_task_description) > 500:
        print(optimized_task_description[:500] + "...")
    else:
        print(optimized_task_description)
    print("-" * 60)

    # Save the optimized prompt to a file for the next iteration
    output_dir = BASE_DIR / "textgrads"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = next_textgrad_path(output_dir, instance_id)
    output_path.write_text(optimized_task_description)
    print(f"\nSaved optimized task description to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset_path",
        required=True,
        help="Path to JSONL dataset file",
    )
    parser.add_argument(
        "--instance_id",
        required=True,
        help="Instance id to load from dataset",
    )
    args = parser.parse_args()
    asyncio.run(main(Path(args.dataset_path), args.instance_id))
