import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TRACE_DIR = BASE_DIR / "trace"

TRACE_SUMMARY_PROMPT = """You are given the full trace of a coding agent's attempt to complete a programming task.
Your job is to produce a concise and precise summary of the attempt to put into memory, including:
1) The codebase context to avoid repeated explorations during next attempts.
2) Pitfalls encountered: what went wrong and why to avoid in future.
3) Correct Steps taken: what worked well to replicate in future.

Trace:
{trace}
"""


def find_latest_extracted_trace(instance_id: Optional[str] = None) -> Optional[Path]:
    """Find the extracted trace file (uses filesystem mtime to determine latest)."""
    if not TRACE_DIR.exists():
        return None

    if instance_id:
        pattern = f"{instance_id}_extracted*.json"
        matching_files = list(TRACE_DIR.glob(pattern))
    else:
        matching_files = list(TRACE_DIR.glob("*_extracted*.json"))
    if not matching_files:
        return None
    return max(matching_files, key=lambda p: p.stat().st_mtime)


def _parse_trace_payload(raw: str) -> Optional[Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    stripped = raw.lstrip()
    if stripped.startswith('"trajectory"'):
        try:
            return json.loads("{" + raw + "}")
        except json.JSONDecodeError:
            return None

    # JSONL fallback: first object with trace/trajectory.
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("trace" in obj or "trajectory" in obj):
            return obj
    return None


def _render_trace_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content_str = json.dumps(content, ensure_ascii=True)
        else:
            content_str = str(content)
        lines.append(f"{role}: {content_str}")
    return "\n".join(lines).strip()


def _render_trajectory_items(items: list[dict]) -> str:
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "unknown")
        if item_type in {"assistant", "user"}:
            msg = item.get("message", {})
            role = msg.get("role", item_type)
            content = msg.get("content", "")
            if isinstance(content, list):
                content_str = json.dumps(content, ensure_ascii=True)
            else:
                content_str = str(content)
            lines.append(f"{role}: {content_str}")
            continue
        if item_type == "system":
            lines.append("system: init")
            continue
        if item_type == "result":
            payload = {
                "subtype": item.get("subtype"),
                "is_error": item.get("is_error"),
                "num_turns": item.get("num_turns"),
                "errors": item.get("errors"),
            }
            lines.append(f"result: {json.dumps(payload, ensure_ascii=True)}")
            continue

        lines.append(f"{item_type}: {json.dumps(item, ensure_ascii=True)}")
    return "\n".join(lines).strip()


def _load_trace_text(trace_path: Path) -> Optional[str]:
    raw = trace_path.read_text(encoding="utf-8")
    data = _parse_trace_payload(raw)
    if data is None:
        logger.warning("Failed to parse trace %s", trace_path)
        return None

    if isinstance(data, dict):
        if isinstance(data.get("trace"), list):
            rendered = _render_trace_messages(data["trace"])
            return rendered or None
        if isinstance(data.get("trajectory"), list):
            rendered = _render_trajectory_items(data["trajectory"])
            return rendered or None

    if isinstance(data, list):
        rendered = _render_trajectory_items(data)
        return rendered or None

    logger.warning("Trace format invalid in %s", trace_path)
    return None


async def call_llm_with_retry(client: AsyncAzureOpenAI, messages: list[Dict[str, Any]]) -> str:
    """Call LLM with automatic retry on all API failures."""
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


async def main(trace_path: Optional[str]) -> None:
    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    if trace_path:
        resolved_path = Path(trace_path)
    else:
        resolved_path = find_latest_extracted_trace()
        if resolved_path is None:
            print(f"No extracted trace found in {TRACE_DIR}")
            return

    trace_text = _load_trace_text(resolved_path)
    if not trace_text:
        print(f"Failed to load trace content from {resolved_path}")
        return

    prompt = TRACE_SUMMARY_PROMPT.format(trace=trace_text)
    response = await call_llm_with_retry(
        client,
        [{"role": "user", "content": prompt}],
    )
    output_path = Path("CLAUDE.md")
    output_path.write_text(response, encoding="utf-8")
    print(f"Wrote summary to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trace_path",
        help="Optional trace file path. Defaults to latest in trace/.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.trace_path))
