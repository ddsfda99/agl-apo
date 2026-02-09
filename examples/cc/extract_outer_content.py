#!/usr/bin/env python3
import argparse
import json
import os
import re
import signal
import sys
from datetime import datetime
from typing import Iterable, Optional


def load_records(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            f.seek(0)
            records = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return records

    if isinstance(data, list):
        return data
    return [data]


def extract_outer_content(record: object) -> Optional[object]:
    if not isinstance(record, dict):
        return None
    if "content" in record:
        return record["content"]
    message = record.get("message")
    if isinstance(message, dict) and "content" in message:
        return message["content"]
    return None


def iter_contents(records: Iterable[object]) -> Iterable[object]:
    for record in records:
        content = extract_outer_content(record)
        if content is not None:
            yield content


def derive_instance_id(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    match = re.match(r"^(?P<instance_id>.+)_\d{8}_\d{6}$", base)
    if match:
        return match.group("instance_id")
    return base


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    parser = argparse.ArgumentParser(
        description="Extract outermost content fields from a JSON file."
    )
    parser.add_argument("input", help="Input JSON or JSONL file path.")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output path; defaults to trace/{instance_id}_{timestamp}.json. Use '-' for stdout.",
    )
    parser.add_argument(
        "--ensure-ascii",
        action="store_true",
        help="Escape non-ASCII characters in output JSON.",
    )
    args = parser.parse_args()

    records = load_records(args.input)
    ensure_ascii = args.ensure_ascii

    output_path = args.output
    if output_path is None:
        instance_id = derive_instance_id(args.input)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join("trace", f"{instance_id}_{timestamp}.json")

    if output_path == "-":
        out = sys.stdout
    else:
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        out = open(output_path, "w", encoding="utf-8")
    try:
        for content in iter_contents(records):
            out.write(json.dumps(content, ensure_ascii=ensure_ascii))
            out.write("\n")
    except BrokenPipeError:
        return 0
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())