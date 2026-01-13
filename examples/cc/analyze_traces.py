#!/usr/bin/env python3
"""
Analyze tool usage patterns in trace files.
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_trace(trace_path):
    """Load a trace file and return the trace data."""
    with open(trace_path, 'r') as f:
        data = json.load(f)
    return data


def extract_tool_sequence(trace_data):
    """Extract sequence of tool calls from trace."""
    tools = []
    for item in trace_data.get('trace', []):
        if item.get('role') == 'assistant':
            for content in item.get('content', []):
                if content.get('type') == 'tool_use':
                    tools.append({
                        'name': content.get('name'),
                        'id': content.get('id'),
                        'input': content.get('input', {})
                    })
    return tools


def analyze_tool_usage(tools):
    """Analyze tool usage patterns."""
    tool_counts = Counter([t['name'] for t in tools])
    tool_sequence = [t['name'] for t in tools]

    # Count transitions
    transitions = defaultdict(Counter)
    for i in range(len(tool_sequence) - 1):
        transitions[tool_sequence[i]][tool_sequence[i+1]] += 1

    # Find patterns
    read_before_edit = 0
    grep_before_edit = 0
    glob_before_edit = 0

    first_edit_idx = None
    for i, tool in enumerate(tools):
        if tool['name'] == 'Edit':
            first_edit_idx = i
            break

    if first_edit_idx:
        for i in range(first_edit_idx):
            if tools[i]['name'] == 'Read':
                read_before_edit += 1
            elif tools[i]['name'] == 'Grep':
                grep_before_edit += 1
            elif tools[i]['name'] == 'Glob':
                glob_before_edit += 1

    return {
        'tool_counts': tool_counts,
        'tool_sequence': tool_sequence,
        'transitions': dict(transitions),
        'first_edit_idx': first_edit_idx,
        'read_before_first_edit': read_before_edit,
        'grep_before_first_edit': grep_before_edit,
        'glob_before_first_edit': glob_before_edit,
        'total_tools': len(tools)
    }


def find_edit_issues(tools):
    """Find specific issues with Edit tool usage."""
    issues = []

    for i, tool in enumerate(tools):
        if tool['name'] == 'Edit':
            input_data = tool.get('input', {})

            # Check for missing file_path
            if 'file_path' not in input_data:
                issues.append({
                    'index': i,
                    'type': 'Edit_missing_file_path',
                    'input': input_data
                })

            # Check for missing old_string
            if 'old_string' not in input_data:
                issues.append({
                    'index': i,
                    'type': 'Edit_missing_old_string',
                    'input': input_data
                })

            # Check for missing new_string
            if 'new_string' not in input_data:
                issues.append({
                    'index': i,
                    'type': 'Edit_missing_new_string',
                    'input': input_data
                })

            # Check for very old_string (potential mismatch)
            if 'old_string' in input_data:
                old_len = len(input_data['old_string'])
                if old_len < 10:
                    issues.append({
                        'index': i,
                        'type': 'Edit_old_string_too_short',
                        'length': old_len,
                        'input': input_data
                    })

    return issues


def find_read_issues(tools):
    """Find issues with Read tool usage."""
    issues = []
    read_files = []

    for i, tool in enumerate(tools):
        if tool['name'] == 'Read':
            input_data = tool.get('input', {})
            file_path = input_data.get('file_path', '')

            if not file_path:
                issues.append({
                    'index': i,
                    'type': 'Read_missing_file_path',
                    'input': input_data
                })
            else:
                read_files.append(file_path)

    # Check if same file read multiple times
    file_read_count = Counter(read_files)
    for file, count in file_read_count.items():
        if count > 3:
            issues.append({
                'type': 'Read_excessive_reads',
                'file': file,
                'count': count
            })

    return issues


def find_bash_issues(tools):
    """Find issues with Bash tool usage."""
    issues = []

    for i, tool in enumerate(tools):
        if tool['name'] == 'Bash':
            input_data = tool.get('input', {})
            command = input_data.get('command', '')

            # Check for test runs without setup
            if 'pytest' in command or 'python -m pytest' in command:
                # Check if this is early in the sequence
                if i < len(tools) * 0.3:  # First 30% of tools
                    issues.append({
                        'index': i,
                        'type': 'Bash_early_test_run',
                        'command': command
                    })

            # Check for git operations (should not commit)
            if 'git commit' in command or 'git push' in command:
                issues.append({
                    'index': i,
                    'type': 'Bash_forbidden_git',
                    'command': command
                })

    return issues


def analyze_trace(trace_path):
    """Full analysis of a trace file."""
    print(f"\n{'='*80}")
    print(f"ANALYZING: {Path(trace_path).name}")
    print(f"{'='*80}")

    data = load_trace(trace_path)
    reward = data.get('reward', 'N/A')
    instance_id = data.get('instance_id', 'N/A')

    print(f"\nInstance ID: {instance_id}")
    print(f"Reward: {reward}")

    tools = extract_tool_sequence(data)

    if not tools:
        print("\nNo tool calls found in trace!")
        return None

    # Basic analysis
    analysis = analyze_tool_usage(tools)

    print(f"\n--- TOOL USAGE SUMMARY ---")
    print(f"Total tool calls: {analysis['total_tools']}")
    print(f"\nTool counts:")
    for tool, count in analysis['tool_counts'].most_common():
        print(f"  {tool}: {count}")

    print(f"\n--- TOOL SEQUENCE (first 30) ---")
    for i, tool in enumerate(analysis['tool_sequence'][:30]):
        print(f"  {i+1}. {tool}")
    if len(analysis['tool_sequence']) > 30:
        print(f"  ... ({len(analysis['tool_sequence']) - 30} more)")

    print(f"\n--- BEFORE FIRST EDIT ---")
    if analysis['first_edit_idx'] is not None:
        print(f"First Edit at position: {analysis['first_edit_idx'] + 1}")
        print(f"  Read operations: {analysis['read_before_first_edit']}")
        print(f"  Grep operations: {analysis['grep_before_first_edit']}")
        print(f"  Glob operations: {analysis['glob_before_first_edit']}")
    else:
        print("No Edit operations found!")

    print(f"\n--- TOOL TRANSITIONS ---")
    for tool, next_tools in sorted(analysis['transitions'].items()):
        print(f"  {tool} ->")
        for next_tool, count in next_tools.most_common(5):
            print(f"    {next_tool}: {count}")

    # Find issues
    print(f"\n--- ISSUES FOUND ---")

    edit_issues = find_edit_issues(tools)
    if edit_issues:
        print(f"\nEdit issues ({len(edit_issues)}):")
        for issue in edit_issues[:10]:  # Show first 10
            print(f"  [{issue.get('index', '?')}] {issue.get('type', 'Unknown')}: {issue}")
    else:
        print("\nNo Edit issues found")

    read_issues = find_read_issues(tools)
    if read_issues:
        print(f"\nRead issues ({len(read_issues)}):")
        for issue in read_issues[:10]:
            print(f"  {issue}")
    else:
        print("\nNo Read issues found")

    bash_issues = find_bash_issues(tools)
    if bash_issues:
        print(f"\nBash issues ({len(bash_issues)}):")
        for issue in bash_issues[:10]:
            print(f"  [{issue.get('index', '?')}] {issue.get('type', 'Unknown')}: {issue.get('command', '')[:100]}")
    else:
        print("\nNo Bash issues found")

    # Detailed analysis of specific patterns
    print(f"\n--- SPECIFIC PATTERNS ---")

    # Count Write operations
    write_count = analysis['tool_counts'].get('Write', 0)
    if write_count > 0:
        print(f"\nWrite operations: {write_count}")
        for i, tool in enumerate(tools):
            if tool['name'] == 'Write':
                input_data = tool.get('input', {})
                file_path = input_data.get('file_path', 'Unknown')
                print(f"  [{i}] Write to: {file_path}")

    # Count TodoWrite operations
    todowrite_count = analysis['tool_counts'].get('TodoWrite', 0)
    if todowrite_count > 0:
        print(f"\nTodoWrite operations: {todowrite_count}")
        if todowrite_count > 3:
            print(f"  WARNING: Excessive TodoWrite usage may indicate over-planning")

    return {
        'path': trace_path,
        'instance_id': instance_id,
        'reward': reward,
        'analysis': analysis,
        'edit_issues': edit_issues,
        'read_issues': read_issues,
        'bash_issues': bash_issues
    }


def compare_traces(results):
    """Compare successful and failed traces."""
    print(f"\n{'='*80}")
    print("COMPARISON: SUCCESS vs FAILURE")
    print(f"{'='*80}")

    successful = [r for r in results if r.get('reward') == 1.0]
    failed = [r for r in results if r.get('reward') == 0.0]

    print(f"\nSuccessful traces: {len(successful)}")
    print(f"Failed traces: {len(failed)}")

    if not successful or not failed:
        print("\nCannot compare - need both successful and failed traces")
        return

    # Calculate averages
    def avg(values):
        return sum(values) / len(values) if values else 0

    success_tools_before_edit = [r['analysis']['read_before_first_edit'] + r['analysis']['grep_before_first_edit'] for r in successful if r['analysis']['first_edit_idx']]
    fail_tools_before_edit = [r['analysis']['read_before_first_edit'] + r['analysis']['grep_before_first_edit'] for r in failed if r['analysis']['first_edit_idx']]

    success_total_tools = [r['analysis']['total_tools'] for r in successful]
    fail_total_tools = [r['analysis']['total_tools'] for r in failed]

    success_edit_count = [r['analysis']['tool_counts'].get('Edit', 0) for r in successful]
    fail_edit_count = [r['analysis']['tool_counts'].get('Edit', 0) for r in failed]

    print(f"\n--- AVERAGE TOOL USAGE BEFORE FIRST EDIT ---")
    print(f"Success: {avg(success_tools_before_edit):.1f} exploration tools")
    print(f"Failure: {avg(fail_tools_before_edit):.1f} exploration tools")

    print(f"\n--- AVERAGE TOTAL TOOL CALLS ---")
    print(f"Success: {avg(success_total_tools):.1f}")
    print(f"Failure: {avg(fail_total_tools):.1f}")

    print(f"\n--- AVERAGE EDIT OPERATIONS ---")
    print(f"Success: {avg(success_edit_count):.1f}")
    print(f"Failure: {avg(fail_edit_count):.1f}")

    print(f"\n--- COMMON ISSUES IN FAILURES ---")
    all_edit_issues = sum([len(r['edit_issues']) for r in failed], 0)
    all_read_issues = sum([len(r['read_issues']) for r in failed], 0)
    all_bash_issues = sum([len(r['bash_issues']) for r in failed], 0)

    print(f"Edit issues: {all_edit_issues}")
    print(f"Read issues: {all_read_issues}")
    print(f"Bash issues: {all_bash_issues}")


def main():
    trace_files = [
        'trace/geopandas__geopandas-3132_extracted_v0.json',
        'trace/beancount__beancount-799_extracted.json',
        'trace/sissbruecker__linkding-613_extracted.json',
        'trace/sphinx-doc__sphinx-11904_extracted_v0.json',
        'trace/reata__sqllineage-557_extracted_v1.json',
        'trace/beeware__briefcase-1598_extracted_v0.json',
    ]

    results = []
    for trace_file in trace_files:
        trace_path = f"/home/v-zitonzhang/agent-lightning/examples/cc/{trace_file}"
        if Path(trace_path).exists():
            try:
                result = analyze_trace(trace_path)
                if result:
                    results.append(result)
            except Exception as e:
                print(f"\nERROR analyzing {trace_file}: {e}")
                import traceback
                traceback.print_exc()
        else:
            print(f"\nFile not found: {trace_path}")

    # Compare results
    if len(results) > 1:
        compare_traces(results)

    # Generate recommendations
    print(f"\n{'='*80}")
    print("RECOMMENDATIONS")
    print(f"{'='*80}")

    print("\n1. Read Tool:")
    print("   - Always read files before editing to verify content")
    print("   - Avoid reading the same file more than 3 times")
    print("   - Read the actual file, not just related files")

    print("\n2. Grep Tool:")
    print("   - Use to find function definitions and usage patterns")
    print("   - Use specific patterns, not overly broad searches")
    print("   - Combine with -n flag to see line numbers")

    print("\n3. Glob Tool:")
    print("   - Avoid using '**/*' which returns too many results")
    print("   - Use specific patterns like '**/*.py' or 'src/**/*.py'")
    print("   - Consider using Grep instead for searching content")

    print("\n4. Edit Tool:")
    print("   - Always verify old_string matches file content exactly")
    print("   - Use enough context (old_string should be unique)")
    print("   - Read the file immediately before editing")

    print("\n5. Write Tool:")
    print("   - Only use for new files, not existing files")
    print("   - Verify file doesn't exist before writing")
    print("   - For test files, verify dependencies exist first")

    print("\n6. Bash Tool:")
    print("   - Run tests after editing to verify changes")
    print("   - Check dependencies before running tests")
    print("   - Never use git commit or git push")

    print("\n7. General Patterns:")
    print("   - Explore before editing (Read, Grep, Glob)")
    print("   - Edit, then test, then edit again if needed")
    print("   - Delete test files after validation")


if __name__ == '__main__':
    main()
