---
name: cr-data_val_excel_inputs
goal: Address code quality issues identified in code/excel_ingestion/data_validation/data_val_excel_inputs.py to align with python-development and sql-development skills.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Config-shape access not guarded - `code/excel_ingestion/data_validation/data_val_excel_inputs.py`
   - 1.1. [minor] Line 205: `sheet_entries = file_entry["sheets"]` (and `entry["sheet"]`/`entry["header_row"]`/`entry["data_start_row"]`/`entry["data_end_row"]` in the per-sheet validators) are accessed directly. `main()` guards only the top-level `source_dir`/`files` via `except KeyError` (lines 195-200); a file entry missing `sheets`, or a sheet entry missing a row-bound field, raises an unguarded `KeyError`/`TypeError` and aborts with a traceback rather than a recorded validation FAIL. Because this is the pre-ingest gate, a malformed config is exactly what it should report cleanly.
        - Current: `sheet_entries = file_entry["sheets"]` with no guard
        - Expected: Either accumulate a FAIL message for a file entry missing `sheets` / a sheet missing required keys (consistent with the accumulate-and-exit-1 model), or wrap the per-file loop so a `KeyError` becomes a recorded failure instead of a crash

2. [completed] Type hints use bare `dict` for sheet entries - `code/excel_ingestion/data_validation/data_val_excel_inputs.py`
   - 2.1. [minor] Lines 50, 82, 131: `sheet_entries: list[dict]` uses a bare `dict` without type parameters. The type-hints skill calls for specific annotations. The element shape is a heterogeneous config record (`dict[str, object]` or a `TypedDict`), so at minimum parameterize it.
        - Current: `sheet_entries: list[dict]`
        - Expected: `sheet_entries: list[dict[str, object]]` (or a shared `SheetEntry` TypedDict reused across this module and `ingest_excel.py`)

## Skills with No Issues

1. Type Hints: No issues found beyond item 2 - all functions annotate params and returns (`-> list[str]`, `-> None`); only the `dict` element type is unparameterized.
2. Docstrings: No issues found - every function has Google-style Args/Returns; `validate_collection_paths` documents why derived paths are not checked here.
3. Comments: No issues found - the "no point parsing a missing file" short-circuit (line 210) and the derive-vs-author rationale are explained.
4. Logging: No issues found - `setup_logging` with the correct `logs/excel_ingestion/data_validation` dir and explicit `log_name`, deferred after argparse, f-strings, run separators, per-check PASS/FAIL lines; no `print`.
5. Exception Handling: No issues found - specific exception tuples on every boundary (`FileNotFoundError, InvalidFileException, OSError`; `tomllib.TOMLDecodeError, OSError`; `FileNotFoundError, ValueError, InvalidFileException`; `ValueError` for the path validator); parse/list errors become accumulated FAILs rather than crashes.
6. Executable Scripts: No issues found - single `--config` argparse arg, `main()` + `if __name__ == "__main__"`, config-existence check, deferred logging.
7. Data Validation: No issues found - `data_val_` prefix, lives under `data_validation/`, validates the input legs (file existence, sheet existence, parse-to >=1 column AND >=1 data row, authored-`collection_path` ltree); failures accumulate and the script exits 1 on any failure.
8. Unit Tests: N/A - tests reviewed separately.
9. SQL Best Practices: N/A - this validator touches no database (pre-ingest, file/parse only).

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: main() now reuses ingest_excel.validate_config as an up-front shape gate, so a malformed config is reported cleanly. 2.1: list[dict] -> list[dict[str, object]]. Also folded in the file-2 follow-on: validate_sheets_exist now catches ValueError (list_sheets wraps corrupt-workbook open failures into it). Input validator exits 0 on qpp_cm; suite green.
**Completed**:
1. Confirmed each input leg is validated: file existence, every configured sheet exists, parse yields >=1 column and >=1 data row, authored `collection_path` is a valid ltree.
2. Confirmed `validate_collection_path` is the same canonical validator the ingester uses (re-exported from `file_ingestion`), and that derived paths are correctly deferred to ingest time.
3. Confirmed the accumulate-failures + exit-1 model and the missing-file short-circuit.
**Next Steps**:
1. (Minor) Guard `file_entry["sheets"]` / per-sheet required keys so a malformed config produces a recorded FAIL rather than a traceback.
2. (Minor) Parameterize the `list[dict]` annotations.
**Blockers**:
1. None
**Notes**:
1. The validator deliberately does not re-check the `data_start_row > header_row` bound (that is `ingest_excel.validate_config`'s job and `parse_sheet` also raises on it); the parse attempt here surfaces a bad bound as a FAIL, so coverage is intact.
2. Most important finding: item 1.1 (minor) - a config missing `sheets`/row-bound keys crashes the pre-ingest gate instead of reporting it.
