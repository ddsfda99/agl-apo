#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
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
ANALYSIS_DIR = BASE_DIR / "analysis"

PROMPT = """You are a code reviewer analyzing the outcomes of multiple coding-agent attempts at the same SWE-bench task. 
You are given the original prompt and each attempt's summary and patch.
You need to determine whether each attempt was successful or not, and produce a structured and comprehensive analysis:
1) Diagnose why successful attempts succeeded.
2) Diagnose why failed attempts failed.
3) Extract reusable good practice/strategy/code-snippet patterns.
4) Extract concrete bad practice/strategy/code-snippet anti-patterns.

original prompt:
{original_prompt}

attempts:
{attempts}
"""

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def next_output_path(output_dir: Path, prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{prefix}_{timestamp}.txt"


def load_attempt_json(path: Path) -> dict[str, str]:
    """Load a single attempt JSON (summary + patch + resolved)."""
    data = json.loads(read_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"Attempt JSON must be an object: {path}")

    summary = data.get("summary", "")
    patch = data.get("patch", "")
    if not isinstance(summary, str):
        raise ValueError(f"`summary` must be string: {path}")
    if not isinstance(patch, str):
        raise ValueError(f"`patch` must be string: {path}")

    return {"summary": summary.strip(), "patch": patch.strip()}


def build_prompt(original_prompt: str, attempts: list[dict[str, str]]) -> str:
    """Assemble the LLM prompt from N attempts."""
    parts: list[str] = []
    for idx, att in enumerate(attempts, 1):
        parts.append(
            f"attempt {idx}:\n"
            f"  summary: {att['summary']}\n"
            f"  patch: {att['patch']}"
        )
    return PROMPT.format(
        n_attempts=len(attempts),
        original_prompt=original_prompt,
        attempts="\n\n".join(parts),
    )


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
        "--attempts",
        nargs="+",
        required=True,
        metavar="JSON",
        help="One or more attempt JSON files (each with summary and patch).",
    )
    parser.add_argument(
        "--instance_id",
        required=True,
        help="Instance ID for this case.",
    )
    parser.add_argument(
        "--original_prompt_path",
        required=True,
        help="Path to original prompt text file.",
    )
    args = parser.parse_args()

    original_prompt_path = Path(args.original_prompt_path)
    if not original_prompt_path.exists():
        raise FileNotFoundError(f"Missing required file: {original_prompt_path}")

    attempt_paths = [Path(p) for p in args.attempts]
    for p in attempt_paths:
        if not p.exists():
            raise FileNotFoundError(f"Missing attempt file: {p}")

    attempts = [load_attempt_json(p) for p in attempt_paths]
    original_prompt = read_text(original_prompt_path).strip()
    payload = build_prompt(original_prompt, attempts)

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    for i, p in enumerate(attempt_paths, 1):
        print(f"Attempt {i}: {p}", file=sys.stderr)
    print(f"Original prompt: {original_prompt_path}", file=sys.stderr)
    print(f"Attempts: {len(attempts)} total", file=sys.stderr)
    print("Generating analysis...", file=sys.stderr)

    output_text = await call_llm_with_retry(
        client=client,
        messages=[{"role": "user", "content": payload}],
    )

    analysis_output = next_output_path(ANALYSIS_DIR, args.instance_id)
    analysis_output.parent.mkdir(parents=True, exist_ok=True)

    # Save the LLM input for debugging.
    payload_path = analysis_output.with_suffix(".payload.txt")
    payload_path.write_text(payload, encoding="utf-8")
    print(f"Saved payload: {payload_path}", file=sys.stderr)

    analysis_output.write_text(output_text.strip() + "\n", encoding="utf-8")

    print(f"Saved analysis: {analysis_output}", file=sys.stderr)
    print(analysis_output)  # stdout for shell capture


if __name__ == "__main__":
    asyncio.run(main())
