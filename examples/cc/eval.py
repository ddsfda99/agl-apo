import argparse
import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
FULL_PROMPTS_DIR = BASE_DIR / "full_prompts"
EVALS_DIR = BASE_DIR / "evals"

EVALUATION_PROMPT = """You are a strict evaluator for iterative coding-agent attempts on SWE-bench tasks.
Your job is to analyze failure evidence and produce high-signal feedback that can improve the next prompt.

You are given:
1) the original prompt sent to the coding agent
2) exactly three attempt summaries with their patch diffs

original prompt:
{original_prompt}

attempt summaries + patch diffs:
{summaries_and_patches}

Write an evaluation with exactly these sections:
## Failure Modes
- concrete failure patterns and likely root causes

## Evidence
- specific observations from the summaries and patch diffs that support your diagnosis

## Prompt Improvement Directions
- actionable, prompt-level guidance to reduce repeated failure
- include checks for correctness validation, edge cases, and patch completeness

Keep it concise, concrete, and evidence-driven.
"""


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
    if not prompt_dir.exists():
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(instance_id))
    pattern = re.compile(rf"^{re.escape(safe_id)}_v\d+\.txt$")
    latest_path = None
    latest_mtime = -1.0
    for path in prompt_dir.iterdir():
        if not path.is_file():
            continue
        if not pattern.match(path.name):
            continue
        mtime = path.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_path = path
    return latest_path


def next_eval_path(output_dir: Path, instance_id: str) -> Path:
    pattern_iter = re.compile(
        rf"^{re.escape(instance_id)}_iter(\d+)_\d{{8}}_\d{{6}}\.txt$"
    )
    next_iter = 0
    if output_dir.exists():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            match = pattern_iter.match(path.name)
            if match:
                next_iter = max(next_iter, int(match.group(1)) + 1)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{instance_id}_iter{next_iter}_{timestamp}.txt"


def load_summary_patch_text(summary_patch_path: Path) -> Optional[str]:
    if not summary_patch_path.exists():
        logger.warning("Summary+patch file does not exist: %s", summary_patch_path)
        return None
    try:
        text = summary_patch_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read summary+patch file %s: %s", summary_patch_path, exc)
        return None
    if not text.strip():
        logger.warning("Summary+patch file is empty: %s", summary_patch_path)
        return None
    logger.info("Loaded summaries+patches from %s", summary_patch_path)
    return text


async def call_llm_with_retry(client: AsyncAzureOpenAI, messages: list[dict[str, str]]) -> str:
    max_retries = 10
    base_delay = 2
    max_delay = 60

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model="gpt-5.2-20251211",
                messages=messages,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries:
                logger.error(
                    "Max retries (%s) reached. Final error: %s: %s",
                    max_retries,
                    type(exc).__name__,
                    str(exc)[:200],
                )
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                "API call failed (attempt %s/%s): %s. Retrying in %ss...",
                attempt,
                max_retries,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError("Unexpected retry state")


async def main(
    dataset_path: Path,
    instance_id: Optional[str],
    summary_patch_path: Path,
    output_path: Optional[Path],
) -> None:
    instance = load_instance(dataset_path, instance_id)
    if instance is None:
        if instance_id:
            print(f"Failed to load instance {instance_id} from {dataset_path}")
        else:
            print(f"Failed to load instance from {dataset_path}")
        return

    instance_id = instance.get("instance_id", "unknown")
    prompt_path = find_latest_prompt(FULL_PROMPTS_DIR, instance_id)
    if prompt_path is None:
        print(f"Error: Full prompt dump not found in {FULL_PROMPTS_DIR} for {instance_id}")
        return
    original_prompt = prompt_path.read_text(encoding="utf-8").strip()

    evidence_text = load_summary_patch_text(summary_patch_path)
    if evidence_text is None:
        print("Error: Failed to load summaries+patches text.")
        return

    payload = EVALUATION_PROMPT.format(
        original_prompt=original_prompt,
        summaries_and_patches=evidence_text,
    )

    if output_path is None:
        output_path = next_eval_path(EVALS_DIR, instance_id)

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    print(f"Loaded instance: {instance_id}")
    print(f"Loaded full prompt from {prompt_path}")
    print(f"Loaded summaries+patches ({len(evidence_text)} chars)")
    print(f"\n=== Computing Evaluation for {instance_id} ===")

    evaluation = await call_llm_with_retry(
        client,
        [{"role": "user", "content": payload}],
    )

    print("\n\n>>> EVALUATION PREVIEW (First 500 chars) >>>")
    print("-" * 60)
    if len(evaluation) > 500:
        print(evaluation[:500] + "...")
    else:
        print(evaluation)
    print("-" * 60)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(evaluation, encoding="utf-8")
    print(f"\nSaved evaluation output to {output_path}")


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
    parser.add_argument(
        "--summary_patch_path",
        required=True,
        help="Path to the summaries+patches bundle text.",
    )
    parser.add_argument(
        "--output_path",
        required=False,
        help="Optional path for evaluation output file.",
    )
    args = parser.parse_args()

    summary_patch_path = Path(args.summary_patch_path)
    output_path = Path(args.output_path) if args.output_path else None
    asyncio.run(
        main(
            dataset_path=Path(args.dataset_path),
            instance_id=args.instance_id,
            summary_patch_path=summary_patch_path,
            output_path=output_path,
        )
    )
