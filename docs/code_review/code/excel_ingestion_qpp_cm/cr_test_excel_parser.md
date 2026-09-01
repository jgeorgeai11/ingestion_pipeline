---
name: cr-test_excel_parser
goal: Address code quality issues identified in code/excel_ingestion_qpp_cm/unit_tests/test_excel_parser.py to align with python-development skills.
status: completed
created: 2026-03-11 00:00:00
updated: 2026-03-11 00:00:00
---

## Implementation Plan

1. [completed] Fix type hint issues - `code/excel_ingestion_qpp_cm/unit_tests/test_excel_parser.py`
   - 1.1. [major] Line 21: Missing type annotation for `tmp_path` parameter in `_create_workbook`
        - Current: `def _create_workbook(tmp_path, sheets_data: dict[str, list[list]]) -> str:`
        - Expected: `def _create_workbook(tmp_path: Path, sheets_data: dict[str, list[list]]) -> str:` (with `from pathlib import Path` at top of file)
        - All function parameters must have type annotations per the type-hints skill. `tmp_path` is a `pathlib.Path` provided by pytest.
   - 1.2. [minor] Line 21: `list[list]` value type in `sheets_data` is not fully specified
        - Current: `sheets_data: dict[str, list[list]]`
        - Expected: `sheets_data: dict[str, list[list[str | int | float | None]]]`
        - The inner `list` has no type parameter. Since worksheet rows contain mixed cell values, specifying the expected cell types improves clarity.

2. [completed] Fix sys.path usage - `code/excel_ingestion_qpp_cm/unit_tests/test_excel_parser.py`
   - 2.1. [minor] Line 8: Relative path in `sys.path.insert` is fragile
        - Current: `sys.path.insert(0, ".claude/skills/python-development/scripts")`
        - Expected: `sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude/skills/python-development/scripts"))`
        - The relative path only works when tests are run from the project root. Using `__file__` to compute the path makes the import work regardless of the working directory.
   - 2.2. [minor] Line 5 in `conftest.py`: Same fragile relative path issue in `code/excel_ingestion_qpp_cm/unit_tests/conftest.py`
        - Current: `sys.path.insert(0, "code/excel_ingestion")`
        - Expected: `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`
        - This is the shared conftest that enables `from excel_parser import ...`. It has the same fragile relative path problem. While this file is not the review target, fixing it alongside the test file ensures consistency.

3. [completed] Remove logging setup from test file - `code/excel_ingestion_qpp_cm/unit_tests/test_excel_parser.py`
   - 3.1. [minor] Lines 8-11: Test files should not call `setup_logging` at module level
        - Current: `sys.path.insert(0, ".claude/skills/python-development/scripts")` / `from logconfig import setup_logging` / `setup_logging(log_dir="logs/excel_ingestion_qpp_cm/unit_tests", log_name="test_excel_parser")`
        - Expected: Remove the `setup_logging` import and call entirely. If the source module under test uses `get_logger`, pytest's `caplog` fixture is the standard way to capture and assert on log output.
        - Test files do not require their own logging setup. The `setup_logging` call at module level creates log files as a side effect of importing the test module, which is unnecessary for unit tests.

4. [completed] Add test function type annotations - `code/excel_ingestion_qpp_cm/unit_tests/test_excel_parser.py`
   - 4.1. [minor] Lines 52, 64, 74, 83, 103, 118, 131, 147, 164, 176, 188, 200: Test functions missing return type annotation
        - Current: e.g., `def test_list_sheets_excludes_overview(tmp_path):`
        - Expected: e.g., `def test_list_sheets_excludes_overview(tmp_path: Path) -> None:`
        - All functions require type hints per the type-hints skill. Test functions should annotate `tmp_path` as `Path` and return `-> None`. The `test_list_sheets_file_not_found` function (line 74) has no parameters but still needs `-> None`.

## Skills with No Issues

1. Docstrings: No issues found - all test functions and the helper function have clear docstrings
2. Comments: No issues found - section separators and inline comments are appropriate
3. Exception Handling: N/A - test files rely on pytest to handle exceptions
4. Executable Scripts: N/A - not an executable script
5. Data Validation: N/A - not a data validation script
6. Unit Tests (patterns): No issues found - tests follow Arrange-Act-Assert, use `pytest.raises` with `match`, are independent, and have descriptive names following the `test_<function>_<scenario>` convention

## Status & Next Steps

**Current Status**: All code review findings implemented and verified (12/12 tests passing)
**Completed**:
1. Code review analysis of test_excel_parser.py against all python-development skills
2. Added `from pathlib import Path` import and full type annotations to `_create_workbook` (items 1.1, 1.2)
3. Added `-> None` return annotations and `tmp_path: Path` parameter annotations to all 12 test functions (item 4.1)
4. Replaced relative `sys.path.insert` path in conftest.py with `__file__`-based path (item 2.2)
5. Removed `setup_logging` import, call, and associated `sys.path.insert` from the test file (items 2.1, 3.1)
**Next Steps**:
1. None - all findings addressed
**Blockers**:
1. None
**Notes**:
1. The test file is well-structured with good coverage of happy paths and error cases, including header detection, filter row skipping, whitespace stripping, empty row handling, missing headers, missing sheets, None cells, and numeric-to-string conversion
2. The `_create_workbook` helper is a clean utility that avoids code duplication across tests and could be promoted to a fixture in conftest.py if other test files in this module need it
