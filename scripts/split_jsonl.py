#!/usr/bin/env python3
from pathlib import Path
import sys

src = Path("examples/cc/swe_100.jsonl")
if not src.exists():
    print(f"Source file not found: {src}")
    sys.exit(1)

out_dir = src.parent
text = src.read_text(encoding='utf-8')
lines = [l for l in text.splitlines() if l.strip()!='']
if len(lines) == 0:
    print("Source file empty")
    sys.exit(1)

chunk_size = 10
n_chunks = (len(lines) + chunk_size - 1) // chunk_size
for i in range(n_chunks):
    chunk = lines[i*chunk_size:(i+1)*chunk_size]
    out = out_dir / f"swe_10_{i+1}.jsonl"
    out.write_text("\n".join(chunk) + "\n", encoding='utf-8')
    print(f"Wrote {out} ({len(chunk)} lines)")

print(f"Split {len(lines)} lines into {n_chunks} files.")
