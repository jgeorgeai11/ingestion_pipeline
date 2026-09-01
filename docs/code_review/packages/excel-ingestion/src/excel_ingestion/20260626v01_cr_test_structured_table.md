---
name: cr-test_structured_table
goal: Re-review code/excel_ingestion/unit_tests/test_structured_table.py against the unit-tests skill; confirm prior-round findings are resolved and assess the FK/cascade realism in light of the configurable sheet_table change.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

1. [pending] FK cascade covered only against the default sheet name - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 1.1. [suggestion] `test_deleting_parent_sheet_cascades_structured_rows` (line 320) and every other DB test pass the literal `"sheet"` as the `sheet_table` FK parent (e.g. `write_rows(engine, schema, "t", "sheet", ...)`). Now that `ingest_excel` threads a configurable `sheet_table` into `write_rows` (`ingest_excel.py:600`) and `ensure_table` renders `references {schema}.{sheet_table}` (`structured_table.py:113-114`), the FK/cascade behavior under a NON-default parent name is exercised only end-to-end in `test_ingest_excel.py::test_pipeline_custom_consolidated_table_names`, not at this unit's boundary. The cascade itself is name-independent (the FK is defined the same way for any validated name), so this is hardening, not a defect.
        - Current: all `write_rows` calls use `sheet_table="sheet"`.
        - Expected (optional): parametrize one append/cascade test over `sheet_table in ("sheet", "xls_sheet")` (creating the matching `_parent` table name) to pin that `ensure_table` renders the FK against the supplied name and the cascade fires for a custom parent. Low priority given the e2e coverage.
        - Rationale: unit-tests 4 (mock realism) — the unit's new configurable parameter is unexercised at the unit level; the e2e covers it, so [suggestion].

## Skills with No Issues

1. unit-tests (prior 2026-06-24 findings all resolved): No issues — 2.1 the empty-rows->0 branch is covered by `test_write_rows_empty_rows_returns_zero` (line 342, asserts `count == 0` and the table is empty); 2.2 the exact-threshold strict-`<` boundary by `test_write_rows_guard_allows_exact_threshold` (line 357, `r_in=r_ex=0.5` equals the threshold and must pass); 4.1 the composite PK by `test_write_rows_composite_pk_rejects_duplicate` (line 378, re-inserting `(collection_path, sort_order)` raises `IntegrityError`); 1.1 the dead `mock_engine` fixture is removed from conftest; 3.1 the look-alike `_parent` carries a documenting docstring (lines 54-61).
2. unit-tests (real schema for DB tests): No issues — DDL/DML run against the real `ephemeral_schema`; mocking DDL would be tautological. The FK cascade is exercised against a real Postgres FK (`test_deleting_parent_sheet_cascades_structured_rows`).
3. unit-tests (line coverage): No issues — `structured_table.py` is at 100% line coverage (verified via `--cov=structured_table --cov-report=term-missing`, no missing lines).
4. unit-tests (append/overwrite/skip coverage): No issues — same-shape append, superset ADD COLUMN, subset NULL, overwrite-replaces-only-that-source, skip-if-present, the compatibility guard (disjoint raises, high overlap allowed, exact threshold allowed), and FK cascade are all covered — the rework's headline behaviors.
5. unit-tests (assertion strength): No issues — tests pin values (`rows[0]["col_code"] == "A1"`, `by_path["a.s"]["col_extra"] is None`, `a_rows[0]["col_code"] == "A-NEW"`), not just row counts or write success; the skip test asserts the row is `"A"` (unchanged), not just `len == 1`.
6. unit-tests (pytest.raises match): No issues — empirically confirmed by the all-passing live run; `match="Incompatible columns"` exists in `structured_table.py:191`; the PK test uses an `IntegrityError` type assertion.
7. unit-tests (mock boundaries / no over-mocking): No issues — the pure `build_column_mapping` test needs no DB; all DB tests use the real schema; nothing is mocked that would make a test tautological.
8. unit-tests (naming / AAA / order independence): No issues — predictable names; `_row`/`_parent`/`_fetch_all`/`_columns` helpers keep Arrange concise; each test gets a fresh UUID schema, no cross-test state.
9. type-hints: No issues — helpers and tests fully annotated.
10. docstrings: No issues — every test and helper has a behavior docstring.
11. logging: No issues — `setup_logging` configured to `logs/excel_ingestion/unit_tests`.
12. sql-development (best-practices): No issues — the `select *` in `_fetch_all` is test read-back introspection (acceptable); `information_schema` queries in `_columns` use explicit columns and bound parameters.

## Status & Next Steps

**Current Status**: Reviewed (review-only, no edits). Effectively clean. 100% line coverage of `structured_table.py`; all 2026-06-24 findings are resolved. The one [suggestion] is a hardening note: the configurable `sheet_table` FK parent is exercised only at the e2e level (`test_pipeline_custom_consolidated_table_names`), not at this unit's boundary.
**Completed**:
1. Re-verified every prior-round finding against the current test file and source.
2. Ran `pytest --cov=structured_table --cov-report=term-missing` (then deleted `.coverage`): no missing lines.
3. Confirmed the FK cascade is exercised against a real Postgres FK and assessed the new configurable-parent surface.
**Next Steps**:
1. Optional: parametrize one cascade/append test over a custom `sheet_table` name (1.1).
**Blockers**:
1. None.
**Notes**:
1. The structured-row cascade for the CUSTOM sheet name is the same nuance flagged in the test_ingest_excel review (1.1 there): the e2e's `structured_after == 1` is pinned by `write_rows`'s own delete, not the cascade. A unit-level custom-name cascade test (1.1 here) would close both at once.
