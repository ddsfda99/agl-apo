#!/usr/bin/env python3
"""Prompt optimizer: take an analysis and produce a generalized agent prompt.

Inputs:  Analysis text (from attempt_analyzer.py) + original prompt
Output:  A universal coding-agent prompt with {description} placeholder.
"""

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
GENERAL_PROMPTS_DIR = BASE_DIR / "general_prompts"

ORIGINAL_PROMPT = """\
You are given a code repository in the current directory (/testbed).
The bug description is:
{description}
=================================================
You task is to fix the bug with the following steps:
(1) write test cases to reproduce the bug.
(2) explore the source codes to locate the bug.
(3) edit the source codes to fix the bug.
(4) rerun your written test cases to validate that the bug is fixed. If not, go back to explore the source codes and fix the codes again.
(5) remember to delete the test cases you write at last.
Please do not commit your edits. We will do it later.
"""

PROMPT = """\
You are a prompt engineer refining a prompt that is intended to teach coding agents how to fix bugs effectively.
You are given an analysis of prior coding-agent attempts and the original task prompt.
Your task is to produce a general, comprehensive and non-repetitive coding-agent prompt based on the analysis.

Prompt requirements:
- Must be a single user prompt.
- Must include exactly one task placeholder: {{description}}.
- Must require 4 steps: bug localization, write a new test to reproduce the issue, plan & fix, fix verification.
- Must not include markdown code fences.
- Incorporate the "do" rules and "avoid" rules from the analysis as concrete guidance.
- Must not include any case-specific details from the original prompt or analysis.
- Must not include any specific implementation wording.

original prompt:
{original_prompt}

analysis:
{analysis}

Output the final universal prompt ONLY.
"""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def call_llm_with_retry(
    client: AsyncAzureOpenAI,
    messages: list[dict[str, str]],
) -> str:
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


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a generalized agent prompt from analysis.",
    )
    parser.add_argument(
        "--analysis",
        required=True,
        help="Path to analysis text file (output of attempt_analyzer.py).",
    )
    parser.add_argument(
        "--instance_id",
        required=True,
        help="Instance ID for this case.",
    )
    args = parser.parse_args()

    analysis_path = Path(args.analysis)
    if not analysis_path.exists():
        raise FileNotFoundError(f"Missing required file: {analysis_path}")

    analysis = read_text(analysis_path).strip()

    payload = PROMPT.format(
        original_prompt=ORIGINAL_PROMPT.strip(),
        analysis=analysis,
    )

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    print(f"Analysis: {analysis_path}", file=sys.stderr)
    print("Generating generalized prompt...", file=sys.stderr)

    output_text = await call_llm_with_retry(
        client=client,
        messages=[{"role": "user", "content": payload}],
    )

    # Strip any accidental code fences.
    output_text = re.sub(r"^```(?:\w+)?\s*", "", output_text.strip())
    output_text = re.sub(r"\s*```$", "", output_text.strip())

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = GENERAL_PROMPTS_DIR / f"{args.instance_id}_{ts}.txt"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Save the LLM input for debugging.
    payload_path = output_path.with_suffix(".payload.txt")
    payload_path.write_text(payload, encoding="utf-8")
    print(f"Saved payload: {payload_path}", file=sys.stderr)

    output_path.write_text(output_text.strip() + "\n", encoding="utf-8")
    print(f"Saved prompt: {output_path}", file=sys.stderr)
    print(output_path)  # stdout for shell capture


if __name__ == "__main__":
    asyncio.run(main())
