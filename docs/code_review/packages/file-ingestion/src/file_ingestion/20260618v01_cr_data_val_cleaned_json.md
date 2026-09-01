---
name: cr-data_val_cleaned_json
goal: Address code quality issues identified in code/file_ingestion/data_validation/data_val_cleaned_json.py to align with python-development core skills (type hints, docstrings, comments, logging, exception handling, executable-scripts, data-validation).
created: 2026-06-18 00:00:00
updated: 2026-06-18 00:00:00
---

## Implementation Plan

1. [pending] Fix correctness/crash bug - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - 1.1. [major] Lines 121-122: `word_count` recomputation crashes on non-string, non-null `heading_text`/`content_text`. The `else` branch runs once `word_count` is a valid non-negative int, then calls `.split()` on `(heading_text or "")` and `(content_text or "")`. A `null` value is handled correctly (`None or ""` -> `""`), but a non-null non-string value (e.g. JSON number `123`) yields `(123 or "")` -> `123`, and `123.split()` raises `AttributeError`. This exception is uncaught in `validate_cleaned_file` and `main()` does not wrap the call at line 205, so a single malformed field crashes the entire validation run with a traceback instead of being reported as a failure. For a validator explicitly designed to handle malformed input gracefully, this is a real defect. This is also the same inconsistency described in 4.1 below: `heading_text`/`content_text` are never type-checked, unlike `word_count`.
        - Current: `expected = len((heading_text or "").split()) + len((content_text or "").split())`
        - Expected: validate that `heading_text` and `content_text` are `str` (or null) before computing words, and append a failure (rather than raising) when they are a non-null non-string, e.g.:
          ```python
          if not isinstance(heading_text, (str, type(None))):
              failures.append(f"FAIL: {loc}: heading_text is not a string ({heading_text!r})")
          elif not isinstance(content_text, (str, type(None))):
              failures.append(f"FAIL: {loc}: content_text is not a string ({content_text!r})")
          else:
              expected = len((heading_text or "").split()) + len((content_text or "").split())
              if word_count != expected:
                  ...
          ```

2. [pending] Fix field-validation inconsistencies (type checks) - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - 2.1. [minor] Lines 71, 78: `n_parsed_sections` accepts `bool`. `isinstance(True, int)` is `True` in Python, so a JSON `true`/`false` for `n_parsed_sections` passes the int check and is then compared to `len(sections)`. Inconsistent with `word_count` (line 117), which explicitly excludes `bool`.
        - Current: `if not isinstance(n_parsed, int):`
        - Expected: `if not isinstance(n_parsed, int) or isinstance(n_parsed, bool):`
   - 2.2. [minor] Line 107: `sort_order` accepts `bool`. A `true` value is treated as the integer `1` and appended to `actual_orders`, which can spuriously satisfy the 1-based-contiguous check at line 142. Inconsistent with `word_count`'s explicit bool exclusion.
        - Current: `if isinstance(sort_order, int):`
        - Expected: `if isinstance(sort_order, int) and not isinstance(sort_order, bool):`
   - 2.3. [minor] Lines 130-136: `page_start`/`page_end` are only compared when both are `int`. A non-null but non-int value (e.g. a string `"5"` or a float) is silently skipped with no failure, whereas `word_count` flags non-int values. The spec is "page_start <= page_end where both are non-null", so non-null-but-non-int page values escape validation. Either type-check these fields like `word_count`, or document that only int pairs are compared.
        - Current: only the `page_start > page_end` mismatch is reported.
        - Expected: also report when `page_start`/`page_end` are non-null but not int (and accept `bool` exclusion for consistency).

3. [pending] Reduce redundant / noisy secondary failures - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - 3.1. [minor] Lines 99-110, 142-146: A single malformed section is reported multiple times. A missing `sort_order` key is flagged once by the missing-keys diff (line 102) and again as "sort_order is not an int" (line 110). Separately, when any section has a non-int `sort_order` or is a non-dict (line 96), `actual_orders` ends up shorter than `expected_orders`, so the 1-based-contiguous check (line 142) fires an additional, confusing failure that merely echoes the upstream problem. Consider suppressing the contiguity check when per-section structural failures already exist for the file (e.g. skip when sort_order extraction was incomplete), so each underlying issue produces one clear message.

## Skills with No Issues

1. Type Hints: No issues found - `validate_cleaned_file(json_path: Path) -> list[str]` and `main() -> None` are fully annotated; local annotations (`failures: list[str]`, `actual_orders: list[int]`) use modern syntax.
2. Docstrings: No issues found - module docstring documents the checks and usage; both functions have Google-style docstrings with Args/Returns where applicable.
3. Comments: No issues found - comments explain the "why" (e.g. lines 83-85 justify the zero-section failure; line 164 explains deferred logging) rather than restating the code.
4. Logging: No issues found - uses `logconfig` (`setup_logging`/`get_logger`), no `print()`, f-strings throughout, run separators `"=" * 60` at start/end, log dir mirrors script location (`logs/file_ingestion/data_validation`).
5. Exception Handling: No issues found in the handlers present - `json.loads` catches `(json.JSONDecodeError, OSError)` (lines 63-64), config read catches `(tomllib.TOMLDecodeError, OSError)` (line 182), and config-field access catches `KeyError` (line 191); all specific, all with context. (The crash in 1.1 is an uncaught path, not a misused handler.)
6. Executable Scripts: No issues found - has `main()` with `if __name__ == "__main__"`, single required `--config` argument, logging setup deferred until after argparse, correct `sys.exit(1)` on every failure path and clean exit on success.
7. Data Validation: No issues found - script is correctly named `data_val_` and located under `data_validation/` alongside the validated module.
8. Unit Tests: N/A - target is the validator script itself; no test file under review.

## Status & Next Steps

**Current Status**: Review complete, pending implementation.
**Completed**:
1. Reviewed all six documented checks for correctness against the stated spec.
2. Verified edge-case handling: missing cleaned dir, missing/malformed file, empty sections list, non-int/bool fields, null `content_text`.
3. Classified findings by severity and listed standards with no issues.
**Next Steps**:
1. Fix the `heading_text`/`content_text` type guard (1.1) to prevent a run-crashing `AttributeError`.
2. Align `bool` handling across `n_parsed_sections`, `sort_order`, and `page_*` with `word_count` (2.1-2.3).
3. Optionally de-duplicate secondary failure messages (3.1).
**Blockers**:
1. None.
**Notes**:
1. Missing cleaned directory (task-named edge case): handled only indirectly. Line 195 builds `Path(cleaned_dir)` with no existence check, so a missing or wrong directory degrades to N separate "file not found" messages (line 58), one per expected stem. This still fails with exit code 1 (acceptable), but there is no distinct directory-level diagnostic that would tell the user the whole `cleaned_dir` is absent rather than individual files. Consider an explicit `cleaned_dir_path.exists()` check after line 195 for a clearer message; left out of the plan as a suggestion since current behavior is functionally correct.
2. Null `content_text`/`heading_text` is handled correctly via `(x or "")`; only the non-null non-string case (1.1) is defective.
3. SQL development skills are N/A - this file contains no SQL.
