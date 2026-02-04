import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SKILL_OUT_DIR = Path("skill_outputs")

META_SKILL_EXTRACTION_PROMPT = """You are a Meta-Cognitive Analyst. Your goal is to review the trajectory of a software
engineering task (SWE-bench execution logs) and distill reusable, generalized skills that can
be applied to future unseen tasks.

Input:
- Trajectory: The step-by-step history of commands, code changes, and tool outputs.
- Outcome: Whether the task was Solved or Failed (infer if possible; otherwise use "unknown").

Core Instruction:
Do not just summarize what happened. Analyze how the agent succeeded or failed. Identify the
underlying cognitive patterns or tactical decisions that determined the outcome.

Analysis Strategy:
1) Identify Turning Points: Find the specific command or decision that significantly advanced
   the solution (Positive Signal) or led to a dead-end (Negative Signal).
2) Abstract from Context: Strip away specific file names and specific bugs. Focus on the method.
   - Specific: "The agent grepped for 'def get_user' in models.py."
   - Abstract Skill: "Definition-First Navigation".
3) Formulate the Skill: Create a distinct name and an actionable definition for this behavior.

Output Schema (JSON only):
[
  {{
    "Skill_Name": "Create a catchy, professional name (e.g., 'Traceback-Driven Localization')",
    "Trigger_Context": "When should an agent activate this skill?",
    "Actionable_Rule": "The specific instruction to follow. Must be imperative.",
    "Rationale": "Why does this work? (Derived from the trajectory analysis)",
    "Type": "Strategic" | "Tactical" | "Anti-Pattern"
  }}
]

Guidance Examples (use for style only):
Skill_Name: "Reproduction Script Anchoring"
Trigger_Context: Start of any bug-fixing task.
Actionable_Rule: Create a standalone reproduce_issue.py that fails with the reported error before modifying any codebase files.
Rationale: The agent wasted 10 turns blindly editing code. Once it wrote the script, it solved the bug in 2 turns.
Type: Strategic

Skill_Name: "Import Verification Check"
Trigger_Context: Before writing import statements for auxiliary tools.
Actionable_Rule: Verify if a package is installed in the environment before importing it in the solution.
Rationale: The agent failed because it tried to use a missing package, causing a distraction loop.
Type: Anti-Pattern

Skill_Name: "Unique String Triangulation"
Trigger_Context: Locating relevant code for a generic error message.
Actionable_Rule: Search for unique string literals or variable names found in the traceback.
Rationale: Searching for generic errors returned many files; unique strings returned the right file.
Type: Tactical

Trajectory:
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
    parser = argparse.ArgumentParser(description="Extract meta-skills from a trace.")
    parser.add_argument("trace_path", help="Path to trace JSON/JSONL file.")
    args = parser.parse_args()

    trace_path = Path(args.trace_path)
    trace_text = trace_path.read_text(encoding="utf-8")
    if not trace_text.strip():
        raise ValueError(f"Empty trace file: {trace_path}")

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    prompt = META_SKILL_EXTRACTION_PROMPT.format(trace=trace_text)
    response = await call_llm_with_retry(client, [{"role": "user", "content": prompt}])

    SKILL_OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SKILL_OUT_DIR / f"{trace_path.stem}.json"
    out_path.write_text(response.strip() + "\n", encoding="utf-8")
    logger.info("Wrote meta-skills to %s", out_path)


if __name__ == "__main__":
    asyncio.run(main())
