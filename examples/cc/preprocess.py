#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json_or_jsonl(path: Path) -> list[Any]:
    raw = read_text(path)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    except json.JSONDecodeError:
        records: list[Any] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def iter_text_chunks(obj: Any):
    if isinstance(obj, str):
        text = obj.strip()
        if text:
            yield text
        return
    if isinstance(obj, list):
        for item in obj:
            yield from iter_text_chunks(item)
        return
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            text = obj.get("text", "").strip()
            if text:
                yield text
        elif isinstance(obj.get("text"), str):
            text = obj.get("text", "").strip()
            if text:
                yield text
        if "content" in obj:
            yield from iter_text_chunks(obj["content"])
        if "message" in obj and isinstance(obj["message"], dict):
            msg = obj["message"]
            if "content" in msg:
                yield from iter_text_chunks(msg["content"])


def find_patch_path(logs_dir: Path) -> Path:
    candidates = list(logs_dir.glob("run_evaluation/epoch_*/cc/*/patch.diff"))
    if not candidates:
        raise FileNotFoundError(f"patch.diff not found under {logs_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_report_path(logs_dir: Path, patch_path: Path) -> Optional[Path]:
    sibling = patch_path.parent / "report.json"
    if sibling.exists() and sibling.is_file():
        return sibling
    candidates = list(logs_dir.glob("run_evaluation/epoch_*/cc/*/report.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def infer_instance_id(report_path: Optional[Path], patch_path: Path) -> Optional[str]:
    if report_path and report_path.exists():
        try:
            data = json.loads(read_text(report_path))
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            top_keys = [k for k, v in data.items() if isinstance(v, dict)]
            if len(top_keys) == 1:
                return top_keys[0]
    # .../cc/<instance_id>/patch.diff
    if patch_path.parent.name:
        return patch_path.parent.name
    return None


def find_runner_log(logs_dir: Path) -> Optional[Path]:
    candidates: list[Path] = []
    for epoch_dir in logs_dir.glob("epoch_*"):
        if not epoch_dir.is_dir():
            continue
        for child in epoch_dir.iterdir():
            if child.is_file():
                candidates.append(child)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_traj_path_from_runner_log(log_path: Optional[Path]) -> Optional[Path]:
    if log_path is None or not log_path.exists():
        return None
    text = read_text(log_path)
    matches = re.findall(r"Trajectory saved to (.+)", text)
    if not matches:
        return None
    traj = Path(matches[-1].strip())
    if traj.exists() and traj.is_file():
        return traj
    return None


def extract_summary_from_traj(traj_path: Optional[Path]) -> str:
    if traj_path is None:
        return ""
    records = load_json_or_jsonl(traj_path)
    last_assistant_text = ""
    last_any_text = ""
    for record in records:
        for chunk in iter_text_chunks(record):
            last_any_text = chunk
        if not isinstance(record, dict):
            continue
        msg = record.get("message")
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for chunk in iter_text_chunks(msg.get("content")):
            last_assistant_text = chunk
    result = (last_assistant_text or last_any_text).strip()
    return result


def parse_report_resolved(report_path: Path, instance_id: Optional[str]) -> Optional[bool]:
    try:
        data = json.loads(read_text(report_path))
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(data, dict):
        return None

    resolved: Any = None
    if instance_id and instance_id in data and isinstance(data[instance_id], dict):
        resolved = data[instance_id].get("resolved")
    elif "resolved" in data:
        resolved = data.get("resolved")
    elif len(data) == 1:
        only_val = next(iter(data.values()))
        if isinstance(only_val, dict):
            resolved = only_val.get("resolved")

    if isinstance(resolved, bool):
        return resolved
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs_dir", required=True, help="Path to one logs_* directory.")
    parser.add_argument("--output_path", required=False, help="Output attempt JSON path.")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.exists() or not logs_dir.is_dir():
        raise FileNotFoundError(f"logs_dir not found or not a directory: {logs_dir}")

    patch_path = find_patch_path(logs_dir)
    report_path = find_report_path(logs_dir, patch_path)
    instance_id = infer_instance_id(report_path, patch_path)
    runner_log = find_runner_log(logs_dir)
    traj_path = find_traj_path_from_runner_log(runner_log)

    summary = extract_summary_from_traj(traj_path)
    patch = read_text(patch_path)
    resolved = parse_report_resolved(report_path, instance_id) if report_path else None

    out: dict[str, Any] = {
        "instance_id": instance_id,
        "summary": summary,
        "patch": patch,
        "resolved": resolved,
    }

    if args.output_path:
        output_path = Path(args.output_path)
    else:
        stem = logs_dir.name
        output_path = Path("attempts") / f"{stem}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved attempt json to {output_path}")
    print(f"Resolved logs dir: {logs_dir}")
    print(f"Patch: {patch_path}")
    print(f"Report: {report_path if report_path else '(missing)'}")
    print(f"Runner log: {runner_log if runner_log else '(missing)'}")
    print(f"Trajectory: {traj_path if traj_path else '(missing)'}")


if __name__ == "__main__":
    main()
