---
name: cr-ingest_excel
goal: Address code quality issues identified in code/excel_ingestion/ingest_excel.py to align with python-development and sql-development skills.
status: completed
created: 2026-04-13 10:00:00
updated: 2026-04-13 14:00:00
---

## Implementation Plan

1. [done] Add SQL identifier validation to `insert_consolidated_rows` - `code/excel_ingestion/ingest_excel.py`
   - 1.1. [done] Lines 427-471: `insert_consolidated_rows` builds SQL via f-strings using `db_schema`, `excel_table`, and `content_table` but never calls `validate_sql_identifier` on any of them. Every other function that interpolates identifiers into SQL (`create_raw_table`, `insert_rows`, `_ensure_schema`, `_ensure_consolidated_tables`) validates its inputs. This function is the exception and could allow SQL injection if called outside `main()`.
        - Current: No validation calls at the start of `insert_consolidated_rows`
        - Expected: Add `validate_sql_identifier(db_schema, "db_schema")`, `validate_sql_identifier(excel_table, "excel_table")`, `validate_sql_identifier(content_table, "content_table")` at the top of the function body, matching the pattern used in `insert_rows` (lines 235-236) and `create_raw_table` (lines 162-163)

2. [done] Add SQL identifier validation for `db_schema` in `_ensure_consolidated_tables` - `code/excel_ingestion/ingest_excel.py`
   - 2.1. [done] Lines 357-358: `_ensure_consolidated_tables` validates `excel_table` and `content_table` but does not validate `db_schema`, which is substituted into the DDL template via `.replace("{schema_name}", db_schema)` on line 367. While `_ensure_schema` validates `db_schema` when called from `main()`, this function should be self-contained.
        - Current: Only `excel_table` and `content_table` are validated
        - Expected: Add `validate_sql_identifier(db_schema, "db_schema")` alongside the existing validation calls

3. [done] Guard `insert_rows` against empty row list - `code/excel_ingestion/ingest_excel.py`
   - 3.1. [done] Lines 255-271: When `rows` is empty, `all_params` will be an empty list, and `conn.execute(text(insert_sql), [])` is called. SQLAlchemy behavior with an empty parameter list in executemany is undefined and dialect-dependent. The caller in `main()` skips empty sheets (line 574-578), but `insert_rows` is a public function and should handle this defensively.
        - Current: No early return for empty rows
        - Expected: Add an early return at the top of the function: `if not rows: logger.debug("No rows to insert, skipping"); return 0`

4. [done] Add type annotation to `all_params` in `insert_consolidated_rows` - `code/excel_ingestion/ingest_excel.py`
   - 4.1. [done] Line 460: `all_params: list[dict]` uses a bare `dict` without type parameters, inconsistent with the fully-typed `all_params: list[dict[str, str | None]]` in `insert_rows` (line 255)
        - Current: `all_params: list[dict] = []`
        - Expected: `all_params: list[dict[str, str | int]] = []` (values include strings and integers for `sort_order` and `word_count`)

5. [done] Add exception handling to `insert_consolidated_rows` - `code/excel_ingestion/ingest_excel.py`
   - 5.1. [done] Lines 427-475: `insert_consolidated_rows` has no try/except block around its database operations, unlike `create_raw_table` (lines 199-208) which logs errors on failure. A failed insert here would propagate an unlogged SQLAlchemy exception.
        - Current: No error handling; raw SQLAlchemy exceptions propagate
        - Expected: Wrap the `with engine.begin()` block in try/except with `logger.error(f"Failed to insert consolidated rows for {sheet_name}: {e}")` and re-raise, matching the pattern in `create_raw_table`

6. [done] Previous review items (retained for history)
   - 6.1. [done] Converted `insert_rows` to batch execution
   - 6.2. [done] Added info/debug logging to `insert_rows`
   - 6.3. [done] Added `finally` clause to `create_raw_table`
   - 6.4. [done] Split INSERT SQL string across lines for readability
   - 6.5. [done] Added CASCADE comment on DROP TABLE

## Skills with No Issues

1. Docstrings: No issues found - all public functions have complete Google-style docstrings with Args, Returns, and Raises sections
2. Comments: No issues found - comments explain "why" not "what"; DataFrame chain commenting N/A
3. Logging: No issues found - uses logconfig correctly; f-strings in log calls; separator lines in main()
4. Executable Scripts: No issues found - uses argparse with --config, TOML config, deferred logging setup
5. Data Validation: N/A - this is the main script, not a data validation script
6. Unit Tests: N/A - reviewed separately in cr_test_ingest_excel.md
7. SQL Best Practices: No issues found - lowercase SQL, parameterized values, identifier validation (with exceptions noted above)

## Status & Next Steps

**Current Status**: All findings resolved
**Completed**:
1. All items from previous review pass (batch insert, logging, finally clause, formatting)
2. SQL identifier validation in `insert_consolidated_rows` (item 1)
3. SQL identifier validation for `db_schema` in `_ensure_consolidated_tables` (item 2)
4. Guard against empty rows in `insert_rows` (item 3)
5. Type annotation fix in `insert_consolidated_rows` (item 4)
6. Exception handling in `insert_consolidated_rows` (item 5)
**Next Steps**:
1. None
**Blockers**:
1. None
**Notes**:
1. Items 1 and 2 are the most important findings -- they represent a consistency gap in SQL injection protection. All other functions that build SQL from identifiers validate their inputs, but these two functions do not.
2. The `load_dotenv()` call at module level (line 39) runs at import time, which is fine for a script but could cause issues if the module is imported in tests without a .env file present.
3. Overall code quality is high -- good docstrings, type hints, proper argparse/TOML pattern, and SQL injection protection in most functions.
