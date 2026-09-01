---
name: cr-file_parser
goal: Address code quality issues identified in code/file_ingestion/file_parser.py to align with python-development skills.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Implementation Plan

1. [completed] Type precision for FORMAT_CONFIG values - `code/file_ingestion/file_parser.py`
   - 1.1. [minor] Lines 28 / 204 / 228 / 239: `FORMAT_CONFIG` is typed `dict[str, dict[str, str | None]]`, so every value access (`cfg["ext"]`, `cfg["method"]`) is statically `str | None`. These values then flow into APIs that require a concrete `str`: `tempfile.mkstemp(suffix=cfg["ext"])` (line 228) and `getattr(doc, cfg["method"], None)` (line 239), plus the f-strings `f"{stem}{cfg['ext']}"` (lines 174, 184, 186). A type checker will flag `mkstemp(suffix=str | None)` and `getattr(doc, str | None)` because both reject `None`. At runtime `ext` is never `None` and `method` is `None` only for `"text"` (handled before line 239), so this is a precision gap, not a live bug. Consider a small typed structure that distinguishes the two value kinds so `ext` is always `str`.
        - Current:
          ```python
          FORMAT_CONFIG: dict[str, dict[str, str | None]] = {
              "markdown": {"method": "save_as_markdown", "ext": ".md"},
              ...
          }
          ```
        - Expected: model the entry as a `TypedDict` (or dataclass) where `ext: str` and `method: str | None`, e.g.
          ```python
          class FormatConfig(TypedDict):
              method: str | None
              ext: str
          FORMAT_CONFIG: dict[str, FormatConfig] = {...}
          ```
          so `cfg["ext"]` narrows to `str` at the `mkstemp`/f-string call sites and only `cfg["method"]` remains optional.
   - **RESOLVED (1.1):** Added a `FormatConfig(TypedDict)` with `method: str | None` and `ext: str`, imported `TypedDict` from `typing`, and re-annotated `FORMAT_CONFIG: dict[str, FormatConfig]` and `_export_atomic`'s `cfg` parameter. `cfg["ext"]` now narrows to `str` at the `mkstemp` and f-string call sites while `cfg["method"]` stays optional (the `"text"` special-case still handled before the `getattr`). Pure typing change, no runtime behavior change.

2. [completed] Atomicity scope is documented as per-file but is per-(file, format) - `code/file_ingestion/file_parser.py`
   - 2.1. [minor] Lines 56-60 (docstring) vs. 171-189: The `parse_files_docling` docstring says "Each output file is written atomically ... so a crash mid-write never leaves a truncated file." That is accurate per output file. However, the surrounding success/failure bookkeeping is per source file: if a file requests two formats and the second `_export_atomic` raises, the first format's `os.replace` has already landed a complete real file on disk (lines 174-186), yet the source file is recorded only as a failure and omitted from `successes` (lines 171, 188-189). The on-disk first-format file is left in place. A re-run that skips by checking only some of the expected outputs could then treat that file as complete. The prior review (`cr_file_parser.md` 3.2) explicitly accepted this and mitigated it in `ingest.py` by requiring all requested formats present before skipping; this remains correct only as long as that orchestration invariant holds. Recommend a one-line docstring note that atomicity is per output file (not per source file) and that partial outputs can survive a multi-format failure, so the guarantee is not over-read by future callers.
   - **RESOLVED (2.1):** Added a docstring paragraph to `parse_files_docling` stating that atomicity is per output file (not per source file) via each format's temp-then-os.replace, that a multi-format request whose later format fails can leave an earlier format's complete output on disk while the source file is still recorded as a failure, and that the `ingest.py` caller guards against mis-skipping by requiring all requested formats present before treating a file as parsed. Docstring-only, no code change.

3. [completed] Temp-file extension can mislead the skip-if-exists scan - `code/file_ingestion/file_parser.py`
   - 3.1. [suggestion] Lines 227-229: The temp file is created with `suffix=cfg["ext"]`, i.e. it ends in the real output extension (e.g. `.json`), differing from the final file only by the leading-dot prefix and the random `mkstemp` infix (`.<stem>.<random>.json`). On a crash between `mkstemp` and `os.replace` this orphan persists with a real extension. Current `ingest.py` skip logic matches exact final filenames, so this is safe today, but a future glob-based scan such as `*.json` would match the orphan. Using a neutral temp suffix (e.g. `suffix=".tmp"`) would make orphaned temp files unambiguous while keeping `os.replace` atomicity (the suffix does not affect atomicity). Minor and defensive; flag only.
   - **RESOLVED (3.1):** Changed `tempfile.mkstemp` to use `suffix=".tmp"` instead of `suffix=cfg["ext"]` so a crash-orphan no longer carries a real output extension, and updated the adjacent comment to note the neutral suffix and that the suffix does not affect `os.replace` atomicity. The temp-file prefix (`.{out_file.stem}.`) is unchanged, so the existing prefix-based cleanup assertion in the tests (`glob(".doc.*")`) still holds; no test change was required.

## Skills with No Issues

1. Type Hints: One minor finding (1.1, `FORMAT_CONFIG` value type forces `str | None` at `str`-only call sites). All public/private signatures otherwise use modern syntax (`list[str]`, `str | Path`, `str | None`) and the lazy Docling document type is correctly supplied via a `TYPE_CHECKING` import (lines 20-23) so `_export_atomic` is precisely typed (`doc: "DoclingDocument"`) without importing Docling at module load.
2. Docstrings: No issues found - Google-style with Args/Returns/Raises/Note. The `Raises` sections match the actual raises: `ValueError` for empty/unsupported `output_formats` and unsupported `pdf_backend`, `RuntimeError` for accumulated failures; `_export_atomic` documents its `ValueError` (missing method) and pass-through `Exception`. One soft caveat is captured in finding 2.1 (per-file vs per-output atomicity wording).
3. Comments: No issues found - comments explain "why": lazy Docling import to keep module import cheap (lines 21-22, 42, 93-95), single-source-of-truth allow-list with drift assert (38-43, 93-95), same-filesystem temp file for atomic `os.replace` (225-226, 210-212), the empty-`output_formats` rejection rationale (118-119), and the pdf-only log-label rationale (152-153). No stale or "what" comments.
4. Logging: No issues found - uses `logconfig.get_logger`, f-strings throughout, appropriate levels (debug for setup/per-export detail, info for milestones, warning for skipped missing file, error for failures), no `print`, no "Entering/Exiting" noise. `_export_atomic` correctly stays silent so the caller is the single source of per-export log lines (no duplicate logging). The pdf-only settings line (154-159) correctly suppresses the misleading `do_ocr/pdf_backend` suffix for non-PDF inputs.
5. Exception Handling: No issues found - no bare `except`; messages carry context (file name, format, underlying error). The broad `except Exception` at lines 162 and 178 is the intended resilient-batch pattern (log, record failure, continue), and `_export_atomic`'s `except Exception: ... ; raise` (246-249) re-raises the original unchanged after cleanup, preserving type and traceback. The final aggregated `RuntimeError` (line 194) intentionally does not chain `from e` because it summarizes multiple independent failures rather than wrapping one. The drift `assert` (101-104) is a developer invariant on a module-internal mapping, not user-input validation, so using `assert` here is acceptable (the user-facing check at 105-108 raises `ValueError`).
6. Executable Scripts: N/A - library module, no `main()` / `--config` entry point (caller `ingest.py` owns that).
7. Data Validation: N/A - not a data-validation script.
8. Unit Tests: N/A for this file - tests live in `unit_tests/test_file_parser.py` and were out of the single-file review scope.
9. SQL Development: N/A - no SQL in this file.

## Status & Next Steps

**Current Status**: Complete - all findings implemented; suite green. The three findings (1.1 `FormatConfig` TypedDict, 2.1 per-output atomicity docstring note, 3.1 neutral `.tmp` temp suffix) are applied to `code/file_ingestion/file_parser.py`. No critical or major issues remained in this pass.
**Completed**:
1. Reviewed `parse_files_docling` and `_export_atomic` against type-hints, docstrings, comments, logging, exception-handling, executable-scripts, data-validation, unit-tests, and sql-development skills.
2. Validated the atomic-write contract (`mkstemp` in target dir, `os.close`, `os.replace`, cleanup-on-failure), the format/backend allow-list validation and assert-on-drift guard, the accumulate-then-raise failure handling, the `"text"` special-case, and the pdf-only log line against the source and against the `ingest.py` caller.
3. Confirmed findings against source line numbers.
4. Implemented 1.1 (`FormatConfig` TypedDict typing for `FORMAT_CONFIG` and `_export_atomic`'s `cfg` param), 2.1 (per-output-file atomicity docstring note), and 3.1 (neutral `.tmp` temp suffix with updated comment).
5. Ran `code/file_ingestion/unit_tests/test_file_parser.py` (18 passed) and the full `code/file_ingestion/unit_tests/` suite (102 passed) from the repo root - all green; no test changes were needed since the prefix-based cleanup assertion is unaffected by the suffix change.
**Next Steps**:
1. None - all findings implemented and verified.
**Blockers**:
1. None.
**Notes**:
1. All findings were minor/suggestion; all are now resolved. Tests must be run from the repo root because `unit_tests/conftest.py` inserts the relative path `code/file_ingestion` onto `sys.path`.
2. Strengths worth keeping: the lazy-import + `TYPE_CHECKING` pattern, the single-source-of-truth allow-list with an explicit drift assert, and the temp-then-`os.replace` write that cleans up on failure are all done correctly.
