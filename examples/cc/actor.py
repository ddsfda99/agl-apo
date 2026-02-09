#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import List

PROMPT = """You are an Actor analyzing previous attempts to solve a SWE-bench bug fixing task.
Your role is to extract insights and patterns that will help the next attempt succeed.

# Your Task
Analyze the previous attempts and provide a reflection that highlights:
1. What worked and should be preserved
2. What failed and why
3. Key insights about the problem

Do NOT prescribe specific solutions or tell the agent what to do next. 
The agent will use your analysis to make its own decisions.

# Previous Attempts
{attempts}

# Your Analysis
Provide your analysis in the following format:

## Patterns Observed
[What patterns do you see across the attempts? What's working? What's failing?]

## Successful Elements
[Identify any code snippets, strategies, or approaches that showed promise or partial success. Be specific about what made them effective.]

## Failures and Root Causes
[What consistently failed? More importantly, WHY did these approaches fail? What's the underlying issue?]

## Critical Insights
[What have we learned about this bug? What's the real challenge here? What constraints or requirements must be satisfied?]

---

**IMPORTANT NOTE TO THE AGENT**: This analysis is provided as REFERENCE ONLY. It may contain partial truths, incorrect interpretations, or biased observations. 

DO NOT blindly follow or trust this analysis. Instead:
- Use it as ONE input among many
- Verify claims against the actual codebase and test results
- Think critically about whether the conclusions make sense
- Form your own understanding of the problem
- Make your own reasoned decisions

Your independent reasoning and problem-solving are essential. This reflection is meant to inform your thinking, not replace it."""


def load_attempts(paths: List[Path]) -> str:
    chunks = []
    for idx, path in enumerate(paths, 1):
        data = json.loads(path.read_text(encoding="utf-8"))
        summary = data.get("summary", "").strip()
        patch = data.get("patch", "").strip()
        chunks.append(f"=== Attempt {idx} ===\nSummary:\n{summary}\n\nPatch:\n{patch}")
    return "\n\n".join(chunks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True, help="Attempt JSON files")
    args = parser.parse_args()

    paths = [Path(p) for p in args.inputs]
    attempts = load_attempts(paths)

    guidance = PROMPT.format(attempts=attempts)
    print(guidance)


if __name__ == "__main__":
    main()
