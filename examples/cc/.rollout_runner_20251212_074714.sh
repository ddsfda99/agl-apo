#!/bin/bash
cd "/home/v-zitonzhang/agent-lightning/examples/cc"
sleep 5  # 等待 store 启动
echo "Starting Rollout Runner at $(date)"
uv run rollout_runner.py 2>&1 | tee "/home/v-zitonzhang/agent-lightning/examples/cc/logs_batch_20251212_074714/rollout_runner_20251212_074714.log"
