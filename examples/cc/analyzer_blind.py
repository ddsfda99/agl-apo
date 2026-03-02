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

COMPARISON_PROMPT = """You are designing a universal coding-agent prompt from two prior attempts.
Inputs are provided only as background evidence; your output must be fully generic.

Goal:
- Produce a reusable prompt that can be applied to many unrelated bug-fix tasks.
- The final prompt must teach process and quality gates, not task/domain specifics.

Strict anti-leak policy (applies to BOTH analysis and final prompt):
- Do not reveal or imply the original case/topic.
- Do not mention or echo: repository names, package names, module names, class/function names, option/flag names, file paths, line numbers, issue IDs, test names, commands, patch fragments, literals, or symbols copied from inputs.
- Do not include problem-domain-specific nouns from this case. Use domain-agnostic wording only.
- Do not quote or closely paraphrase any sentence from the inputs.
- If any sentence sounds tied to one concrete bug pattern, rewrite it into a general engineering principle.

Distillation requirements:
- Compare both attempts by process quality, rigor, risk handling, and validation discipline.
- Extract transferable "do" rules and "avoid" rules.
- Favor principles that generalize across languages/frameworks/repositories.
- Keep the final prompt concise, actionable, and execution-oriented.

Final prompt requirements:
- Must be a single user prompt.
- Must include exactly one task placeholder: {{description}}.
- Must require full requirement coverage (no partial completion).
- Must require: reproduce-first testing, minimal-correct fix, regression verification, and cleanup of temporary artifacts.
- Does NOT need to follow any fixed section format.
- Must not include markdown code fences.

Self-check before finalizing:
1) Remove any case/topic clues.
2) Remove any overly specific implementation wording.
3) Ensure the text still gives concrete execution guidance.
4) Ensure {{description}} appears exactly once.

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

Output format:
- Two parts separated by this exact marker line:
  [[REPHRASED_PROMPT]]
- Before marker: generalized analysis only (no case/topic leak).
- After marker: final universal prompt only.
"""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def next_output_path(output_dir: Path, prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{prefix}_{timestamp}.txt"


def load_attempt_json(path: Path) -> tuple[str, str]:
    data = json.loads(read_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"Attempt JSON must be an object: {path}")

    summary = data.get("summary", "")
    patch = data.get("patch", "")
    if not isinstance(summary, str):
        raise ValueError(f"`summary` must be string: {path}")
    if not isinstance(patch, str):
        raise ValueError(f"`patch` must be string: {path}")
    return summary.strip(), patch.strip()


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
        help="Attempt A JSON with required summary+patch fields.",
    )
    parser.add_argument(
        "--attempt_b_json",
        required=True,
        help="Attempt B JSON with required summary+patch fields.",
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

    summary_a, patch_a = load_attempt_json(attempt_a_path)
    summary_b, patch_b = load_attempt_json(attempt_b_path)

    original_prompt = read_text(original_prompt_path).strip()
    payload = COMPARISON_PROMPT.format(
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
    print("Generating blind generalized comparison + rephrased prompt...")

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
    analysis_output.write_text(f"{analysis_text.strip()}\n", encoding="utf-8")
    prompt_output.write_text(rephrased_prompt, encoding="utf-8")

    print(f"Saved comparison output: {analysis_output}")
    print(f"Saved rephrased prompt: {prompt_output}")


if __name__ == "__main__":
    asyncio.run(main())
