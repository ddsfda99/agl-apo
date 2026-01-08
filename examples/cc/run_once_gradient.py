import asyncio
from pathlib import Path

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

TEST_OUTPUT_PATH = Path(
    "examples/cc/logs1/run_evaluation/epoch_0/cc/astropy__astropy-14182/test_output.txt"
)

# Keep the prompt short and focused so the gradient model returns concise guidance.
SYSTEM_PROMPT = (
    "You are the gradient model. Given raw pytest output, identify why tests failed and "
    "propose brief guidance to fix the failure. Return concise bullet points (max 6) in "
    "plain text, no code unless needed."
)


async def main() -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    # Limit the log length to avoid prompt overflow. Take the tail of the file.
    raw = TEST_OUTPUT_PATH.read_text()
    log = raw[-1200:]

    resp = await client.chat.completions.create(
        model="gpt-4o-20241120",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Pytest output:\n{log}"},
        ],
        response_format={"type": "text"},
        max_completion_tokens=500,
    )
    content = resp.choices[0].message.content
    print("=== Gradient model output ===")
    if content:
        print(content)
    else:
        print("[empty]")
        print("Full response for debugging:")
        print(resp)


if __name__ == "__main__":
    asyncio.run(main())
