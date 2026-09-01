---
name: excel-ingestion
goal: Build a generic Excel ingestion script that loads Excel sheets into PostgreSQL as raw tables with auto-generated DDL, with optional row-as-text column for embedding readiness.
status: active
created: 2026-03-18 12:00:00
updated: 2026-03-18 12:00:00
---

## Implementation Plan

1. [completed] Create Excel parser module - `code/excel_ingestion/excel_parser.py`
   - 1.1. Function `list_sheets(filepath: str | Path) -> list[str]` — return all sheet names from an Excel workbook
   - 1.2. Function `parse_sheet(filepath: str | Path, sheet_name: str, header_row: int, data_start_row: int) -> tuple[list[str], list[dict[str, str | None]]]` — read a sheet, use `header_row` for column names and `data_start_row` for the first data row; read until the first fully empty row; return (column_names, rows) as dicts keyed by header values; all values converted to str or None
   - 1.3. Function `excel_col_to_snake(col_name: str) -> str` — convert Excel header to snake_case SQL column name (replace non-alphanumeric with underscore, collapse runs, lowercase, strip)

2. [completed] Create and run unit tests for Excel parser - `code/excel_ingestion/unit_tests/test_excel_parser.py`
   - 2.1. Test `excel_col_to_snake` converts headers correctly (spaces, special chars, mixed case)
   - 2.2. Test `parse_sheet` reads headers and rows from a sample Excel file
   - 2.3. Test `list_sheets` returns all sheet names
   - 2.4. Run tests with pytest and verify all pass

3. [completed] Create ingestion script - `code/excel_ingestion/ingest_excel.py`
   - 3.1. CLI entry point using argparse with a single `--config` argument pointing to a TOML config file
   - 3.2. TOML config contains: `source_dir`, `db_name`, `db_schema`, `overwrite` flag, and a `[files]` dict keyed by filename where each value has a `sheets` array (each element with required `sheet`, required `table`, required `pk` list, required `header_row` integer, required `data_start_row` integer, and optional `row_text` boolean defaulting to `false`); fail with error if any required fields are missing
   - 3.3. Function `build_row_text(column_names: list[str], row: dict[str, str | None]) -> str` — serialize a row as "Col1: val1 | Col2: val2 | ..." skipping None/empty values
   - 3.4. Function `create_raw_table(engine: Engine, db_schema: str, table_name: str, column_names: list[str], pk_columns: list[str], include_row_text: bool) -> None` — generate and execute CREATE TABLE DDL with all columns as TEXT, plus `source_file TEXT NOT NULL` and `ingested_at TIMESTAMP NOT NULL DEFAULT NOW()`, with PRIMARY KEY on pk_columns; add `row_text TEXT` column only if `include_row_text` is True
   - 3.5. Function `insert_rows(engine: Engine, db_schema: str, table_name: str, source_file: str, column_names: list[str], rows: list[dict[str, str | None]], include_row_text: bool) -> int` — insert rows into raw table; populate row_text only if `include_row_text` is True
   - 3.6. `main()` orchestrates: read config, validate all sheet entries have required fields, connect to DB, ensure schema exists, iterate files and sheets, handle overwrite/create/insert logic, log summary
   - 3.7. Overwrite behavior: if `overwrite = true`, DROP raw table if it exists, recreate, and insert; if `overwrite = false` and table already exists, skip with log message
   - 3.8. Config validation: fail with error if any sheet entry is missing `sheet`, `table`, `pk`, `header_row`, or `data_start_row` fields
   - 3.9. Logging via project logconfig (`setup_logging`, `get_logger`)
   - 3.10. Uses `ensure_schema` and `validate_sql_identifier` from `code/file_ingestion/_utils.py`

4. [completed] Create and run unit tests for ingestion script - `code/excel_ingestion/unit_tests/test_ingest_excel.py`
   - 4.1. Test `build_row_text` serializes row with correct format, skips empty values
   - 4.2. Test `build_row_text` returns empty string for all-empty row
   - 4.3. Test `create_raw_table` generates correct DDL with pk_columns
   - 4.4. Test `create_raw_table` omits row_text column when `include_row_text` is False
   - 4.5. Test config validation fails when `sheet`, `table`, `pk`, `header_row`, or `data_start_row` is missing
   - 4.6. Test skip behavior when overwrite is false and table already exists
   - 4.7. Run tests with pytest and verify all pass

5. [completed] Create TOML config file - `code/excel_ingestion/config/briefs/ingest_data_tables.toml`
   - 5.1. `source_dir = "data/input/briefs/data/"`
   - 5.2. `db_name = "briefs_db"`
   - 5.3. `db_schema = "data"`
   - 5.4. `overwrite = true`
   - 5.5. Three `[files."..."]` entries for summary tables, workplan tables, and Q&A Excel files with sheet-to-table mappings and pk columns

6. [completed] Create and run input validation - `code/excel_ingestion/data_validation/data_val_excel_inputs.py`
   - 6.1. Parameters: `source_dir`, `files` list (from TOML config)
   - 6.2. Verify each Excel file exists in source_dir
   - 6.3. Verify each configured sheet name exists in the corresponding Excel file
   - 6.4. Verify header_row and data_start_row yield non-empty column names and at least one data row
   - 6.5. Run validation on briefs data files

7. [completed] Run ingestion for briefs - `code/excel_ingestion/ingest_excel.py`
   - 7.1. Run: `uv run code/excel_ingestion/ingest_excel.py --config code/excel_ingestion/config/briefs/ingest_data_tables.toml`
   - 7.2. Verify raw tables created in briefs_db.data schema

8. [completed] Create and run output validation - `code/excel_ingestion/data_validation/data_val_excel_ingestion.py`
   - 8.1. Parameters: `db_name`, `db_schema`, `tables` list
   - 8.2. Verify each raw table exists and has rows
   - 8.3. Verify row_text column is non-null for all rows in tables where row_text is enabled
   - 8.4. Verify source_file column is non-null for all rows in each raw table
   - 8.5. Log row counts per table
   - 8.6. Run validation on briefs_db.data schema

## Key Data Decisions and Considerations

1. **All columns as TEXT** — Avoids type-guessing errors; downstream consumers can cast as needed; consistent with the row-as-text use case where everything is text anyway
2. **Row-as-text is optional per sheet** — Controlled by `row_text` boolean in TOML (defaults to `false`); keeps embedding-ready text co-located with structured data when needed, avoids unnecessary overhead when not
3. **PK required in TOML** — Auto-detecting primary keys from Excel data is unreliable; requiring explicit user specification avoids silent duplicates and ensures ON CONFLICT DO NOTHING works correctly for append mode
4. **All sheet config fields required** — `sheet`, `table`, and `pk` must all be specified per sheet entry; script fails early with a clear error if any are missing, avoiding ambiguous defaults
5. **Separate from excel_ingestion_qpp_cm** — The QPP_CM ingestion has domain-specific logic (column overrides, measures table, sentinel values) that doesn't generalize; a clean generic script avoids polluting either module
6. **Reuses _utils.py from file_ingestion** — `ensure_schema` and `validate_sql_identifier` are general-purpose; no need to duplicate
7. **New excel_parser.py instead of reusing QPP_CM's** — The QPP_CM parser has QPP_CM-specific header detection, filter rows, and excluded sheets; a generic parser with simpler assumptions (first non-empty row = header) is more appropriate
8. **Packages** — `openpyxl` is already installed; no new dependencies needed

## Status & Next Steps

**Current Status**: All tasks completed
**Completed**:
1. Activity plan created
2. Excel parser module (`code/excel_ingestion/excel_parser.py`) with `list_sheets`, `parse_sheet`, `excel_col_to_snake`
3. Unit tests for Excel parser (22 tests, all passing)
4. Ingestion script (`code/excel_ingestion/ingest_excel.py`) with auto-generated DDL, row_text support, overwrite logic, config validation, and duplicate column name deduplication
5. Unit tests for ingestion script (21 tests, all passing)
6. TOML config for briefs data (`code/excel_ingestion/config/briefs/ingest_data_tables.toml`) with 3 files, 11 sheets
7. Input validation passed for all 3 Excel files and 11 sheets
8. Ingestion completed: 360 total rows across 11 tables in briefs_db.data
9. Output validation passed: all tables exist, have rows, source_file non-null, row_text non-null
**Next Steps**:
1. None -- all tasks complete
**Blockers**:
1. None
**Notes**:
1. Brief Excel files already created: summary tables (7 sheets), workplan tables (3 sheets), Q&A (1 sheet)
2. Target database is briefs_db with schema "data"
3. Each sheet gets its own raw table; row_text added only when enabled in TOML config
4. Q&A sheet has duplicate column names ("Question #" and "Question") which are deduplicated to "question" and "question_2"
5. Workload Estimates TOTAL row has null task_number; PK uses task_name + recurrent_standard_analyses + source_file instead
