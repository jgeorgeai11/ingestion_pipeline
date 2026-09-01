---
name: cr-test_ingest_excel
goal: Address assertion-strength and coverage gaps in code/excel_ingestion/unit_tests/test_ingest_excel.py to align with the unit-tests skill.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Weak hash assertion (rework-critical) - `code/excel_ingestion/unit_tests/test_ingest_excel.py`
   - 1.1. [major] `test_pipeline_stores_title_n_rows_and_hash` (line 304) asserts only `0 <= source_binary_hash < 2**64`. A broken implementation that stored a constant `0` (or any fixed value) for `source_binary_hash` would PASS this test, because `0` satisfies the range. The rework's whole point is that the per-sheet hash is the content fingerprint stored on the `sheet` row; this assertion does not pin that it reflects the sheet's content.
        - Current: `assert 0 <= int(row["source_binary_hash"]) < 2**64`
        - Expected: recompute the expected hash from the known workbook and assert equality, e.g.
          `expected = compute_source_hash([build_row_text(["Code","Label"], r) for r in <the two parsed rows>])` then `assert int(row["source_binary_hash"]) == expected` (and `!= 0`). At minimum assert it is nonzero AND that a content change produces a different stored hash.
        - Rationale: unit-tests 7 — the assertion must discriminate the correct implementation from a wrong one; the brief explicitly asks "is the per-sheet hash stored on the sheet row?" and this test does not verify it is the *right* hash.

2. [completed] row_text assertion realism - `code/excel_ingestion/unit_tests/test_ingest_excel.py`
   - 2.1. [suggestion] The pipeline tests assert `sheet_content` row COUNTS but never assert a stored `row_text` equals the documented newline-KV form end-to-end. `build_row_text` is unit-tested directly (lines 147-162, good), but no pipeline test confirms the value actually persisted to `sheet_content.row_text` is the newline-KV string (the leg the embedding model consumes).
        - Expected: in `test_pipeline_embed_only_and_both_legs`, assert one fetched `sheet_content` row's `row_text == "Code: A1\nLabel: alpha"` and that `word_count` matches.
        - Rationale: unit-tests 7.1 — the rework changed `row_text` to newline-KV; pinning it once at the persistence boundary guards the format the model is trained on.

3. [partial] Missing config-error coverage - `code/excel_ingestion/unit_tests/test_ingest_excel.py`
   - 3.1. [minor] `ensure_consolidated_tables`' two documented guards are untested: the missing-DDL-template `FileNotFoundError` (`ingest_excel.py:153-154`) and the unsubstituted-placeholder `ValueError` (`ingest_excel.py:165-170`) — coverage marks 154 and 167 missed. These are defense-in-depth against template/code drift.
        - Expected: a test that monkeypatches the DDL path to a missing file (assert `FileNotFoundError`) and one that points it at a template containing a stray `{foo}` (assert `ValueError, match="Unsubstituted placeholder"`).
        - Rationale: unit-tests 7.1 — documented `Raises:` conditions with no test.
   - 3.2. [suggestion] The parse-failure resilience branch (`ingest_excel.py:388-398`: a sheet that fails `parse_sheet` is recorded as a failure and the run exits 1 while other sheets proceed) is uncovered. `test_pipeline_resilient_mixed_batch` exercises the *structured* failure branch but not the *parse* branch.
        - Expected: a config naming a non-existent sheet (or bad row bounds reaching the parser) alongside a good sheet; assert exit 1, the good sheet still wrote, and the parse failure was the cause.
        - Rationale: unit-tests 7.1 — the resilience contract (one sheet's failure does not abort the others) has multiple failure stages; only one is covered.

4. [completed] Skip-then-overwrite assertion gap - `code/excel_ingestion/unit_tests/test_ingest_excel.py`
   - 4.1. [suggestion] `test_pipeline_skip_if_present_then_overwrite` proves the structured leg's overwrite collapses 2 rows to 1 (line 369-370), but for the *content* leg it only re-asserts the count after overwrite. It does not assert that on the skipped re-run the `sheet` metadata row (e.g. `source_binary_hash` or `ingested_at`) was NOT rewritten — the documented "skip WITHOUT re-inserting the metadata row" behavior.
        - Expected: capture the `sheet` row before the no-op re-run and assert it is byte-identical afterward (skip did not touch it).
        - Rationale: unit-tests 7 — "skip" must be distinguished from "re-insert identical"; only an unchanged-metadata assertion pins it.

## Skills with No Issues

1. unit-tests (config validation coverage): No issues — missing top-level, missing sheet field, optional table/title/collection_path, invalid collection_path, both bad-row-bound directions, unsafe table identifier, and missing `sheets` key are all parametrized/covered.
2. unit-tests (real schema E2E): No issues — the pipeline runs against the real `ephemeral_schema` with in-memory workbooks and skips cleanly without a DB; the embed sheet->sheet_content cascade is covered implicitly by the overwrite 2->1 assertion.
3. unit-tests (derived vs authored collection_path): No issues — `test_pipeline_embed_only_and_both_legs` pins the derived `wb.alpha`/`wb.bravo`, and `test_pipeline_authored_collection_path_and_title` pins the authored override and that the derived path is absent.
4. unit-tests (zero-row skip): No issues — `test_pipeline_skips_zero_row_sheet` asserts no orphan `sheet` metadata and no structured table (the n_rows>=1 CHECK invariant).
5. unit-tests (resilient batch / exit code): No issues — `test_pipeline_resilient_mixed_batch` confirms a structured-leg failure yields exit 1 while the embed leg and other sheets succeed across separate transactions.
6. unit-tests (pytest.raises match): No issues — config `match=` strings ("Missing required config field", "missing required", "Invalid collection_path", "must be greater than header_row", "must be >= data_start_row", "Unsafe SQL identifier", "missing required field 'sheets'") all exist in `ingest_excel.py`/the validators.
7. unit-tests (naming / AAA / order independence): No issues — fresh schema per test; helpers keep Arrange concise.
8. type-hints: No issues — all helpers and tests annotated.
9. docstrings: No issues.
10. logging: No issues.
11. sql-development (best-practices): The `select *` in `_content_rows`/`_sheet_row` helpers is test read-back introspection (acceptable), not a query on raw inputs. No actionable issue.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: hash test now recomputes the expected fingerprint and asserts equality + nonzero. 2.1: pinned a persisted newline-KV row_text + word_count. 3.2: added a parse-failure resilience test (missing sheet -> exit 1, good sheet still loads). 4.1: skip re-run now asserts the sheet row is byte-identical (skip != re-insert). 3.1 skipped (ensure_consolidated_tables guards need fragile internal monkeypatching; low-value drift-protection). Suite green.
**Completed**:
1. Reviewed all config-validation, `build_row_text`, and end-to-end pipeline tests against `ingest_excel.py`.
2. Ran coverage: `ingest_excel.py` at 84% — the uncovered lines are `ensure_consolidated_tables` guards (154, 167), the parse-failure resilience branch (388-398), and CLI/setup error paths (327-342, 361-363) that are entry-point-only.
**Next Steps**:
1. Strengthen the hash assertion to recompute the expected value (1.1) — highest priority.
2. Pin a persisted `row_text` value (2.1) and add the `ensure_consolidated_tables` guard tests (3.1).
**Blockers**:
1. None.
**Notes**:
1. Coverage gaps at 327-342/361-363 are `main()` CLI/setup failure exits (config-not-found, TOML decode error, engine setup failure). These are entry-point error paths; covering them needs argv/env manipulation and is lower value than the hash assertion above.
