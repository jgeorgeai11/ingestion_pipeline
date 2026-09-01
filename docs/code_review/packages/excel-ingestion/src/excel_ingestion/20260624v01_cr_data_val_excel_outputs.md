---
name: cr-data_val_excel_outputs
goal: Address code quality issues identified in code/excel_ingestion/data_validation/data_val_excel_outputs.py to align with python-development and sql-development skills.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Embedding leg lacks a configured-source-presence check - `code/excel_ingestion/data_validation/data_val_excel_outputs.py`
   - 1.1. [suggestion] Lines 49-168 vs 235-247: the structured leg verifies every configured source `collection_path` is present (lines 235-247), but `validate_content_leg` performs only schema-wide integrity checks (orphans, hashes, contiguity, `n_rows` match) — it never asserts that each ALWAYS-written sheet's `collection_path` actually landed in `sheet`. A sheet that silently skipped or failed on the universal embedding leg would not be caught here. The task's outputs spec lists source-presence only for the structured leg, so this is a reviewer value-add, not a spec gap.
        - Current: `validate_content_leg(engine, db_schema)` takes no expected-source set
        - Expected: (optional) pass the full set of derived `collection_path`s (every sheet, not just table-bound ones) and assert each has a `sheet` row, mirroring the structured-leg presence loop

2. [accepted] `validate_content_leg` is schema-wide, not config-scoped - `code/excel_ingestion/data_validation/data_val_excel_outputs.py`
   - 2.1. [suggestion] Lines 60-165: every content-leg query scans the whole `sheet`/`sheet_content` table rather than the `collection_path`s from this config. This is sound as a global integrity check (and catches cross-config corruption), but means a different config's rows can fail this run. Acceptable by design; noting it so the schema-wide scope is an explicit choice.
        - Current: `select count(*) from {db_schema}.{CONTENT_TABLE} ...` (no path filter)
        - Expected: (optional) scope integrity checks to the configured `collection_path`s if per-config isolation is ever desired

## Skills with No Issues

1. Type Hints: No issues found - `validate_content_leg(engine: Engine, db_schema: str) -> list[str]`, `validate_structured_table(..., collection_paths: set[str]) -> list[str]`, `table_sources: dict[str, set[str]]` all fully and specifically typed.
2. Docstrings: No issues found - module docstring enumerates both legs' checks; each function has Google-style Args/Returns.
3. Comments: No issues found - the contiguity invariant (lines 149-150), the orphan-meta rationale (lines 61-63), and the derive-same-as-ingester note (lines 291-292) explain "why".
4. Logging: No issues found - correct `data_validation` log dir + explicit `log_name`, deferred after argparse, f-strings, run separators, PASS/FAIL per leg; no `print`.
5. Exception Handling: No issues found - `tomllib.TOMLDecodeError, OSError` on read; `KeyError` for missing top-level fields; `ValueError` around `make_collection_path` resolution (with its own abort + separator); `SQLAlchemyError, ValueError` around the DB phase. No bare excepts.
6. Executable Scripts: No issues found - single `--config` arg, `main()` + guard, config-existence check, deferred logging.
7. Data Validation: No issues found - `data_val_` prefix under `data_validation/`; validates BOTH legs (content: FK/cascade, orphans, row_text non-empty, word_count, contiguity, hash range, n_rows match; structured: existence, rows, null-identity, orphan FK to sheet, configured-source presence); failures accumulate, exit 1 on any. `make_collection_path` derives paths the SAME way the ingester does (same util, same override arg), satisfying the parity requirement.
8. Unit Tests: N/A - tests reviewed separately.
9. SQL Best Practices: No issues found - lowercase SQL, `db_schema`/`table_name` validated via `validate_sql_identifier` before interpolation, all variable values parameterized (`:s`/`:t`/`:cp`/`:ceiling`), explicit left joins with aliases, explicit `group by`/`having` columns; the contiguity `having min<>1 or max<>count(*)` is sound because the PK `(collection_path, sort_order)` forbids duplicates.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1: validate_content_leg now takes the full set of configured collection_paths and asserts each has a sheet row (one query + set-diff), matching the structured leg's presence guarantee. 2.1 accepted as-is (schema-wide integrity is an intentional design choice). Output validator still exits 0 on qpp_cm.
**Completed**:
1. Verified both legs are validated and the structured leg checks existence/rows/null-identity/orphan-FK/source-presence.
2. Verified the content leg checks FK/cascade integrity, contiguity (1..N), hash range `[0, 2^64)`, and `n_rows` == content count.
3. Verified failure accumulation and exit codes, and that `make_collection_path` derivation matches the ingester (same util + override).
4. Verified SQL-injection safety: validated identifiers, parameterized values, ltree-bound `collection_path`.
**Next Steps**:
1. (Suggestion) Optionally add a configured-source-presence check to the embedding leg so a silently-skipped universal-leg sheet is caught.
**Blockers**:
1. None
**Notes**:
1. The contiguity check is correct (no duplicate `sort_order` possible under the composite PK); the hash-range check correctly uses `_UINT64_CEILING` as an exclusive bound matching the schema CHECK.
2. Most important finding: item 1.1 (suggestion) - the embedding leg, unlike the structured leg, does not assert configured-source presence.
