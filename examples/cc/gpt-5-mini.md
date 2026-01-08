# CloudGPT gpt-5-mini Model Results Summary

## All Test Cases Performance

| Test Case | Turns | Input Tokens | Output Tokens | Total Tokens | Reward |
|-----------|-------|--------------|---------------|--------------|--------|
| django__django-12741 | 16 | 42652 | 228 | 42880 | 1.0 |
| django__django-13821 | 16 | 28539 | 175 | 28714 | 1.0 |
| django__django-14349 | 16 | 31210 | 166 | 31376 | 1.0 |
| django__django-16899 | 16 | 55002 | 45 | 55047 | 1.0 |
| sympy__sympy-19637 | 15 | 21760 | 569 | 22329 | 1.0 |
| django__django-11728 | 16 | 29012 | 2070 | 31082 | 0.0 |
| django__django-12663 | 2 | 13063 | 105 | 13168 | 0.0 |
| django__django-16801 | 3 | 12715 | 267 | 12982 | 0.0 |
| sphinx-doc__sphinx-9591 | 16 | 89745 | 742 | 90487 | 0.0 |
| sympy__sympy-21612 | 16 | 29367 | 1072 | 30439 | 0.0 |

## Summary Statistics

- **Total Test Cases**: 10
- **Successful**: 5 (50.0%)
- **Failed**: 5 (50.0%)

### By Completion Status
- **Complete Runs** (≥30 prompts): 8 cases
  - Success rate: 5/8 = **62.5%**
- **Short Runs** (<30 prompts): 2 cases
  - Success rate: 0/2 = **0%**

### Average Metrics
- **Average Turns (all)**: 13.2
- **Average Turns (complete runs)**: 16.0
- **Average Turns (successful)**: 15.8

### Token Usage Statistics
- **Total Input Tokens (all)**: 353065
- **Total Output Tokens (all)**: 5439
- **Total Tokens (all)**: 358504
- **Average Input Tokens per case**: 35307
- **Average Output Tokens per case**: 544
- **Average Total Tokens per case**: 35850

#### Successful Cases (reward = 1.0)
- **Total Input Tokens**: 179163
- **Total Output Tokens**: 1183
- **Total Tokens**: 180346
- **Average per case**: 36069 tokens

#### Failed Cases (reward = 0.0)
- **Total Input Tokens**: 173902
- **Total Output Tokens**: 4256
- **Total Tokens**: 178158
- **Average per case**: 35632 tokens

## Notes

- **Turns**: Number of assistant responses (= complete interaction rounds)
- **Input Tokens**: Total prompt tokens for the longest trace (includes conversation history)
- **Output Tokens**: Total completion tokens for the longest trace
- **Total Tokens**: Sum of input and output tokens
- **Reward**: Terminal reward (1.0 = success, 0.0 = failure)
