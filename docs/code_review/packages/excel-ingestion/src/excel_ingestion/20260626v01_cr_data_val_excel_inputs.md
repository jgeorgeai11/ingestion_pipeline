---
name: cr-data_val_excel_inputs
goal: Re-review code/excel_ingestion/data_validation/data_val_excel_inputs.py (current state) against python-development and sql-development skills and for correctness (file existence, sheet/bounds sanity, authored collection_path, accumulate-and-exit-1).
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

_No findings. The file is clean against all loaded skills and the input-leg correctness checks._

The single prior [minor] (v02 item 1.1 — module docstring omitted `start_col`/`end_col`) is RESOLVED: lines 5-7 now read "configured (or defaulted) row/column bounds (`header_row` / `data_start_row` / `data_end_row` / `start_col` / `end_col`)" and note `sheet` is the only required per-sheet field, matching the `parse_sheet` call at lines 104-111. The prior v01 [minor] findings (unguarded config-shape access; bare `list[dict]`) are also resolved — `main()` runs `validate_config` as an up-front shape gate (lines 197-202) and the annotations are `list[dict[str, object]]`.

## Skills with No Issues

1. Type Hints: No issues found. All functions annotate params/returns (`-> list[str]`, `-> None`); `sheet_entries: list[dict[str, object]]` is parameterized; the `parse_sheet` call threads all five bounds via `.get()` (lines 107-111) matching the parser's `int | None` / `str | None` defaults.
2. Docstrings: No issues found. The module docstring now lists all five bounds and the optional-vs-required contract; every function retains accurate Google-style Args/Returns. `validate_sheet_data` correctly documents ">=1 column and >=1 data row".
3. Comments: No issues found. The `list_sheets`-wraps-corrupt-workbook note (lines 68-69), the unreachable-`if not columns` rationale (lines 117-118), the missing-file short-circuit (line 215), and the `validate_config` shape-gate rationale (lines 194-196) all explain "why".
4. Logging: No issues found. `setup_logging` with `logs/excel_ingestion/data_validation` and explicit `log_name`, deferred after argparse; f-strings; run separators; per-check PASS/FAIL lines; no `print`.
5. Exception Handling: No issues found. Specific exception tuples on every boundary — `(FileNotFoundError, ValueError, InvalidFileException, OSError)` for `list_sheets`, `(FileNotFoundError, ValueError, InvalidFileException)` for `parse_sheet`, `(tomllib.TOMLDecodeError, OSError)` for the config read, `ValueError` for `validate_config` and the path validator. Parse/list errors become accumulated FAILs rather than crashes; no bare except, no generic wrap.
6. Executable Scripts: No issues found. Single `--config` argparse arg, `main()` + `if __name__ == "__main__"`, config-existence check, deferred logging.
7. Data Validation: No issues found. `data_val_` prefix under `data_validation/`; validates each input leg (file existence, sheet existence, parse to >=1 column AND >=1 data row, authored-`collection_path` ltree); failures accumulate and the script exits 1 on any failure. The `validate_config` reuse runs before the per-file loops so a malformed config is reported cleanly here rather than crashing a per-field access.
8. Unit Tests: N/A — tests reviewed separately.
9. SQL Best Practices: N/A — this validator touches no database (pre-ingest, file/parse only).

### Verified correct (general correctness, no issue)

- Bounds threading: `validate_sheet_data` passes `header_row`/`data_start_row`/`data_end_row`/`start_col`/`end_col` via `.get()` (None defaults to the parser), so omitted fields default rather than `KeyError`.
- Removed `if not columns` branch is sound: `parse_sheet` raises `ValueError` on missing/blank/duplicate headers, so a successful return always has a non-empty `columns` list; the `>=1 data row` check (lines 121-124) is the remaining FAIL on an empty data range.
- The validator does not re-check `data_start_row > header_row` itself; `validate_config` checks explicit pairs and `parse_sheet` re-validates resolved bounds, so a bad bound surfaces as a parse FAIL here. Coverage intact.
- `validate_collection_paths` validates only authored paths and defers derived paths to ingest time, using the same canonical `validate_collection_path` re-exported from `_utils`/file_ingestion.

## Status & Next Steps

**Current Status**: REVIEWED (v01, 2026-06-26) — CLEAN. All prior findings (v02 docstring currency; v01 config-shape guard and bare `list[dict]`) are resolved on disk. No new findings.
**Completed**:
1. Re-reviewed against all python-development core skills and sql-development best-practices.
2. Verified the v02 module-docstring fix (lines 5-7 list all five bounds and the optional contract).
3. Verified `validate_config` runs as an up-front shape gate (lines 197-202) and the per-leg checks accumulate failures with exit 1.
**Next Steps**:
1. None.
**Blockers**:
1. None
**Notes**:
1. The validator deliberately delegates bound-relationship checks to `validate_config` + `parse_sheet`; a bad bound is surfaced as a FAIL here.
2. Most important finding: none — the file is clean.
