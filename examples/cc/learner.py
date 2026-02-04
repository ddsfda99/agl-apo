import argparse
import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict

from openai import AsyncAzureOpenAI

from utils.cloudgpt_aoai import get_openai_token_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SKILLS_DIR = Path("skills")
LEARNER_OUT_DIR = Path("learner_outputs")

REFLECTION_PROMPT = """You are a senior software engineer reviewing an agent trajectory.
Your task:
1) Decide whether the agent solved the task.
2) Assess logical soundness of its reasoning.
3) Verify steps are justified and edge cases handled.
4) Identify repetitive patterns/actions that could be abstracted.

Return STRICT JSON only, with exactly these keys:
{
  "solved": true|false|"unknown",
  "confidence": 0.0-1.0,
  "logical_soundness": {
    "rating": "sound"|"mixed"|"weak",
    "rationale": "string"
  },
  "justification_gaps": ["string", ...],
  "edge_cases": {
    "mentioned": ["string", ...],
    "missing": ["string", ...],
    "notes": "string"
  },
  "repetitions": [
    {"pattern": "string", "count": 0, "abstraction": "string"}
  ],
  "verification": {
    "tests_observed": ["string", ...],
    "gaps": ["string", ...]
  },
  "summary": "string"
}

Trajectory:
{trace}
"""


SKILL_CREATION_PROMPT = """You are creating a Codex skill. Follow the skill-creator guidelines:
- Output ONLY the SKILL.md content.
- YAML frontmatter must include only: name, description.
- Body must use imperative/infinitive form.
- Keep concise; include sections: Workflow, Approaches, Common Pitfalls, Verification Strategies.
- Do not mention additional files or packaging steps.

{skill_name_instruction}
Skill purpose: Reflect on coding-agent trajectories to assess task success, reasoning soundness,
justification coverage, edge-case handling, and abstraction opportunities.

Optional context (reflection output):
{reflection}
"""


def _load_trace_text(trace_path: Path) -> str:
    raw = trace_path.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError(f"Empty trace file: {trace_path}")
    return raw


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        parts = stripped.split("```")
        if len(parts) >= 3:
            body = parts[1].strip()
            lines = body.splitlines()
            if lines and re.fullmatch(r"[A-Za-z0-9_-]+", lines[0].strip()):
                body = "\n".join(lines[1:]).strip()
            return body
    return stripped


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


def _sanitize_skill_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not name:
        return "trajectory-reflection"
    return name[:63]


def _extract_skill_name(skill_md: str) -> str | None:
    lines = skill_md.strip().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return None
    for line in lines[1:end_idx]:
        match = re.match(r"\s*name:\s*(.+)\s*$", line)
        if match:
            name = match.group(1).strip().strip('"').strip("'")
            return name or None
    return None


async def generate_reflection(client: AsyncAzureOpenAI, trace_text: str) -> str:
    prompt = TRACE_REFLECTION_PROMPT.format(trace=trace_text)
    response = await call_llm_with_retry(client, [{"role": "user", "content": prompt}])
    return _strip_code_fence(response)


async def generate_skill(client: AsyncAzureOpenAI, reflection_text: str) -> str:
    skill_name_instruction = (
        "Choose a concise skill name in kebab-case (<=63 chars) and put it in YAML frontmatter."
    )
    prompt = SKILL_CREATION_PROMPT.format(
        skill_name_instruction=skill_name_instruction,
        reflection=reflection_text,
    )
    response = await call_llm_with_retry(client, [{"role": "user", "content": prompt}])
    return _strip_code_fence(response)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reflect on agent trajectories using an LLM.")
    parser.add_argument("trajectory", help="Path to trajectory JSON/JSONL file.")
    args = parser.parse_args()

    trace_path = Path(args.trajectory)
    trace_text = _load_trace_text(trace_path)

    token_provider = get_openai_token_provider()
    client = AsyncAzureOpenAI(
        api_version="2025-04-01-preview",
        azure_endpoint="https://cloudgpt-openai.azure-api.net/",
        azure_ad_token_provider=token_provider,
    )

    reflection_text = await generate_reflection(client, trace_text)

    LEARNER_OUT_DIR.mkdir(parents=True, exist_ok=True)
    reflection_path = LEARNER_OUT_DIR / f"{trace_path.stem}.json"
    reflection_path.write_text(reflection_text + "\n", encoding="utf-8")
    logger.info("Wrote reflection to %s", reflection_path)

    skill_md = await generate_skill(client, reflection_text)
    inferred_name = _extract_skill_name(skill_md) or "trajectory-reflection"
    skill_name = _sanitize_skill_name(inferred_name)
    skill_dir = SKILLS_DIR / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md + "\n", encoding="utf-8")
    logger.info("Wrote SKILL.md to %s", skill_dir / "SKILL.md")


if __name__ == "__main__":
    asyncio.run(main())
