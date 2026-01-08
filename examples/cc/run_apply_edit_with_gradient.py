"""
Call the gradient model on pytest output, then feed that feedback plus the original
seed prompt into an apply-edit model to produce an improved prompt.
"""

import asyncio
from pathlib import Path

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

TEST_OUTPUT_PATH = Path(
    "examples/cc/logs/run_evaluation/epoch_0/cc/astropy__astropy-14182/test_output.txt"
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


def print_resp_details(label, resp) -> None:
    print(f"\n=== {label} raw response ===")
    try:
        print(resp.model_dump(exclude_none=True))
    except Exception:
        try:
            print(resp.to_dict())
        except Exception:
            print(resp)


async def main() -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    # Read and trim log to avoid oversized prompts.
    raw = TEST_OUTPUT_PATH.read_text()
    log = raw

    # 1) Gradient model pass
    grad_resp = await client.chat.completions.create(
        model="gpt-5-20250807",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the gradient model. Given raw pytest output, identify why tests "
                    "failed and propose brief guidance to fix the failure. Return concise bullet "
                    "points (max 6) in plain text, no code unless needed."
                ),
            },
            {"role": "user", "content": f"Pytest output:\n{log}"},
        ],
    )
    grad_text = grad_resp.choices[0].message.content or ""

    print("=== Gradient model output ===")
    print(grad_text if grad_text else "[empty]")
    print_resp_details("Gradient model", grad_resp)

    # 2) Apply-edit model pass: rewrite/adjust the seed prompt using gradient feedback.
    apply_system_prompt = (
        "You are the apply-edit model. Given an original prompt and gradient feedback about a "
        "failure case, produce a revised prompt that is highly specific and actionable. "
        "Explicitly call out data-format/type checks, conversion logic, and where to inspect or "
        "patch (e.g., file/module hints). Keep it concise, plain text, and preserve the core intent, "
        "but strengthen it with concrete steps and verification guidance."
    )

    apply_user_prompt = (
        "Original prompt:\n"
        f"{SEED_PROMPT}\n\n"
        "Gradient feedback (issues and possible fixes):\n"
        f"{grad_text}\n\n"
        "Return only the revised prompt, making the fixes as concrete and specific as possible."
    )

    apply_resp = await client.chat.completions.create(
        model="gpt-5-20250807",
        messages=[
            {"role": "system", "content": apply_system_prompt},
            {"role": "user", "content": apply_user_prompt},
        ],
    )
    apply_text = apply_resp.choices[0].message.content or ""

    print("\n=== Apply-edit model output (revised prompt) ===")
    print(apply_text if apply_text else "[empty]")
    print_resp_details("Apply-edit model", apply_resp)


if __name__ == "__main__":
    asyncio.run(main())
