---
name: cr-excel_parser
goal: v01 (2026-06-26) review of code/excel_ingestion/excel_parser.py current state, after the v01/v02 findings (workbook-open wrapping, bare-list type hints, _cell_value coercion note) were resolved. Whole-file pass against python-development skills and correctness, focused on header/bounds resolution, Excel-letter column bounds, missing/duplicate-header rejection, openpyxl usage, BadZipFile wrapping, deterministic dedup, and type/None handling.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

No [critical], [major], [minor], or [suggestion] findings against the current
state. The file is clean against the python-development core skills and for
correctness. The items below are recorded only as verification context; none
require a change.

1. [pending] [suggestion] (optional, no change recommended) `data_end_row` lower-bound message wording — `code/excel_ingestion/excel_parser.py`
   - 1.1. Lines 184-188: the guard `if explicit_end and data_end_row < data_start_row`
        rejects only the strictly-less case, so a single data row
        (`data_end_row == data_start_row`) is allowed and the "must be greater
        than or equal to data_start_row" message is accurate. This was already
        noted (and accepted) in the v02 review; no change is needed. Listed here
        only so the snapshot is complete.
        - Current: message reads "must be greater than or equal to data_start_row".
        - Expected: unchanged (message already matches the `<` comparison).

## Skills with No Issues

1. **Type Hints**: No issues found. The two bare `list` hints flagged in v02 are
   now parameterized: `_first_duplicate(items: list[str | None]) -> str | None`
   (line 347) and `_auto_data_end_row(all_rows: list[tuple[Cell, ...]], ...)`
   (lines 357-362). `Cell` is imported (line 31). `parse_sheet` keeps the precise
   return `tuple[list[str], list[dict[str, str | int | None]]]`; the keyword-only
   bounds are `int | None` / `str | None`; `_column_index -> int`;
   `_extract_column_names -> list[str | None]`; `validate_column_letter -> str`.
   The public `list[str]` return is accurate because `_validate_headers` (line
   234) raises on any `None` before the names are returned.

2. **Docstrings**: No issues found. Google-style Args/Returns/Raises throughout.
   The module docstring (lines 1-23) documents the default-resolution rules and
   the rejection contract. `parse_sheet`'s `Raises` (lines 157-163) enumerates the
   open-failure, invalid-letter, invalid-bound, missing-header, and collision
   modes. `_cell_value` (lines 393-407) retains the non-string `str()` coercion
   note added in v01.

3. **Comments**: No issues found. Load-bearing "why" comments are accurate: lines
   327-328 (strip-duplicate defends the row-dict keying), lines 336-337
   (normalize-duplicate defends the `col_*` identifier space), line 252-253 (the
   `cell_idx = start_idx + j` span alignment), line 292 (auto-span stop at first
   blank), lines 246-247 (end-inclusive slice).

4. **Logging**: No issues found. Library-module pattern (`get_logger`, no
   `setup_logging`); INFO for milestones (parse start with all resolved bounds,
   lines 195-201; parsed result, line 260), DEBUG for intermediate detail (header
   column count line 236, auto `data_end_row` line 243). f-strings with context;
   no entering/exiting noise.

5. **Exception Handling**: No issues found. Both `load_workbook` calls catch the
   specific tuple `(OSError, ValueError, zipfile.BadZipFile, InvalidFileException)`
   and wrap into a domain `ValueError` with `from e` (lines 107-112, 207-213); the
   sheet-lookup `KeyError` is wrapped with `from e` (lines 216-220);
   `validate_column_letter` and the resolved-bound checks raise `ValueError` with
   the offending value and field label. The `wb = None` + `if wb is not None`
   guard prevents a secondary `NameError` in `finally`. The new rejection
   `ValueError`s are caught by the orchestrator's per-sheet `(FileNotFoundError,
   ValueError)` handler (ingest_excel.py lines 523).

6. **Executable Scripts**: N/A — library module, no `main()`/argparse.

7. **Data Validation**: N/A — not a `data_val_*` script.

8. **Unit Tests**: N/A — reviewed separately.

### Verified correct (scrutinized — no issue)

- **Header/bounds resolution order** (lines 170-193): `header_row` defaults to 1,
  `data_start_row` to `header_row + 1`, `start_idx` from `start_col` (default
  "A"). The relational checks run after resolution: `data_start_row <= header_row`
  can only fire for an explicit `data_start_row` (the `header_row + 1` default
  always passes); `data_end_row < data_start_row` is gated on `explicit_end`;
  `end_idx < start_idx` is gated on `end_idx is not None`. Column-letter
  validation happens inside `_column_index` before any index arithmetic.

- **Excel-letter column bounds** (lines 56-79): `_COLUMN_LETTER_RE = [A-Za-z]+`
  with `fullmatch` rejects empty, mixed-with-digits, and whitespace labels; the
  `isinstance(col, str)` guard precedes the regex. `column_index_from_string(
  col.upper()) - 1` is the correct 0-based conversion and case-folds lowercase
  input. Validity is checked before conversion.

- **Missing/duplicate-header rejection after normalization** (lines 298-344):
  blank headers in an explicit span are collected positionally and rejected with
  their column letters (lines 319-325). Two duplicate checks run and are
  non-redundant: `_first_duplicate(column_names)` on stripped raw headers defends
  `parse_sheet`'s row-dict keying (lines 251-255); `_first_duplicate(normalized)`
  on the normalized names defends the `col_*` SQL identifier space (e.g.
  `Code`/`code` -> `col_code`). Degenerate identical headers (`"###"`/`"###"`)
  normalize to distinct `col_0`/`col_1`, so only the strip check catches those —
  both checks are required. `_first_duplicate` is order-deterministic (insertion
  order, returns the first repeat).

- **63-byte identifier cap (context, lives in `_utils`)**: `normalize_column_name`
  caps the `col_*` name at `MAX_IDENTIFIER_LENGTH = 63` via `[:63]` slicing. The
  body is produced by `_NON_ALNUM_RE = [^a-z0-9]+` after `.lower()`, so the name
  is pure ASCII `[a-z0-9_]` and 63 chars == 63 bytes — the char cap IS the byte
  cap. `excel_parser` only invokes `normalize_column_name` for collision
  detection, so the cap is correct upstream context, not a finding here.

- **openpyxl usage**: `load_workbook(filepath, read_only=True)` with a
  `wb.close()` in `finally`; `list(ws.rows)` materialized once (line 221); cells
  read by index with `cell_idx < len(row)` bounds guards (lines 256, 285, 387).

- **BadZipFile/open-failure wrapping**: both open paths wrap into `ValueError`
  (see Exception Handling). `InvalidFileException` and `zipfile.BadZipFile` (a
  subclass of `Exception`, not `ValueError`) are explicitly in the catch tuple, so
  a corrupt workbook is recorded as a parse failure rather than aborting the run.

- **Deterministic column dedup**: `_extract_column_names` preserves sheet order;
  the actual `col_*` dedup happens in `structured_table.build_column_mapping`
  (reviewed separately) over the same ordered `column_names`.

- **Type/None handling**: `_cell_value` returns `None` for `cell.value is None` and
  for whitespace-only cells (`str(val).strip()` -> empty). `_extract_column_names`
  returns `None` (not a skip) for a blank in an explicit span so positional
  alignment to the configured span is preserved and the caller can reject;
  `i < len(header_cells)` handles a ragged header row. `_auto_data_end_row` clamps
  `upper = min(end_excl, len(row))` so `row[c]` never indexes past a ragged row,
  and returns the empty-range `data_start_row - 1` when no content row exists.

- **`_utils` import / sys.path** (lines 35-36): `sys.path.insert(0, str(Path(
  __file__).resolve().parent))` puts excel_ingestion's own directory first so
  `from _utils import normalize_column_name` resolves to the sibling
  `excel_ingestion/_utils.py`. No circular import.

## Status & Next Steps

**Current Status**: REVIEW COMPLETE — CLEAN. No critical/major/minor findings; one
optional suggestion (1.1) recommends no change. All v01 findings (workbook-open
wrapping into ValueError, reconciled `Raises`, `_cell_value` coercion note) and v02
findings (the two bare `list` hints) are confirmed resolved in the current source.

**Completed**:
1. Read all python-development core sub-docs and the SQL best-practices doc.
2. Read the three prior reviews and skipped their resolved findings; confirmed each
   is fixed on disk (parameterized `list` hints at lines 347/357; the
   `(OSError, ValueError, zipfile.BadZipFile, InvalidFileException)` wrap at lines
   107/207; the `_cell_value` coercion note at lines 396-400).
3. Reviewed the whole current file against the skills and for correctness
   (header/bounds resolution, Excel-letter column bounds, missing/duplicate-header
   rejection after normalization, the 63-byte cap context, openpyxl usage,
   BadZipFile wrapping, deterministic dedup, type/None handling).
4. Verified the parser against its caller (`ingest_excel.py` lines 514-533): all
   five optional bounds are passed and the per-sheet handler catches
   `(FileNotFoundError, ValueError)`, which covers every rejection path.

**Next Steps**:
1. None required.

**Blockers**:
1. None.

**Notes**:
1. The 63-byte cap and the `col_*` dedup live in `_utils`/`structured_table`, not
   in `excel_parser`; the parser only uses `normalize_column_name` for collision
   detection. Both are sound (see the structured_table review for the dedup leg).
