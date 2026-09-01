---
name: cr-ingest_excel
goal: Address code quality issues identified in code/excel_ingestion/ingest_excel.py to align with python-development and sql-development skills.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Config validation type-safety - `code/excel_ingestion/ingest_excel.py`
   - 1.1. [minor] Lines 90-127: `validate_config` assumes the TOML shapes are well-typed. `config["files"].items()` assumes `files` is a table, `file_entry["sheets"]` is iterated, and `data_start_row <= header_row` (lines 106, 112) compares row bounds without checking they are ints. A config typo (`header_row = "5"`, `files` authored as an array, a sheet entry that is not a table) raises `TypeError`/`AttributeError`, which is NOT a `ValueError` and therefore escapes the `except ValueError` abort guard in `main()` (lines 338-342), crashing with a raw traceback instead of the intended clean `sys.exit(1)`.
        - Current: `if data_start_row <= header_row:` with no prior `isinstance(..., int)` guard, and `for filename, file_entry in config["files"].items():` with no type guard on `files`/`file_entry`/`sheets`
        - Expected: Validate that `files` is a dict, each `file_entry` is a dict with a list `sheets`, and that `header_row`/`data_start_row`/`data_end_row` are ints (raising `ValueError` with context on mismatch) before the relational comparisons, so all config-shape errors funnel through the `ValueError` abort path
   - 1.2. [minor] Lines 84-127, 349-351: `validate_config` never validates `min_column_overlap`. It is read with a default in `main()` (line 349) and passed through to `structured_table.reconcile_columns`, where it is used as `r_in < min_column_overlap` (line 174). A non-numeric config value (`min_column_overlap = "0.5"`) makes that `float < str` comparison raise `TypeError` inside `write_rows` — which `main()` catches only as `(ValueError, SQLAlchemyError)` (line 468), so it escapes as an uncaught traceback rather than a clean abort. This is the one genuine gap on the "config validation completeness" axis.
        - Current: `min_column_overlap` is unvalidated; `config.get("min_column_overlap", _DEFAULT_MIN_COLUMN_OVERLAP)` flows straight to `reconcile_columns`
        - Expected: In `validate_config`, if `min_column_overlap` is present, raise `ValueError` unless it is a number (`int`/`float`) within `[0, 1]`, so a bad value aborts cleanly at config time

2. [skipped] SQL identifier interpolation consistency - `code/excel_ingestion/ingest_excel.py`
   - 2.1. [suggestion] Lines 205-209, 259-264, 267-272, 292-297: the consolidated-leg SQL interpolates `{db_schema}.{SHEET_TABLE}` / `{CONTENT_TABLE}` unquoted, whereas `structured_table.py` double-quotes every interpolated identifier (`_quote`). Both are injection-safe here (`db_schema` passes `validate_sql_identifier` fullmatch and the table names are module constants), so this is a consistency/style note, not a vulnerability.
        - Current: `f"select 1 from {db_schema}.{SHEET_TABLE} ..."`
        - Expected: Optionally mirror `structured_table._quote` for a single quoting convention across the module (low value; the identifiers here are validated constants)

3. [skipped] Embedding-leg source-presence not asserted in this script's lifecycle - `code/excel_ingestion/ingest_excel.py`
   - 3.1. [suggestion] Lines 365-491: the run accumulates `embedded`/`structured` counts and reports failures, which is correct. A skipped sheet (returns `-1`) is silently not counted as embedded and not recorded as a failure — correct by design (skip-if-present), but there is no post-run reconciliation that every non-skipped, non-failed sheet actually produced a `sheet` row. Output validation covers DB-side integrity; flagging only as a possible defense-in-depth enhancement.
        - Current: counts and failure list only
        - Expected: (optional) a final assertion that `embedded + skipped + len(failures-on-embed) == total_sheets`; low priority since the output validator is the authority

4. [skipped] Leftover-placeholder guard regex breadth - `code/excel_ingestion/ingest_excel.py`
   - 4.1. [suggestion] Lines 165-170: `re.findall(r"\{[^}]*\}", rendered)` flags ANY surviving brace pair. The current DDL (`sql/excel_schema.sql`) contains no literal braces, so the guard is correct today. If the DDL ever gains a literal brace (e.g. an array default `'{}'`, or a plpgsql `$$ ... {} ... $$` body) this guard would false-positive and raise on valid DDL.
        - Current: `leftover = re.findall(r"\{[^}]*\}", rendered)`
        - Expected: (latent only) restrict to the known placeholder names, e.g. match `\{(schema_name|sheet_table|content_table)\}`, if literal braces are ever introduced into the template

## Skills with No Issues

1. Type Hints: No issues found - all functions annotate parameters and returns with modern syntax (`str | None`, `list[dict[str, str | int | None]]`); `main() -> None`.
2. Docstrings: No issues found - Google-style with Args/Returns/Raises; `write_consolidated` documents the `-1` skip marker and the cascade behavior.
3. Comments: No issues found - comments explain "why" (skip-empty rationale lines 401-402, embed-before-structured FK ordering lines 453-454, leftover-guard rationale lines 164-165).
4. Logging: No issues found - uses `logconfig` via `setup_module_logging`, f-strings throughout, `"=" * 60` run separators, deferred after argparse; no `print`.
5. Exception Handling: No issues found - specific exception tuples (`tomllib.TOMLDecodeError, OSError`; `ValueError`; `SQLAlchemyError`); config-level errors abort, per-sheet failures accumulate and continue; embed failure correctly suppresses the dependent structured leg (lines 453-455).
6. Executable Scripts: No issues found - single `--config` argparse arg, `main()` + `if __name__ == "__main__"`, logging deferred until after parse, config-existence check before read.
7. Data Validation: N/A - this is the orchestrator, not a `data_val_` script.
8. Unit Tests: N/A - tests reviewed separately.
9. SQL Best Practices: No issues found - lowercase SQL, all values parameterized (`:cp`, `:hash`, executemany param lists), `collection_path` bound as a param into the ltree column, identifiers validated; explicit column lists on every insert.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1+1.2: validate_config now guards files/file_entry/sheets shapes, int row bounds, and min_column_overlap range — config typos funnel through the clean ValueError abort (3 regression tests added). 2.1/3.1/4.1 skipped (style/redundant-with-output-validator/latent).
**Completed**:
1. Verified executable-scripts shape, deferred logging, config-existence guard.
2. Verified `validate_config` covers required top-level + per-sheet fields, row-bound sanity, identifier safety, and authored-`collection_path` ltree validation.
3. Verified `write_consolidated` transaction: presence check, overwrite-delete (cascade), metadata insert, content executemany; `-1` skip marker handled by caller.
4. Verified SQL-injection safety (validated identifiers, parameterized values, ltree-bound `collection_path`) and the leftover-placeholder guard.
5. Verified the accumulate-and-report model and non-zero exit on any failure.
**Next Steps**:
1. (Minor) Add type guards in `validate_config` so malformed-TOML errors raise `ValueError` and hit the clean abort path.
**Blockers**:
1. None
**Notes**:
1. `build_row_text` `row.get(col) or ''` (line 196) only collapses parser `None` to empty; values are `str | None`, so this is not data loss.
2. The most important finding is 1.1 (minor): a mistyped config field bypasses the `ValueError` abort and crashes with a traceback rather than exiting cleanly.
