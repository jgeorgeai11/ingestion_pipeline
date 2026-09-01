---
name: cr-excel_parser
goal: Address code quality issues identified in code/excel_ingestion/excel_parser.py to align with python-development skills and general correctness (explicit-row-bound parsing, workbook open error handling).
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Handle and document workbook-open failures in `parse_sheet` - `code/excel_ingestion/excel_parser.py`
   - 1.1. [major] Lines 121-134: `parse_sheet` opens the workbook inside a `try/finally` with NO `except`. A corrupt/unreadable workbook raises openpyxl errors (typically `OSError`, `zipfile.BadZipFile`, or `ValueError`/`InvalidFileException`) from `load_workbook`, which propagates raw and undocumented. The docstring's `Raises` only lists `FileNotFoundError` and `ValueError`. By contrast `list_sheets` (lines 49-53) deliberately catches `(OSError, ValueError)`, logs context, and re-raises. The orchestrator in `ingest_excel.py` catches only `(FileNotFoundError, ValueError)` around `parse_sheet`, so a workbook-open failure that surfaces as `BadZipFile` (a subclass of `Exception`, not `ValueError`) escapes the per-sheet handler and aborts the whole run instead of being recorded as a parse failure.
        - Current: `wb = load_workbook(filepath, read_only=True)` inside `try:` with only a `finally:` guard; no `except` for the open call.
        - Expected: Catch the open failure (e.g. `except (OSError, ValueError) as e:` mirroring `list_sheets`, optionally including `zipfile.BadZipFile`), log with context, and re-raise so it surfaces consistently; align the docstring `Raises` with the actual error surface (or wrap into `ValueError` so the caller's existing handler records it as a parse failure).
   - 1.2. [minor] Lines 124-130: The inner `try/except KeyError` for sheet lookup is correct, but the surrounding `try/finally` lacks the `else`-block logging the exception-handling skill recommends ("Log at every stage"). Minor: the function already INFO-logs the parse start (line 115) and the parsed result (line 163), so observability is adequate; consider a DEBUG log on successful workbook open for symmetry with `list_sheets`.
        - Current: outer `try: ... finally: wb.close()` with no `else` and no success log on open.
        - Expected: Optionally add a DEBUG log after `load_workbook` succeeds, consistent with `list_sheets`'s `else` logging.

2. [completed] Tighten `_cell_value` numeric/whitespace coercion note - `code/excel_ingestion/excel_parser.py`
   - 2.1. [suggestion] Lines 191-204: `_cell_value` coerces every non-None cell via `str(val).strip()`. For numeric/datetime/bool cells this stringifies the Python repr (e.g. a float `1.0`, a `datetime` ISO-ish repr, `True`). This is intentional for the all-text structured columns and the `row_text` embedding, but the behavior (numbers/dates become their `str()` form, not Excel's displayed format) is not documented. Consider a one-line docstring note so callers are not surprised that `1.0` rather than `1` (or a date's display string) lands in the column.
        - Current: Docstring says "stripped string value" without noting non-string cell coercion semantics.
        - Expected: Add a brief note that non-string cells are coerced via `str()` (so numbers/dates use their Python string form, not Excel's display format).

## Skills with No Issues

1. Type Hints: No issues found. All functions are fully annotated with modern syntax; `parse_sheet` returns the precise `tuple[list[str], list[dict[str, str | int | None]]]`, and the keyword-only row-bound params are typed `int`.
2. Docstrings: Mostly clean, Google-style with Args/Returns/Raises. The only gap is the under-documented `Raises` on `parse_sheet` (finding 1.1) and the coercion note (finding 2.1).
3. Comments: No issues found. Comments explain the "why" (the header-gap stop at lines 182-184 prevents silent column misalignment; the end-inclusive slice note at lines 152-153).
4. Logging: No issues found. Library-module pattern (`get_logger`, no `setup_logging`); INFO for milestones (sheets found, rows parsed), DEBUG for intermediate detail; f-strings with context throughout.
5. Exception Handling: One major finding (1.1 — missing `except` on the `parse_sheet` workbook open). Otherwise good: `list_sheets` uses the full `try/except/else/finally` with specific `(OSError, ValueError)`; the sheet-lookup `KeyError` is wrapped into a domain `ValueError` with `from e`; the `wb = None` + `if wb is not None` guard correctly prevents a secondary `NameError` in both functions' `finally`.
6. Executable Scripts: N/A - library module, not an entry point.
7. Data Validation: N/A - not a `data_val_*` script.
8. Unit Tests: N/A - reviewed separately.

### Verified correct (general correctness, no issue)

- Row-bound validation: `data_start_row <= header_row` and `data_end_row < data_start_row` are rejected with clear messages before any I/O; `header_idx >= len(all_rows)` is caught; zero headers at `header_row` raises. These mirror the duplicate checks in `validate_config`, giving defense in depth.
- Header extraction: `_extract_column_names` stops at the first empty cell AFTER at least one header (the prior review's gap-misalignment finding 2.1 is resolved in current code), preventing trailing-gap column misalignment.
- Data slice: `all_rows[data_start_row - 1 : data_end_row]` is correctly 0-based and end-inclusive; rows beyond the sheet extent are simply absent (no IndexError), and short rows are right-padded with `None` via the `i < len(row)` guard.
- `ROW_NUMBER_KEY` is kept distinct from any header so the synthetic ordinal never leaks into `row_text` or the `col_*` map (consistent with the orchestrator's `build_row_text`, which iterates only `column_names`).

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: parse_sheet + list_sheets now wrap open failures (incl. BadZipFile/InvalidFileException) into ValueError, with reconciled Raises docstrings. 1.2 skipped (low value). 2.1: _cell_value coercion documented. NOTE: list_sheets now raises ValueError on open failure, so data_val_excel_inputs.validate_sheets_exist must add ValueError to its catch (handled in that file's review).
**Completed**:
1. Reviewed `excel_parser.py` against all python-development core skills and against the orchestrator's exception handling in `ingest_excel.py`.
2. Confirmed the prior review's resolved findings (header-gap stop, `wb = None` guard) are intact in current code.
**Next Steps**:
1. Add an `except` for the `parse_sheet` workbook open mirroring `list_sheets`, and reconcile the `Raises` docstring (finding 1.1).
**Blockers**:
1. None
**Notes**:
1. `excel_parser.py` was UNCHANGED by the recent schema-alignment rework; it is in scope here for completeness. The asymmetry between `list_sheets` (catches open errors) and `parse_sheet` (does not) is the one substantive finding.
