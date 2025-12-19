#!/bin/bash

# 创建 tmux 会话
SESSION_NAME="cc_apo"

# 创建新会话，第一个窗口运行 store
tmux new-session -d -s $SESSION_NAME -n "store"
tmux send-keys -t $SESSION_NAME:0 "cd examples/cc && uv run agl store --port 4748" C-m

# 创建第二个窗口运行 rollout_runner，日志输出到文件
tmux new-window -t $SESSION_NAME:1 -n "runner"
tmux send-keys -t $SESSION_NAME:1 "cd examples/cc && uv run rollout_runner.py 2>&1 | tee logs/rollout_runner_$(date +%Y%m%d_%H%M%S).log" C-m

# 创建第三个窗口运行 cc_apo_algo，日志输出到文件
tmux new-window -t $SESSION_NAME:2 -n "apo"
tmux send-keys -t $SESSION_NAME:2 "cd examples/cc && uv run cc_apo_algo.py 2>&1 | tee logs/cc_apo_algo_$(date +%Y%m%d_%H%M%S).log" C-m

# 选择第一个窗口
tmux select-window -t $SESSION_NAME:0

echo "Tmux session '$SESSION_NAME' created with 3 windows:"
echo "  Window 0 (store): agl store --port 4748"
echo "  Window 1 (runner): rollout_runner.py (logging to logs/rollout_runner_*.log)"
echo "  Window 2 (apo): cc_apo_algo.py (logging to logs/cc_apo_algo_*.log)"
echo ""
echo "To attach: tmux attach -t $SESSION_NAME"
echo "To switch windows: Ctrl+b then 0/1/2"
echo "To detach: Ctrl+b then d"
