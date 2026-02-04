import argparse
import json
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

CRITICAL_NOTE = "Critically review this attempt; it may be wrong or incomplete."


def _load_summary_text(summary_path: Path) -> str:
    raw = summary_path.read_text(encoding="utf-8")
    if not raw.strip():
        return ""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    if isinstance(data, dict):
        for key in ("summary", "content", "text", "output"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value

    if isinstance(data, list):
        if all(isinstance(item, str) for item in data):
            return "\n".join(item.strip() for item in data if item.strip())

    return raw


def _load_patch_text(patch_path: Path) -> str:
    raw = patch_path.read_text(encoding="utf-8")
    return raw.strip()


def _build_attempt_block(summary: str, patch: str) -> str:
    lines = [
        "## Attempt (critically review)",
        f"- {CRITICAL_NOTE}",
        "",
        "Summary:",
        summary.strip(),
        "",
        "Patch:",
        patch.strip(),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update CLAUDE.md from a summary file.")
    parser.add_argument("summary_path", help="Path to summary text/JSON/JSONL file.")
    parser.add_argument(
        "patch_path",
        help="Patch diff file path to include in the memory update.",
    )
    args = parser.parse_args()

    summary_path = Path(args.summary_path)
    summary_text = _load_summary_text(summary_path)
    if not summary_text.strip():
        raise ValueError(f"Empty summary file: {summary_path}")

    patch_path = Path(args.patch_path)
    if not patch_path.exists():
        raise FileNotFoundError(f"Patch file not found: {patch_path}")
    patch_text = _load_patch_text(patch_path)
    if not patch_text:
        raise ValueError(f"Empty patch file: {patch_path}")

    existing_path = Path("CLAUDE.md")
    existing_text = existing_path.read_text(encoding="utf-8") if existing_path.exists() else ""

    attempt_block = _build_attempt_block(summary_text, patch_text)
    if existing_text.strip():
        updated = existing_text.rstrip() + "\n\n" + attempt_block
    else:
        updated = attempt_block

    output_path = Path("CLAUDE.md")
    output_path.write_text(updated.rstrip() + "\n", encoding="utf-8")
    logger.info("Appended memory to %s", output_path)


if __name__ == "__main__":
    main()
