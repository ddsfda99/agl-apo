# applyedit.py 文档

## 概述

`applyedit.py` 是 APO (Automatic Prompt Optimization) 流程中的 **Apply Edit** 步骤。它根据 Text Gradient（对失败案例的评估反馈）来优化原始 prompt，生成改进后的 prompt。

## 工作流程

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Original       │     │   Text          │     │   Optimized     │
│  Prompt         │ ──► │   Gradient      │ ──► │   Prompt        │
│  (full_prompts/)│     │   (textgrads/)  │     │   (applyedits/) │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

## 输入/输出

### 输入

| 目录/文件 | 说明 |
|-----------|------|
| `examples/cc/full_prompts/{instance_id}_v{N}.txt` | 原始 prompt（最新版本） |
| `examples/cc/textgrads/{instance_id}_v{N}.txt` | Text Gradient 评估结果（最新版本） |
| `--dataset_path` | JSONL 数据集文件路径 |
| `--instance_id` | 要处理的实例 ID |

### 输出

| 目录/文件 | 说明 |
|-----------|------|
| `examples/cc/applyedits/{instance_id}_v{N}.txt` | 优化后的 prompt（版本化） |

## 使用方法

```bash
uv run examples/cc/applyedit.py \
    --dataset_path examples/cc/swe_100.jsonl \
    --instance_id sphinx-doc__sphinx-8595
```

## 核心逻辑

### 1. 加载原始 Prompt

从 `full_prompts/` 目录找到最新版本的 prompt 文件：
```
{instance_id}_v0.txt
{instance_id}_v1.txt  ← 选择最新版本
{instance_id}_v2.txt
```

### 2. 加载 Text Gradient

从 `textgrads/` 目录找到最新版本的评估反馈：
```
{instance_id}_v0.txt
{instance_id}_v1.txt  ← 选择最新版本
```

### 3. 调用 LLM 优化 Prompt

使用 `OPTIMIZER_PROMPT` 模板，将原始 prompt 和评估反馈发送给 GPT-5，生成优化后的 prompt。

```python
OPTIMIZER_PROMPT = """You are an expert in coding agent prompt optimization.
Your task is to improve the original prompt so that the coding agent can learn from previous failures...

the original prompt:
{original_prompt}

the evaluations of previous failures:
{evaluations}

Return the revised prompt text.
"""
```

### 4. 保存结果

将优化后的 prompt 保存到 `applyedits/{instance_id}_v{N}.txt`。

## 与 APO 算法的关系

在完整的 APO 流程中，`applyedit.py` 对应 **Apply Edit** 步骤：

```
1. Rollout      → 执行 agent，收集执行轨迹
2. Text Gradient → 分析失败原因，生成评估反馈 (textgrad.py)
3. Apply Edit   → 根据反馈优化 prompt (applyedit.py) ← 当前脚本
4. Evaluate     → 评估新 prompt 的效果
5. 重复...
```

## 依赖

- `openai`: Azure OpenAI SDK
- `utils.cloudgpt_aoai`: CloudGPT 认证工具

## 注意事项

1. 需要先运行 `textgrad.py` 生成评估反馈
2. 需要确保 `full_prompts/` 目录中有对应的原始 prompt
3. 使用 CloudGPT 的 Azure AD 认证，需要有效的 token
