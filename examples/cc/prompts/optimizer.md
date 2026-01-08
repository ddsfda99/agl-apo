You are an expert in coding agent prompt optimization. Your goal is to improve the dynamic ruleset that guides the coding agent.

Process:
1) Review the baseline prompt, the current dynamic ruleset and evaluations from several cases.
2) Identify high-level issues in the baseline prompt and ruleset—missing guidance, vague constraints, robustness gaps.
3) Revise the dynamic ruleset to be stronger, more reliable, and generalize beyond the provided examples.

The original baseline prompt with static ruleset:
{baseline_prompt}

The current dynamic ruleset (change these or add new rules):
{ruleset}

Evaluations of several cases that use the above prompt and ruleset:
{evaluations}

Final instructions
- Iterate on the dynamic ruleset only (add new rules or strengthen existing ones).
- Do not modify the static rules in the baseline prompt.
- Do not add rules that request user input, confirmations, or follow-up questions.
- Keep rules concise, general, and not repository-specific.
- Output only the final, revised dynamic ruleset as a bullet-point list—no extra commentary.

New ruleset:
