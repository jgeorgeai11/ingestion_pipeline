---
name: cr-structured_table
goal: v01 (2026-06-26) review of code/excel_ingestion/structured_table.py current state, after the v01 findings (raw-headers-distinct invariant doc, ltree-SAWarning suppression) were resolved. Whole-file pass against python-development + sql-development skills and correctness, focused on SQL-injection safety, additive schema evolution, the min_column_overlap guard, the FK-to-sheet-table-with-cascade (including a custom sheet_table name), the all-text design, and transaction boundaries.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

No [critical], [major], [minor], or [suggestion] findings against the current
state. The file is clean against the python-development core skills, the
sql-development best-practices, and for correctness. The most load-bearing
verification — that a custom `sheet_table` name still produces a creatable FK — is
recorded under "Verified correct" below; no change is required.

(no pending tasks)

## Skills with No Issues

1. **Type Hints**: No issues found. Every function (`_quote`,
   `build_column_mapping`, `ensure_table`, `reconcile_columns`, `_source_present`,
   `write_rows`) carries full modern-syntax annotations (`list[str]`,
   `dict[str, str]`, `str | int | None`, `Connection`/`Engine`, keyword-only `*`
   params); return types present including `-> None`, `-> bool`, `-> int`.

2. **Docstrings**: No issues found. Google-style Args/Returns/Raises throughout.
   `build_column_mapping` (lines 60-77) documents the "raw headers assumed
   distinct" invariant added in the v01 resolution. The `-1` skipped marker, the
   FK/cascade lifecycle, and the overlap-guard semantics are documented and match
   the code.

3. **Comments**: No issues found. Comments explain "why" (the empty-denominator
   overlap guard at lines 184-186, the defensive re-delete under cascade at lines
   302-304, the reconcile-after-ensure ordering at lines 319-320, the
   names-only/ltree-NullType reflection at lines 170-173), not "what".

4. **Logging**: No issues found. `logconfig.get_logger`; f-strings throughout; no
   `print`; DEBUG for DDL (lines 126, 128), INFO for row counts (lines 313, 351),
   WARNING for schema drift (lines 209, 213). No redundant entry/exit messages.

5. **Exception Handling**: No issues found. Raises specific `ValueError` for the
   compatibility guard (lines 190-197); lets `SQLAlchemyError` propagate to the
   orchestrator, which catches `(ValueError, SQLAlchemyError)` per-sheet
   (ingest_excel.py line 607). No bare excepts, no generic-`Exception` wrapping.

6. **SQL best-practices (sql-development)**: No issues found. Generated DDL/DML is
   lowercase; the insert lists explicit columns (no `SELECT *`); identifiers are
   quoted. The generated SQL is short, parameterized statements rather than CTEs,
   so the CTE conventions do not apply.

7. **Executable Scripts / Data Validation / Unit Tests**: N/A — library module, not
   an entry point, validator, or test file.

### Verified correct (scrutinized — no issue)

- **SQL-injection safety**: `db_schema`, `table_name`, `sheet_table`, and every
  generated `col_*` data column pass `validate_sql_identifier` (re-exported from
  file_ingestion's canonical `fullmatch` validator) and are then double-quoted via
  `_quote` (lines 106-110, 165-168, 291-293). All data values
  (`collection_path`, `sort_order`, every `col_*`) are bound as named parameters
  (lines 242, 310, 339-347), never interpolated. The identity-column literals
  embedded in SQL (`collection_path`, `sort_order`) are fixed constants. A `col_*`
  param key can never clobber an identity key because `normalize_column_name`
  always prefixes `col_`, so no generated data column can be named
  `collection_path` or `sort_order`.

- **FK to the sheet table with cascade — custom `sheet_table` name (the
  task-flagged item)**: `ensure_table` (lines 112-115) emits
  `collection_path ltree not null references {schema}.{sheet_table}
  (collection_path) on delete cascade`, with `sheet_table` validated and quoted.
  `write_rows` is parameterized on `sheet_table` (line 251) and the orchestrator
  threads the SAME validated name into both `ensure_consolidated_tables` (which
  CREATES the sheet table) and `write_rows` (ingest_excel.py lines 493 and 600).
  The sheet-table DDL declares `collection_path ltree primary key`
  (sql/excel_schema.sql line 26), so the referenced column carries a PRIMARY KEY
  — the FK is creatable and valid for ANY custom `sheet_table` name, not only the
  default "sheet". Verified end-to-end (DDL template + caller + structured DDL).

- **Additive schema evolution** (lines 199-217): `to_add = [c for c in col_names
  if c not in existing]` adds only incoming columns absent from the reflected set,
  one `alter table ... add column "col" text` each; existing columns are never
  dropped. Added columns and the columns the sheet is missing are WARN-logged so
  drift is visible. On a freshly created table all incoming columns match, so
  nothing is added (the reconcile-after-ensure ordering, line 321).

- **`min_column_overlap` guard** (lines 180-197): `r_in = |intersection| /
  |incoming|`, `r_ex = |intersection| / |existing|`, each guarded to `0.0` on an
  empty denominator. The `ValueError` fires only when BOTH ratios are below the
  threshold, so a subset relationship in either direction passes. The threshold is
  validated in `validate_config` (ingest_excel.py) before reaching here.

- **All-text-columns design** (lines 116-118): identity columns are
  `collection_path ltree` and `sort_order integer not null check (sort_order >=
  1)`; every data column is `text` (nullable), so a row missing a column from
  another sheet inserts as NULL. The all-text choice matches the text-to-SQL leg
  and the parser's `str()` coercion.

- **Transaction boundaries** (lines 298-349): ensure / delete-or-skip / reconcile
  (ALTER) / insert all run inside one `engine.begin()` block. Postgres
  transactional DDL makes the ALTER + INSERT atomic, so a failure rolls the whole
  sheet back. The early `return -1` (skip, line 317) and `return 0` (no rows, line
  327) both exit cleanly inside the context manager.

- **Overwrite / skip semantics** (lines 301-317): overwrite deletes this
  collection_path's rows first (a harmless no-op when the FK cascade from the embed
  leg already removed them, and the cover for a structured-only re-run);
  non-overwrite returns the `-1` skip marker when `_source_present` is true.

- **ltree / NullType reflection** (lines 174-179): `inspector.get_columns` reads
  only `c["name"]`; the expected `SAWarning` from the typeless `collection_path
  ltree` column is suppressed via `warnings.catch_warnings()` +
  `simplefilter("ignore", SAWarning)`, with a comment explaining the read is
  names-only and therefore safe.

## Status & Next Steps

**Current Status**: REVIEW COMPLETE — CLEAN. No findings. Both v01 findings (the
"raw headers assumed distinct" invariant on `build_column_mapping`, and the
expected-ltree-SAWarning suppression) are confirmed resolved on disk. The
task-flagged FK check is verified end-to-end: a custom `sheet_table` name produces
a valid, creatable FK because the same validated name creates the parent table
(whose `collection_path` is a PRIMARY KEY) and is referenced by the structured
table's FK.

**Completed**:
1. Read all python-development core sub-docs and the SQL best-practices doc.
2. Read the prior structured_table review and confirmed both resolved findings on
   disk (invariant doc at lines 63-69; SAWarning suppression at lines 175-179).
3. Reviewed the whole current file against the skills and for correctness
   (injection safety, additive evolution, the overlap guard, the FK/cascade, the
   all-text design, transaction boundaries).
4. Traced the custom-`sheet_table` FK end-to-end through `ingest_excel.py`
   (lines 478, 493, 600), `ensure_table` (lines 112-115), and
   `sql/excel_schema.sql` (line 26, `collection_path ltree primary key`).

**Next Steps**:
1. None required.

**Blockers**:
1. None.

**Notes**:
1. `validate_sql_identifier` is re-exported from file_ingestion's canonical
   validator; the injection-safety chain (validate + double-quote identifiers,
   bind values) holds for every dynamically generated statement.
2. The embed-leg-then-structured-leg ordering plus the defensive re-delete keep
   re-runs self-repairing rather than divergent (the structured leg's FK requires
   the embed leg's sheet row, which the orchestrator writes first).
