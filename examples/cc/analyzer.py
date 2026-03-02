#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
COMPARISONS_DIR = BASE_DIR / "comparisons"
REPHRASED_PROMPTS_DIR = BASE_DIR / "rephrased_prompts"

COMPARISON_PROMPT = """You are a senior SWE-bench reviewer analyzing two coding-agent attempts.
You are given the original prompt and two attempts (summary + patch + outcome for each).

Primary goals:
1) Diagnose why the successful attempt succeeded (causal chain, not just "tests passed").
2) Diagnose why the failed attempt failed (root causes, not just symptoms).
3) Extract reusable good practice/strategy/code-snippet patterns.
4) Extract concrete bad practice/strategy/code-snippet anti-patterns.
5) Produce one complete rephrased prompt for the next run.

Evidence policy:
- Prioritize patch evidence over summary claims.
- If summary and patch conflict, trust the patch and call out the mismatch.
- Keep evidence concrete (function names, hunk intent, validation behavior).

Review dimensions (cover all):
- Correctness and bug-mechanism alignment
- Scope control (minimal necessary changes vs overreach)
- Validation quality (targeted tests, regressions, edge cases)
- Patch completeness (all touched call paths, config/API behavior)
- Risk profile (possible regressions, assumptions, hidden failure modes)

Hard requirements for the rephrased prompt:
- Must be directly runnable as one user prompt.
- Must follow this structure: Objective, Constraints, Plan, Validation & Completion Criteria.
- Validation & Completion Criteria must include:
  targeted test, regression check, sanity check, and explicit pass conditions for task completion.
- Must NOT mention attempt labels, run numbers, trace files, or "previous attempts".
- Must NOT include markdown code fences.

attempt outcomes:
- Attempt A outcome: {outcome_a}
- Attempt B outcome: {outcome_b}

original prompt:
{original_prompt}

attempt A summary:
{summary_a}

attempt A patch:
{patch_a}

attempt B summary:
{summary_b}

attempt B patch:
{patch_b}

Output requirements:
- Analysis content can use any structure you think is best.
- It must cover success/failure diagnosis, good/bad practices, and keep-vs-avoid comparison.
- Output the final response in two parts separated by an exact marker line:
  [[REPHRASED_PROMPT]]
- Everything before the marker is analysis text.
- Everything after the marker is only the final revised prompt text.
"""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def next_output_path(output_dir: Path, prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{prefix}_{timestamp}.txt"


def load_attempt_json(path: Path) -> tuple[str, str, str]:
    data = json.loads(read_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"Attempt JSON must be an object: {path}")

    summary = data.get("summary", "")
    patch = data.get("patch", "")
    resolved = data.get("resolved")
    if not isinstance(summary, str):
        raise ValueError(f"`summary` must be string: {path}")
    if not isinstance(patch, str):
        raise ValueError(f"`patch` must be string: {path}")
    if not isinstance(resolved, bool):
        raise ValueError(f"`resolved` must be boolean: {path}")

    outcome = "success" if resolved else "failure"
    return summary.strip(), patch.strip(), outcome


def split_model_output(full_output: str) -> tuple[str, str]:
    marker = "[[REPHRASED_PROMPT]]"
    if marker in full_output:
        analysis_text, prompt_text = full_output.split(marker, 1)
        analysis = analysis_text.strip()
        prompt = prompt_text.strip()
    else:
        # Fallback for older prompt versions.
        match = re.search(r"##\s*Rephrased Prompt\s*(.+)$", full_output, flags=re.S | re.I)
        if match:
            analysis = full_output[: match.start()].strip()
            prompt = match.group(1).strip()
        else:
            analysis = full_output.strip()
            prompt = full_output.strip()

    prompt = re.sub(r"^```(?:\w+)?\s*", "", prompt)
    prompt = re.sub(r"\s*```$", "", prompt)
    return analysis, prompt.strip()


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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--attempt_a_json",
        required=True,
        help="Attempt A JSON with required summary+patch+resolved(boolean).",
    )
    parser.add_argument(
        "--attempt_b_json",
        required=True,
        help="Attempt B JSON with required summary+patch+resolved(boolean).",
    )
    parser.add_argument(
        "--original_prompt_path",
        required=True,
        help="Path to original prompt text file.",
    )
    parser.add_argument(
        "--analysis_output",
        required=False,
        help="Optional output path for full comparison output text.",
    )
    parser.add_argument(
        "--prompt_output",
        required=False,
        help="Optional output path for extracted rephrased prompt text.",
    )
    args = parser.parse_args()

    attempt_a_path = Path(args.attempt_a_json)
    attempt_b_path = Path(args.attempt_b_json)
    original_prompt_path = Path(args.original_prompt_path)

    for path in (attempt_a_path, attempt_b_path, original_prompt_path):
        if not path.exists():
            raise FileNotFoundError(f"Missing required file: {path}")

    summary_a, patch_a, outcome_a = load_attempt_json(attempt_a_path)
    summary_b, patch_b, outcome_b = load_attempt_json(attempt_b_path)

    original_prompt = read_text(original_prompt_path).strip()
    payload = COMPARISON_PROMPT.format(
        outcome_a=outcome_a,
        outcome_b=outcome_b,
        original_prompt=original_prompt,
        summary_a=summary_a,
        patch_a=patch_a,
        summary_b=summary_b,
        patch_b=patch_b,
    )

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    print(f"Attempt A JSON: {attempt_a_path}")
    print(f"Attempt B JSON: {attempt_b_path}")
    print(f"Original prompt: {original_prompt_path}")
    print(f"Attempt A outcome: {outcome_a}")
    print(f"Attempt B outcome: {outcome_b}")
    print("Generating comparison + rephrased prompt...")

    output_text = await call_llm_with_retry(
        client=client,
        messages=[{"role": "user", "content": payload}],
    )

    analysis_text, rephrased_prompt = split_model_output(output_text)

    analysis_output = (
        Path(args.analysis_output)
        if args.analysis_output
        else next_output_path(COMPARISONS_DIR, "comparison")
    )
    prompt_output = (
        Path(args.prompt_output)
        if args.prompt_output
        else next_output_path(REPHRASED_PROMPTS_DIR, "rephrased_prompt")
    )

    analysis_output.parent.mkdir(parents=True, exist_ok=True)
    prompt_output.parent.mkdir(parents=True, exist_ok=True)
    output_with_outcomes = (
        "## Attempt Outcomes\n"
        f"- Attempt A: {outcome_a}\n"
        f"- Attempt B: {outcome_b}\n\n"
        f"{analysis_text.strip()}\n"
    )
    analysis_output.write_text(output_with_outcomes, encoding="utf-8")
    prompt_output.write_text(rephrased_prompt, encoding="utf-8")

    print(f"Saved comparison output: {analysis_output}")
    print(f"Saved rephrased prompt: {prompt_output}")


if __name__ == "__main__":
    asyncio.run(main())
