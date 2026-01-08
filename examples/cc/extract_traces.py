import json
import os
from collections import defaultdict


def extract_trace(result):
    """Extract trace messages from span attributes."""
    res = {}
    for k, v in result["attributes"].items():
        if "gen_ai.prompt" in k:
            try:
                v_eval = eval(v)
            except:
                v_eval = v
            index = k.split(".")[2]
            if index not in res:
                res[index] = {}
            if "content" in k:
                res[index]["content"] = v_eval
            elif "role" in k:
                res[index]["role"] = v_eval
    
    trace = []
    for idx in sorted(res.keys(), key=lambda x: int(x)):
        trace.append(res[idx])
    
    return trace


def process_file(input_path, output_dir):
    """Process a single JSON file and extract trace information."""
    results = []
    reward = None
    
    with open(input_path, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                record = json.loads(line)
                results.append(record)
            except json.JSONDecodeError:
                continue
    
    # Find the span with the most prompts and extract reward
    max_sequence_id = 0
    max_sequence_index = -1
    
    for i, result in enumerate(results):
        # Check for reward attribute
        if "reward" in result.get("attributes", {}):
            reward = result["attributes"]["reward"]
        
        # Count prompt messages
        tm = 0
        for k in result.get("attributes", {}).keys():
            if k.startswith("gen_ai.prompt"):
                parts = k.split(".")
                if len(parts) > 2 and parts[2].isdigit():
                    idx = int(parts[2])
                    if idx > tm:
                        tm = idx
        
        if tm > max_sequence_id:
            max_sequence_id = tm
            max_sequence_index = i
    
    if max_sequence_index == -1:
        print(f"Warning: No valid trace found in {input_path}")
        return None
    
    result = results[max_sequence_index]
    trace = extract_trace(result)
    
    # Extract instance info from filename
    filename = os.path.basename(input_path)
    instance_id = filename.replace(".json", "")
    
    print(f"{instance_id}: {max_sequence_id} prompts, reward={reward}")
    
    # Save extracted trace
    output_path = os.path.join(output_dir, f"{instance_id}_extracted.json")
    with open(output_path, "w", encoding="utf-8") as outfile:
        json.dump({
            "instance_id": instance_id,
            "trace": trace,
            "terminal_reward": reward,
            "max_prompts": max_sequence_id
        }, outfile, indent=2, ensure_ascii=False)
    
    return instance_id, max_sequence_id, reward


if __name__ == "__main__":
    import sys
    
    # Configuration - can be overridden by command line args
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
        if not os.path.exists(input_path):
            print(f"Error: Path not found: {input_path}")
            sys.exit(1)

        # If input_path is a directory, process all .json files inside
        if os.path.isdir(input_path):
            # Determine output directory: arg2 if provided, else <input_dir>
            if len(sys.argv) > 2:
                output_dir = sys.argv[2]
            else:
                output_dir = input_path

            os.makedirs(output_dir, exist_ok=True)

            print(f"Processing directory: {input_path}")
            print(f"Output will be saved to {output_dir}")
            print("-" * 80)

            processed_count = 0
            for filename in sorted(os.listdir(input_path)):
                if not filename.endswith(".json"):
                    continue
                input_file = os.path.join(input_path, filename)
                try:
                    result = process_file(input_file, output_dir)
                    if result:
                        processed_count += 1
                except Exception as e:
                    print(f"Error processing {filename}: {e}")

            print("-" * 80)
            print(f"Processed {processed_count} files successfully")
            print(f"Extracted traces saved to {output_dir}")
        else:
            # Single file mode
            # Determine output directory
            if len(sys.argv) > 2:
                output_dir = sys.argv[2]
            else:
                output_dir = os.path.dirname(input_path)

            os.makedirs(output_dir, exist_ok=True)

            print(f"Processing single file: {input_path}")
            print(f"Output will be saved to {output_dir}")
            print("-" * 80)

            try:
                result = process_file(input_path, output_dir)
                if result:
                    print("-" * 80)
                    print(f"Successfully extracted trace to {output_dir}")
                else:
                    print("-" * 80)
                    print("No valid trace found in file")
            except Exception as e:
                print(f"Error processing file: {e}")
                import traceback
                traceback.print_exc()
    else:
        # Directory mode (default)
        input_dir = "data"
        output_dir = "extracted_traces"

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Process all JSON files
        print(f"Processing files from {input_dir}...")
        print(f"Output will be saved to {output_dir}")
        print("-" * 80)

        processed_count = 0
        for filename in sorted(os.listdir(input_dir)):
            if not filename.endswith(".json"):
                continue

            input_path = os.path.join(input_dir, filename)
            try:
                result = process_file(input_path, output_dir)
                if result:
                    processed_count += 1
            except Exception as e:
                print(f"Error processing {filename}: {e}")

        print("-" * 80)
        print(f"Processed {processed_count} files successfully")
        print(f"Extracted traces saved to {output_dir}")
