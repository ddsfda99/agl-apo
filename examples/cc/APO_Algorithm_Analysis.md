# APO (Automatic Prompt Optimization) 算法详细分析

## 目录
1. [算法概述](#算法概述)
2. [核心概念](#核心概念)
3. [算法流程](#算法流程)
4. [关键组件](#关键组件)
5. [配置参数](#配置参数)
6. [实际运行示例](#实际运行示例)

---

## 算法概述

APO (Automatic Prompt Optimization) 是一个基于 **文本梯度 (Textual Gradients)** 和 **束搜索 (Beam Search)** 的迭代式 prompt 优化算法。

### 核心思想
- 使用 LLM 生成对当前 prompt 的批评（critique），作为"文本梯度"
- 基于批评内容，使用另一个 LLM 对 prompt 进行编辑改进
- 通过束搜索在多个候选 prompt 中选择最优的进行下一轮优化
- 在验证集上评估 prompt 性能，保留历史最佳结果

### 理论基础
基于以下研究：
- **ProTeGi**: 使用 LLM 生成的反馈来优化 prompt
- **TextGrad**: 将梯度概念扩展到文本空间

---

## 核心概念

### 1. 文本梯度 (Textual Gradient)
传统机器学习中的梯度是数值，指示参数更新方向。APO 中的"文本梯度"是：
- **输入**: 当前 prompt + rollout 执行结果（包括 spans、messages、rewards）
- **输出**: LLM 生成的批评文本，描述 prompt 的问题和改进方向
- **作用**: 指导 prompt 的修改方向

### 2. 束搜索 (Beam Search)
- 维护一个大小为 `beam_width` 的候选 prompt 集合（beam）
- 每轮从 beam 中采样父 prompt，生成新候选
- 评估所有候选后，保留得分最高的 `beam_width` 个进入下一轮

### 3. 分支因子 (Branch Factor)
- 从每个父 prompt 生成 `branch_factor` 个子 prompt
- 控制搜索空间的扩展程度
- 更大的分支因子 = 更多样化的探索

### 4. Rollout
- 使用特定 prompt 在任务上执行 agent 的完整过程
- 记录执行轨迹（spans）、消息（messages）和最终奖励（reward）
- 用于评估 prompt 的性能

---

## 算法流程

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                    APO Algorithm Flow                        │
└─────────────────────────────────────────────────────────────┘

1. 初始化阶段
   ├── 加载 seed prompt
   ├── 准备 train/val 数据集
   └── 初始化 beam = [seed_prompt]

2. (可选) 基线评估
   └── 在验证集上评估 seed prompt，建立基线分数

3. 迭代优化 (beam_rounds 轮)
   │
   ├── Round 1
   │   ├── 采样父 prompt
   │   ├── 生成候选 prompt
   │   ├── 评估所有候选
   │   └── 选择 top-k 进入 beam
   │
   ├── Round 2
   │   ├── ...
   │   └── ...
   │
   └── Round N
       └── ...

4. 输出最佳 prompt
```

### 详细流程

#### Phase 1: 初始化 (`_initialize_beam`)

```python
# 1. 获取 seed prompt
resource_name, seed_prompt = self.get_seed_prompt_template()

# 2. 创建数据集迭代器
grad_iterator = batch_iter_over_dataset(train_dataset, gradient_batch_size)
val_iterator = batch_iter_over_dataset(val_dataset, val_batch_size)

# 3. 初始化历史最佳记录
self._history_best_prompt = seed_prompt
self._history_best_score = -inf
```

#### Phase 2: 基线评估 (可选)

```python
if run_initial_validation:
    # 在完整验证集上评估 seed prompt
    rollouts, seed_score = evaluate_prompt_on_batch(
        seed_prompt, 
        val_dataset, 
        mode="val"
    )
    # 更新历史最佳
    history_best_score = seed_score
```

#### Phase 3: 迭代优化 (每轮)

每轮包含 4 个主要步骤：

##### 3.1 采样父 Prompt (`_sample_parent_prompts`)

```python
# 从当前 beam 中随机采样 beam_width 个父 prompt
# 如果 beam 大小 < beam_width，则复制现有 prompt
parent_prompts = random.sample(beam, beam_width)
```

**示例**:
- beam_width = 2, beam = [v0:0.6, v1:0.7, v2:0.5]
- 采样结果: [v1, v0] (随机选择)

##### 3.2 生成候选 Prompt (`_generate_candidate_prompts`)

对每个父 prompt，生成 `branch_factor` 个子 prompt：

```python
for parent in parent_prompts:
    for branch in range(branch_factor):
        # Step 1: 在训练集上评估父 prompt
        grad_samples = next(grad_iterator)  # 获取训练样本
        rollout_results = evaluate_prompt_on_batch(
            parent, 
            grad_samples, 
            mode="train"
        )
        
        # Step 2: 计算文本梯度（批评）
        critique = compute_textual_gradient(
            parent, 
            rollout_results
        )
        
        # Step 3: 应用编辑生成新 prompt
        new_prompt = apply_edit(parent, critique)
        
        candidates.append(new_prompt)
```

**文本梯度计算细节** (`compute_textual_gradient`):

```python
# 1. 采样 rollout 结果
sampled_rollouts = random.sample(
    rollout_results, 
    gradient_batch_size
)

# 2. 构造 gradient prompt
gradient_prompt = f"""
Current prompt: {current_prompt}

Experiments (rollouts):
{sampled_rollouts}  # 包含 spans, messages, rewards

Task: Analyze the experiments and provide critique on how to improve the prompt.
"""

# 3. 调用 LLM 生成批评
critique = await openai_client.chat.completions.create(
    model=gradient_model,  # e.g., gpt-5-20250807
    messages=gradient_prompt,
    temperature=diversity_temperature
)
```

**应用编辑细节** (`textual_gradient_and_apply_edit`):

```python
# 构造 apply_edit prompt
apply_edit_prompt = f"""
Current prompt: {current_prompt}

Critique: {critique}

Task: Based on the critique, generate an improved version of the prompt.
"""

# 调用 LLM 生成新 prompt
new_prompt = await openai_client.chat.completions.create(
    model=apply_edit_model,  # e.g., gpt-5-20250807
    messages=apply_edit_prompt,
    temperature=diversity_temperature
)
```

##### 3.3 评估候选 (`_evaluate_and_select_beam`)

```python
# 1. 合并现有 beam 和新候选
all_candidates = beam + new_candidates

# 2. 在验证集上评估所有候选
val_batch = next(val_iterator)
for candidate in all_candidates:
    rollouts, score = evaluate_prompt_on_batch(
        candidate, 
        val_batch, 
        mode="val"
    )
    candidate.score = score

# 3. 按分数排序，选择 top-k
sorted_candidates = sorted(
    all_candidates, 
    key=lambda x: x.score, 
    reverse=True
)
new_beam = sorted_candidates[:beam_width]
```

##### 3.4 更新历史最佳 (`_update_best_prompt`)

```python
# 1. 获取当前 beam 中的最佳 prompt
best_in_beam = beam[0]  # beam 已排序

# 2. 在完整验证集上评估
_, best_score = evaluate_prompt_on_batch(
    best_in_beam, 
    full_val_dataset, 
    mode="val"
)

# 3. 更新历史最佳
if best_score > history_best_score:
    history_best_prompt = best_in_beam
    history_best_score = best_score
```

---

## 关键组件

### 1. LLMProxy
- 作为 LLM 代理服务器，统一管理模型调用
- 支持多个模型配置（gradient_model, apply_edit_model）
- 集成 OpenTelemetry 进行 tracing

### 2. Store (LightningStore)
- 存储和管理 rollout 数据
- 提供 rollout 排队和执行机制
- 存储 spans（执行轨迹）和 resources（prompt 等）

### 3. Rollout Runner
- 独立进程，从 Store 获取 rollout 任务
- 执行 agent 代码（如 cc_agent.py）
- 将执行结果（spans）写回 Store

### 4. TraceToMessages Adapter
- 将 spans 转换为 messages 格式
- 提取最长 trace 用于梯度计算
- 适配不同的 trace 格式

---

## 配置参数

### 核心参数

| 参数 | 说明 | 典型值 | 影响 |
|------|------|--------|------|
| `gradient_model` | 计算文本梯度的模型 | gpt-5-20250807 | 批评质量 |
| `apply_edit_model` | 应用编辑的模型 | gpt-5-20250807 | 新 prompt 质量 |
| `diversity_temperature` | LLM 调用的温度参数 | 0.7-1.0 | 生成多样性 |
| `gradient_batch_size` | 计算梯度时采样的 rollout 数量 | 1-4 | 梯度稳定性 |
| `val_batch_size` | 验证时使用的样本数量 | 1-16 | 评估准确性 |
| `beam_width` | beam 中保留的 prompt 数量 | 2-4 | 搜索广度 |
| `branch_factor` | 每个父 prompt 生成的子 prompt 数量 | 2-4 | 搜索深度 |
| `beam_rounds` | 优化轮数 | 2-5 | 优化程度 |
| `rollout_batch_timeout` | rollout 批次超时时间（秒） | 3600 | 容错性 |

### 参数选择建议

#### 单实例场景 (1 train + 1 val)
```python
gradient_batch_size = 1
val_batch_size = 1
beam_width = 2
branch_factor = 2
beam_rounds = 2-3
```

#### 小规模场景 (3 train + 2 val)
```python
gradient_batch_size = 3
val_batch_size = 2
beam_width = 2-4
branch_factor = 2-4
beam_rounds = 3-5
```

#### 大规模场景 (10+ train + 5+ val)
```python
gradient_batch_size = 4-8
val_batch_size = 4-16
beam_width = 4-8
branch_factor = 4-8
beam_rounds = 5-10
```

---

## 实际运行示例

### 配置示例 (cc_apo_algo.py)

```python
# 1. 启动 LLMProxy
llm_proxy = LLMProxy(port=12358, store=store)
llm_proxy.update_model_list([
    ModelConfig(
        model_name="claude-sonnet-4-5-20250929",
        litellm_params={
            "model": "azure/gpt-5-mini-20250807",
            ...
        }
    )
])

# 2. 配置 APO
apo = APO(
    async_openai_client=async_openai_client,
    gradient_model="gpt-5-20250807",
    apply_edit_model="gpt-5-20250807",
    gradient_batch_size=1,
    val_batch_size=1,
    beam_width=2,
    branch_factor=2,
    beam_rounds=3,
)

# 3. 设置初始资源
apo.set_initial_resources({
    "llm": llm_proxy.as_resource(model="local"),
    "prompt_template": PromptTemplate(template=SEED_PROMPT)
})

# 4. 加载数据集
train = load_dataset("astropy__astropy-7606.jsonl")
val = load_dataset("astropy__astropy-7606.jsonl")

# 5. 运行优化
await apo.run(train_dataset=train, val_dataset=val)
```

### 执行流程示例

假设配置：
- beam_width = 2
- branch_factor = 2
- beam_rounds = 3
- 1 train instance, 1 val instance

#### Round 0: 基线评估
```
[Round 00 | Prompt v0] Evaluating seed prompt on validation dataset
[Round 00 | Prompt v0] Seed prompt baseline score: 0.500
```

#### Round 1: 第一轮优化
```
[Round 01] Starting optimization round 1/3
[Round 01] Parent prompts: v0:0.500

# 从 v0 生成 2 个子 prompt
[Round 01 | Beam 01 | Branch 01 | Prompt v0] Evaluating on train set
[Round 01 | Beam 01 | Branch 01 | Prompt v0] Computing textual gradient
[Round 01 | Beam 01 | Branch 01 | Prompt v0] Applying edit
[Round 01 | Prompt v1] New prompt created from parent v0

[Round 01 | Beam 01 | Branch 02 | Prompt v0] Evaluating on train set
[Round 01 | Beam 01 | Branch 02 | Prompt v0] Computing textual gradient
[Round 01 | Beam 01 | Branch 02 | Prompt v0] Applying edit
[Round 01 | Prompt v2] New prompt created from parent v0

# 评估所有候选 (v0, v1, v2)
[Round 01 | Prompt v0] Evaluation score: 0.500
[Round 01 | Prompt v1] Evaluation score: 0.650
[Round 01 | Prompt v2] Evaluation score: 0.600

# 选择 top-2
[Round 01] Top 2 candidates: v1:0.650, v2:0.600
[Round 01 | Prompt v1] Best prompt updated. New best score: 0.650 (prev: 0.500)
```

#### Round 2: 第二轮优化
```
[Round 02] Starting optimization round 2/3
[Round 02] Parent prompts: v1:0.650, v2:0.600

# 从 v1 生成 2 个子 prompt
[Round 02 | Beam 01 | Branch 01 | Prompt v1] ...
[Round 02 | Prompt v3] New prompt created from parent v1

[Round 02 | Beam 01 | Branch 02 | Prompt v1] ...
[Round 02 | Prompt v4] New prompt created from parent v1

# 从 v2 生成 2 个子 prompt
[Round 02 | Beam 02 | Branch 01 | Prompt v2] ...
[Round 02 | Prompt v5] New prompt created from parent v2

[Round 02 | Beam 02 | Branch 02 | Prompt v2] ...
[Round 02 | Prompt v6] New prompt created from parent v2

# 评估所有候选 (v1, v2, v3, v4, v5, v6)
[Round 02] Top 2 candidates: v3:0.700, v1:0.650
[Round 02 | Prompt v3] Best prompt updated. New best score: 0.700 (prev: 0.650)
```

#### Round 3: 第三轮优化
```
[Round 03] Starting optimization round 3/3
[Round 03] Parent prompts: v3:0.700, v1:0.650
...
[Round 03] Top 2 candidates: v7:0.750, v3:0.700
[Round 03 | Prompt v7] Best prompt updated. New best score: 0.750 (prev: 0.700)
```

#### 最终结果
```
APO optimization complete!
Best prompt: v7
Best score: 0.750
Improvement: +0.250 (50% increase from baseline)
```

---

## 关键设计决策

### 1. 为什么使用束搜索？
- **平衡探索与利用**: 保留多个候选避免过早收敛
- **容错性**: 即使某个分支失败，其他分支仍可继续
- **多样性**: 不同的父 prompt 可能探索不同的优化方向

### 2. 为什么需要两个模型？
- **gradient_model**: 需要强大的分析能力，理解 rollout 结果并提供有价值的批评
- **apply_edit_model**: 需要良好的文本生成能力，基于批评生成改进的 prompt
- 可以使用相同模型，也可以使用不同模型（如 gpt-5 vs gpt-5-mini）

### 3. 为什么分离 train 和 val？
- **train**: 用于计算梯度，可以使用较小的样本量
- **val**: 用于评估和选择，需要更全面的评估
- 避免过拟合到训练样本

### 4. 为什么需要 rollout_batch_timeout？
- Agent 执行可能失败或卡住
- 设置超时确保算法能继续进行
- 未完成的 rollout 不会阻塞整个流程

---

## 性能优化建议

### 1. 并行化
- 多个 rollout 可以并行执行（通过多个 rollout_runner）
- 候选评估可以并行（异步 API 调用）

### 2. 缓存
- 相同 prompt 在相同任务上的结果可以缓存
- 避免重复评估

### 3. 早停
- 如果连续多轮没有改进，可以提前停止
- 节省计算资源

### 4. 自适应参数
- 根据数据集大小动态调整 batch_size
- 根据改进速度调整 beam_width

---

## 常见问题

### Q1: 为什么我的 APO 没有改进？
- 检查 gradient_model 是否足够强大
- 增加 gradient_batch_size 获得更稳定的梯度
- 增加 beam_rounds 进行更多轮优化
- 检查 seed prompt 是否已经很好

### Q2: 为什么 rollout 超时？
- 增加 rollout_batch_timeout
- 检查 agent 代码是否有死循环
- 检查任务是否过于复杂

### Q3: 如何调试 APO？
- 设置 `_poml_trace=True` 记录详细日志
- 查看 logs 目录下的日志文件
- 使用 Store 的 Web UI 查看 rollout 状态

### Q4: 内存不足怎么办？
- 减小 beam_width 和 branch_factor
- 减小 batch_size
- 使用更小的模型

---

## 总结

APO 算法通过以下机制实现 prompt 优化：

1. **迭代优化**: 多轮优化逐步改进 prompt
2. **文本梯度**: 使用 LLM 生成的批评指导优化方向
3. **束搜索**: 维护多个候选避免局部最优
4. **验证驱动**: 在验证集上评估确保泛化性能

关键成功因素：
- 合适的参数配置（beam_width, branch_factor, rounds）
- 高质量的 gradient_model
- 充足的训练和验证数据
- 稳定的 rollout 执行环境

APO 特别适合需要复杂推理的任务，如代码修复、问题解答等，在这些任务中 prompt 的质量对性能有显著影响。
