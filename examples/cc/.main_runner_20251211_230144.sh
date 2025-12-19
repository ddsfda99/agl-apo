#!/bin/bash
WORK_DIR="/home/v-zitonzhang/agent-lightning/examples/cc"
cd "$WORK_DIR"

TIMESTAMP="$1"
LOG_DIR="$2"

INSTANCE_IDS=(
    "astropy__astropy-12907"
    "astropy__astropy-13033"
    "astropy__astropy-13236"
    "astropy__astropy-13398"
    "astropy__astropy-13453"
)

echo "=========================================="
echo "Main Runner Started at $(date)"
echo "=========================================="

# 等待 store 和 rollout_runner 启动
echo "Waiting for store and rollout_runner to start..."
sleep 15

for i in "${!INSTANCE_IDS[@]}"; do
    INSTANCE_ID="${INSTANCE_IDS[$i]}"
    INDEX=$((i+1))
    INSTANCE_TS=$(date +"%Y%m%d_%H%M%S")
    APO_LOG="${LOG_DIR}/apo_${INSTANCE_ID}_${INSTANCE_TS}.log"
    
    echo ""
    echo "=========================================="
    echo "[${INDEX}/${#INSTANCE_IDS[@]}] Instance: $INSTANCE_ID"
    echo "Time: $(date)"
    echo "APO Log: $APO_LOG"
    echo "=========================================="
    
    # 备份并修改 cc_apo_algo.py
    cp cc_apo_algo.py cc_apo_algo.py.bak_${TIMESTAMP}
    sed -i "s|dataset = load_dataset(\".*\.jsonl\")|dataset = load_dataset(\"temp_instance_${INSTANCE_ID}.jsonl\")|g" cc_apo_algo.py
    
    # 运行 APO
    {
        echo "========================================"
        echo "Instance: $INSTANCE_ID"
        echo "Index: ${INDEX}/${#INSTANCE_IDS[@]}"
        echo "Start time: $(date)"
        echo "Dataset: temp_instance_${INSTANCE_ID}.jsonl"
        echo "========================================"
        
        cd "$WORK_DIR"
        uv run cc_apo_algo.py 2>&1
        
        echo "========================================"
        echo "End time: $(date)"
        echo "Exit code: $?"
        echo "========================================"
    } 2>&1 | tee "$APO_LOG"
    
    # 恢复原始文件
    mv cc_apo_algo.py.bak_${TIMESTAMP} cc_apo_algo.py
    
    echo "Completed: $INSTANCE_ID"
    
    # 等待一下再继续下一个
    sleep 10
done

echo ""
echo "=========================================="
echo "All instances completed at $(date)"
echo "=========================================="

# 清理临时文件
rm -f ${WORK_DIR}/temp_instance_*.jsonl
echo "Cleanup done!"
