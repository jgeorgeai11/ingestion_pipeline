---
name: cr-data_val_loaded_documents
goal: Review the new source_binary_hash provenance check and re-verify code/file_ingestion/data_validation/data_val_loaded_documents.py against python-development and sql-development skills.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

This is the v02 re-review, focused on the new PROVENANCE check (check 1a, lines 173-191) added since v01, plus a regression sweep over the seven v01-resolved findings. The new check is SQL-correct: `source_binary_hash is null or < 0 or >= 18446744073709551616` correctly detects a null, a negative, and an overflow past the unsigned 64-bit range on the `numeric(20,0)` column, with no off-by-one — the literal `18446744073709551616` equals `2^64` exactly, `>=` excludes it, and the maximum valid value `2^64 - 1` (= 18446744073709551615) passes. The check is genuinely non-vacuous: the producer (`cleaned_models.py:121`, `binary_hash: int = Field(ge=0)`) enforces only the lower bound, and the DDL column `numeric(20,0)` physically admits values up to `10^20 - 1` and negatives, so both the `< 0` and the `>= 2^64` predicates guard states the column type allows but upstream does not reject — exactly what a DB-level validator should assert. SQL-injection safety is intact (the only interpolated token is the validated `{doc}`; the bound literal is a compile-time constant, not user data). All seven v01-resolved findings are still present — no regression. Findings: 0 critical / 0 major / 0 minor / 2 suggestion. Top issue: the new check emits a single aggregate `count(*)` message that does not name the offending `collection_path`, unlike the per-document checks 5/6 — consistent with check 1's style, but per-document reporting would improve diagnostics.

## Implementation Plan

1. [pending] Diagnostic specificity of the new provenance check - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 1.1. [suggestion] Lines 177-191: The new source_binary_hash check uses a global `count(*)` and emits one aggregate failure message that does not name the offending document(s). This mirrors check 1 (null collection_path, lines 167-171), which is also a count-only message, so it is internally consistent. However, unlike check 1 — where the only offending column *is* the key and there is nothing else to name — `collection_path` is available on the `document` table here, so a `group by collection_path` form (as in checks 5/6, lines 280-307) could name each offending document. For a failure that signals provenance corruption this would speed diagnosis (which document carries the bad hash).
        - Current: `select count(*) from {doc} where source_binary_hash is null or source_binary_hash < 0 or source_binary_hash >= 18446744073709551616` → one aggregate `FAIL` message
        - Expected (optional): `select collection_path::text as cp, count(*) as n from {doc} where source_binary_hash is null or source_binary_hash < 0 or source_binary_hash >= 18446744073709551616 group by collection_path` → one `FAIL` naming each offending `cp`, matching the per-document style of checks 5/6.

2. [pending] Docstring symmetry - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 2.1. [suggestion] Lines 96-126 (`validate_loaded_documents` docstring): The module-level "Checks (all SQL)" list (lines 16-30) correctly enumerates the new provenance check (lines 20-22), so the task's docstring question is satisfied. The function docstring is a prose summary that has never enumerated the single-table checks (it also omits checks 1, 5, and 6), so omitting an explicit mention of 1a is consistent, not a defect. Only if symmetry is desired, a one-line mention of the provenance assertion in the function docstring would match how the prose already calls out the presence and "at least one content row" checks.

## Skills with No Issues

1. SQL correctness (new provenance check, check 1a, lines 177-191): No issues. The three-way predicate `is null OR < 0 OR >= 2^64` is correct: the null row is caught by the first term (a null short-circuits the comparisons), the all-`OR` precedence is unambiguous, and the bound is exact. In Postgres a bare integer constant exceeding bigint range (`> 9223372036854775807`) is typed `numeric`, so `numeric(20,0) >= 18446744073709551616` is an exact numeric-vs-numeric comparison with no float precision loss. No off-by-one: `18446744073709551616 == 2^64`, `>=` rejects it, and the max valid `2^64 - 1` passes.
2. SQL correctness (existing checks 0, 0a, 1, 2, 2a, 3, 4, 5, 6): No issues. Re-verified against v01; the count-match (check 2) is safe because `content_table.sort_order` is NOT NULL (schema.sql:35), so the left-join `count(content_row.sort_order)` is 0 (not 1) for a content-less document; the contiguity check (check 4) min=1/max=count(*) test is correct given the composite PK guarantees `sort_order` uniqueness; the orphan anti-join (check 3) and NULL-aware checks 5/6 are correct.
3. SQL-injection safety (sql-development best-practices + identifier validation): No issues. The only interpolated tokens are `{doc}`/`{content}`, both composed solely from `db_schema`/`document_table`/`content_table` validated via `validate_sql_identifier` (lines 127-129). The new check's `18446744073709551616` is a compile-time constant, not user/config data, so it is not an injection vector. The only data value (`expected_collection_paths`) remains a bound parameter (`:cps`, line 154).
4. SQL best-practices (style): No issues. The new check is lowercase, uses explicit NULL handling (`is null`), and fits within 100 chars per line. Join aliases elsewhere are descriptive (`doc_row`/`content_row`); single-table checks use bare column names.
5. Type Hints: No issues. All functions retain complete parameter and return annotations using modern syntax (`list[str]`, `-> None`, `Engine`).
6. Docstrings: No issues. The module "Checks" list (lines 16-30) was updated to include the new provenance check (lines 20-22); the new check carries an explanatory comment (lines 173-176) stating the column is `numeric(20,0)` NOT NULL and why the range guard exists. See finding 2.1 for optional function-docstring symmetry.
7. Comments: No issues. The new check's comment explains the why (column type allows out-of-band values the producer's `ge=0` does not reject), consistent with the comment style across the file.
8. Exception Handling: No issues. Unchanged from v01: specific exceptions caught (`KeyError`, `tomllib.TOMLDecodeError`/`OSError`, `ValueError`, `SQLAlchemyError`), `raise ... from e` chaining in `_get_engine`, no bare `except`, failures logged before `sys.exit(1)`.
9. Logging: No issues. Uses the shared `logconfig` `setup_logging`/`get_logger`; no `print`.
10. Executable Scripts: No issues. Single `--config` argument, `main()` + `if __name__ == "__main__"`, logging deferred until after argparse, config-existence and TOML-parse guards present.
11. Data Validation (naming/organization): No issues. File named `data_val_*` under `code/file_ingestion/data_validation/`.
12. Completeness vs producer schema (provenance): No issues. There is no uniqueness invariant on the hash (distinct documents may share source bytes), so no additional provenance assertion is owed; the lower- and upper-bound checks fully cover the range the producer leaves unguarded.
13. Regression vs v01-resolved findings: No regression. All seven resolved items are present — check 2a (zero content rows, lines 219-232); dropped dead `count(*) <> count(distinct sort_order)` predicate with `n_distinct` retained only for the message (lines 264-277); descriptive `doc_row`/`content_row` aliases (checks 2, 2a, 3); engine `dispose()` in `finally` (line 400); empty-document-list guard (lines 371-373); named-offender loop on missing `collection_path` (lines 362-369); module-docstring check-list updates (lines 16-30).

## Status & Next Steps

**Current Status**: Re-review complete. The new provenance check is SQL-correct, injection-safe, and consistent with the collect-failures style; no critical/major/minor issues; two optional suggestions. No code modified, no commit.
**Completed**:
1. Read the code-review template/example, sql-development best-practices, and python-development skill index; read the prior v01 review for context.
2. Reviewed the current whole file, focusing on the new check 1a, and cross-checked against the producer model (`cleaned_models.py:121`), the DDL (`sql/schema.sql:25`), the loader insert (`ingest.py:562-575`), and the shared validators (`_utils.py`).
3. Verified the off-by-one bound (`18446744073709551616 == 2^64`; `2^64 - 1` passes), the Postgres numeric-literal typing (exact numeric comparison, no precision loss), injection safety, and the non-vacuousness of both the `< 0` and `>= 2^64` predicates relative to upstream `ge=0` and the `numeric(20,0)` column.
4. Confirmed all seven v01-resolved findings are still in place (no regression).
**Next Steps**:
1. Optionally apply suggestions 1.1 (per-document `group by` for the provenance failure message) and 2.1 (function-docstring symmetry).
**Blockers**:
1. None.
**Notes**:
1. No code changes were made and no commit was created, per task instructions.
2. The producer enforces only `binary_hash >= 0` (`cleaned_models.py:121`) with no upper bound, and the `numeric(20,0)` column admits up to `10^20 - 1` and negatives — so the new check's `< 0` and `>= 2^64` predicates are the only place the uint64 range is asserted on the data at rest.
