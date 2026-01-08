"""
Fill the LLM-evals meta-prompt with a task description and pytest output, then
send it to the text gradient model for scoring. No other passes are run.

Outputs JSON results to examples/cc/prompts/GT-evals.md with instance_id and model output.
Only evaluates instances that have existing logs in examples/cc/logs/epoch_0/.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

# Path to the SWE-bench lite dataset (JSONL format)
SWEBENCH_DATASET_PATH = Path("examples/cc/swebench-lite.json")
LOGS_DIR = Path("examples/cc/logs/epoch_0")
OUTPUT_PATH = Path("examples/cc/prompts/GT-evals.md")

SEED_PROMPT = """You are given a code repository in the current directory (/testbed).
    The bug description is:
    {description}
    =================================================
    You task is to fix the bug with the following steps:
    (1) write test cases to reproduce the bug.
    (2) explore the source codes to locate the bug.
    (3) edit the source codes to fix the bug.
    (4) rerun your written test cases to validate that the bug is fixed. If not, go back to explore the source codes and fix the codes again.
    (5) remember to delete the test cases you write at last.
    Please do not commit your edits. We will do it later."""

PROMPTS_DIR = Path(__file__).resolve().parent
LLM_EVALS_PROMPT = (PROMPTS_DIR / "supervised-evals.md").read_text().strip()


def get_logged_instance_ids(logs_dir: Path) -> Set[str]:
    """Get the set of instance_ids that have logs in the logs directory."""
    if not logs_dir.exists():
        return set()
    # Each file in the logs dir is named by instance_id
    return {f.name for f in logs_dir.iterdir() if f.is_file()}


def load_instances_from_jsonl(dataset_path: Path, instance_ids: Set[str]) -> List[Dict[str, Any]]:
    """Load instances from JSONL file that match the given instance_ids."""
    instances = []
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                if record.get("instance_id") in instance_ids:
                    instances.append(record)
    return instances


async def evaluate_instance(
    client: AsyncAzureOpenAI,
    instance: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Evaluate a single instance and return the result dict."""
    instance_id = instance.get("instance_id", "unknown")
    problem_statement = instance.get("problem_statement", "")
    patch = instance.get("patch", "")
    
    # For now, we use the problem_statement as test_output since we don't have actual test output
    # In a real scenario, you would read test_output from logs
    test_output = f"Problem Statement:\n{problem_statement}"
    
    # Build the LLM-as-judge prompt
    eval_prompt = LLM_EVALS_PROMPT.format(
        task_description=SEED_PROMPT.strip(),
        test_output=test_output.strip(),
        patch=patch,
    )
    
    print(f"\n=== Evaluating {instance_id} ===")
    
    try:
        resp = await client.chat.completions.create(
            model="gpt-5-20250807",
            messages=[{"role": "user", "content": eval_prompt}],
        )
        
        # Handle raw string response
        if isinstance(resp, str):
            content = resp
        else:
            content = resp.choices[0].message.content or ""
        
        print(f"[done] {instance_id}: Got response ({len(content)} chars)")
        
        return {
            "instance_id": instance_id,
            "model_output": content,
        }
        
    except Exception as exc:
        print(f"[error] {instance_id}: API call failed: {exc}")
        return {
            "instance_id": instance_id,
            "model_output": f"[ERROR] {exc}",
        }


async def main() -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    # Get instance_ids from existing logs
    logged_ids = get_logged_instance_ids(LOGS_DIR)
    print(f"Found {len(logged_ids)} instances with logs in {LOGS_DIR}")
    
    if not logged_ids:
        print("No logged instances found. Exiting.")
        return
    
    # Load only instances that have logs
    instances = load_instances_from_jsonl(SWEBENCH_DATASET_PATH, logged_ids)
    print(f"Matched {len(instances)} instances from dataset")
    
    if not instances:
        print("No matching instances found in dataset. Exiting.")
        return
    
    # Evaluate all instances
    results = []
    for instance in instances:
        result = await evaluate_instance(client, instance)
        if result:
            results.append(result)
    
    # Write results to output file as JSON
    output_content = json.dumps(results, indent=2, ensure_ascii=False)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(output_content, encoding="utf-8")
    
    print(f"\n=== Results ===")
    print(f"Evaluated {len(results)} instances")
    print(f"Output written to: {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
