"""
Fill the LLM-evals meta-prompt with a task description and pytest output, then
send it to the text gradient model for scoring. No other passes are run.
"""

import asyncio
from pathlib import Path

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

TEST_OUTPUT_PATH = Path(
    "examples/cc/logs/run_evaluation/epoch_0/cc/matplotlib__matplotlib-22835/test_output.txt"
)

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


async def main() -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    log = TEST_OUTPUT_PATH.read_text()

    # Build the LLM-as-judge prompt by filling the meta-prompt with the seed task and test output.
    eval_prompt = LLM_EVALS_PROMPT.format(
        task_description=SEED_PROMPT.strip(),
        test_output=log.strip(),
    )
    print("\n=== LLM evals meta-prompt ===")
    print(eval_prompt)

    # Send the filled prompt to the gradient model and print the judgment.
    resp = await client.chat.completions.create(
        model="gpt-5-20250807",
        messages=[{"role": "user", "content": eval_prompt}],
    )

    # Some SDK/runtime combos can return a raw string/dict on error; guard for that.
    if isinstance(resp, str):
        print("\n=== LLM evals model output ===")
        print(resp)
        return

    content = ""
    try:
        content = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        print("\n[warn] Unable to parse completion response:", repr(exc))
        print("Raw response:", resp)

    print("\n=== LLM evals model output ===")
    print(content if content else "[empty]")


if __name__ == "__main__":
    asyncio.run(main())
