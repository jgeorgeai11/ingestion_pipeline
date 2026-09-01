---
name: cr-ingest-excel
goal: Address code quality issues identified in code/excel_ingestion_qpp_cm/ingest_excel.py to align with python-development and sql-development skills.
status: completed
created: 2026-03-11 00:00:00
updated: 2026-03-11 00:00:00
---

## Implementation Plan

1. [completed] Fix docstring issues - `code/excel_ingestion_qpp_cm/ingest_excel.py`
   - 1.1. [major] Line 195: `insert_rows()` docstring is missing a Raises section despite `validate_sql_identifier()` raising `ValueError` on lines 221-222
        - Current: No Raises section in docstring
        - Expected: Add `Raises:` section:
          ```
          Raises:
              ValueError: If db_schema or table_name fails SQL identifier validation.
          ```
   - 1.2. [major] Line 269: `_populate_measures()` docstring is missing a Raises section despite `validate_sql_identifier()` raising `ValueError` on line 283
        - Current: No Raises section in docstring
        - Expected: Add `Raises:` section:
          ```
          Raises:
              ValueError: If db_schema fails SQL identifier validation.
          ```
   - 1.3. [minor] Line 308: `ingest_file()` docstring Raises section only documents `FileNotFoundError` but exceptions from `insert_rows()` (ValueError) and database errors from `_table_exists()` can also propagate
        - Current: `Raises: FileNotFoundError: If the Excel file is not found.`
        - Expected: Add `ValueError` to the Raises section:
          ```
          Raises:
              FileNotFoundError: If the Excel file is not found.
              ValueError: If db_schema or table_name fails SQL identifier validation.
          ```

2. [completed] Add security comments for SQL identifier validation - `code/excel_ingestion_qpp_cm/ingest_excel.py`
   - 2.1. [minor] Line 221: The `validate_sql_identifier()` calls guard the f-string SQL interpolation on lines 247, 253-255. Adding a brief comment would make the security intent explicit for future readers.
        - Current:
          ```python
          validate_sql_identifier(db_schema, "db_schema")
          validate_sql_identifier(table_name, "table_name")
          ```
        - Expected:
          ```python
          # Guard against SQL injection -- only allows [a-z0-9_] identifiers
          validate_sql_identifier(db_schema, "db_schema")
          validate_sql_identifier(table_name, "table_name")
          ```
   - 2.2. [minor] Line 283: Same issue in `_populate_measures()` -- the `validate_sql_identifier()` call guards the f-string SQL on lines 286-289
        - Current: `validate_sql_identifier(db_schema, "db_schema")`
        - Expected:
          ```python
          # Guard against SQL injection -- only allows [a-z0-9_] identifiers
          validate_sql_identifier(db_schema, "db_schema")
          ```

3. [completed] Fix logging level issue - `code/excel_ingestion_qpp_cm/ingest_excel.py`
   - 3.1. [minor] Line 343: `logger.error()` is used when a table does not exist and the sheet is skipped. Per logging guidelines, ERROR is for "failures that don't stop execution." While this does result in data loss for one sheet, the message includes actionable guidance ("Add a CREATE TABLE..."). The message reads as an instruction rather than an error report. Consider whether WARNING is more appropriate since the pipeline continues successfully and the operator can act on the guidance.
        - Current: `logger.error(f"Table {db_schema}.{table_name} does not exist -- ...")`
        - Expected: No change required -- ERROR is defensible here since data is lost. Flagging for team discussion only.

## Skills with No Issues

1. Type Hints: No issues found -- all functions have parameter and return type hints using modern syntax (e.g., `list[str]`, `str | None`, `dict[str, int]`)
2. Comments: No issues found beyond the security comment items above -- section separators, inline comments on override mappings (unicode, typos, reserved words), and "why" comments are all appropriate
3. Logging: No issues found beyond the minor level item above -- uses logconfig correctly with `setup_logging` and `get_logger`, separators at run boundaries, f-strings for interpolation, deferred setup after argparse, appropriate levels throughout
4. Exception Handling: No issues found -- catches specific exceptions with context, uses `raise ... from e` for chaining, `main()` has both specific and fallback `except Exception` blocks, logs at each stage
5. Executable Scripts: No issues found -- follows the pattern with `main()`, `if __name__ == "__main__"`, single `--config` argument, deferred logging setup, TOML config
6. Data Validation: N/A -- not a data validation script
7. Unit Tests: N/A -- this is the source file, not a test file
8. SQL Development: No issues found -- embedded SQL uses lowercase keywords, parameterized values for user data, and identifier validation for schema/table interpolation

## Status & Next Steps

**Current Status**: All findings implemented
**Completed**:
1. Full code review against all python-development and sql-development skills
2. Added Raises sections to `insert_rows()` and `_populate_measures()` docstrings
3. Extended Raises section in `ingest_file()` docstring with `ValueError`
4. Added security intent comments above `validate_sql_identifier()` calls
5. Step 3 (logging level) kept as-is per review note -- ERROR is defensible since data is lost
**Next Steps**:
1. None -- all actionable findings addressed
**Blockers**:
1. None
**Notes**:
1. The code is well-structured overall, following the executable-scripts pattern cleanly with clear separation between column mapping, database operations, and the main pipeline
2. The `_COLUMN_NAME_OVERRIDES` dict is well-documented with inline comments explaining why each override exists (unicode, typos, reserved words, abbreviations)
3. The superset schema pattern (lines 228-242) is a good design choice that allows different Excel files with different column sets to coexist in the same table
4. The `validate_sql_identifier()` function imported from `_utils.py` uses a strict regex allowlist (`^[a-z_][a-z0-9_]*$`), which is adequate SQL injection protection for the schema/table name interpolation
