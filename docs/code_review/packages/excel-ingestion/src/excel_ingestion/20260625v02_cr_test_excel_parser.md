---
name: cr-test_excel_parser
goal: Address coverage and assertion gaps in the new optional-bounds / bad-header tests in code/excel_ingestion/unit_tests/test_excel_parser.py to align with the unit-tests skill.
created: 2026-06-25
updated: 2026-06-25
---

## Implementation Plan

1. [completed] Uncovered + unasserted parser branches - `code/excel_ingestion/unit_tests/test_excel_parser.py`
   - 1.1. [major] `end_col < start_col` is the one genuinely uncovered line. `excel_parser.py:189-193` raises `ValueError("end_col (...) must be at or after start_col ...")`; `--cov-report=term-missing` flags line 190 as the sole missed line in `excel_parser.py`. This is a documented `Raises:` contract (parse_sheet docstring: "the (resolved) row/column bounds are invalid") with no test. The v01 pass classified the equivalent "documented `Raises:` with no test" as [major] (items 1.1/1.2), so the same bar applies.
        - Current: no test passes `end_col` before `start_col`.
        - Expected: add `test_parse_sheet_end_col_before_start_col_raises` building any 2+ column sheet and `parse_sheet(..., start_col="C", end_col="A")`, asserting `pytest.raises(ValueError, match="at or after start_col")`.
        - Rationale: unit-tests 7.1 — every documented error condition needs a test; this is the only uncovered line in the source under review.
   - 1.2. [minor] Auto `data_end_row` on a header-only sheet (zero data rows) is unasserted. `_auto_data_end_row` (`excel_parser.py:380,387`) returns `data_start_row - 1` (an empty range) when no data row has content; both lines always execute so line coverage shows green, but no parser test exercises the empty-span path. `test_parse_sheet_auto_data_end_row` (line 313) and `..._includes_footer` (line 328) both have content rows; the zero-row case is only covered with an *explicit* `data_end_row` in `test_ingest_excel.py::test_pipeline_skips_zero_row_sheet`, never via auto-resolution.
        - Current: no parser test runs `parse_sheet` with defaults on a header-only sheet.
        - Expected: add a test on a workbook with a header row and no data rows; assert `parse_sheet(filepath, "S")` returns the headers and `rows == []`.
        - Rationale: unit-tests 7.1 — boundary value (empty data span) the `last = data_start_row - 1` branch exists for; line coverage cannot reveal it.
   - 1.3. [minor] `data_start_row` defaulting relative to a non-1 `header_row` is unasserted. `excel_parser.py:171-172` resolves `data_start_row = header_row + 1`; the lines execute but only `test_parse_sheet_defaults` (line 295) exercises the default, and it uses `header_row=1`. Every offset-header test (e.g. `test_parse_sheet_offset_header`, line 107) passes `data_start_row` explicitly, so "header_row=2, data_start_row omitted -> data starts at row 3" is never proven.
        - Current: no test omits `data_start_row` while passing a non-1 `header_row`.
        - Expected: add a test with `parse_sheet(..., header_row=2)` (data_start_row omitted) on a sheet whose row 1 is a title, row 2 the header, row 3+ data; assert the first data row is row 3's content.
        - Rationale: unit-tests 7.1 — the relative-default branch is a documented behavior (docstring: "Defaults to `header_row + 1`") proven only at header_row=1.
   - 1.4. [suggestion] Explicit span wider than the actual row width (the `else None` side of the ternary at `excel_parser.py:285`) is unasserted. `test_parse_sheet_explicit_span_missing_header_raises` (line 401) uses a width-3 row with `end_col="C"` and a blank cell *within* the row, which hits `_cell_value(...) -> None`, not the `i >= len(header_cells)` guard. An `end_col` past the populated header width is a realistic config error (over-wide span) that currently has no dedicated assertion.
        - Current: no test sets `end_col` beyond the last populated header cell.
        - Expected: a width-2 header row with `end_col="D"`; assert it is rejected with `pytest.raises(ValueError, match="Missing header")` (the trailing Nones are missing headers in the explicit span).
        - Rationale: unit-tests 7.1 — exercises the `i < len(header_cells) else None` guard that the within-row blank-cell test does not reach.

## Skills with No Issues

1. unit-tests (naming): No issues — new tests follow `test_<function>_<scenario>_<expected>` (e.g. `test_parse_sheet_start_col_offsets_span`, `test_parse_sheet_rejects_normalize_collision`).
2. unit-tests (AAA): No issues — each new test arranges a real workbook, acts via `parse_sheet`, asserts distinctly.
3. unit-tests (fixtures / mock boundaries): No issues — every new test builds a real openpyxl workbook in `tmp_path`; no mocking of the file boundary, the correct realism choice for a parser.
4. unit-tests (order independence): No issues — each test writes its own workbook; no shared state.
5. unit-tests (pytest.raises match): No issues — verified against source: `Excel column letter` (excel_parser.py:71), `Missing header` (line 323), `identical after cleaning` (line 333), `after normalization` (line 343), `No column headers found` (line 314), `at or after start_col` (line 191, the one path with no test — see 1.1). All present.
6. unit-tests (assertion strength, covered cases): No issues — `start_col`/`end_col` tests pin behavior, not just parse success: `test_parse_sheet_start_col_offsets_span` asserts `rows[0]["Code"] == "A1"` and `"row label" not in rows[0]` (proves data is read from column B); `test_parse_sheet_end_col_limits_span` asserts `"junk" not in rows[0].values()`. The v01 tautology (line 124) is resolved — `test_parse_sheet_synthetic_row_number` now asserts `set(rows[0]) == {...}` against real keys.
7. unit-tests (duplicate/normalize/leading-blank coverage): No issues — `test_parse_sheet_rejects_duplicate_header`, `..._rejects_normalize_collision`, and `..._leading_blank_header_no_longer_misaligns` each cover a distinct `_validate_headers` branch and the last asserts BOTH the rejection AND the start_col fix (`cols == ["Code", "Desc"]`, `rows[0]["Code"] == "bval"`).
8. type-hints: No issues — all fixtures and tests annotated (`tmp_path: Path`, `-> None`).
9. docstrings: No issues — every test has a one-line behavior docstring.
10. logging: No issues — `setup_logging` configured to `logs/excel_ingestion/unit_tests`.
11. sql-development: N/A — no SQL in this file.

## Status & Next Steps

**RESOLUTION (2026-06-25):** all findings addressed — parameterized the bare `list` type hints, refreshed the stale `validate_config` and input-validator docstrings, added the config-time `start_col <= end_col` check, and added 7 tests (end_col<start_col, auto empty-span, data_start-off-non-1-header, explicit-span-wider-than-row, valid/invalid column span, partial bounds). Suite 111 passed.

**Current Status**: Reviewed. Suite passes (104 tests). New behavior is broadly well covered; the gaps are one genuinely uncovered branch (end_col < start_col) and three line-coverage-invisible scenarios (auto empty-span, non-1 relative default, over-wide explicit span).
**Completed**:
1. Reviewed all 10 new tests (defaults, auto end + footer caveat, start_col/end_col span, bad column letter, explicit-span missing header, duplicate, normalize-collision, leading-blank-rejected) against excel_parser.py.
2. Ran `pytest --cov=excel_parser --cov-report=term-missing`: 99% line coverage, sole missed line 190 (end_col < start_col). Confirmed the other three gaps by reading assertions, not the report (line coverage cannot reveal ternary/scenario gaps).
3. Verified all new `match=` strings exist in source; deleted `.coverage`.
**Next Steps**:
1. Add the `end_col < start_col` rejection test (1.1).
2. Add the auto empty-span, non-1 relative-default, and over-wide explicit-span tests (1.2-1.4).
**Blockers**:
1. None.
**Notes**:
1. `term-missing` is LINE coverage; three of the four flagged gaps are intra-line ternary branches or whole scenarios the line executes via the other path, so 99% does not clear them.
2. The footer-inclusion caveat is correctly and explicitly tested (`test_parse_sheet_auto_data_end_row_includes_footer` pins `"End of worksheet"` as a data row).
