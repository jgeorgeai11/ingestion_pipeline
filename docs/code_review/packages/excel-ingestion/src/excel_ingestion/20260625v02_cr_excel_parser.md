---
name: cr-excel_parser
goal: v02 review of code/excel_ingestion/excel_parser.py after commit fa9d649 (optional row bounds with defaults, Excel-letter column bounds start_col/end_col, and header rejection on blank/colliding headers in the resolved span). Focus on the new logic; whole-file pass against python-development skills and correctness.
created: 2026-06-25
updated: 2026-06-25
---

## Implementation Plan

1. [completed] [minor] Parameterize the `list` type hints on the private helpers - `code/excel_ingestion/excel_parser.py`
   - 1.1. [minor] Lines 347, 357: `_first_duplicate(items: list)` and `_auto_data_end_row(all_rows: list, ...)` use the bare, unparameterized `list`. The type-hints skill requires being specific (`list[str]` not `List`/`list`). `_first_duplicate` is called both with `list[str | None]` (the raw header names, line 329) and with `list[str]` (the normalized names, line 339), so its accurate element type is `str | None`; its return is best typed `str | None` rather than `object | None` (every element is a string-or-None, never an arbitrary object). `_auto_data_end_row`'s `all_rows` is the materialized `list(ws.rows)`, whose elements are `tuple[Cell, ...]`.
        - Current: `def _first_duplicate(items: list) -> object | None:` and `def _auto_data_end_row(all_rows: list, data_start_row: int, start_idx: int, num_cols: int) -> int:`.
        - Expected: `def _first_duplicate(items: list[str | None]) -> str | None:` and `def _auto_data_end_row(all_rows: list[tuple[Cell, ...]], data_start_row: int, start_idx: int, num_cols: int) -> int:`. (`Cell` is already imported at line 31.)

2. [completed] [suggestion] Make the `data_end_row` lower-bound message match the actual `<=`/`<` comparison wording - `code/excel_ingestion/excel_parser.py`
   - 2.1. [suggestion] Lines 184-188: the guard `if explicit_end and data_end_row < data_start_row` correctly rejects only the strictly-less case (so `data_end_row == data_start_row`, a single data row, is allowed). The message reads "must be greater than or equal to data_start_row", which is accurate. This is consistent with `validate_config` in `ingest_excel.py` (lines 164-173). No change required for correctness; noting only that the parser re-validates the resolved bounds as documented (defense in depth), and the wording already matches the comparison. Optional: none.

## Skills with No Issues

1. **Type Hints**: One minor finding (1.1 — bare `list` on the two private helpers). Everything else is fully and specifically annotated with modern syntax: `parse_sheet` keeps the precise return `tuple[list[str], list[dict[str, str | int | None]]]`; the new keyword-only bounds are `int | None` / `str | None`; `_column_index -> int`; `_extract_column_names -> list[str | None]` (correctly admits `None` for blanks in an explicit span); `validate_column_letter -> str`. The task flagged the `parse_sheet` return (`tuple[list[str], ...]`) vs `_extract_column_names -> list[str | None]` — these are consistent, NOT a mismatch: `_extract_column_names` may return `None` entries, but `_validate_headers` (called immediately after at line 234) raises on any `None` before the names are used, so by the time `column_names` is returned from `parse_sheet` every entry is a non-`None` `str`. The narrower `list[str]` on the public return is therefore correct.

2. **Docstrings**: No issues found. Google-style with Args/Returns/Raises throughout. The module docstring (lines 1-23) accurately documents the new default-resolution rules and the rejection contract. `parse_sheet`'s `Raises` (lines 157-163) now enumerates the new failure modes (invalid column letter, invalid resolved bounds, blank header in an explicit span, normalization collision). `_extract_column_names`, `_validate_headers`, and `_auto_data_end_row` each document the explicit-vs-auto span behavior, the empty-span `data_start_row - 1` return, and the best-effort/footer caveat. `_cell_value` retains the non-string coercion note from v01.

3. **Comments**: No issues found, and the load-bearing "why" comments are correct. Lines 327-328 explain the strip-duplicate check defends the raw-header row-dict keying; lines 336-337 explain the normalize-duplicate check defends the `col_*` identifier space; line 252-253 explains the `cell_idx = start_idx + j` data/header span alignment; line 292 explains the auto-span stop at the first blank; lines 246-247 explain the end-inclusive slice. All match the code.

4. **Logging**: No issues found. Library-module pattern (`get_logger`, no `setup_logging`); INFO for milestones (parse start with all resolved bounds at lines 195-201, parsed result at line 260), DEBUG for intermediate detail (header column count, auto `data_end_row`). f-strings with context; no entering/exiting noise.

5. **Exception Handling**: No issues found. Specific exception tuples on both `load_workbook` calls (`OSError, ValueError, zipfile.BadZipFile, InvalidFileException`), wrapped into a domain `ValueError` with `from e`; the sheet-lookup `KeyError` is wrapped with `from e`; `validate_column_letter` and the resolved-bound checks raise `ValueError` with full context (the offending value and field label). The `wb = None` + `if wb is not None` guard prevents a secondary `NameError` in `finally`. New `ValueError`s (blank/duplicate headers) are caught by both callers, which list `ValueError` in their except tuples.

6. **Executable Scripts**: N/A — library module, not an entry point (no `main()`/argparse here).

7. **Data Validation**: N/A — not a `data_val_*` script. (`data_val_excel_inputs.py` was read only as a caller and is reviewed separately; its `parse_sheet` call passes all five optional bounds and catches `(FileNotFoundError, ValueError, InvalidFileException)`, which covers the new rejection `ValueError`s.)

8. **Unit Tests**: N/A — reviewed separately.

### Verified correct (new logic, scrutinized — no issue)

- **Default-resolution order vs bound checks** (lines 170-193): `header_row` defaults to 1, then `data_start_row` defaults to `header_row + 1`, then `start_idx` resolves from `start_col` (default "A"). The relational checks run AFTER resolution: `data_start_row <= header_row` can only fire for an explicit `data_start_row` (the default `header_row + 1` always passes); `data_end_row < data_start_row` is gated on `explicit_end`; `end_idx < start_idx` is gated on `end_idx is not None`. Column-letter validation happens inside `_column_index` before any index arithmetic. Order and guarding are correct.
- **`_column_index` / `validate_column_letter`** (lines 56-79): `_COLUMN_LETTER_RE = [A-Za-z]+` with `fullmatch` rejects empty, mixed-with-digits, and whitespace labels; the `isinstance(col, str)` guard prevents a non-string from reaching the regex. `column_index_from_string(col.upper()) - 1` is the correct 0-based conversion and case-folds lowercase input ("a" -> 0). Validity check precedes the conversion.
- **`_extract_column_names` explicit vs auto span** (lines 264-295): explicit span iterates `range(start_idx, end_idx + 1)` — end-inclusive, no off-by-one — and returns `None` (not a skip) for a blank so positional alignment to the configured span is preserved and the caller can reject. The `i < len(header_cells)` guard handles a ragged (short) header row by yielding `None` for columns past the row's extent. Auto span iterates to `len(header_cells)` and breaks at the first `None` after `start_idx`, so it never reads past the row.
- **`_validate_headers` — both duplicate checks are necessary and non-redundant** (lines 298-344, verified empirically): the strip check (`_first_duplicate(column_names)` on RAW stripped headers) defends `parse_sheet`'s row-dict keying — `row_dict[name] = ...` at lines 251-255 keys on the raw header, so two identical raw headers would collapse the dict and silently drop the earlier column's data. The normalize check (`_first_duplicate(normalized)`) defends the `col_*` SQL identifier space — distinct raw headers that collide after normalization. The `col_<index>` fallback in `normalize_column_name` CAN mask a duplicate from the normalize check: two identical degenerate headers `["###", "###"]` normalize to `["col_0", "col_1"]` (distinct, verified: `col_0`/`col_1`), so the normalize check does NOT fire — only the strip check catches them. Conversely `["Code", "code"]` both normalize to `col_code` (verified) and only the normalize check catches them. For non-degenerate identical headers both fire. Both checks are therefore required; neither is redundant. (Note: `_validate_headers` passes the span-relative `j` to `normalize_column_name`; for collision DETECTION the index scheme only needs to be internally consistent within the single `enumerate`, which it is, so the check is sound.)
- **`_auto_data_end_row`** (lines 357-387): scans `range(data_start_row - 1, len(all_rows))`, tracking the last 1-based row with any non-empty cell in `[start_idx, start_idx + num_cols)`. `upper = min(end_excl, len(row))` correctly clamps a ragged data row so `row[c]` never indexes past the row. The empty-span case returns the initial `last = data_start_row - 1`, yielding an end-exclusive empty slice at line 248. The footer/best-effort caveat is documented (lines 365-369) and surfaced to callers via the module docstring.
- **Data-read offset** (lines 248-258): `data_slice = all_rows[data_start_row - 1 : data_end_row]` is 0-based start, end-inclusive (1-based `data_end_row` used directly as the exclusive Python stop) — correct. `cell_idx = start_idx + j` reads each header's data from exactly the same absolute column as the header, so the data is aligned to the header span even when `start_col != "A"`. The `cell_idx < len(row)` guard right-pads a short data row with `None`. `row_number` is `offset + 1` (1-based positional within the data range), kept under `ROW_NUMBER_KEY` distinct from any header so it never leaks into `row_text` or the `col_*` map.
- **`_utils` import / sys.path** (lines 35-36): `sys.path.insert(0, str(Path(__file__).resolve().parent))` puts excel_ingestion's own directory first, so `from _utils import normalize_column_name` resolves to the sibling `excel_ingestion/_utils.py` (not `file_ingestion/_utils.py`, which `_utils` itself loads by explicit path under a distinct module name to avoid that very clash). No circular import: `_utils` does not import `excel_parser`.

## Status & Next Steps

**RESOLUTION (2026-06-25):** all findings addressed — parameterized the bare `list` type hints, refreshed the stale `validate_config` and input-validator docstrings, added the config-time `start_col <= end_col` check, and added 7 tests (end_col<start_col, auto empty-span, data_start-off-non-1-header, explicit-span-wider-than-row, valid/invalid column span, partial bounds). Suite 111 passed.

**Current Status**: REVIEW COMPLETE. The new logic (default resolution, column-letter bounds, blank/duplicate-header rejection, auto `data_end_row`, span-aligned data read) is correct. One minor type-hint finding and one suggestion; no critical or major issues. v01 findings remain resolved.

**Completed**:
1. Read all python-development core sub-docs and reviewed the whole file against them.
2. Scrutinized every new-logic item the task called out (default-resolution order, `_column_index`/`validate_column_letter`, `_extract_column_names` explicit/auto span and off-by-one/ragged handling, both `_validate_headers` duplicate checks, `_auto_data_end_row`, the `cell_idx` data offset, the `_utils` import).
3. Empirically confirmed the strip-vs-normalize duplicate checks are both necessary (degenerate `"###"`/`"###"` -> `col_0`/`col_1` caught only by strip; `Code`/`code` -> `col_code` caught only by normalize) via a scratch check, then removed it.

**Next Steps**:
1. Parameterize the two bare `list` hints (finding 1.1).

**Blockers**:
1. None.

**Notes**:
1. The task asked whether the `parse_sheet` return (`tuple[list[str], ...]`) conflicts with `_extract_column_names -> list[str | None]`. It does not: `_validate_headers` raises on any `None` before the names are returned, so the public `list[str]` is accurate.
2. `structured_table.write_rows` is the actual `col_*` name consumer but is out of scope and was not read; the duplicate-collision detection in `_validate_headers` is internally consistent and sound regardless of how the final names are assigned downstream.
