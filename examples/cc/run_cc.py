"""
Use the base cc prompt, write a temporary agent config overriding user_prompt,
and run `cc_agent.py --official` with that config.
"""

import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CC_DIR = ROOT / "examples" / "cc"
BASE_CONFIG_PATH = CC_DIR / "agent_config.yaml"
RULESET_PATH = CC_DIR / "prompts" / "rulesets" / "ruleset_1.md"

# Base prompt (originally in this file)
BASE_PROMPT = """You are given a code repository in the current directory (/testbed).
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


def load_ruleset() -> str:
    if not RULESET_PATH.exists():
        return ""
    return RULESET_PATH.read_text().strip()


def write_temp_config(new_prompt: str) -> Path:
    with open(BASE_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["agent"]["user_prompt"] = new_prompt

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    with tmp as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=False)
    return Path(tmp.name)


def run_cc_agent(config_path: Path) -> int:
    cmd = [
        "uv",
        "run",
        str(CC_DIR / "cc_agent.py"),
        "--official",
        "--agent_config",
        str(config_path),
    ]
    proc = subprocess.run(cmd, cwd=CC_DIR)
    return proc.returncode


def main() -> None:
    prompt = BASE_PROMPT.strip()
    ruleset = load_ruleset()

    if ruleset:
        prompt = f"{prompt}\n\nDynamic ruleset:\n{ruleset}"

    print("=== Using prompt sent to cc_agent ===")
    print(prompt if prompt else "[empty]")

    cfg_path = write_temp_config(prompt)
    print(f"\nWrote temp agent config: {cfg_path}")

    code = run_cc_agent(cfg_path)
    print(f"\ncc_agent.py exited with code {code}")


if __name__ == "__main__":
    main()
