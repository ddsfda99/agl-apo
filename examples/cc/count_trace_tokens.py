import json
import os


def process_file(input_path):
    """Process a single JSON file and count tokens for the longest trace."""
    results = []
    
    with open(input_path, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                record = json.loads(line)
                results.append(record)
            except json.JSONDecodeError:
                continue
    
    # Find the span with the most prompts (same logic as extract_traces.py)
    max_sequence_id = 0
    max_sequence_index = -1
    
    for i, result in enumerate(results):
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
    
    # Extract token usage from the longest trace
    result = results[max_sequence_index]
    attrs = result.get("attributes", {})
    
    input_tokens = attrs.get("gen_ai.usage.prompt_tokens", 0)
    output_tokens = attrs.get("gen_ai.usage.completion_tokens", 0)
    total_tokens = attrs.get("llm.usage.total_tokens", 0)
    
    # Extract instance info from filename
    filename = os.path.basename(input_path)
    instance_id = filename.replace(".json", "")
    
    print(f"{instance_id}: {max_sequence_id} prompts, "
          f"input={input_tokens}, output={output_tokens}, total={total_tokens}")
    
    return {
        "instance_id": instance_id,
        "max_prompts": max_sequence_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens
    }


if __name__ == "__main__":
    import sys
    
    # Configuration - can be overridden by command line args
    if len(sys.argv) > 1:
        # Single file mode
        input_path = sys.argv[1]
        if not os.path.exists(input_path):
            print(f"Error: File not found: {input_path}")
            sys.exit(1)
        
        print(f"Processing single file: {input_path}")
        print("-" * 80)
        
        try:
            result = process_file(input_path)
            if result:
                print("-" * 80)
                print(f"Token statistics for longest trace:")
                print(f"  Instance ID: {result['instance_id']}")
                print(f"  Max prompts: {result['max_prompts']}")
                print(f"  Input tokens: {result['input_tokens']}")
                print(f"  Output tokens: {result['output_tokens']}")
                print(f"  Total tokens: {result['total_tokens']}")
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
        
        # Process all JSON files
        print(f"Processing files from {input_dir}...")
        print("-" * 80)
        
        all_results = []
        for filename in sorted(os.listdir(input_dir)):
            if not filename.endswith(".json"):
                continue
            
            input_path = os.path.join(input_dir, filename)
            try:
                result = process_file(input_path)
                if result:
                    all_results.append(result)
            except Exception as e:
                print(f"Error processing {filename}: {e}")
        
        print("-" * 80)
        print(f"Processed {len(all_results)} files successfully")
        
        # Summary statistics
        if all_results:
            total_input = sum(r['input_tokens'] for r in all_results)
            total_output = sum(r['output_tokens'] for r in all_results)
            total_all = sum(r['total_tokens'] for r in all_results)
            
            print(f"\nSummary:")
            print(f"  Total input tokens: {total_input:,}")
            print(f"  Total output tokens: {total_output:,}")
            print(f"  Total tokens: {total_all:,}")
            print(f"  Average input tokens per instance: {total_input / len(all_results):.0f}")
            print(f"  Average output tokens per instance: {total_output / len(all_results):.0f}")
