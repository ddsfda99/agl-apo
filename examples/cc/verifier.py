import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TRACE_DIR = BASE_DIR / "trace"

VERIFIER_PROMPT = """You are a verifier agent. You are given:

Task description:
{description}

Trace from a previous run:
{trace}

Your tasks:
1) Extract the minimal reproduction test the agent wrote or implied in the trace. Include exact file paths, code snippets, and test commands if present.
2) Produce a new user prompt for cc_agent that includes:
   - The literal placeholder {{description}} (exactly as written, including braces).
   - A clear "Reproduction test" section containing the extracted steps.
   - The required workflow: write tests, locate bug, implement fix, rerun tests, cleanup temporary tests.
   - Concise, actionable instructions only.

Output ONLY the new prompt text. Do not include analysis or extra commentary.
"""


def load_instance(dataset_path: Path, instance_id: str) -> Optional[Dict[str, Any]]:
    if not dataset_path.exists():
        logger.error("Dataset file not found: %s", dataset_path)
        return None
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("instance_id") == instance_id:
                return item
    logger.error("Instance %s not found in %s", instance_id, dataset_path)
    return None


def find_latest_extracted_trace(instance_id: Optional[str] = None) -> Optional[Path]:
    if not TRACE_DIR.exists():
        return None
    if instance_id:
        pattern = f"{instance_id}_extracted*.json"
        matches = list(TRACE_DIR.glob(pattern))
    else:
        matches = list(TRACE_DIR.glob("*_extracted*.json"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


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


def load_trace_text(trace_path: Path) -> Optional[str]:
    raw = trace_path.read_text(encoding="utf-8")
    data = _parse_trace_payload(raw)
    if data is None:
        logger.error("Failed to parse trace %s", trace_path)
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

    logger.error("Trace format invalid in %s", trace_path)
    return None


def ensure_description_placeholder(prompt_text: str) -> str:
    if "{description}" in prompt_text or "{{description}}" in prompt_text:
        return prompt_text
    header = "Task description:\n{description}\n\n"
    return header + prompt_text.lstrip()


def next_output_path(output_dir: Path, instance_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in instance_id)
    return output_dir / f"{safe_id}_verifier_{timestamp}.txt"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_path", required=True, help="Path to JSONL dataset file.")
    parser.add_argument("--instance_id", required=True, help="Instance id to load from dataset.")
    parser.add_argument("--trace_path", help="Path to extracted trace JSON. Defaults to latest.")
    parser.add_argument("--output_dir", default=str(BASE_DIR / "verifier_prompts"), help="Output directory.")
    parser.add_argument("--model", default="gpt-5-20250807", help="Model name.")
    args = parser.parse_args()

    dataset_path = Path(args.dataset_path)
    instance = load_instance(dataset_path, args.instance_id)
    if instance is None:
        return

    description = instance.get("problem_statement") or instance.get("description") or ""
    if not description:
        logger.error("Missing problem_statement/description for %s", args.instance_id)
        return

    if args.trace_path:
        trace_path = Path(args.trace_path)
    else:
        trace_path = find_latest_extracted_trace(args.instance_id)
        if trace_path is None:
            logger.error("No extracted trace found in %s", TRACE_DIR)
            return

    trace_text = load_trace_text(trace_path)
    if not trace_text:
        logger.error("Failed to load trace content from %s", trace_path)
        return

    prompt = VERIFIER_PROMPT.format(description=description, trace=trace_text)

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    response = await client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content or ""
    content = ensure_description_placeholder(content.strip())

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = next_output_path(output_dir, args.instance_id)
    output_path.write_text(content, encoding="utf-8")
    print(f"Wrote verifier prompt to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
