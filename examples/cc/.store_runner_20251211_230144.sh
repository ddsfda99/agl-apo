#!/bin/bash
cd /home/v-zitonzhang/agent-lightning
echo "Starting Store at $(date)" 
uv run agl store --port 4748 2>&1 | tee "/home/v-zitonzhang/agent-lightning/examples/cc/logs_batch_20251211_230144/store_20251211_230144.log"
