# Prompt Impact Analysis: Same Cases with Different Prompts

## Executive Summary

Analysis of 16 cases with multiple prompt versions reveals significant patterns in how prompt design affects agent behavior, execution efficiency, and success rates.

## Key Findings

### 1. Trace Size Reduction Patterns

Most cases show **significant reduction in trace size** with refined prompts:

| Case | Initial Size | Final Size | Reduction | Success Change |
|------|--------------|------------|-----------|----------------|
| conan-io__conan-15538 | 545 KB | 70 KB | **-87.1%** | 0.0 → 0.0 |
| reflex-dev__reflex-2457 | 111 KB | 24 KB | **-78.2%** | 0.0 → 0.0 |
| matplotlib__matplotlib-27845 | 211 KB | 52 KB | **-75.1%** | 0.0 → 0.0 |
| matplotlib__matplotlib-27613 | 235 KB | 94 KB | **-59.8%** | 0.0 → 0.0 |
| kubernetes-client__python-2187 | 162 KB | 74 KB | **-54.4%** | 0.0 → 1.0 ✓ |
| psf__requests-6644 | 228 KB | 105 KB | **-54.0%** | 0.0 → 0.0 |
| beancount__beancount-799 | 133 KB | 70 KB | **-47.0%** | 0.0 → 0.0 |

**Key Insight**: Trace size reduction indicates the agent is taking a more direct path, making fewer exploratory tool calls. This suggests refined prompts successfully guide the agent toward the correct solution approach.

### 2. Success Rate Improvements

Only 2 out of 16 cases showed success rate improvements:

- **kubernetes-client__python-2187**: 0.0 → 1.0 (v0 → v5)
  - Prompt size: 4.1K (both versions identical!)
  - Trace reduced by 54.4%
  - **Paradox**: Same prompt, different outcome - suggests inherent model variance

- **reata__sqllineage-557**: 0.0 → 1.0 (v0 → v3)
  - Trace increased by 13.6%
  - Shows success doesn't always correlate with smaller traces

### 3. Prompt Design Analysis: matplotlib__matplotlib-27845 Case Study

**V0 Prompt (2.1K, failed)**:
- Simple 5-step generic instruction
- No prior context or constraints
- Led to 211KB trace (lots of exploration)

**V5 Prompt (5.6K, still failed)**:
- Detailed constraints based on "prior failed attempts"
- Step-by-step plan with pseudocode
- Specific pitfalls to avoid
- Led to 52KB trace (75% smaller)

**Observation**: Despite failing, v5 shows the agent:
- Avoided previous mistakes
- Took a more direct approach
- Made fewer exploratory tool calls
- But still couldn't solve the core issue

### 4. Cases with Increased Trace Size

Some cases showed trace size **increases** with refined prompts:

- django__django-11815: +27.3%
- reflex-dev__reflex-2617: +17.8%
- reata__sqllineage-557: +13.6% (but succeeded!)
- reflex-dev__reflex-2985: +7.0%

**Hypothesis**: More detailed prompts sometimes lead agents to:
- Perform more thorough validation
- Add more test cases
- Take more careful implementation steps

### 5. Stable Cases (No Change)

- **sympy__sympy-15976**: All 3 versions identical (127KB)
  - Suggests the prompt reached a "convergence point"
  - Agent follows the same strategy regardless of minor prompt variations

## Detailed Case Analysis

### Case 1: kubernetes-client__python-2187 (Identical Prompts, Different Outcomes)

**Critical Discovery**: v0 and v5 have **exactly the same 4.1K prompt**, yet:
- v0 failed (reward 0.0, 162KB trace)
- v5 succeeded (reward 1.0, 74KB trace)

This reveals:
- **Model non-determinism**: Same input can produce different outputs
- **Sampling effects**: Temperature, top-p, or other sampling parameters matter
- **Prompt versioning may not always reflect actual changes**: Need to verify actual prompt content

### Case 2: matplotlib__matplotlib-27845 (Detailed Guidance Still Failed)

**V5 Prompt Includes**:
```
Important constraints based on prior failed attempts
- Make the fix minimal and localized
- Do not change public API, error handling semantics
- Preserve existing special-cases in to_rgba_array
...

Step-by-step plan:
1) Reproduce the bug with a minimal test
2) Locate the bug
3) Implement a minimal fix in lib/matplotlib/colors.py
   [Detailed pseudocode provided]
4) Run tests
5) Cleanup

Tips and pitfalls to avoid:
- Do not add explicit checks or custom error messages
- Do not change Collection code
...
```

Despite this extensive guidance:
- Agent still failed to solve the issue
- Trace reduced from 211KB to 52KB (agent was more focused)
- Suggests the core issue is inherently difficult, not just about prompt clarity

## Implications for Prompt Engineering

### 1. More Detailed ≠ Always Better

- **Efficiency gains**: Detailed prompts reduce exploration (smaller traces)
- **Success limitations**: Even very detailed prompts can't guarantee success on hard problems
- **Diminishing returns**: After a certain detail level, more context doesn't help

### 2. Model Variance Matters

- Same prompt can yield different outcomes
- Need multiple runs for statistical significance
- Temperature/sampling parameters are critical

### 3. Effective Prompt Patterns

Successful prompts tend to include:
- Clear step-by-step plans
- Explicit constraints from prior failures
- Specific files and functions to focus on
- Pitfalls to avoid
- Expected validation criteria

### 4. When Detailed Prompts Help Most

- Reducing wasted exploration
- Avoiding known pitfalls
- Guiding toward specific solution approaches
- Improving execution efficiency

### 5. When Detailed Prompts Don't Help

- Core problem is ambiguous or underspecified
- Multiple valid solution approaches exist
- Problem requires creative insight beyond prompt scope
- Agent needs to explore to understand the codebase

## Recommendations

1. **Start Simple**: Begin with minimal prompts to establish baseline
2. **Iterate Based on Failures**: Add constraints and guidance based on observed failure patterns
3. **Multiple Runs**: Same prompt should be tested multiple times for robustness
4. **Track Efficiency**: Monitor trace size as a proxy for agent efficiency
5. **Balance Detail**: Find the sweet spot between guidance and over-specification
6. **Document Variants**: Keep clear records of what changed between prompt versions

## Metrics Summary

Across all 16 cases with multiple versions:
- **Average trace size reduction**: 38.4%
- **Cases with improved success**: 2/16 (12.5%)
- **Cases with reduced trace but same outcome**: 11/16 (68.8%)
- **Cases with increased trace**: 4/16 (25%)

## Conclusion

Refined prompts consistently improve **execution efficiency** (smaller traces, fewer tool calls), but don't guarantee **solution success**. The biggest gains come from:
1. Adding specific constraints based on prior failures
2. Providing clear step-by-step plans
3. Highlighting common pitfalls

However, inherently difficult problems remain challenging regardless of prompt detail, and model variance means the same prompt can produce different results across runs.
