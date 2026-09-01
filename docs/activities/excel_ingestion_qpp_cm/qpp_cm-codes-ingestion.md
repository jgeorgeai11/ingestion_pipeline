---
name: qpp_cm-codes-ingestion
goal: Build an Excel ingestion pipeline to load QPP_CM cost measure codes lists (XLSX) into PostgreSQL. Each Excel sheet maps to its own database table in the qpp_cm schema, preserving raw column structure without normalization.
status: completed
created: 2026-03-11 15:45:00
updated: 2026-03-11 15:45:00
---

## Implementation Plan

1. [completed] Create Excel parser module - `code/excel_ingestion_qpp_cm/excel_parser.py`
   - 1.1. Function `parse_excel_sheet(filepath, sheet_name) -> tuple[list[str], list[dict]]` returns (column_names, rows)
   - 1.2. Locate header row by finding the row where first cell is "Initial Sort Order"
   - 1.3. Skip the filter row ("This is a filter cell") immediately after the header
   - 1.4. Extract all data rows after the filter row, keyed by header column names
   - 1.5. Strip whitespace from all string values
   - 1.6. Skip empty rows
   - 1.7. Function `list_sheets(filepath) -> list[str]` returns sheet names excluding "Overview"

2. [completed] Create and run unit tests for Excel parser - `code/excel_ingestion_qpp_cm/unit_tests/test_excel_parser.py`
   - 2.1. Test header row detection with "Initial Sort Order"
   - 2.2. Test filter row is skipped
   - 2.3. Test whitespace stripping
   - 2.4. Test empty row handling
   - 2.5. Test list_sheets excludes Overview
   - 2.6. Run tests with pytest

3. [completed] Create DDL for codes tables - `code/excel_ingestion_qpp_cm/sql/qpp_cm_codes.sql`
   - 3.1. One CREATE TABLE per unique sheet name across all 29 files
   - 3.2. Every table includes `source_file text not null` as first column to identify the originating measure
   - 3.3. All data columns are `text` type (no type coercion — store raw values)
   - 3.4. Primary key: (source_file, initial_sort_order) where applicable
   - 3.5. Use `{schema_name}` placeholder for schema, same pattern as file_ingestion/sql/schema.sql
   - 3.6. Unique sheet names to create tables for:
     - Shared across most EBCM measures: triggers_hcpcs, triggers_dgn, triggers, triggers_details, sub_groups, sub_groups_details, attribution, service_assignment_ab, service_assignment_d, service_assignment, exclusions, exclusions_details, ra, ra_details
     - Emergency-specific: ed_visit_types, ed_visit_types_details, services
     - TPCC-specific: e_m_prim_care, prim_care_services, hcpcs_surgery, hcpcs_anesthesia, hcpcs_ther_rad, hcpcs_chemo, eligible_clinicians, hcc_risk_adjust
     - Other: el_ha and op_pci specific sheets (if any beyond common ones)

4. [completed] Create ingestion script - `code/excel_ingestion_qpp_cm/ingest_excel.py`
   - 4.1. TOML config driven (--config argument), same pattern as file_ingestion/ingest.py
   - 4.2. Config specifies: source_dir, file list, db_name, db_schema, overwrite flag
   - 4.3. For each file, iterate all sheets (excluding Overview)
   - 4.4. Parse each sheet using excel_parser.parse_excel_sheet
   - 4.5. Map sheet name to lowercase table name (e.g., "Triggers_HCPCS" -> "triggers_hcpcs")
   - 4.6. Insert rows into the corresponding table with source_file column set to the Excel filename
   - 4.7. If overwrite=true, delete existing rows for that source_file before inserting
   - 4.8. Use SQLAlchemy for DB access, dotenv for env vars, structured logging (logconfig)
   - 4.9. Ensure schema and tables exist via DDL before loading

5. [completed] Create and run unit tests for ingestion script - `code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py`
   - 5.1. Test TOML config parsing
   - 5.2. Test sheet-to-table name mapping
   - 5.3. Test overwrite deletes existing rows for source_file
   - 5.4. Test skips Overview sheet
   - 5.5. Run tests with pytest

6. [completed] Create TOML config for 2024 QPP_CM codes lists - `code/excel_ingestion_qpp_cm/config/qpp_cm/ingest_qpp_cm_2024_codes_lists.toml`
   - 6.1. List all 29 XLSX files (exclude temp ~$ files)
   - 6.2. Set source_dir, db_name=policy_db, db_schema=qpp_cm, overwrite=true

7. [completed] Run ingestion - `code/excel_ingestion_qpp_cm/ingest_excel.py`
   - 7.1. Run with the 2024 QPP_CM codes lists TOML config
   - 7.2. Verify row counts in each table match expected sheet row counts

8. [completed] Create and run data validation - `code/excel_ingestion_qpp_cm/data_validation/data_val_qpp_cm_codes.py`
   - 8.1. Verify all 29 source files are represented in each expected table
   - 8.2. Verify no empty initial_sort_order values
   - 8.3. Verify row counts per source_file are reasonable (> 0)
   - 8.4. Run validation against policy_db.qpp_cm

## Key Data Decisions and Considerations

1. All columns stored as text — avoids type coercion issues with mixed data (codes that look numeric but have leading zeros, descriptions with special characters). Downstream queries can cast as needed.
2. Sheet name = table name — direct 1:1 mapping, lowercased. No merging of similar sheets (e.g., triggers_hcpcs and triggers remain separate tables).
3. source_file column on every table — since multiple measures share the same sheet structure, this identifies which measure each row belongs to.
4. Overview sheet excluded — contains only navigation links and descriptive text, not tabular data suitable for database storage.
5. TPCC tables are naturally separate — TPCC sheet names (e_m_prim_care, hcpcs_surgery, etc.) don't overlap with EBCM sheet names, so no special handling needed.
6. Column count varies within same sheet type — e.g., Exclusions_Details has 9 columns in some measures, 15 in others. The DDL must use the superset of columns; missing columns will be null for measures that don't have them.
7. Filter row ("This is a filter cell") — always appears immediately after the header row and must be skipped.

## Status & Next Steps

**Current Status**: Complete
**Completed**:
1. Activity plan created
2. Analyzed Excel file structure across all 29 files (8-12 sheets each, varying columns)
3. Excel parser module (`excel_parser.py`) with header detection, filter row skipping, whitespace stripping
4. Unit tests for Excel parser (12 tests passing)
5. DDL for 34 tables across all unique sheet types (`qpp_cm_codes.sql`)
6. Ingestion script (`ingest_excel.py`) with TOML config, column name mapping, overwrite support
7. Unit tests for ingestion script (28 tests passing)
8. TOML config listing all 29 files
9. Ingestion run: 310,017 rows loaded across 34 tables in qpp_cm schema
10. Data validation: all checks passed (source file coverage, no empty sort orders, positive row counts)
**Next Steps**:
1. None - all tasks complete
**Blockers**:
1. None
**Notes**:
1. 29 Excel files in data/input/qpp_cm/2024/2024-cost-measure-codes-lists/
2. Sheet counts vary: 7-11 sheets per file; 34 unique sheet types total
3. MSPB Clinician has 9 unique tables (attribution_rule, se_* tables, etc.)
4. TPCC has 8 unique tables (e_m_prim_care, hcpcs_*, eligible_clinicians, hcc_risk_adjust)
5. "window" renamed to "window_period" in DDL to avoid PostgreSQL reserved word conflict
6. "Window" Excel column mapped to "window_period" via override in column name mapping
