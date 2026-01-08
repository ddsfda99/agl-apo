"""
Use a given prompt, write a temporary agent config overriding user_prompt,
and run `cc_agent.py --official` with that config.
"""

import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CC_DIR = ROOT / "examples" / "cc"
BASE_CONFIG_PATH = CC_DIR / "agent_config.yaml"

# Fixed prompt that keeps the original instructions, notes the prior run issue, and appends gradient + apply-edit guidance.
FIXED_PROMPT = """Final goal:
You are given a code repository in the current directory (/testbed).
The bug description is:
{description}
=================================================
You task is to fix the bug with the following steps:
(1) write test cases to reproduce the bug.
(2) explore the source codes to locate the bug.
(3) edit the source codes to fix the bug.
(4) rerun your written test cases to validate that the bug is fixed. If not, go back to explore the source codes and fix the codes again.
(5) remember to delete the test cases you write at last.
Please do not commit your edits. We will do it later.

Previous run note:
The prior attempt with gpt-5-20250807 returned empty content (finish_reason=length). Use the following gradient feedback and revised apply-edit prompt for guidance.

Gradient feedback (from pytest logs):
- Failure occurs in test_rst_with_header_rows: the dtype header row ("float64 float32 int8") is being parsed as a data row, leading to “could not convert string to float: 'float64'”.
- Root cause: RST now forwards header_rows to FixedWidth, but SimpleRSTHeader does not implement parsing of multiple header rows (name/unit/dtype) and thus doesn’t consume them before data.
- Fix: Update the RST header class to handle header_rows=["name","unit","dtype"] (parse names, units, dtypes from the three header lines and skip them), or switch RST.header_class to a FixedWidthHeader variant that already supports header_rows.
- Ensure the parser identifies the header separator line correctly and excludes all specified header rows from data conversion.
- If writing is expected, adjust RST.write to emit unit/dtype rows when header_rows is set, matching the round-trip test expectations.
- Note: The NumPy binary compatibility warning is unrelated to this failure but indicates extensions were built against older headers; consider aligning build/runtime NumPy versions to avoid such warnings.

Apply-edit revised prompt (actionable plan):
You are given a code repository in /testbed.

Bug description:
Parsing reStructuredText (RST) tables with header_rows=["name","unit","dtype"] misinterprets the dtype header line (e.g., "float64 float32 int8") as a data row, causing “could not convert string to float: 'float64'”.

Your task:
(1) Add tests that reproduce the failure and assert correct behavior for RST read/write with multiple header rows (name/unit/dtype).
(2) Inspect the source to locate where RST header parsing/writing occurs and how header_rows is forwarded/consumed.
(3) Patch the RST header parsing to consume multiple header rows and exclude them from data conversion, and update writing to emit unit/dtype rows when requested.
(4) Rerun tests to validate the fix. Iterate if needed.
(5) Delete your added tests at the end. Do not commit edits.

Concretely:

Where to inspect and patch:
- Look for the RST ASCII reader/writer implementation:
  - astropy/io/ascii/rst.py (class RST, SimpleRSTHeader, write logic)
  - astropy/io/ascii/fixedwidth.py (FixedWidth, FixedWidthHeader parsing helpers and header_rows handling)
  - astropy/io/ascii/core.py (Reader base, header parsing flow, header_rows propagation)
- Verify RST.header_class and how header_rows is used. Root cause is SimpleRSTHeader not consuming multiple header rows and not parsing dtype/unit before data.

Tests to add (e.g., tests/io/ascii/test_rst.py):
- test_rst_with_header_rows_read:
  - Build an RST grid table string with three header lines:
    - names: "a b c"
    - units: "m s kg"
    - dtypes: "float64 float32 int8"
    - A header separator line (==== style) and at least two data rows, e.g., "1.0 2.0 3", "4.0 5.0 6".
  - Use astropy.io.ascii.read with format="rst" and header_rows=["name","unit","dtype"].
  - Assertions:
    - Table column names == ["a","b","c"].
    - Units parsed and attached correctly (e.g., str(col.unit) == "m"/"s"/"kg").
    - Dtypes applied: np.float64, np.float32, np.int8 respectively.
    - First data row values match without attempting to parse "float64".
    - No ValueError about converting "float64".
- test_rst_with_header_rows_roundtrip:
  - Create a Table with names ["a","b","c"], units ["m","s","kg"], dtypes [np.float64, np.float32, np.int8], and some rows.
  - Write using astropy.io.ascii.write with format="rst" and header_rows=["name","unit","dtype"] to a string buffer.
  - Read back with the same options.
  - Assert round-trip equality for names, units, dtypes, and data.
- Ensure tests cover both grid and simple RST table styles if supported (focus on the style your reader handles). Include a negative control where header_rows only includes ["name"] to verify backward compatibility.

Parsing and conversion logic to implement:
- In astropy/io/ascii/rst.py:
  - Update SimpleRSTHeader (or switch RST.header_class) to correctly handle header_rows=["name","unit","dtype"].
  - If retaining SimpleRSTHeader:
    - Implement parsing that:
      - Identifies the header block above the header separator (==== or ---- depending on style).
      - Consumes as many header lines as specified in header_rows, in order:
        - "name": parse column names.
        - "unit": parse units; attach as Column.unit (ensure unit strings are stored/converted properly; if using astropy.units, use Unit(unit_str); otherwise store string).
        - "dtype": parse dtype strings; convert via numpy.dtype(dtype_str) and set Column.dtype accordingly.
      - Validate header_rows contains only allowed values {{\"name\",\"unit\",\"dtype\"}}; raise a clear error if unknown.
      - Ensure all consumed header lines are excluded from data rows before type conversion.
    - Ensure the parser reliably detects the header separator line and does not misclassify the dtype line as data. For grid tables, look for the ruler with "=" after header; for simple tables, check the underline pattern.
  - Alternatively, set RST.header_class = FixedWidthHeader (from fixedwidth.py) if it already supports multi-line header_rows. Verify compatibility with existing RST behavior and adjust as needed to ensure the header separator is honored.
- Writing:
  - In RST.write (or writer for RST in rst.py), when header_rows includes "unit" and/or "dtype":
    - Emit lines for name, unit, dtype in that exact order before the header separator.
    - For dtype line, write numpy dtype names ("float64", "float32", "int8", etc.) derived from columns (col.dtype.name).
    - Ensure the emitted table style matches the reader (grid/simple). Units and dtypes must align with parsed positions.
- Type checks and conversions:
  - Map dtype strings robustly: use numpy.dtype(str).name or numpy.dtype(str) to set Table column dtypes.
  - If unit strings are empty or missing when "unit" is requested, set unit to None or "" consistently; ensure write/read symmetry.
  - Validate that the number of tokens in each header line matches the column count; raise a descriptive error if mismatched.

Validation:
- Run the added tests: pytest -q tests/io/ascii/test_rst.py::test_rst_with_header_rows_read tests/io/ascii/test_rst.py::test_rst_with_header_rows_roundtrip
- Also run existing tests that were failing (e.g., test_rst_with_header_rows) to confirm resolution.
- Inspect for any regression in single-line header parsing and basic RST reading/writing.

Cleanup:
- Delete the tests you added after validation.
- Do not commit any changes."""

# Append latest gradient/apply-edit outputs for additional guidance.
FIXED_PROMPT += """

another gradient feedback (recent run):
- The failing test reads an RST table with header_rows ["name", "unit", "dtype"]; the dtype row (“float64 float32 int8”) is being parsed as data, causing conversion of column “wave” to fail on the string “float64”.
- Root cause: RST reader still assumes a single header row; your change only updated write() and didn’t adjust reading logic (start_line/position_line) to account for multiple header rows.
- Fix the reader init: propagate header_rows to the header/data and set data.start_line to skip top rule + all header rows + the separator rule (e.g., start_line = 2 + len(header_rows)); set the position/separator line index accordingly.
- Ensure SimpleRSTHeader/SimpleRSTData respect header_rows for parsing units/dtypes and that separator placement matches both read and write paths.
- Keep a sane default when header_rows is None (e.g., ["name"]) to preserve backward compatibility.
- Add a unit test for reading RST with multiple header rows to confirm dtype/unit rows are not treated as data.

another apply-edit revised prompt (actionable plan):
You are given a code repository in the current directory (/testbed).
The bug description is:
{description}
=================================================
Task: Fix the RST table reader so it correctly handles multiple header rows (e.g., ["name", "unit", "dtype"]) during read, not just write. The current reader treats the dtype row as data, causing type conversion to fail (e.g., trying to convert the string "float64" in column "wave").

Follow these steps:

1) Write a failing unit test that reproduces the issue.
- Create tests/test_rst_reader_multi_header.py (to be deleted at the end).
- In the test, construct an RST grid table string with:
  - Top border rule
  - Three header rows: names, units, dtypes
  - A header/data separator rule
  - Two data rows
  Example layout (preserve exact borders and spacing; columns: wave, flux, flag):
  +------+-------+------+
  | name | unit  | dtype|
  +------+-------+------+
  | wave | nm    | float64 |
  | flux | Jy    | float32 |
  | flag |       | int8    |
  +======+=======+=========+
  | 500  | 1.5   | 1       |
  +------+-------+---------+
  | 600  | 2.0   | 0       |
  +------+-------+---------+
- Read with header_rows=["name","unit","dtype"] and assert:
  - No row contains strings "float64", "float32", "int8" as data.
  - Dtypes: wave -> float64, flux -> float32, flag -> int8.
  - Units: wave -> "nm", flux -> "Jy", flag -> None or "" as appropriate.
  - Shape: 2 rows x 3 columns.
- Also add a backward-compat test reading a table with a single header row (names only) when header_rows=None; behavior should match current single-header expectation.

2) Locate the reader code that parses RST grid tables.
- Search for classes/functions related to RST read/write:
  - grep -R "RST" or "rst" or "grid table" or "SimpleRST" in /testbed.
  - Look for modules like rst_reader.py, simple_rst.py, readers/rst.py, io/rst.py, or similar.
- Identify:
  - Reader initialization that sets header_rows, start_line, position_line/separator_line.
  - Classes SimpleRSTHeader and SimpleRSTData (or similarly named) used for parsing header and data.
  - Existing write() logic already updated for multiple header rows; mirror its separator placement in read path.

3) Patch the reader to respect multiple header rows.
- Propagate header_rows from the public read API into header/data parsing. If header_rows is None, use default ["name"] for backward compatibility.
- Adjust line indexing in the reader init (use 0-based):
  - Line 0: top border rule (e.g., "+---+...").
  - Lines 1..len(header_rows): header rows (names, units, dtypes).
  - Line 1 + len(header_rows): header/data separator rule (e.g., "+===+...").
  - Data starts at line 2 + len(header_rows).
- Concretely set:
  - data.start_line = 2 + len(header_rows)
  - header.separator_line_index (or position_line) = 1 + len(header_rows)
- Ensure SimpleRSTHeader:
  - Parses the "name" row into column names.
  - Optionally parses "unit" and "dtype" rows when present in header_rows.
  - Stores units and dtypes on the header/column metadata.
- Ensure SimpleRSTData:
  - Starts reading data from data.start_line.
  - Applies parsed dtypes to convert column values; do not treat dtype strings as data.
  - Units should not affect parsing of values (just metadata).
- Ensure separator placement mirrors write(): the same line used as header/data separator in write() must be the one read as the separator in the reader.

4) Add explicit data-format/type checks and conversion logic.
- When header_rows contains "dtype":
  - Map strings "float64", "float32", "int8", etc., to numpy/pandas dtypes.
  - Validate that all data rows for that column are converted to the target dtype; raise/handle errors appropriately.
- When header_rows contains "unit":
  - Capture unit strings as metadata only; do not alter numeric parsing.
- If header_rows lacks "unit" or "dtype":
  - Do not attempt to parse those rows; default units to None/"" and infer dtypes as before.
- Verify that blank unit cells are allowed and do not become data values.

5) Rerun tests.
- Run only the new test file and any existing RST reader tests.
- Confirm that:
  - The multi-header test passes (dtype/unit rows not treated as data).
  - Backward-compat single-header test passes.
  - No regressions in existing tests.

6) Cleanup.
- Delete tests/test_rst_reader_multi_header.py after validation.
- Do not commit changes.
"""

# Reference of prior code change (for context / further improvement):
FIXED_PROMPT += """

Earlier patch to astropy/io/ascii/rst.py (already attempted):
- __init__ accepts header_rows and forwards to FixedWidth so header/data start lines align:
  def __init__(self, header_rows=None):
      super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)
- write wraps output using the separator at index len(header_rows):
  def write(self, lines):
      lines = super().write(lines)
      header_rows = getattr(self.header, "header_rows", ["name"]) or ["name"]
      pos_idx = len(header_rows)
      if pos_idx < 0:
          pos_idx = 0
      if pos_idx >= len(lines):
          sep = lines[0] if lines else ""
      else:
          sep = lines[pos_idx]
      lines = [sep] + lines + [sep]
      return lines
- Use this as a baseline and improve the read path accordingly."""


def write_temp_config(new_prompt: str) -> Path:
    with open(BASE_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["agent"]["user_prompt"] = new_prompt

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    with tmp as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=False)
    return Path(tmp.name)


def run_cc_agent(config_path: Path) -> int:
    cmd = [
        "uv",
        "run",
        str(CC_DIR / "cc_agent.py"),
        "--official",
        "--agent_config",
        str(config_path),
    ]
    proc = subprocess.run(cmd, cwd=CC_DIR)
    return proc.returncode


def main() -> None:
    revised_prompt = FIXED_PROMPT.strip()
    print("=== Using fixed prompt ===")
    print(revised_prompt if revised_prompt else "[empty]")

    cfg_path = write_temp_config(revised_prompt)
    print(f"\nWrote temp agent config: {cfg_path}")

    code = run_cc_agent(cfg_path)
    print(f"\ncc_agent.py exited with code {code}")


if __name__ == "__main__":
    main()
