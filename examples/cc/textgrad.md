# TextGrad - 基于文本梯度的 Prompt 优化工具

## 概述

`textgrad.py` 是一个基于 **文本梯度 (Text Gradient)** 思想的 prompt 优化工具。它通过分析 coding agent 的执行轨迹（trace），找出失败原因，并生成改进建议，用于优化下一轮的 prompt。

## 核心思想

```
Agent 执行轨迹 (包含原始 Prompt) → LLM 分析 → 优化建议（文本梯度）
```

这类似于深度学习中的梯度下降：
- **损失函数**: Agent 执行失败
- **梯度**: LLM 生成的改进建议
- **参数更新**: 将建议融入下一轮 prompt

## 使用方法

```bash
uv run examples/cc/textgrad.py \
    --dataset_path examples/cc/swe_100.jsonl \
    --instance_id django__django-11066
```

### 参数说明

| 参数 | 必需 | 说明 |
|------|------|------|
| `--dataset_path` | ✅ | SWE-bench JSONL 数据集路径 |
| `--instance_id` | ✅ | 要分析的实例 ID |

## 工作流程

```
┌─────────────────────────────────────────────────────────────┐
│                    TextGrad 工作流程                         │
└─────────────────────────────────────────────────────────────┘

1. 加载数据
   └── 从 JSONL 加载指定 instance

2. 加载执行轨迹 (包含原始 prompt)
   ├── 查找 data_*-{instance_id}/ 目录下的原始 trace
   ├── 调用 extract_traces.py 提取结构化 trace
   └── 保存到 trace/{instance_id}_extracted_v{N}.json

3. 构造优化 Prompt
   └── 将执行轨迹填入 TASK_OPTIMIZATION_PROMPT

4. 调用 LLM 分析
   └── 使用 gpt-5-20250807 生成改进建议

5. 保存结果
   └── 输出到 textgrads/{instance_id}_v{N}.txt
```

## 目录结构

```
examples/cc/
├── trace/                           # 提取后的 trace 存放目录
│   └── {instance_id}_extracted_v{N}.json
├── textgrads/                       # 优化建议输出目录
│   └── {instance_id}_v{N}.txt       # 版本化的优化建议
└── data_*-{instance_id}/            # 原始 agent 执行数据
    └── {instance_id}.json
```

**注意**: 原始 prompt 已包含在 trace 的第一条 user 消息中，无需单独加载。

## 核心函数

### `load_instance(dataset_path, instance_id)`
从 JSONL 数据集中加载指定的实例。

### `find_latest_prompt(prompt_dir, instance_id)`
查找指定实例的最新版本 prompt 文件。
- 文件命名格式: `{instance_id}_v{N}.txt`
- 返回版本号最大的文件路径

### `find_latest_raw_trace(instance_id)`
查找原始 agent 执行轨迹文件。
- 搜索 `data_*-{instance_id}/` 目录
- 返回最新修改的文件

### `_load_agent_log_from_data_dir(instance_id)`
加载并处理 agent 执行轨迹：
1. 查找原始 trace 文件
2. 调用 `extract_traces.py` 提取结构化数据
3. 将 trace 渲染为可读文本格式

### `next_textgrad_path(output_dir, instance_id)`
生成下一个版本的输出文件路径。
- 自动递增版本号
- 避免覆盖已有文件

## 优化 Prompt 模板

```python
TASK_OPTIMIZATION_PROMPT = """
You are a senior software engineer and an expert code reviewer.
Your goal is to review a coding agent's trace to solve a problem.
...
Return your analysis and insights so that your output can be 
combined with the original prompt to make the coding agent pass all tests.
"""
```

LLM 会分析：
- Agent 在哪些地方做得不好
- 为什么会失败
- 如何改进

## 输出示例

```
Loaded instance: django__django-11066
Loaded full prompt from examples/cc/full_prompts/django__django-11066_v0.txt
Loaded agent trace from extracted file (15234 chars)

=== Computing Task Optimization for django__django-11066 ===

>>> OPTIMIZED TASK DESCRIPTION PREVIEW (First 500 chars) >>>
------------------------------------------------------------
The agent failed because it did not correctly identify the root cause...
1. The agent should first run the existing tests to understand...
2. When modifying the code, ensure that...
------------------------------------------------------------

Saved optimized task description to examples/cc/textgrads/django__django-11066_v0.txt
```

## 与 APO 的关系

TextGrad 是 APO (Automatic Prompt Optimization) 的简化版本：

| 特性 | TextGrad | APO |
|------|----------|-----|
| 梯度计算 | 单次 LLM 调用 | 多次 rollout + 批量分析 |
| 搜索策略 | 无（单次优化） | Beam Search |
| 评估方式 | 无自动评估 | 验证集评估 |
| 迭代方式 | 手动 | 自动多轮 |
| 适用场景 | 快速调试、单例分析 | 系统化优化 |

## 典型使用场景

1. **调试失败案例**: 分析为什么某个 instance 失败
2. **快速迭代**: 手动优化 prompt 的辅助工具
3. **生成训练数据**: 为 APO 提供初始优化方向
4. **理解 Agent 行为**: 通过 LLM 分析 trace 获得洞察

## 依赖

- `openai`: Azure OpenAI SDK
- `utils.cloudgpt_aoai`: CloudGPT 认证
- `extract_traces.py`: Trace 提取工具
