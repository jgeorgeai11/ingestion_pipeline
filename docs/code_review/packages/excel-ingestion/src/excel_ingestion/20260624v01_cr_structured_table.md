---
name: cr-structured_table
goal: Address code quality issues identified in code/excel_ingestion/structured_table.py to align with python-development core skills and general correctness.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Correctness (column mapping) - `code/excel_ingestion/structured_table.py`
   - 1.1. [minor] Lines 68-70: `build_column_mapping` keys the returned dict on the
        ORIGINAL header via `dict(zip(column_names, deduped))`. If a sheet has two
        columns with byte-identical raw header text, the dict collapses them
        (last-wins), and `write_rows`'s `for header, col in column_map.items()`
        loop then only populates one of the resulting `col_*` columns. This is NOT
        new data loss because `excel_parser.parse_sheet` already keys each
        `row_dict` on the raw header name (line ~159), so identical raw headers are
        already collapsed in the row data before they reach here — the dedup
        machinery correctly handles *normalization* collisions (e.g. `Q1`/`q1`),
        only byte-identical raw strings are affected, and that is an upstream
        property. Worth a note so the invariant (raw headers are assumed distinct)
        is documented rather than implicit.
        - Current: `return dict(zip(column_names, deduped))`
        - Expected: Either document the "raw headers are assumed distinct"
          assumption in the docstring, or key the mapping positionally (e.g. return
          `list[tuple[str, str]]` of (header, col) pairs) so two identical raw
          headers each map to their own deduplicated `col_*` column.

2. [completed] Robustness (reflection warning) - `code/excel_ingestion/structured_table.py`
   - 2.1. [suggestion] Lines 160-163: `inspect(conn).get_columns(...)` emits a
        SAWarning because the reflected `collection_path ltree` column has no
        registered SQLAlchemy type and is mapped to `NullType`. Reading only
        `c["name"]` is SAFE — `NullType` only causes problems when the type object
        is actually used for binding/coercion, and the code never touches
        `c["type"]`. This is a non-finding for correctness; the only improvement is
        cosmetic noise reduction.
        - Current: `reflected = {c["name"] for c in inspector.get_columns(...)}`
        - Expected: Optionally register/silence the ltree type once (e.g. a module
          comment noting the warning is expected, or `warnings.catch_warnings()`
          around the reflection) so the log isn't polluted; behaviour is already
          correct.

## Skills with No Issues

1. Type Hints: No issues found. All functions (`_quote`, `build_column_mapping`,
   `ensure_table`, `reconcile_columns`, `_source_present`, `write_rows`) carry full
   modern-syntax annotations (`list[str]`, `str | int | None`, keyword-only `*`
   params); return types present including `-> None` and `-> int`.
2. Docstrings: No issues found. Google-style with Args/Returns/Raises; the `-1`
   skipped-marker, the FK/cascade lifecycle, and the overlap-guard semantics are all
   documented and match the code.
3. Comments: No issues found. Comments explain "why" (overlap empty-denominator
   guard, defensive re-delete under cascade, reconcile-after-ensure ordering) not
   "what", and are current with the reworked schema.
4. Logging: No issues found. Uses `logconfig.get_logger`; f-strings throughout; no
   `print`; appropriate levels (debug for DDL, info for row counts, warning for
   schema drift); no redundant entry/exit messages.
5. Exception Handling: No issues found. Functions raise specific `ValueError` for the
   compatibility guard and let `SQLAlchemyError` propagate to the caller, which
   catches `(ValueError, SQLAlchemyError)` specifically per-sheet. No bare excepts,
   no generic-`Exception` wrapping.
6. SQL-injection safety: No issues found. `db_schema`, `table_name`, `sheet_table`,
   and every generated `col_*` identifier pass `validate_sql_identifier` (file_ingestion's
   canonical `fullmatch` validator) and are then double-quoted via `_quote`; all data
   values (`collection_path`, `sort_order`, `col_*`) are bound as named parameters,
   never interpolated. Identity column literals embedded in SQL are fixed string
   constants.
7. Transaction boundaries: No issues found. `write_rows` performs ensure/delete/
   reconcile(ALTER)/insert inside a single `engine.begin()` block; Postgres
   transactional DDL makes the ALTER+INSERT atomic, and a failure rolls the whole
   sheet back.
8. Overwrite/skip + reconcile-overlap logic: No issues found. Overwrite deletes the
   collection_path's rows first (a harmless no-op when the FK cascade already removed
   them); non-overwrite returns the `-1` skipped marker when present; the guard
   raises only when BOTH overlap ratios are below threshold (subset-in-either-direction
   passes), with empty denominators guarded to 0.0.
9. FK / collection_path lifecycle: No issues found. PK `(collection_path, sort_order)`
   with `sort_order` from the per-sheet 1-based `row_number` is unique because
   `collection_path` is per-sheet; the FK onto `sheet(collection_path)` with
   `on delete cascade` correctly ties structured rows to the embedding leg's sheet row.
10. Executable Scripts / Data Validation / Unit Tests: N/A - this is a library module,
    not an entry point, validator, or test file.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: documented the 'raw headers assumed distinct' invariant on build_column_mapping. 2.1: suppressed the expected ltree SAWarning around the reflection (names-only read) with an explaining comment. Suite green.
**Completed**:
1. Reviewed against all python-development core skills and for general correctness.
2. Verified SQL-injection safety (validate + double-quote identifiers; parameterized values).
3. Verified the create-or-append + reconcile overlap guard, FK/cascade lifecycle,
   transaction boundaries, and overwrite/skip semantics against the source.
4. Confirmed the `ltree`/`NullType` reflection is safe (only column names read).
**Next Steps**:
1. Optionally document the "raw headers assumed distinct" invariant on
   `build_column_mapping` (finding 1).
2. Optionally silence the expected ltree SAWarning around the reflection (finding 2).
**Blockers**:
1. None.
**Notes**:
1. No [critical]/[major] findings. The embed-leg-then-structured-leg ordering plus the
   defensive re-delete make re-runs self-repairing rather than divergent.
