# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Agent Lightning is a training framework for AI agents that works with any agent framework (LangChain, OpenAI Agents SDK, AutoGen, CrewAI, or plain OpenAI). It enables reinforcement learning, automatic prompt optimization, and supervised fine-tuning with minimal code changes.

## Development Commands

### Environment Setup

```bash
# Install with uv (recommended)
uv sync

# Install with optional dependencies for specific workflows
uv sync --group torch-stable        # Core PyTorch dependencies
uv sync --group torch-gpu-stable    # GPU dependencies (includes flash-attn, verl)
uv sync --group trl                 # For TRL/Unsloth examples
uv sync --group tinker              # For Tinker integration
uv sync --group agents              # All agent frameworks (autogen, langchain, etc.)

# Install specific agent framework
uv sync --group langchain
uv sync --group autogen
```

### Testing

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_client.py

# Run with pytest options
pytest -v  # verbose output
pytest -x  # stop on first failure
```

### Code Quality

```bash
# Format code
black .

# Sort imports
isort .

# Run linter
flake8

# Run pre-commit hooks
pre-commit run --all-files

# Type checking (optional)
pyright
```

### Documentation

```bash
# Serve docs locally
mkdocs serve
```

## Architecture

Agent Lightning follows a modular architecture centered around the **LightningStore**, which acts as the central hub connecting algorithms, runners, and telemetry.

### Core Components

- **LightningStore** (`agentlightning/store/base.py`): The persistent control-plane that coordinates training. Manages rollout lifecycle (queuing → running → succeeded/failed), span ingestion (telemetry), and resource versioning (prompts, model checkpoints).

- **Trainer** (`agentlightning/trainer/trainer.py`): High-level orchestration layer that wires together Algorithm → Runner → Store. Handles execution strategy (shared memory vs client/server), spawns runner workers, and manages telemetry plumbing.

- **Runner** (`agentlightning/runner/base.py`): Executes agent tasks by acquiring work from the store, hydrating LitAgent instances, and emitting Rollout objects. Supports both `iter()` for continuous task polling and `step()` for single-task execution.

- **Algorithm** (`agentlightning/algorithm/base.py`): Base class for training strategies (RL, APO, SFT). Algorithms read spans from the store, learn from them, and post updated resources back.

- **LitAgent** (`agentlightning/litagent/litagent.py`): User's agent implementation. Override `training_rollout()`, `validation_rollout()`, or the unified `rollout()` methods. The decorator version (`@agent`) in `decorator.py` converts plain functions into LitAgent subclasses.

- **Emitter** (`agentlightning/emitter/`): Helper functions for emitting structured telemetry during rollouts:
  - `emit_reward()` - Mark reward values in traces
  - `emit_message()` - Log messages
  - `emit_object()` - Attach arbitrary objects

- **Tracer** (`agentlightning/tracer/`): Collects execution traces. Default is `AgentOpsTracer`.

- **LLMProxy** (`agentlightning/llm_proxy.py`): Intercepts LLM calls for token-level tracking (required for some RL algorithms).

- **Adapter** (`agentlightning/adapter/`): Converts raw traces into algorithm-specific formats (e.g., `TracerTraceToTriplet` for RL triplets).

### Data Flow

1. **Collection**: Runner executes LitAgent, Tracer collects spans, Emitter functions attach rewards/metadata
2. **Storage**: Spans flow into LightningStore, rollouts track status
3. **Learning**: Algorithm reads spans via Adapter, computes updates
4. **Update**: Algorithm posts new resources (prompts, weights) back to store
5. **Iteration**: Runners fetch latest resources on next rollout

### Execution Strategies

- `SharedMemoryExecutionStrategy`: All components run in the same process
- `ClientServerExecutionStrategy`: Algorithm runs in server mode, runners connect as clients (configured via `port` parameter on Trainer)

## Working with Examples

Examples are in `examples/`. Each example has its own README with specific instructions:

```bash
# Run an example (e.g., spider)
cd examples/spider
python train_sql_agent.py qwen

# Interactive debugging
python sql_agent.py
```

For VERL-based RL training (search_r1, calc_x, spider):
- Training is invoked via `python -m agentlightning.verl` with hydra config
- Requires `torch-gpu-stable` dependency group
- Uses vLLM for rollout generation

## Key Patterns

- **Component Specification**: Many Trainer/constructor params accept flexible specs: concrete instances, classes, callables, registry strings, or config dicts. See `build_component()` in `trainer/init_utils.py`.

- **Weak References**: Algorithms hold weak refs to trainer/store/adapter to avoid circular references.

- **Async Support**: Both sync and async code paths throughout. Check `is_async()` on Algorithm/LitAgent to detect which path to use.

- **Registry Pattern**: ExecutionStrategyRegistry allows registering custom execution strategies.

## Dependency Notes

- Uses `uv` for dependency management with lockfile (`uv.lock`)
- PyTorch dependencies have separate CPU/GPU index sources
- VERL and vLLM have version compatibility requirements
- Some agent frameworks have version conflicts resolved in `override-dependencies`
