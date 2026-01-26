import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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


def find_latest_extracted_trace(instance_id: str) -> Optional[Path]:
    """Find the latest trace file (prefers timestamp in filename)."""
    if not TRACE_DIR.exists():
        return None

    patterns = [
        f"{instance_id}_????????_??????.json",  # new JSONL traces
    ]
    matching_files: list[Path] = []
    for pattern in patterns:
        matching_files.extend(TRACE_DIR.glob(pattern))

    matching_files = [p for p in matching_files if p.is_file()]
    if not matching_files:
        return None

    def timestamp_key(path: Path) -> tuple[int, int]:
        match = re.match(
            rf"^{re.escape(instance_id)}_(\d{{8}})_(\d{{6}})\.json$",
            path.name,
        )
        if match:
            return (1, int(match.group(1) + match.group(2)))
        return (0, int(path.stat().st_mtime))

    latest_file = max(matching_files, key=timestamp_key)
    return latest_file


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
    """Load the latest trace file from trace/ directory as raw text."""
    extracted_path = find_latest_extracted_trace(instance_id)
    if extracted_path is None:
        logger.warning(f"No trace found for {instance_id} in {TRACE_DIR}")
        return None

    try:
        raw = extracted_path.read_text(encoding="utf-8")
        if raw.strip():
            logger.info(f"Loaded trace from {extracted_path.name}")
            return raw
    except Exception as e:
        logger.warning(f"Failed to load trace from {extracted_path}: {e}")
    return None


async def call_llm_with_retry(client, messages: list) -> str:
    """Call LLM with automatic retry on ALL API failures for maximum stability."""
    max_retries = 10
    base_delay = 2  # seconds
    max_delay = 60  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model="gpt-5-20250807",
                messages=messages,
            )
            return resp.choices[0].message.content or ""

        except Exception as e:
            # Retry on ALL errors to maximize stability
            if attempt >= max_retries:
                logger.error(f"Max retries ({max_retries}) reached. Final error: {type(e).__name__}: {str(e)[:200]}")
                raise

            # Exponential backoff: 2s, 4s, 8s, 16s, 32s, 60s, ...
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)

            error_type = type(e).__name__
            error_msg = str(e)[:150] if str(e) else "No error message"
            logger.warning(
                f"API call failed (attempt {attempt}/{max_retries}): {error_type} - {error_msg}. "
                f"Retrying in {delay} seconds..."
            )
            await asyncio.sleep(delay)

    # Should never reach here
    raise RuntimeError("Unexpected state in retry logic")


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

    # 4. Call LLM to optimize (with automatic retry on rate limit)
    optimized_task_description = await call_llm_with_retry(
        client,
        [{"role": "user", "content": optimization_payload}],
    )

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
