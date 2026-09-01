---
name: cr-ingest_excel
goal: Address code quality issues in code/excel_ingestion/ingest_excel.py (v02, optional row/column bounds delta) to align with python-development and sql-development skills.
created: 2026-06-25
updated: 2026-06-25
---

## Implementation Plan

1. [completed] `validate_config` docstring is stale against the new optional-bounds behavior - `code/excel_ingestion/ingest_excel.py`
   - 1.1. [minor] Lines 67-83: the docstring no longer matches the function. It states "each sheet has the four required fields with sane row bounds" but after this commit only `sheet` is required (`_REQUIRED_SHEET_FIELDS = ("sheet",)`, line 57) and the row bounds are optional. It also omits behavior this commit added: the new Excel-letter `start_col`/`end_col` validation (lines 176-178) and the pre-existing `min_column_overlap` range check (lines 93-101). The Raises section likewise does not mention the invalid-column-letter case. Per docstrings skill #4 (keep docstrings current), a docstring that exists but describes superseded behavior is a currency defect.
        - Current: `"""... that each sheet has the four required fields with sane row bounds (data_start_row > header_row and data_end_row >= data_start_row), that db_schema and any per-sheet table are safe SQL identifiers, and that any per-sheet authored collection_path is a valid lowercase ltree. ..."""`
        - Expected: state that only `sheet` is required; that row bounds are optional and type-checked when present, with relationship checks done only for explicitly-provided pairs; that `min_column_overlap` (if present) must be a number in `[0, 1]`; and that any present `start_col`/`end_col` must be valid Excel column letters (add this to Raises).

2. [completed] Explicit `start_col`/`end_col` pair has no config-time relationship check (asymmetric with the row pair) - `code/excel_ingestion/ingest_excel.py`
   - 2.1. [suggestion] Lines 176-178 vs 154-173: when both `data_start_row` and `data_end_row` are explicit, `validate_config` relationship-checks the pair (`data_end_row >= data_start_row`) and aborts cleanly at config time. For columns it only validates each letter individually; it never checks `start_col <= end_col` even when both are explicit. An inverted explicit span (`start_col = "Z"`, `end_col = "A"`) therefore passes config validation and surfaces only later in `parse_sheet` (excel_parser.py lines 189-193) as a per-sheet parse failure. This is correct (still exit 1, run continues) but inconsistent with how the row pair is treated. Verify the asymmetry is intended; if a uniform config-time abort is wanted, add the pair check.
        - Current: only `validate_column_letter(sheet_entry[col_field], col_field)` per field; no pair comparison
        - Expected: (optional) when both `start_col` and `end_col` are present, compare their `column_index_from_string` indices and raise `ValueError` if `end_col` precedes `start_col`, mirroring the explicit row-pair check

## Skills with No Issues

1. Type Hints: No issues found - all functions annotate parameters and returns with modern syntax (`str | None`, `list[dict[str, str | int | None]]`); `main() -> None`. The `parse_sheet` call threads `header_row`/`data_start_row`/`data_end_row`/`start_col`/`end_col` via `.get()` (lines 445-449), matching the parser's `int | None` / `str | None` defaults.
2. Docstrings: One issue (item 1); all other functions retain accurate Google-style Args/Returns/Raises.
3. Comments: No issues found - the optional-bound rationale (lines 138-139), the "checked only for explicitly-provided pairs; the parser ... fully re-validates" note (lines 149-150), the simplified zero-row skip (lines 463-465), and the embed-before-structured FK ordering (lines 514-515) all explain "why".
4. Logging: No issues found - `setup_module_logging`, f-strings, `"=" * 60` run separators, deferred after argparse; the simplified zero-row skip log (lines 466-470) no longer references the now-optional `data_start_row`/`data_end_row` keys; no `print`.
5. Exception Handling: No issues found - specific exception tuples (`tomllib.TOMLDecodeError, OSError`; `ValueError`; `SQLAlchemyError`); config-level errors abort via the `except ValueError` guard (lines 401-403); per-sheet failures accumulate and continue; embed failure suppresses the dependent structured leg.
6. Executable Scripts: No issues found - single `--config` argparse arg, `main()` + `if __name__ == "__main__"`, config-existence check before read, logging deferred after parse.
7. Data Validation: N/A - this is the orchestrator, not a `data_val_` script.
8. Unit Tests: N/A - tests reviewed separately.
9. SQL Best Practices: No issues found - lowercase SQL, all values parameterized (`:cp`, `:hash`, executemany param lists), validated identifiers, explicit column lists on every insert. Unchanged by this commit.

## Status & Next Steps

**RESOLUTION (2026-06-25):** all findings addressed — parameterized the bare `list` type hints, refreshed the stale `validate_config` and input-validator docstrings, added the config-time `start_col <= end_col` check, and added 7 tests (end_col<start_col, auto empty-span, data_start-off-non-1-header, explicit-span-wider-than-row, valid/invalid column span, partial bounds). Suite 111 passed.

**Current Status**: REVIEWED (v02). The optional-bounds delta is correct: only `sheet` required; row bounds int-guarded only when present (lines 140-147); relationship checks run only for explicitly-provided pairs; `start_col`/`end_col` validated via `validate_column_letter`; bad config aborts cleanly (ValueError) while per-sheet failures accumulate. One [minor] docstring-currency finding and one [suggestion] consistency note.
**Completed**:
1. Verified `_REQUIRED_SHEET_FIELDS = ("sheet",)` and that the missing-field check (lines 131-136) only flags a missing `sheet`.
2. Verified the optional row bounds are type-guarded only when present (`if row_field in sheet_entry`), and that absent fields default in the parser.
3. Verified the `parse_sheet` call (lines 442-450) passes all five bounds via `.get()` so omitted fields arrive as `None` and the parser supplies defaults.
4. Verified no remaining hard `sheet_entry["..."]` access on optional fields; the zero-row skip log (lines 466-470) was simplified and references no optional keys.
5. Confirmed the bound-relationship "gap" is acceptable (see Notes).
**Next Steps**:
1. (Minor) Refresh the `validate_config` docstring to match the optional-bounds + column-letter + `min_column_overlap` behavior (item 1).
2. (Suggestion) Decide whether to add a config-time `start_col <= end_col` pair check for symmetry with the row pair (item 2).
**Blockers**:
1. None
**Notes**:
1. The "gap" the task asks about is safe. Walking the combinations: explicit `header_row` + omitted `data_start_row` defaults to `header_row + 1`, which is always `> header_row`, so nothing needs checking. The only invalid combo that escapes the config-time check is explicit `data_start_row` with a defaulted `header_row = 1` where `data_start_row <= 1`; the parser re-validates and raises `ValueError` (excel_parser.py lines 179-183) as a per-sheet failure (still exit 1). Every invalid combo is caught either as a clean config abort or a per-sheet parse failure - no integrity risk.
2. v01's resolved items (int guards on row bounds, `min_column_overlap` range check, files/file_entry/sheets shape guards) are present and correct; only their mention in the `validate_config` docstring lags (folded into item 1).
3. Most important finding: item 1.1 (minor) - the `validate_config` docstring describes the superseded "four required fields" contract and omits the column-letter validation this commit added.
