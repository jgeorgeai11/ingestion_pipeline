---
name: cr-ingest_excel
goal: Address code quality issues in code/excel_ingestion/ingest_excel.py (v01, configurable consolidated table names delta) to align with python-development and sql-development skills.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

1. [pending] No config-time guard that `sheet_table` and `content_table` are distinct (and distinct from any per-sheet `table`) - `code/excel_ingestion/ingest_excel.py`
   - 1.1. [minor] Lines 105-110 (validation), 264-269 (DDL render): each of `sheet_table` / `content_table` is individually validated as a safe SQL identifier, but nothing checks they differ from each other. If a config sets `sheet_table = "x"` and `content_table = "x"`, the rendered DDL emits two `create table if not exists {schema}.x (...)` statements; the second is a silent no-op (the table already exists from the first), so the consolidated content table is never created with its `sort_order`/`row_text`/`word_count` columns. The run then proceeds and EVERY sheet fails at the content insert (line 419 `insert into {db_schema}.{content_table} (collection_path, sort_order, row_text, word_count) ...`) as an opaque per-sheet `SQLAlchemyError` ("column does not exist"), accumulated into `failures` and exiting 1 — instead of a single clean config-time abort. The same collision class exists if `content_table` (or `sheet_table`) equals a per-sheet `table`. Verified by rendering the DDL with `sheet_table == content_table == "thing"`: both `create table` lines resolve to `s.thing`. This is the same config-completeness spirit as the `min_column_overlap` range check prior rounds added (a bad value that would otherwise surface as a deep runtime error is hoisted to a clean `ValueError` abort).
        - Current: `validate_sql_identifier(config.get("sheet_table", SHEET_TABLE), "sheet_table")` and `validate_sql_identifier(config.get("content_table", CONTENT_TABLE), "content_table")` with no cross-check
        - Expected: after both pass `validate_sql_identifier`, raise `ValueError` if the resolved `sheet_table == content_table` (and, optionally, if either collides with any per-sheet `table`), so the misconfiguration aborts cleanly at config time rather than failing every sheet at insert
   - Rationale: per executable-scripts / exception-handling, config-shape errors should funnel through the `except ValueError` abort in `main()` (lines 466-468) as a clean `sys.exit(1)`. A name collision currently escapes that path and degrades into N identical per-sheet `SQLAlchemyError`s, which is harder to diagnose than one config-level message.

2. [pending] Consolidated-leg SQL interpolates identifiers unquoted, unlike `structured_table._quote` - `code/excel_ingestion/ingest_excel.py`
   - 2.1. [suggestion] Lines 324, 379, 386, 394, 419: the consolidated-leg SQL interpolates `{db_schema}.{sheet_table}` / `{content_table}` unquoted, whereas `structured_table.py` double-quotes every interpolated identifier via `_quote`. Both are injection-safe here (`db_schema`, `sheet_table`, `content_table` all pass `validate_sql_identifier` fullmatch before reaching any interpolation — lines 100, 105-110, re-checked 257-259), so this is a consistency/style note, not a vulnerability. Carried forward from v01 item 2.1 (still describes the current code verbatim after the configurable-name change).
        - Current: `f"select 1 from {db_schema}.{sheet_table} where collection_path = :cp"`
        - Expected: (optional) mirror `structured_table._quote` for a single quoting convention across the module; low value since the identifiers are validated

3. [pending] Leftover-placeholder guard regex flags any surviving brace pair - `code/excel_ingestion/ingest_excel.py`
   - 3.1. [suggestion] Lines 273-278: `re.findall(r"\{[^}]*\}", rendered)` flags ANY surviving `{...}`. The current DDL (`sql/excel_schema.sql`) contains no literal braces, so the guard correctly catches only a template/code drift (a new unsubstituted placeholder) today. If the DDL ever gains a literal brace (an array default `'{}'`, or a plpgsql `$$ ... {} ... $$` body) this guard would false-positive and raise on otherwise-valid DDL. Latent only; carried forward from v01 item 4.1. Verified the rendered DDL has zero leftover placeholders for both default and custom names.
        - Current: `leftover = re.findall(r"\{[^}]*\}", rendered)`
        - Expected: (latent only) restrict to the known placeholder names, e.g. `\{(schema_name|sheet_table|content_table)\}`, if literal braces are ever introduced into the template

## Skills with No Issues

1. Type Hints: No issues found - all functions annotate parameters and returns with modern syntax (`str | None`, `list[dict[str, str | int | None]]`, `int`); `main() -> None`. The new `sheet_table`/`content_table` params on `ensure_consolidated_tables` (233-234), `_content_source_present` (309), and `write_consolidated` (342-343) are `str` with `SHEET_TABLE`/`CONTENT_TABLE` defaults, matching the `config.get(..., SHEET_TABLE)` values threaded from `main` (478-479).
2. Docstrings: No issues found - Google-style with Args/Returns/Raises throughout. The v02 stale-`validate_config` finding is RESOLVED: lines 71-85 now describe sheet-only-required, optional/int-guarded row bounds, the `min_column_overlap` range, the column letters, AND the new `sheet_table`/`content_table` identifier checks. `ensure_consolidated_tables` (236-255) documents the configurable names and the FK-shared-name constraint; `write_consolidated` (345-368) and `_content_source_present` (311-320) document the new params and the `-1` skip marker.
3. Comments: No issues found - comments explain "why": the SHEET_TABLE/CONTENT_TABLE defaults rationale (53-55, 102-104), the read-once-thread-through-both-legs note (474-477), the leftover-guard drift rationale (271-272), the optional-bound notes (160-161, 171-172), the embed-before-structured FK ordering (592-594).
4. Logging: No issues found - `setup_module_logging`, f-strings throughout, `"=" * 60` run separators (450, 630), deferred after argparse (446 -> 449); the config-loaded line now reports `tables={sheet_table}/{content_table}` (487) and the ensured-tables line reports both configured names (282-285); no `print`.
5. Exception Handling: No issues found - specific exception tuples (`tomllib.TOMLDecodeError, OSError` at 459; `ValueError` config abort at 466; `(ValueError, FileNotFoundError, SQLAlchemyError)` setup at 494; `(FileNotFoundError, ValueError)` parse at 523; `SQLAlchemyError` embed at 582; `(ValueError, SQLAlchemyError)` structured at 607). Config-level errors abort; per-sheet failures accumulate and continue; embed failure suppresses the dependent structured leg (592-594). (See item 1 for the one config-completeness gap that escapes the clean abort.)
6. Executable Scripts: No issues found - single `--config` argparse arg, `main()` + `if __name__ == "__main__"`, config-existence check before read (452-454), logging deferred after parse.
7. Data Validation: N/A - this is the orchestrator, not a `data_val_` script.
8. Unit Tests: N/A - tests reviewed separately.
9. SQL Best Practices: No issues found - lowercase SQL, all values parameterized (`:cp`, `:title`, `:n_rows`, `:hash`, `:structured_table`, executemany content param lists at 408-424), validated identifiers, explicit column lists on every insert. The defaults render byte-identical to the prior fixed-name SQL (verified: `{sheet_table}`->"sheet", `{content_table}`->"sheet_content" reproduces the original DDL/inserts/selects), and the content table's FK correctly references the configured `sheet_table` (verified: custom names yield `references s.wb (collection_path)`).

## Status & Next Steps

**Current Status**: REVIEWED (v01, configurable-table-names delta). The change is correct and injection-safe: both `sheet_table` and `content_table` are validated via `validate_sql_identifier` in `validate_config` (105-110) BEFORE any use, and re-validated in `ensure_consolidated_tables` (258-259) before the `.replace` interpolation. The configured value flows to the DDL render (264-269), the embed presence/select/delete/insert (324/379/386/394/419), the log lines (282-285, 427, 487), and the structured-leg FK (`write_rows(..., sheet_table, ...)` at 600 -> `ensure_table` `references {db_schema}.{sheet_table}`). Defaults reproduce the prior SQL exactly; no stray hardcoded `SHEET_TABLE`/`CONTENT_TABLE` where the configured value should flow (the constants appear only as signature defaults and `config.get` fallbacks). Three findings: one [minor] (name-collision guard), two [suggestion] (carried-forward style/latent).
**Completed**:
1. Verified both consolidated names are validated before any interpolation, and the defaults render byte-identical to the prior fixed-name SQL (scratch render test, deleted).
2. Verified the structured-leg FK uses the configured `sheet_table` (custom name -> `references s.wb (collection_path)`).
3. Verified the v02 findings are RESOLVED in the current file: the `validate_config` docstring matches the optional-bounds + column-letter + `min_column_overlap` + new-table-name behavior (71-85), and the config-time `start_col <= end_col` pair check is present (201-212).
4. Verified the leftover-placeholder guard yields zero leftovers for both default and custom names.
5. Confirmed source-hash is stored-but-not-compared by design (skip is presence-based, matching file_ingestion) — intended, not a finding.
**Next Steps**:
1. (Minor) Add a config-time check that `sheet_table != content_table` (and optionally that neither collides with a per-sheet `table`), so a name collision aborts cleanly instead of failing every sheet at insert (item 1).
2. (Suggestion) Optionally adopt `_quote` in the consolidated leg for a uniform quoting convention (item 2).
3. (Suggestion/latent) Narrow the leftover-placeholder regex to known names if literal braces are ever added to the DDL (item 3).
**Blockers**:
1. None
**Notes**:
1. The most important finding is item 1.1 (minor): equal `sheet_table`/`content_table` names cause the second `create ... if not exists` to be a silent no-op, so the content table is never provisioned and every sheet fails at the content insert as an opaque per-sheet `SQLAlchemyError` (exit 1 with N failures) rather than one clean config-time abort.
2. v02's two findings (stale `validate_config` docstring; missing `start_col <= end_col` pair check) are both resolved in the current file and are NOT re-reported here.
3. Items 2.1 and 3.1 are carried forward from v01 (skipped there); both still describe the current code verbatim after the configurable-name change and remain style/latent only.
