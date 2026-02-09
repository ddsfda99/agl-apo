#!/usr/bin/env python3
import argparse
import asyncio
from pathlib import Path
from typing import List

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

BASE_DIR = Path(__file__).resolve().parent
TEXTGRADS_DIR = BASE_DIR / "textgrads"

LEARNER_PROMPT = """You are a learner to extract valuable lessons from evaluations of previous attempts.
You are given three evaluation analyses from different attempts on the same task.
Your job is to extract durable rules , tactics that can help improve future attempts on similar tasks,
failures to avoid, and strategies that worked well.
Summarize these insights into three sections: Task-Specific, Repo-Specific, and General

Output format:
Task-Specific:
- ...
Repo-Specific:
- ...
General:
- ...

Evaluations:
{evaluations}
"""


def _read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    return path.read_text(encoding="utf-8").strip()


def _find_latest_textgrads(instance_id: str, limit: int) -> List[Path]:
    if not TEXTGRADS_DIR.exists():
        return []
    candidates = [
        p
        for p in TEXTGRADS_DIR.iterdir()
        if p.is_file() and p.name.startswith(f"{instance_id}_") and p.suffix == ".txt"
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:limit]


def _format_evaluations(instance_id: str, paths: List[Path]) -> str:
    sections = []
    for idx, path in enumerate(paths, 1):
        text = _read_required(path)
        sections.append(f"=== Evaluation {idx} ({instance_id}) ===\n{text}")
    return "\n\n".join(sections)


async def _summarize_with_llm(evaluations: str) -> str:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )
    prompt = LEARNER_PROMPT.format(evaluations=evaluations)
    resp = await client.chat.completions.create(
        model="gpt-5.2-20251211",
        messages=[{"role": "user", "content": prompt}],
    )
    content = ""
    try:
        content = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        print("\n[warn] Unable to parse completion response:", repr(exc))
        print("Raw response:", resp)
    return content.strip()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance_id", required=True, help="Instance id")
    parser.add_argument(
        "--inputs",
        nargs="*",
        help="Optional list of textgrad output paths (defaults to latest 3)",
    )
    parser.add_argument(
        "--output_path",
        required=False,
        help="Where to write the learned memory text",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=3,
        help="Number of evaluations to combine when using auto-discovery",
    )
    args = parser.parse_args()

    instance_id = args.instance_id
    if args.inputs:
        paths = [Path(p) for p in args.inputs]
    else:
        paths = _find_latest_textgrads(instance_id, args.max)

    required = args.max
    if len(paths) < required:
        raise SystemExit(
            f"Need {required} textgrad outputs, found {len(paths)}. Provide --inputs or ensure textgrads exist."
        )

    evaluations = _format_evaluations(instance_id, paths[:required])
    learned = await _summarize_with_llm(evaluations)

    if args.output_path == "-":
        print(learned)
        return

    if args.output_path:
        out_path = Path(args.output_path)
    else:
        out_path = TEXTGRADS_DIR / f"{instance_id}_memory.txt"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(learned, encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    asyncio.run(main())
