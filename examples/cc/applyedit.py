import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

BASE_DIR = Path(__file__).resolve().parent
FULL_PROMPTS_DIR = BASE_DIR / "full_prompts"
TEXTGRADS_DIR = BASE_DIR / "textgrads"
OPTIMIZER_PROMPT = """You are an expert in coding agent prompt optimization.
Your task is to improve the original prompt so that the coding agent can learn from previous failures and solve the problem this time.
You are given the original prompt and the evaluations of previous failures, and you should modify or add new prompts to 
produce a well-informed and guided prompt so that the coging agent can solve the problem in the original prompt this time.

the original prompt:
{original_prompt}

the evaluations of previous failures:
{evaluations}

You must carefully read the problems identified in the evaluations and address them in your optimized prompt.
Return a complete revised prompt text that can be used directly as the user prompt.
It MUST include the literal placeholder {{description}} unchanged.
Do NOT introduce any other {{...}} placeholders.
If you need literal braces in the text, escape them as {{ and }}.
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


def find_latest_prompt(prompt_dir: Path, instance_id: str) -> Optional[Path]:
    """Find the latest versioned prompt for the instance."""
    if not prompt_dir.exists():
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(instance_id))
    pattern = re.compile(rf"^{re.escape(safe_id)}_v(\d+)\.txt$")
    latest_path = None
    latest_version = -1
    for path in prompt_dir.iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version > latest_version:
            latest_version = version
            latest_path = path
    return latest_path


def find_latest_textgrad(textgrads_dir: Path, instance_id: str) -> Optional[Path]:
    """Find the latest versioned textgrad output for the instance."""
    if not textgrads_dir.exists():
        return None
    pattern_iter = re.compile(
        rf"^{re.escape(instance_id)}_iter(\d+)_([0-9]{{8}}_[0-9]{{6}})\.txt$"
    )
    latest_path = None
    latest_key = (-1, "00000000_000000")
    for path in textgrads_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        match = pattern_iter.match(name)
        if not match:
            continue
        iter_idx = int(match.group(1))
        ts = match.group(2)
        key = (iter_idx, ts)
        if key > latest_key:
            latest_key = key
            latest_path = path
    return latest_path


def next_applyedit_path(output_dir: Path, instance_id: str) -> Path:
    """Get the next versioned applyedit path for the instance."""
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


async def main(dataset_path: Path, instance_id: Optional[str]) -> None:
    instance = load_instance(dataset_path, instance_id)
    if instance is None:
        if instance_id:
            print(f"Failed to load instance {instance_id} from {dataset_path}")
        else:
            print(f"Failed to load instance from {dataset_path}")
        return

    instance_id = instance.get("instance_id", "unknown")
    evaluations_path = find_latest_textgrad(TEXTGRADS_DIR, instance_id)
    if evaluations_path is None:
        print(f"Missing textgrad output in {TEXTGRADS_DIR} for {instance_id}")
        return

    evaluations = evaluations_path.read_text(encoding="utf-8").strip()

    prompt_path = find_latest_prompt(FULL_PROMPTS_DIR, instance_id)
    if prompt_path is None:
        print(f"Error: Full prompt dump not found in {FULL_PROMPTS_DIR} for {instance_id}")
        return
    original_prompt = prompt_path.read_text(encoding="utf-8").strip()

    filled_prompt = OPTIMIZER_PROMPT.format(
        original_prompt=original_prompt,
        evaluations=evaluations,
    )

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    resp = await client.chat.completions.create(
        model="gpt-5-20250807",
        messages=[{"role": "user", "content": filled_prompt}],
    )

    content = ""
    try:
        content = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        print("\n[warn] Unable to parse completion response:", repr(exc))
        print("Raw response:", resp)

    print("=== Optimized dynamic ruleset ===")
    print(content if content else "[empty]")

    if "{description}" not in content:
        print("[error] Optimized prompt is missing required {description} placeholder.")
        return

    output_dir = BASE_DIR / "applyedits"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = next_applyedit_path(output_dir, instance_id)
    output_path.write_text(content if content else "", encoding="utf-8")
    print(f"\nSaved applyedit output to {output_path}")


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
