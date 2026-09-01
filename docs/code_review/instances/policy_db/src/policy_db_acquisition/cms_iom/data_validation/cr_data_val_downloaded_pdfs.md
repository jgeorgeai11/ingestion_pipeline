---
name: cr-data_val_downloaded_pdfs
goal: Address code quality issues identified in code/data_acquisition/cms_iom/data_validation/data_val_downloaded_pdfs.py to align with python-development skills.
status: active
created: 2026-03-04 00:00:00
updated: 2026-03-04 00:00:00
---

## Implementation Plan

1. [pending] Fix exception handling - `code/data_acquisition/cms_iom/data_validation/data_val_downloaded_pdfs.py`
   - 1.1. [major] Line 191: Bare `except Exception` catches overly broad exceptions during TOML parsing; should catch `tomllib.TOMLDecodeError`
        - Current: `except Exception as e:`
        - Expected: `except tomllib.TOMLDecodeError as e:`
   - 1.2. [minor] Line 102: Missing exception handling for `pdf_path.stat()` call in `validate_no_zero_byte_files`; an `OSError` would propagate unhandled unlike the analogous `validate_pdf_headers` which catches `OSError`
        - Current:
          ```python
          size = pdf_path.stat().st_size
          ```
        - Expected:
          ```python
          try:
              size = pdf_path.stat().st_size
          except OSError as e:
              msg = f"Failed to stat {pdf_path.name}: {e}"
              logger.error(msg)
              errors.append(msg)
              continue
          ```
   - 1.3. [minor] Line 251: `except Exception` wrapping the main logic block is overly broad; should catch specific exceptions that the validation loop could raise (e.g., `OSError`) rather than generic `Exception`
        - Current: `except Exception as e:`
        - Expected: `except OSError as e:`

2. [pending] Fix logging issue - `code/data_acquisition/cms_iom/data_validation/data_val_downloaded_pdfs.py`
   - 2.1. [minor] Lines 213, 221: Missing closing separator before `sys.exit(1)`; the early exits at lines 213 and 221 skip the `logger.info("=" * 60)` closing separator, breaking the start/end boundary pattern established at line 179
        - Current: `sys.exit(1)` at lines 213 and 221 with no separator before exit
        - Expected: Add `logger.info("=" * 60)` before each `sys.exit(1)` call at lines 213 and 221, consistent with the pattern used elsewhere in the function

3. [pending] Fix docstring issue - `code/data_acquisition/cms_iom/data_validation/data_val_downloaded_pdfs.py`
   - 3.1. [minor] Line 168: `main()` docstring is minimal; should document Raises/exits behavior per Google style since it calls `sys.exit(1)` on failure
        - Current: `"""Entry point: parse config and run PDF validation."""`
        - Expected:
          ```python
          """Entry point: parse config and run PDF validation.

          Raises:
              SystemExit: With code 1 if config is missing, invalid, or validation fails.
          """
          ```

## Skills with No Issues

1. Type Hints: No issues found - all functions have complete parameter and return type annotations using modern syntax
2. Comments: No issues found - comments explain "why" and mark logical steps clearly
3. Data Validation: No issues found - script follows `data_val_` naming convention
4. Executable Scripts: No issues found - uses `main()` with `if __name__ == "__main__"`, single `--config` argument, deferred logging setup
5. Unit Tests: N/A - this is the source file, not a test file; no corresponding test file was reviewed

## Status & Next Steps

**Current Status**: Review complete, pending implementation
**Completed**:
1. Code review analysis of all python-development core skills
**Next Steps**:
1. Implement fixes from Implementation Plan
2. Consider adding a unit test file at `code/data_acquisition/cms_iom/data_validation/unit_tests/test_data_val_downloaded_pdfs.py`
**Blockers**:
1. None
**Notes**:
1. The code is well-structured overall with good separation of validation concerns into individual functions
2. The most impactful fix is item 1.1 -- catching specific TOML parsing exceptions rather than generic Exception
