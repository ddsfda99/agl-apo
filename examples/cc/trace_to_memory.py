import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TRACE_TO_MEMORY_PROMPT = """You are preparing repository-specific durable memory for a coding agent.
You are given:
1) The existing CLAUDE.md (may be empty).
2) A trace to perform a coding task in the repository.

Your task is to update CLAUDE.md by:
- Correcting any wrong statements.
- Refining existing statements to be more precise.
- Adding new verified and useful information from the trace.

Guidelines:
1. Extract Validated Facts and Confirmed Findings, e.g.:
- facts verified by execution logs
- reproduction commands that triggered the bug
- root cause analysis
- fixes that worked
- improvements that succeeded
2. Pitfalls to Avoid:
- common mistakes made during the attempt
- environment quirks that caused issues
- misunderstandings of requirements
- approaches that were tried and failed
- missteps to not repeat
3. Current Progress:
- what has been successfully implemented
- tests that are passing

CLAUDE.md:
{existing}

Trace:
{trace}
"""

async def call_llm_with_retry(client: AsyncAzureOpenAI, messages: list[Dict[str, Any]]) -> str:
    max_retries = 10
    base_delay = 2
    max_delay = 60

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model="gpt-5-20250807",
                messages=messages,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            if attempt >= max_retries:
                logger.error(
                    "Max retries (%s) reached. Final error: %s",
                    max_retries,
                    type(exc).__name__,
                )
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                "API call failed (attempt %s/%s): %s. Retrying in %s seconds...",
                attempt,
                max_retries,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError("Unexpected state in retry logic")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Update CLAUDE.md from a trace file.")
    parser.add_argument("trace_path", help="Path to trace JSON/JSONL file.")
    args = parser.parse_args()

    trace_path = Path(args.trace_path)
    trace_text = trace_path.read_text(encoding="utf-8")
    if not trace_text.strip():
        raise ValueError(f"Empty trace file: {trace_path}")

    existing_path = Path("CLAUDE.md")
    existing_text = existing_path.read_text(encoding="utf-8") if existing_path.exists() else ""

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    prompt = TRACE_TO_MEMORY_PROMPT.format(trace=trace_text, existing=existing_text)
    response = await call_llm_with_retry(client, [{"role": "user", "content": prompt}])

    output_path = Path("CLAUDE.md")
    output_path.write_text(response.strip() + "\n", encoding="utf-8")
    logger.info("Wrote memory to %s", output_path)


if __name__ == "__main__":
    asyncio.run(main())
