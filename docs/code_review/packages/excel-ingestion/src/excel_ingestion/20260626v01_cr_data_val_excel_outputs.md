---
name: cr-data_val_excel_outputs
goal: Re-review code/excel_ingestion/data_validation/data_val_excel_outputs.py (current state) against python-development and sql-development skills and for correctness (both-leg invariants, configurable consolidated table names, SQL-injection safety, aliases).
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

1. [pending] Consolidated table names are hardcoded; a custom-named run is unvalidatable (and can silently validate the wrong tables) - `code/excel_ingestion/data_validation/data_val_excel_outputs.py`
   - 1.1. [major] Lines 43-44, 50-188, 191-271, 303-309: the validator hardcodes `SHEET_TABLE = "sheet"` / `CONTENT_TABLE = "sheet_content"` as module constants and interpolates them into every embedding-leg query and the structured-leg orphan-FK query (line 245). But `ingest_excel.py` makes these configurable: `validate_config` validates the optional top-level `sheet_table` / `content_table` keys (lines 105-110 there) and `main()` reads them (`config.get("sheet_table", SHEET_TABLE)` at lines 478-479 there) and threads them into the DDL, the writes, and the structured-leg FK. The output validator never reads those config keys, so a config with custom names cannot validate the tables that were actually written. Worse than a loud failure: if the default `sheet`/`sheet_content` tables also exist (from a prior default run in the same schema), this validator validates the WRONG tables and can report OUTPUT VALIDATION PASSED while the custom-named tables are never checked. The loud case (default tables absent → `SQLAlchemyError` → exit 1) is the benign one; the silent-pass case is the reason this rates major rather than minor. Unlike the input validator, `main()` here also does NOT call `validate_config`, so a malformed/typed table-name key is not gate-checked here either.
        - Current: module constants `SHEET_TABLE`/`CONTENT_TABLE` used everywhere; `main()` extracts only `db_name`/`db_schema`/`files` (lines 304-306) and never reads `sheet_table`/`content_table`.
        - Expected: in `main()`, read `sheet_table = config.get("sheet_table", SHEET_TABLE)` and `content_table = config.get("content_table", CONTENT_TABLE)`; call `validate_sql_identifier` on both before any interpolation (preserving the injection-safety that the constants currently provide for free); and thread both into `validate_content_leg(...)` and `validate_structured_table(...)` so all queries — including the structured-leg orphan-FK at line 245 — reference the configured names. (Optionally reuse `ingest_excel.validate_config` as an up-front shape gate, mirroring the input validator.)

2. [pending] Single-letter SQL aliases violate the best-practices descriptive-alias rule - `code/excel_ingestion/data_validation/data_val_excel_outputs.py`
   - 2.1. [minor] Lines 71-76, 84-89, 140-147, 244-247: the queries use single-letter aliases `s` (sheet), `c` (content), `t` (structured table). sql-development best-practices guideline #1 explicitly says "use descriptive aliases (no single-letter)." (The prior 20260624v01 review marked SQL "No issues found" and even praised the aliases; that was an oversight — the skill text forbids single-letter aliases.)
        - Current: `from {db_schema}.{SHEET_TABLE} s left join {db_schema}.{CONTENT_TABLE} c on c.collection_path = s.collection_path` and `from {db_schema}.{table_name} t where not exists (... s ...)`.
        - Expected: rename to descriptive aliases, e.g. `sheet_meta` / `content` / `structured` (and `sheet_meta` in the structured-leg subquery), qualifying columns accordingly.

## Skills with No Issues

1. Type Hints: No issues found. `validate_content_leg(engine: Engine, db_schema: str, expected_paths: set[str]) -> list[str]`, `validate_structured_table(engine: Engine, db_schema: str, table_name: str, collection_paths: set[str]) -> list[str]`, `table_sources: dict[str, set[str]]`, `all_paths: set[str]` all fully and specifically typed.
2. Docstrings: No issues found as written (Google-style Args/Returns on both functions; module docstring enumerates both legs' checks). Note: if item 1.1 is implemented, the function docstrings/signatures must be updated to document the new `sheet_table`/`content_table` params.
3. Comments: No issues found. The orphan-meta rationale (lines 66-68), the contiguity invariant (lines 154-155), the embedding-leg presence rationale (lines 171-173), and the derive-same-as-ingester note (lines 311-313) explain "why".
4. Logging: No issues found. `data_validation` log dir + explicit `log_name`, deferred after argparse; f-strings; run separators; PASS/FAIL per leg/table; no `print`.
5. Exception Handling: No issues found. `(tomllib.TOMLDecodeError, OSError)` on read; `KeyError` for missing top-level fields; `ValueError` around the `make_collection_path` resolution loop (its own abort + separator); `(SQLAlchemyError, ValueError)` around the DB phase. No bare except, no generic wrap.
6. Executable Scripts: No issues found. Single `--config` arg, `main()` + guard, config-existence check, deferred logging.
7. Data Validation: No issues found in coverage. `data_val_` prefix under `data_validation/`; validates BOTH legs — content: orphan-meta, orphan-content FK, `row_text` non-empty, `word_count >= 0`, `source_binary_hash` in `[0, 2^64)`, `n_rows` == content count, contiguity (1..N), and every configured sheet's `collection_path` present (resolves the prior v01 1.1 suggestion); structured: existence, rows, null-identity, orphan FK to sheet, configured-source presence. `make_collection_path` derives paths the same way the ingester does. Failures accumulate; exit 1 on any. (Item 1.1 is about WHICH tables are validated, not which invariants.)
8. Unit Tests: N/A — tests reviewed separately.
9. SQL Best Practices: One issue (item 2.1, single-letter aliases). Otherwise sound: lowercase SQL, `db_schema`/`table_name` validated via `validate_sql_identifier` before interpolation, all variable values parameterized (`:s`/`:t`/`:cp`/`:ceiling`), explicit `left join`, explicit `group by`/`having` columns. The contiguity `having min(sort_order) <> 1 or max(sort_order) <> count(*)` is sound because the PK `(collection_path, sort_order)` forbids duplicate `sort_order`.

### Verified correct (general correctness, no issue)

- Hash-range check uses `_UINT64_CEILING = 18446744073709551616` as an EXCLUSIVE upper bound (`>= :ceiling`), matching the schema CHECK `< 18446744073709551616`; also flags null and negative.
- `n_rows` check uses `count(c.sort_order)` over a `left join`, so a 0-content sheet yields `actual=0` and is caught (it also fails the orphan-meta check); the embedding-leg presence loop (lines 174-184) is set-diff `expected_paths - present` with `collection_path::text` to compare against the resolved string paths.
- Structured-leg presence: per-source `select 1 ... where collection_path = :cp limit 1` parameterized; existence pre-check via `information_schema.tables` with `:s`/`:t` params; `db_schema` and `table_name` are `validate_sql_identifier`-guarded before any f-string interpolation.

## Status & Next Steps

**Current Status**: REVIEWED (v01, 2026-06-26). The prior v01 suggestions are resolved (embedding-leg configured-source presence added; schema-wide scope accepted by design). Two new findings: [major] hardcoded consolidated table names diverge from the now-configurable `sheet_table`/`content_table` (silent-wrong-validation risk), and [minor] single-letter SQL aliases that the prior review missed.
**Completed**:
1. Re-reviewed against all python-development core skills and sql-development best-practices.
2. Verified against `ingest_excel.py` that `sheet_table`/`content_table` are configurable top-level keys (validated in `validate_config`, read in `main()` at lines 478-479, threaded into DDL/writes/FK) while this validator hardcodes them.
3. Verified both legs' invariants, the hash range, contiguity, presence loops, and identifier/parameter injection-safety.
**Next Steps**:
1. (Major, item 1.1) Read + `validate_sql_identifier` + thread `sheet_table`/`content_table` from config into both validation functions; update their signatures/docstrings.
2. (Minor, item 2.1) Replace single-letter SQL aliases with descriptive ones per best-practices #1.
**Blockers**:
1. None
**Notes**:
1. The most important finding is 1.1: a custom-named run is unvalidatable, and if default tables also exist the validator can PASS while the actual tables go unchecked.
2. Implementing 1.1 must keep the identifier guard before interpolation so the dynamic names remain injection-safe (the constants provide this for free today).
