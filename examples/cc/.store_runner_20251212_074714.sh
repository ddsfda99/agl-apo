#!/bin/bash
cd /home/v-zitonzhang/agent-lightning
echo "Starting Store at $(date)" 
uv run agl store --port 4748 2>&1 | tee "/home/v-zitonzhang/agent-lightning/examples/cc/logs_batch_20251212_074714/store_20251212_074714.log"
