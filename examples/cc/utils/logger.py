import datetime
import os


def logger(run_id, instance_id, text):
    base_dir = os.environ.get("CC_LOGS_DIR", "logs")
    log_dir = os.path.join(".", base_dir, run_id)
    os.makedirs(log_dir, exist_ok=True)
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(log_dir, instance_id), mode="a") as f:
        print(f"\n\n{current_time}\n{text}\n", file=f)
