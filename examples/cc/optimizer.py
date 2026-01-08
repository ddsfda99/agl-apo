import asyncio
from pathlib import Path

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
OPTIMIZER_PROMPT = (PROMPTS_DIR / "optimizer.md").read_text().strip()

BASELINE_PROMPT = """You are given a code repository in the current directory (/testbed).
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

RULESET_PATH = PROMPTS_DIR / "ruleset.md"
EVALUATIONS_PATH = Path(__file__).resolve().parent / "evals.md"


def read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    return path.read_text().strip()


async def main() -> None:
    ruleset = read_required(RULESET_PATH)
    evaluations = read_required(EVALUATIONS_PATH)

    filled_prompt = OPTIMIZER_PROMPT.format(
        baseline_prompt=BASELINE_PROMPT.strip(),
        ruleset=ruleset,
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


if __name__ == "__main__":
    asyncio.run(main())
