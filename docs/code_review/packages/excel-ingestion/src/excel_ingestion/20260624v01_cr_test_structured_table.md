---
name: cr-test_structured_table
goal: Address coverage, boundary, and fixture issues in code/excel_ingestion/unit_tests/test_structured_table.py (and the conftest.py it owns) to align with the unit-tests skill.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Dead fixture in shared conftest - `code/excel_ingestion/unit_tests/conftest.py`
   - 1.1. [minor] The `mock_engine` fixture (lines 20-29) is defined but requested by no test in the suite (`grep -rn mock_engine` finds only the definition; coverage marks lines 23-29 unexecuted). The DB tests use the real `ephemeral_schema`, which is correct — so `mock_engine` is unused scaffolding.
        - Expected: remove the `mock_engine` fixture (and its now-unused `MagicMock` import) unless a forthcoming test needs it; an unused fixture invites future tests to mock the DB where the real schema is the right boundary.
        - Rationale: unit-tests 4 (mock external boundaries only) — keeping a DB-mock fixture around encourages the anti-pattern this module deliberately avoids.

2. [completed] Boundary-value coverage in reconcile / write_rows - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 2.1. [major] No test covers the empty-rows branch of `write_rows` (`structured_table.py:309-311`, `if not rows: return 0`) — coverage marks 310-311 missed. The orchestrator skips zero-row sheets, but `write_rows` is a public function whose `0` return is documented and reachable on a direct call.
        - Expected: call `write_rows(..., rows=[])` against a created table and assert the return is `0` and the table is empty.
        - Rationale: unit-tests 7.1 — a documented return path with no test.
   - 2.2. [suggestion] The compatibility guard is tested only well-below and well-above the threshold (`test_write_rows_guard_raises_on_disjoint_columns`, `test_write_rows_guard_allows_high_overlap`). The `<` boundary itself — overlap EXACTLY equal to `min_column_overlap` (which must be ALLOWED, since the guard is strict `<`) — is untested.
        - Expected: a case where `r_in == min_column_overlap` (e.g. 1 shared of 2 incoming with `min_column_overlap=0.5`) and assert the write succeeds, pinning the `<` (not `<=`) at `structured_table.py:174`.
        - Rationale: unit-tests 7.1 — boundary value; a regression to `<=` would silently reject valid sheets and only this case catches it.
   - 2.3. [suggestion] The empty-denominator guard (`structured_table.py:171-172`, incoming/existing empty -> ratio `0.0`) is not directly exercised; a sheet with zero data columns (all-empty headers degenerate to no `col_*`) would hit it. Low priority, as the parser rejects empty headers upstream, but the guard is defensive code with no test.
        - Rationale: unit-tests 7.1 — boundary (empty set) the code explicitly guards against.

3. [completed] FK / schema realism - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 3.1. [suggestion] The `_parent` helper (lines 52-79) hand-rolls the `sheet` table DDL rather than rendering it from `sql/excel_schema.sql`. It correctly includes `collection_path ltree primary key` (so the FK and cascade are genuinely exercised), but the copy can drift from the real schema (e.g. the real `n_rows`/`source_binary_hash` CHECK constraints are omitted here). The FK-cascade test (`test_deleting_parent_sheet_cascades_structured_rows`) is therefore real, but the parent is a look-alike, not the production table.
        - Expected: optionally build the parent via `ingest_excel.ensure_consolidated_tables` (as `test_ingest_excel.py` does for its mixed-batch test) so the FK target is the exact production DDL; or add a comment that the look-alike intentionally carries only the FK-relevant column.
        - Rationale: unit-tests 4 / mock realism — a real-schema parent removes a drift seam; this is a hardening suggestion, not a correctness defect (the cascade IS tested against a real Postgres FK).

4. [completed] Assertion strength - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 4.1. [suggestion] `test_write_rows_creates_table_with_identity_and_data_cols` (line 112) verifies columns and a few values but never asserts the composite PK `(collection_path, sort_order)` is enforced. The docstring claims "a PK"; a missing PK would let duplicate `(path, sort_order)` rows in undetected.
        - Expected: add an assertion that re-inserting the same `(collection_path, sort_order)` raises an integrity error, or query `information_schema` for the PK columns.
        - Rationale: unit-tests 7 — the PK is a load-bearing part of the documented identity but is not pinned by any assertion.

## Skills with No Issues

1. unit-tests (real schema for DB tests): No issues — DDL/DML run against the real `ephemeral_schema`; mocking DDL would be tautological. This is the correct boundary choice.
2. unit-tests (teardown): No issues — `ephemeral_schema` drops the UUID-named schema with CASCADE in a `finally`, so it runs even on assert/raise; `engine.dispose()` is always called.
3. unit-tests (skip without DB): No issues — the fixture `pytest.skip`s cleanly when the DB is unreachable, keeping the suite green without a database.
4. unit-tests (order independence): No issues — each test gets a fresh unique schema; no cross-test state.
5. unit-tests (pytest.raises match): No issues — "Incompatible columns" exists in `structured_table.py:176`.
6. unit-tests (naming / AAA): No issues — predictable names; `_row`/`_parent`/`_fetch_all`/`_columns` helpers keep Arrange concise.
7. unit-tests (append/overwrite/skip coverage): No issues — same-shape append, superset ADD COLUMN, subset NULL, overwrite-only-that-source, skip-if-present, and FK cascade are all covered (the rework's headline behaviors).
8. type-hints: No issues — helpers and tests fully annotated.
9. docstrings: No issues.
10. logging: No issues.
11. sql-development (best-practices): The `select *` in `_fetch_all` (line 84) is test introspection (acceptable for "read everything back"), not a query on raw inputs; `information_schema` queries in `_columns` use explicit columns. No actionable issue.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: removed the dead mock_engine fixture + MagicMock import from conftest. 2.1: empty-rows->0 test. 2.2: exact-threshold (strict <) allow test. 4.1: composite-PK duplicate-insert -> IntegrityError test. 3.1: documented the look-alike _parent. 2.3 skipped (parser rejects empty headers upstream; unreachable). Suite green.
**Completed**:
1. Reviewed all DB-backed `write_rows` tests plus `build_column_mapping` against `structured_table.py`.
2. Reviewed `conftest.py` (owner of `ephemeral_schema`); confirmed `mock_engine` is unused via grep + coverage.
3. Ran coverage: `structured_table.py` at 98% (only the empty-rows branch 310-311 missed).
**Next Steps**:
1. Remove the dead `mock_engine` fixture (1.1).
2. Add the empty-rows, exact-threshold, and PK-enforcement tests (2.1, 2.2, 4.1).
**Blockers**:
1. None.
**Notes**:
1. The FK-cascade-from-parent test added in the rework is present and genuinely exercises `on delete cascade` against a real Postgres FK — the key new behavior is covered. conftest findings are folded in here per the review brief.
