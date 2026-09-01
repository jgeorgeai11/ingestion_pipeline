---
name: cr-test_excel_parser
goal: Address coverage and assertion gaps in code/excel_ingestion/unit_tests/test_excel_parser.py to align with the unit-tests skill.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [completed] Missing error-path coverage - `code/excel_ingestion/unit_tests/test_excel_parser.py`
   - 1.1. [major] No test covers `parse_sheet`'s documented "no headers found" path (`excel_parser.py:145-148`, raises `ValueError("No column headers found at row ...")`) — coverage marks line 146 as missed. The `Raises:` docstring promises it; a regression that silently returns an empty column list would not be caught.
        - Expected: add a test on a workbook whose `header_row` is entirely empty, asserting `pytest.raises(ValueError, match="No column headers found")`.
        - Rationale: unit-tests 7.1 — every documented error condition needs a test; this is a `Raises:` contract with no test.
   - 1.2. [major] No test covers the `header_row` beyond the sheet's last row path (`excel_parser.py:137-141`, raises `ValueError("header_row {n} is beyond the last row ...")`) — coverage marks line 138 as missed.
        - Expected: parse a small sheet with `header_row` larger than the populated extent; assert `pytest.raises(ValueError, match="beyond the last row")`.
        - Rationale: unit-tests 7.1 — boundary value (header_row at/over the sheet extent) plus a documented `Raises:` with no test.
   - 1.3. [minor] No test covers the `load_workbook` failure re-raise in `list_sheets` (`excel_parser.py:51-53`, `except (OSError, ValueError): ... raise`) — coverage marks 51-53 as missed. A corrupt/non-xlsx file with a valid `.xlsx` name reaches this branch.
        - Expected: write a non-Excel file with an `.xlsx` suffix and assert `list_sheets` re-raises (the bare `raise` preserves the original type — assert on `(OSError, ValueError)`).
        - Rationale: unit-tests 7.1 — exception path with no coverage; this is the only documented failure mode of `list_sheets` besides FileNotFound.

2. [completed] Assertion strength - `code/excel_ingestion/unit_tests/test_excel_parser.py`
   - 2.1. [suggestion] Line 124 `assert ROW_NUMBER_KEY not in ("Product ID", "Product Name", "Unit Price")` asserts the constant `"row_number"` is unequal to three string literals — it is a tautology that does not exercise the parse result. The intended invariant (row_number is keyed separately from headers) is already proven by line 122 and by `test_parse_sheet_header_gap_terminates_header_set` (line 158).
        - Current: `assert ROW_NUMBER_KEY not in ("Product ID", "Product Name", "Unit Price")`
        - Expected: assert against the actual row keys, e.g. `assert set(rows[0]) == {"Product ID", "Product Name", "Unit Price", ROW_NUMBER_KEY}` (pins separateness against the real dict rather than literals).
        - Rationale: unit-tests 7 — an assertion that can never fail regardless of implementation adds no signal.

3. [completed] Missing boundary coverage - `code/excel_ingestion/unit_tests/test_excel_parser.py`
   - 3.1. [suggestion] No test exercises a data row that is SHORTER than the header width (the `_cell_value(row[i]) if i < len(row) else None` guard at `excel_parser.py:160`). A ragged sheet (fewer cells in a data row than headers) should yield trailing `None`s; this guard is untested.
        - Expected: a sheet whose data row has fewer populated cells than the header; assert the missing trailing columns come back as `None`.
        - Rationale: unit-tests 7.1 — ragged rows are a realistic Excel boundary the guard exists for.

## Skills with No Issues

1. unit-tests (naming): No issues — predictable `test_<function>_<scenario>_<expected>` names.
2. unit-tests (fixtures): No issues — `sample_workbook` / `gappy_header_workbook` build real `.xlsx` files via `tmp_path`; no over-mocking.
3. unit-tests (mock boundaries): No issues — the parser is tested against real openpyxl workbooks, the correct realism choice for a file-reading module.
4. unit-tests (pytest.raises match): No issues — `match=` strings ("must be greater than", "greater than or equal to", "not found", "Excel file not found") all exist in `excel_parser.py`.
5. unit-tests (keyword-only enforcement): No issues — `test_parse_sheet_keyword_only_row_args` correctly pins the `*` boundary with `TypeError`.
6. unit-tests (Arrange-Act-Assert): No issues.
7. unit-tests (order independence): No issues — each test builds its own workbook; no shared state.
8. type-hints: No issues — fixtures and tests are fully annotated.
9. docstrings: No issues.
10. logging: No issues — log dir configured correctly.
11. sql-development: N/A — no SQL in this file.

## Status & Next Steps

**Current Status**: RESOLVED. 1.1/1.2: added tests for the 'No column headers found' and 'header_row beyond the last row' ValueErrors. 1.3: added corrupt-file tests for BOTH list_sheets and parse_sheet (asserting the wrapped ValueError — also backfills the file-2 open-failure change). 2.1: replaced the tautological assert with a set-equality on the real row keys. 3.1: added a ragged-row pad-with-None test. 17 passed.
**Completed**:
1. Reviewed all happy-path and validation tests against the `excel_parser.py` source.
2. Ran coverage: confirmed lines 51-53, 138, 146 are unexecuted (the three error/empty-header paths called out above).
**Next Steps**:
1. Add the three missing error-path tests (1.1-1.3) and the ragged-row boundary test (3.1).
2. Replace the tautological assertion (2.1).
**Blockers**:
1. None.
**Notes**:
1. Happy-path coverage (bounded range, offset header, blank->None, header-gap termination) is strong; the gaps are exclusively documented error/boundary paths.
