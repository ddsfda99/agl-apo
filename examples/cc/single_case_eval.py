# Evaluate a single case.
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

# Single case dataset and logs
SINGLE_CASE_PATH = Path("examples/cc/astropy__astropy-7606.jsonl")
TEST_OUTPUT_PATH = Path("examples/cc/logs/run_evaluation/epoch_0/cc/astropy__astropy-14182/test_output.txt")

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

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
LLM_EVALS_PROMPT = (PROMPTS_DIR / "llm-evals.md").read_text().strip()


def load_single_case(dataset_path: Path) -> Optional[Dict[str, Any]]:
    """Load the single case from JSONL file."""
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                return json.loads(line)
    return None


async def main() -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    # Load the single case
    instance = load_single_case(SINGLE_CASE_PATH)
    if instance is None:
        print(f"Failed to load instance from {SINGLE_CASE_PATH}")
        return
    
    instance_id = instance.get("instance_id", "unknown")
    print(f"Loaded instance: {instance_id}")
    
    # Read test output log
    if TEST_OUTPUT_PATH.exists():
        test_output = TEST_OUTPUT_PATH.read_text()
        print(f"Loaded test output from {TEST_OUTPUT_PATH} ({len(test_output)} chars)")
    else:
        test_output = f"Problem Statement:\n{instance.get('problem_statement', '')}"
        print(f"No test output found, using problem_statement")
    
    # Build the LLM-as-judge prompt
    eval_prompt = LLM_EVALS_PROMPT.format(
        task_description=SEED_PROMPT.strip(),
        test_output=test_output.strip(),
    )
    
    print(f"\n=== Evaluating {instance_id} ===")
    
    resp = await client.chat.completions.create(
        model="gpt-5-20250807",
        messages=[{"role": "user", "content": eval_prompt}],
    )
    
    content = resp.choices[0].message.content or ""
    print(content)


if __name__ == "__main__":
    asyncio.run(main())
