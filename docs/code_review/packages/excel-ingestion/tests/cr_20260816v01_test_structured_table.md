---
name: cr_20260816v01_test_structured_table
goal: Address code quality issues identified in code/excel_ingestion/unit_tests/test_structured_table.py to align with the python-development unit-tests skill; re-review since 20260626v01, paired with the grouped review of structured_table.py (all 16 tests verified passing against a live policy_db).
created: 2026-08-16 19:29:14
updated: 2026-08-17 09:48:46
---

## Implementation Plan

1. [completed] Cover the identifier-validation guard at this unit's boundary - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 1.1. [minor] The suite never exercises `write_rows`'s injection guard: `validate_sql_identifier` is called on `db_schema`, `table_name`, and `sheet_table` before any SQL is generated (`structured_table.py:318-320`), and it is the module's only defense against a config-supplied identifier being spliced into DDL. Only the underlying validator is tested, one module away (`test_utils.py:207-215`); no test asserts that `write_rows` itself rejects a hostile name, so a refactor that dropped the three `validate_sql_identifier` calls would leave this suite green. unit-tests 7.1 (cover error conditions).
        - Current: no test passes an invalid identifier to `write_rows`.
        - Expected: `with pytest.raises(ValueError): write_rows(engine, schema, 'bad"; drop table t --', "sheet", "a.s", column_names=["Code"], rows=[_row(1, Code="A")], overwrite=False, min_column_overlap=0.5)`, parametrized over the `db_schema` / `table_name` / `sheet_table` positions.
        - Resolution: Implemented with one added accommodation — added `test_write_rows_rejects_unsafe_identifier` under a new `# write_rows: identifier validation` banner, `@pytest.mark.parametrize`d over the three positions (a `names` dict supplies the valid values and the parameter replaces one with `'bad"; drop table t --'`). Deviation: the `pytest.raises` is `match="Unsafe SQL identifier for {position}"` rather than a bare `ValueError`, so each case also pins that the failing identifier is the one under test — a bare `ValueError` would pass even if a different position raised. No `_parent` call is needed because the three `validate_sql_identifier` calls run before `engine.begin()`. All three cases pass against a live `policy_db`.

2. [completed] Repair the stale section banners - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 2.1. [minor] Lines 333-336: the banner `# write_rows: FK cascade from the parent sheet` covers only `test_deleting_parent_sheet_cascades_structured_rows` (line 338); five later tests were appended beneath it without their own banners — `test_write_rows_empty_rows_returns_zero` (line 360), `test_write_rows_guard_allows_exact_threshold` (line 375, which belongs under the compatibility-guard banner at lines 228-231), and the three table-comment tests (lines 396, 418, 434) plus `test_write_rows_composite_pk_rejects_duplicate` (line 458). The banners are the file's navigation aid, so a banner that no longer describes the block beneath it misleads. Comments guideline 3 (keep comments current).
        - Current: one `FK cascade from the parent sheet` banner spanning lines 338-478.
        - Expected: move `test_write_rows_guard_allows_exact_threshold` under the existing compatibility-guard banner and add banners for `edge cases (empty rows, composite PK)` and `table comments` above their blocks.
        - Resolution: Implemented as specified — `test_write_rows_guard_allows_exact_threshold` moved verbatim under `# write_rows: compatibility guard` (now line 273, after `..._guard_allows_high_overlap`); the `# write_rows: FK cascade from the parent sheet` banner now spans only `test_deleting_parent_sheet_cascades_structured_rows`; added `# write_rows: table comments` above the three comment tests and `# write_rows: edge cases (empty rows, composite PK)` above the final block. Accommodation: to make the new edge-cases banner accurate, `test_write_rows_empty_rows_returns_zero` was also moved down next to `test_write_rows_composite_pk_rejects_duplicate` (both tests unchanged) rather than leaving it stranded under the FK banner. Final section order: build_column_mapping, helpers, create-on-first-write, append paths, compatibility guard, overwrite/skip, identifier validation (new, from 1.1), FK cascade, table comments, edge cases.

3. [completed] Tighten the helper return annotation - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 3.1. [minor] Line 90: `_fetch_all` is annotated `-> list[dict]`; the bare `dict` is implicitly `dict[Any, Any]`, so every `rows[0]["col_code"]` lookup in the callers is unchecked. type-hints guideline 3 (be specific).
        - Current: `def _fetch_all(engine: Engine, schema: str, table: str) -> list[dict]:`
        - Expected: `def _fetch_all(engine: Engine, schema: str, table: str) -> list[dict[str, object]]:`
        - Resolution: Implemented as specified — `_fetch_all` now returns `list[dict[str, object]]` (line 90). No caller changed: the existing `rows[0]["col_code"] == "A1"` / `is None` comparisons and the `{r["collection_path"] for r in rows}` comprehensions are all valid on `object` values.

4. [completed] Optional hardening and naming polish - `code/excel_ingestion/unit_tests/test_structured_table.py`
   - 4.1. [suggestion] Line 37: `test_build_column_mapping_normalizes_and_dedupes` pins `"Question #"` and `"Question"` collapsing to `col_question` / `col_question_2`, but `excel_parser._validate_headers` now rejects a sheet whose headers collide after normalization (excel_parser.py:341-346), so this pair can no longer reach `build_column_mapping` through the pipeline. The test still has value as a direct-caller/defense-in-depth check, but nothing records that. Cross-reference: the same drift is flagged as a stale docstring in `docs/code_review/excel_ingestion/cr_20260816v01_structured_table.md`, finding 1.1.
        - Current: `"""Headers map to deduplicated col_* names preserving order."""`
        - Expected: add a line noting the dedup path is defense-in-depth for direct callers because `parse_sheet` rejects normalization collisions upstream.
        - Resolution: Deferred — documentation-only; the assertion itself is correct and the behavior under test is real. Best folded into the fix for the source-side docstring (1.1 of the paired review) so both statements are updated together.
   - 4.2. [suggestion] Every DB test passes the literal `"sheet"` as the `sheet_table` FK parent (lines 141, 174-175, 188-197, ...), so `ensure_table`'s `references {schema}.{sheet_table}` rendering and the cascade are exercised at this unit only for the default name; a custom parent name is covered end-to-end in `test_ingest_excel.py::test_pipeline_custom_consolidated_table_names`. Carried forward unresolved from the 2026-06-26 review.
        - Current: all `write_rows` calls use `sheet_table="sheet"`.
        - Expected: parametrize one append/cascade test over `sheet_table in ("sheet", "xls_sheet")`, creating the matching `_parent` table name.
        - Resolution: Deferred — the FK DDL is name-independent (the same `references` clause is rendered for any validated name) and the custom-name path already has end-to-end coverage, so this is hardening rather than a gap.
   - 4.3. [suggestion] Lines 233-249 and 206-226: the drift WARNINGs that `reconcile_columns` emits for added and missing columns (`structured_table.py:232-241`) are documented behavior ("so schema drift is visible") but no test asserts them; the ADD COLUMN and NULL-fill tests check only the resulting schema and data.
        - Current: no `caplog` assertions.
        - Expected: `assert "added 1 column(s)" in caplog.text` in `test_write_rows_superset_append_adds_column`, using the built-in `caplog` fixture at `WARNING` level.
        - Resolution: Deferred — optional; asserting on log text couples the test to message wording, and the observable outcome (the column exists / the value is NULL) is already pinned.
   - 4.4. [suggestion] Line 338: `test_deleting_parent_sheet_cascades_structured_rows` omits the unit under test from its name, unlike every sibling (`test_write_rows_*`). unit-tests 2.2 (`test_<function>_<scenario>_<expected>`).
        - Current: `def test_deleting_parent_sheet_cascades_structured_rows(...)`
        - Expected: `def test_write_rows_fk_cascades_when_parent_sheet_deleted(...)`
        - Resolution: Deferred — cosmetic; the current name reads clearly and the subject under test (the FK that `write_rows` creates) is unambiguous from the docstring and body.

## Skills with No Issues

1. Unit Tests (pytest usage): No issues found — pytest throughout, `pytest.raises(ValueError, match="Incompatible columns")` at line 244 (the message exists at `structured_table.py:216`), `pytest.raises(IntegrityError)` at line 470, and the shared `ephemeral_schema` fixture lives in `conftest.py`.
2. Unit Tests (mock boundaries): No issues found — DDL/DML run against a real UUID-named ephemeral schema rather than mocks (mocking DDL would be tautological), and the pure `build_column_mapping` test needs no database; nothing is mocked that would make an assertion vacuous.
3. Unit Tests (independence): No issues found — each test gets a fresh schema dropped with CASCADE in teardown, so no ordering or shared-state coupling; no assertions touch private attributes of the module under test.
4. Unit Tests (coverage of behaviors): One gap found (1.1, the identifier guard). Otherwise the module's branches are covered — create-on-first-write, same-shape append, superset ADD COLUMN, subset NULL-fill, guard raises on disjoint, guard allows high overlap, guard allows the exact `<` boundary, overwrite scoped to one `collection_path`, skip-if-present returning `-1`, empty rows returning `0`, FK cascade, composite-PK rejection, and all three `table_comment` states (applied with quote-bearing text, absent, refreshed on a skipped re-run).
5. Unit Tests (assertion strength): No issues found — tests pin values, not just counts (`rows[0]["col_code"] == "A1"`, `by_path["a.s"]["col_extra"] is None`, `a_rows[0]["col_code"] == "A-NEW"`, and the skip test asserting the row is still `"A"`).
6. Unit Tests (naming/AAA): One suggestion (4.4). File name matches `test_<module>.py`; the `_row`/`_parent`/`_fetch_all`/`_columns`/`_table_comment` helpers keep every test in a readable arrange-act-assert shape.
7. Docstrings: No issues found — module docstring explains the real-DB choice and the parent-row precondition; every test and helper carries a behavior docstring, and `_parent` documents why the look-alike omits the production CHECK constraints (lines 58-65).
8. Type Hints: One issue found (3.1). All other signatures are fully and specifically annotated (`tuple[Engine, str]`, `dict[str, str | int | None]`, `str | None`, `set[str]`, `-> None`).
9. Comments: One issue found (2.1, stale banners). Inline comments otherwise explain intent — why `ingested_at` must be absent (line 152), why `r_in = r_ex = 0.5` must pass (lines 386-387), and why the second comment write is a data skip (line 449).
10. Logging: No issues found — `setup_logging` targets `logs/excel_ingestion/unit_tests` per the log-directory convention, and `sys.path` resolves `code/lib` from the file's own location.
11. sql-development (best-practices): No issues found — generated SQL is lowercase with bound parameters; `information_schema` and `pg_class` probes list explicit columns and use `inner join`; the `select *` in `_fetch_all` is deliberate read-back introspection.
12. Executable Scripts / Data Validation: N/A - test module, not an entry point or validator.
