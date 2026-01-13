#!/usr/bin/env python3
"""Analyze tool usage patterns in extracted trace files."""

import json
import os
from collections import defaultdict, Counter
from pathlib import Path


def extract_tool_uses(trace_data):
    """Extract all tool uses from a trace."""
    tool_uses = []

    for item in trace_data.get("trace", []):
        if item.get("role") == "assistant":
            content = item.get("content", [])
            if isinstance(content, list):
                for entry in content:
                    if entry.get("type") == "tool_use":
                        tool_uses.append({
                            "name": entry.get("name"),
                            "id": entry.get("id"),
                            "input": entry.get("input", {})
                        })

    return tool_uses


def analyze_tool_sequence(tool_uses):
    """Analyze the sequence of tool usage."""
    return [t["name"] for t in tool_uses]


def count_tools(tool_uses):
    """Count tool usage frequency."""
    return Counter(t["name"] for t in tool_uses)


def find_edits(tool_uses):
    """Find all Edit tool uses and extract details."""
    edits = []
    for t in tool_uses:
        if t["name"] == "Edit":
            edits.append({
                "file": t["input"].get("file_path", ""),
                "old_length": len(t["input"].get("old_string", "")),
                "new_length": len(t["input"].get("new_string", ""))
            })
    return edits


def find_reads(tool_uses):
    """Find all Read tool uses."""
    reads = []
    for t in tool_uses:
        if t["name"] == "Read":
            reads.append({
                "file": t["input"].get("file_path", ""),
            })
    return reads


def find_grep(tool_uses):
    """Find all Grep tool uses."""
    greps = []
    for t in tool_uses:
        if t["name"] == "Grep":
            greps.append({
                "pattern": t["input"].get("pattern", ""),
                "path": t["input"].get("path", ""),
            })
    return greps


def analyze_first_edit(tool_uses):
    """Analyze context before first Edit."""
    first_edit_idx = None
    for i, t in enumerate(tool_uses):
        if t["name"] == "Edit":
            first_edit_idx = i
            break

    if first_edit_idx is None:
        return {
            "edits_before_first_edit": 0,
            "tools_before_first_edit": []
        }

    tools_before = tool_uses[:first_edit_idx]
    tool_counts = Counter(t["name"] for t in tools_before)

    return {
        "edits_before_first_edit": first_edit_idx,
        "tools_before_first_edit": list(tool_counts.items()),
        "sequence": analyze_tool_sequence(tools_before)
    }


def analyze_trace_file(filepath):
    """Analyze a single trace file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    instance_id = data.get("instance_id", "unknown")
    reward = data.get("terminal_reward", None)
    max_prompts = data.get("max_prompts", 0)

    tool_uses = extract_tool_uses(data)
    tool_counts = count_tools(tool_uses)
    tool_sequence = analyze_tool_sequence(tool_uses)

    edits = find_edits(tool_uses)
    reads = find_reads(tool_uses)
    greps = find_grep(tool_uses)

    first_edit_analysis = analyze_first_edit(tool_uses)

    # Find Write operations (often for tests)
    writes = [t for t in tool_uses if t["name"] == "Write"]

    # Find Bash operations (often for running tests)
    bash_ops = [t for t in tool_uses if t["name"] == "Bash"]

    return {
        "instance_id": instance_id,
        "reward": reward,
        "max_prompts": max_prompts,
        "total_tools": len(tool_uses),
        "tool_counts": dict(tool_counts),
        "tool_sequence": tool_sequence,
        "num_edits": len(edits),
        "num_reads": len(reads),
        "num_grep": len(greps),
        "num_writes": len(writes),
        "num_bash": len(bash_ops),
        "edits": edits,
        "first_edit_analysis": first_edit_analysis,
    }


def print_analysis(results):
    """Print analysis results."""
    print("\n" + "=" * 80)
    print("TOOL USAGE ANALYSIS")
    print("=" * 80)

    for r in results:
        status = "✓ SUCCESS" if r["reward"] == 1.0 else "✗ FAILURE"
        print(f"\n{r['instance_id']} ({status})")
        print(f"  Reward: {r['reward']}, Prompts: {r['max_prompts']}, Total Tools: {r['total_tools']}")
        print(f"\n  Tool Counts:")
        for tool, count in sorted(r["tool_counts"].items(), key=lambda x: -x[1]):
            print(f"    {tool}: {count}")

        print(f"\n  First Edit Context:")
        fea = r["first_edit_analysis"]
        if fea["edits_before_first_edit"] == 0:
            print(f"    No edits made!")
        else:
            print(f"    Tools before first edit: {fea['edits_before_first_edit']}")
            print(f"    Tool sequence: {' → '.join(fea['sequence'][:10])}{'...' if len(fea['sequence']) > 10 else ''}")

        print(f"\n  Edit Details:")
        for i, edit in enumerate(r["edits"][:5], 1):
            print(f"    Edit {i}: {edit['file']}")
        if len(r["edits"]) > 5:
            print(f"    ... and {len(r['edits']) - 5} more edits")

        print(f"\n  Write Operations: {r['num_writes']}")
        print(f"  Bash Operations: {r['num_bash']}")


def compare_success_failure(results):
    """Compare success vs failure patterns."""
    print("\n" + "=" * 80)
    print("SUCCESS VS FAILURE COMPARISON")
    print("=" * 80)

    successes = [r for r in results if r["reward"] == 1.0]
    failures = [r for r in results if r["reward"] == 0.0]

    if not successes or not failures:
        print("Not enough data for comparison")
        return

    print(f"\nSuccesses: {len(successes)}, Failures: {len(failures)}\n")

    # Average edits
    success_edits = sum(r["num_edits"] for r in successes) / len(successes)
    failure_edits = sum(r["num_edits"] for r in failures) / len(failures)
    print(f"Average Edits:")
    print(f"  Success: {success_edits:.1f}")
    print(f"  Failure: {failure_edits:.1f}")
    print(f"  Ratio: {success_edits / failure_edits if failure_edits > 0 else 'N/A':.2f}x")

    # Average reads before first edit
    success_reads_before = []
    failure_reads_before = []

    for r in successes:
        reads = sum(1 for t in r["first_edit_analysis"]["tools_before_first_edit"] if t[0] == "Read")
        if r["num_edits"] > 0:
            success_reads_before.append(reads)

    for r in failures:
        reads = sum(1 for t in r["first_edit_analysis"]["tools_before_first_edit"] if t[0] == "Read")
        if r["num_edits"] > 0:
            failure_reads_before.append(reads)

    if success_reads_before and failure_reads_before:
        print(f"\nAverage Reads Before First Edit:")
        print(f"  Success: {sum(success_reads_before) / len(success_reads_before):.1f}")
        print(f"  Failure: {sum(failure_reads_before) / len(failure_reads_before):.1f}")

    # No edit cases
    success_no_edit = sum(1 for r in successes if r["num_edits"] == 0)
    failure_no_edit = sum(1 for r in failures if r["num_edits"] == 0)
    print(f"\nCases with No Edits:")
    print(f"  Success: {success_no_edit}/{len(successes)} ({100*success_no_edit/len(successes):.0f}%)")
    print(f"  Failure: {failure_no_edit}/{len(failures)} ({100*failure_no_edit/len(failures):.0f}%)")


def main():
    trace_dir = Path("/home/v-zitonzhang/agent-lightning/examples/cc/trace")

    # Analyze specific traces
    files_to_analyze = [
        "geopandas__geopandas-3132_extracted.json",
        "beancount__beancount-799_extracted.json",
        "sissbruecker__linkding-613_extracted.json",
        "sphinx-doc__sphinx-11904_extracted_v0.json",
        "reata__sqllineage-557_extracted_v1.json",
        "beeware__briefcase-1598_extracted_v0.json",
    ]

    results = []
    for filename in files_to_analyze:
        filepath = trace_dir / filename
        if filepath.exists():
            try:
                result = analyze_trace_file(filepath)
                results.append(result)
            except Exception as e:
                print(f"Error analyzing {filename}: {e}")

    print_analysis(results)
    compare_success_failure(results)


if __name__ == "__main__":
    main()
