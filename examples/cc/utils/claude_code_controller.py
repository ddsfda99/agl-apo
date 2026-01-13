import json
from functools import partial
from pathlib import Path
from typing import Literal

import dotenv
from utils.docker_runtime import Runtime
from utils.logger import logger
from utils.type import CC_ALL_TOOLS as all_tools
from utils.type import AgentResult


class ClaudeController:
    system_prompt = """You are an expert software engineer solving swebench bug fixing tasks."""
    _instructions_path = "/testbed/CLAUDE.md"
    _telemetry_error_markers = (
        "event logging",
        "failed to export",
    )

    def __init__(
        self, image: str, instance: dict, run_id: str, tools: set, user_prompt: str, endpoint: str, api_key: str
    ) -> None:
        self.image = image
        self.instance = instance
        self.run_id = run_id
        self.endpoint = endpoint
        self.api_key = api_key
        self.container: Runtime = self.init_container(self.image, self.instance)
        self.allowed_tools: str = ",".join([f'"{i}"' for i in tools])
        self.disallowed_tools: str = ",".join([f'"{i}"' for i in (all_tools - tools)])
        assert "{description}" in user_prompt
        self.user_prompt: str = user_prompt
        self._instructions_text = self._load_instructions_text()
        return

    def init_container(self, image: str, instance: dict) -> Runtime:
        container = Runtime.start_session(
            image,
            instance,
            log_function=partial(logger, run_id=self.run_id, instance_id=instance["instance_id"]),
            platform="linux",
        )
        container.send_command("curl -fsSL https://claude.ai/install.sh | bash -s -- 2.0.65")
        container.send_command('alias claude="$HOME/.local/bin/claude"')
        dotenv.load_dotenv()
        # anthropic_api_key = os.getenv('ANTHROPIC_API_KEY')
        # container.send_command(f"export ANTHROPIC_API_KEY={anthropic_api_key}")
        # if (not os.getenv("ANTHROPIC_BASE_URL")) or (not os.getenv("ANTHROPIC_AUTH_TOKEN")):
        #     raise RuntimeError("ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN not found!")
        container.send_command(f"export ANTHROPIC_BASE_URL={self.endpoint}")
        container.send_command(f"export ANTHROPIC_AUTH_TOKEN={self.api_key}")
        container.send_command("export IS_SANDBOX=1")
        return container

    def _load_instructions_text(self) -> str | None:
        repo_root = Path(__file__).resolve().parents[3]
        for name in ("CLAUDE.md", "claude.md"):
            candidate = repo_root / name
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        return None

    def _run_cli(self, instance: dict, max_step: int, timelimit: int) -> list[dict]:
        # prepare prompt safely: write it to a file inside the container using a single-quoted heredoc
        # directly applying prompt for heredoc may raise error for windows line ending \r\n
        prompt_text = self.user_prompt.format(description=instance["problem_statement"].replace('"""', "'''"))
        
        # ===== DEBUG =====
        print("\n" + "=" * 80)
        print("DEBUG: Prompt being sent to Claude Code:")
        print("=" * 80)
        print(prompt_text)
        print("=" * 80 + "\n")
        # ===== END DEBUG =====
        
        # choose a simple filename and a heredoc delimiter unlikely to collide
        heredoc_cmd = "cat > /tmp/cc_prompt.txt <<'CC_PROMPT'\n" + prompt_text + "\nCC_PROMPT\n"
        self.container.send_command(heredoc_cmd)

        self.container.send_command("mkdir -p /testbed/.claude")
        with open("utils/settings.template.json") as f:
            setting = f.read()
        setting = setting.replace("<allowedTools>", self.allowed_tools).replace(
            "<excludedTools>", self.disallowed_tools
        )
        setting_cmd = "cat > /testbed/.claude/settings.json <<'CC_SETTING'\n" + setting + "\nCC_SETTING\n"
        self.container.send_command(setting_cmd)

        # Write CLAUDE.md to /testbed - Claude Code will auto-load it
        if self._instructions_text:
            instructions_cmd = (
                f"cat > {self._instructions_path} <<'CC_INSTRUCTIONS'\n"
                + self._instructions_text
                + "\nCC_INSTRUCTIONS\n"
            )
            self.container.send_command(instructions_cmd)

        # with open("utils/handle_hook.template.sh") as f:
        #     handler = f.read()
        # handler_cmd = "cat > /tmp/handle_hook.sh <<'CC_HOOK'\n" + handler + "\nCC_HOOK\n"
        # self.container.send_command(handler_cmd)
        # self.container.send_command("chmod +x /tmp/handle_hook.sh")

        # run claude reading the prompt from the file to avoid shell interpolation issues
        claude_cmd = (
            f'claude -p "$(cat /tmp/cc_prompt.txt)"'
            f' --system-prompt "{self.system_prompt}" --max-turns {max_step}  --output-format json --verbose'
        )
        res = self.container.send_command(claude_cmd, timelimit * 60)
        output = res.output
        parsed = None
        stripped_output = output.strip()
        if stripped_output:
            try:
                parsed = json.loads(stripped_output)
            except json.JSONDecodeError:
                parsed = None

        # Parse output as full JSON, JSONL, or a trailing JSON block.
        json_lines: list[dict] = []
        if parsed is None:
            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                if not (line.startswith("{") or line.startswith("[")):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    if "type" in obj or "session_id" in obj:
                        json_lines.append(obj)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict) and ("type" in item or "session_id" in item):
                            json_lines.append(item)

        if parsed is None and not json_lines:
            raw_lines = [line for line in output.splitlines() if line.strip()]
            for idx, line in enumerate(raw_lines):
                if not line.lstrip().startswith(("{", "[")):
                    continue
                candidate = "\n".join(raw_lines[idx:])
                try:
                    parsed = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue

        if parsed is None and not json_lines:
            raise AssertionError("traj not found!")

        def _assistant_turns(items: list[dict]) -> int:
            return sum(1 for obj in items if obj.get("type") == "assistant")

        def _finalize_traj(traj_obj, items_for_count: list[dict] | None):
            if isinstance(traj_obj, list):
                result_idx = None
                for i in range(len(traj_obj) - 1, -1, -1):
                    item = traj_obj[i]
                    if isinstance(item, dict) and item.get("type") == "result":
                        result_idx = i
                        break
                if result_idx is not None:
                    result = traj_obj[result_idx]
                    if not result.get("num_turns"):
                        assistant_turns = _assistant_turns([item for item in traj_obj if isinstance(item, dict)])
                        if assistant_turns:
                            result = dict(result)
                            result["num_turns"] = assistant_turns
                            traj_obj = list(traj_obj)
                            traj_obj[result_idx] = result
                    cleaned = self._scrub_telemetry_errors(result)
                    if cleaned is not result:
                        traj_obj = list(traj_obj)
                        traj_obj[result_idx] = cleaned
                return traj_obj
            if isinstance(traj_obj, dict):
                if items_for_count and not traj_obj.get("num_turns"):
                    assistant_turns = _assistant_turns(items_for_count)
                    if assistant_turns:
                        traj_obj = dict(traj_obj)
                        traj_obj["num_turns"] = assistant_turns
                return self._scrub_telemetry_errors(traj_obj)
            return traj_obj

        if parsed is not None:
            traj = _finalize_traj(parsed, parsed if isinstance(parsed, list) else None)
        else:
            traj = json_lines if len(json_lines) > 1 else json_lines[0]
            traj = _finalize_traj(traj, json_lines)
        # self.container.send_command("cat /tmp/hook.out")
        return traj

    def _run_python_sdk(self, instance: dict, max_step: int, timelimit: int) -> list[dict]:
        self.container.send_command(
            f"""
if ! command -v python3 &> /dev/null; then
    echo "Python is not installed. Installing Python 3.12..."
    sudo apt-get update && sudo apt-get install -y python3.12
else
    echo "Python is already installed."
fi
"""
        )
        self.container.send_command("python3 -m pip install claude-code-sdk")
        with open("src/agent/cc/claude_code_main.py.template") as f:
            entrance_template = f.read()
        entrance_template.replace("SYS_PROMPT", self.system_prompt).replace(
            "PROMPT", self.user_prompt.format(description=instance["problem_statement"].replace('"""', "'''"))
        ).replace("MAX_STEP", str(max_step))
        self.container.send_command(f"cat > /tmp/claude_code_main.py <<'CC_MAIN'\n{entrance_template}\nCC_MAIN\n")
        self.container.send_command("python3 /tmp/claude_code_main.py", timelimit * 60)
        return

    def _scrub_telemetry_errors(self, traj: dict) -> dict:
        if not isinstance(traj, dict):
            return traj
        errors = traj.get("errors")
        if not isinstance(errors, list):
            return traj

        def is_telemetry_error(item: object) -> bool:
            if not isinstance(item, str):
                return False
            lowered = item.lower()
            return any(marker in lowered for marker in self._telemetry_error_markers)

        filtered = [err for err in errors if not is_telemetry_error(err)]
        if filtered == errors:
            return traj

        cleaned = dict(traj)
        cleaned["errors"] = filtered
        if not filtered:
            if cleaned.get("is_error"):
                cleaned["is_error"] = False
            if cleaned.get("subtype") == "error_during_execution":
                cleaned["subtype"] = "completed"
        return cleaned

    def run_instance(
        self, instance: dict, max_step: int = 40, timelimit: int = 30, run_method: Literal["python", "cli"] = "python"
    ) -> AgentResult:
        """
        timelimit: in minute
        """
        if run_method == "python":
            raise NotImplementedError("Claude Code Python SDK has not been fully implemented...")
            # traj = self._run_python_sdk(instance, max_step, timelimit)
        elif run_method == "cli":
            traj = self._run_cli(instance, max_step, timelimit)
        else:
            raise ValueError(f"wrong run_method {run_method}, run_method should be in [python, cli]")
        solution_patch = self.container.send_command("git --no-pager diff HEAD --diff-filter=M --text").output
        solution_patch = solution_patch.replace("git --no-pager diff HEAD --diff-filter=M --text\n", "")
        reproduction_file = self.container.send_command("cat /testbed/reproduction.py").output
        reproduction_file = reproduction_file.replace("cat /testbed/reproduction.py\n", "")
        return_value: AgentResult = {
            "instance_id": instance["instance_id"],
            "model_patch": solution_patch,
            "reproduction_file": reproduction_file,
            "model_name_or_path": "cc",
            "trajectory": traj,
        }
        return return_value

    def __del__(self):
        if hasattr(self, "container"):
            self.container.cleanup()
