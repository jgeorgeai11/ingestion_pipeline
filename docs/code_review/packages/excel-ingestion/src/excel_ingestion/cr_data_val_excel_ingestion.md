---
name: cr-data_val_excel_ingestion
goal: Address code quality issues identified in code/excel_ingestion/data_validation/data_val_excel_ingestion.py to align with python-development skills.
status: completed
created: 2026-04-13 10:00:00
updated: 2026-04-13 12:00:00
---

## Implementation Plan

1. [done] Fix critical bug in config iteration - `code/excel_ingestion/data_validation/data_val_excel_ingestion.py`
   - 1.1. [done] Line 203: `for file_entry in files:` iterates over dict keys (strings), not values -- the TOML config defines `files` as a table (dict), so iterating directly yields filename strings, not file entry dicts. This causes `file_entry["sheets"]` on line 204 to fail with a `TypeError` at runtime.
        - Current: `for file_entry in files:`
        - Expected: `for filename, file_entry in files.items():`

2. [done] Fix SQL best-practices issues - `code/excel_ingestion/data_validation/data_val_excel_ingestion.py`
   - 2.1. [done] Line 90: `select count(*)` query uses f-string interpolation for schema/table names -- while `validate_sql_identifier` is called upstream, the function itself does not call it before constructing the query
        - Current: `text(f"select count(*) from {db_schema}.{table_name}")`
        - Expected: Add `validate_sql_identifier` calls at the top of `check_table_exists_and_has_rows` and `check_source_file_not_null`, or document that callers are responsible for validation
   - 2.2. [done] Line 129: Same f-string interpolation pattern in `check_source_file_not_null` without local validation
        - Current: `text(f"select count(*) from {db_schema}.{table_name} where source_file is null")`
        - Expected: Same fix as 2.1

3. [done] Fix exception handling issues - `code/excel_ingestion/data_validation/data_val_excel_ingestion.py`
   - 3.1. [done] Line 239: Catches bare `Exception` for the entire validation block -- consider catching more specific exceptions or at minimum logging the traceback
        - Current: `except Exception as e:`
        - Expected: `except (SQLAlchemyError, ValueError) as e:` to be more specific about expected failure modes

4. [done] Fix duplicate code - `code/excel_ingestion/data_validation/data_val_excel_ingestion.py`
   - 4.1. [deferred] Lines 33-56: `get_engine` is duplicated verbatim from `ingest_excel.py` -- should be extracted to a shared utility module
        - Current: Identical `get_engine` function in both files
        - Expected: Move to a shared module (e.g., `code/excel_ingestion/_db_utils.py`) and import from both scripts

5. [done] Fix blank line style issue - `code/excel_ingestion/data_validation/data_val_excel_ingestion.py`
   - 5.1. [done] Line 150-151: Extra blank line before `def main():`
        - Current: Two blank lines between end of function and next function (3 total with the blank line 150)
        - Expected: Exactly two blank lines between top-level definitions per PEP 8

## Skills with No Issues

1. Type Hints: No issues found
2. Docstrings: No issues found
3. Comments: No issues found
4. Logging: No issues found
5. Executable Scripts: No issues found
6. Unit Tests: N/A - no unit tests exist for this validation script (could be a gap to address)

## Status & Next Steps

**Current Status**: All findings implemented
**Completed**:
1. Code review analysis against all python-development and sql-development core skills
2. Fixed critical `files` iteration bug (item 1.1)
3. Added `validate_sql_identifier` calls to check functions (items 2.1, 2.2)
4. Narrowed exception catch to `(SQLAlchemyError, ValueError)` (item 3.1)
5. Fixed extra blank line (item 5.1)
**Deferred**:
1. Extract shared `get_engine` to a common utility module (item 4.1) -- requires coordinated refactor across multiple files
**Next Steps**:
1. None
**Blockers**:
1. None
**Notes**:
1. The critical bug in item 1.1 means this validation script has never been successfully run against the current TOML config format, or the config format changed after the script was written
2. Compare with `ingest_excel.py` line 373 which correctly uses `for filename, file_entry in files.items():`
