---
name: cr-test_ingest_excel
goal: Address code quality issues identified in code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py to align with python-development skills.
status: completed
created: 2026-03-11 00:00:00
updated: 2026-03-11 00:00:00
---

## Implementation Plan

1. [completed] Fix type hint issues - `code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py`
   - 1.1. [major] Lines 36, 62, 83: Parametrized test functions missing return type annotation
        - Current: `def test_excel_col_to_sql_generic_conversion(excel_col: str, expected_sql: str):`
        - Expected: `def test_excel_col_to_sql_generic_conversion(excel_col: str, expected_sql: str) -> None:`
        - All test functions should have `-> None` return type annotations per the type-hints skill ("All functions require type hints - Parameters and return types").
   - 1.2. [major] Lines 91, 132, 158, 196, 211, 229, 262, 293, 330: Test functions using fixtures are missing type annotations for fixture parameters
        - Current: `def test_toml_config_parsing_dict_format(tmp_path):` and `def test_populate_measures_executes_upsert_for_each_entry(mocker):`
        - Expected: `def test_toml_config_parsing_dict_format(tmp_path: Path) -> None:` (with `from pathlib import Path`) and `def test_populate_measures_executes_upsert_for_each_entry(mocker: MockerFixture) -> None:` (with `from pytest_mock import MockerFixture`)
        - `tmp_path` should be typed as `Path`, `mocker` should be typed as `MockerFixture`, and `caplog` should be typed as `pytest.LogCaptureFixture`. All functions need `-> None`.

2. [completed] Fix import placement - `code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py`
   - 2.1. [minor] Lines 93, 135: `import tomllib` imported inside test methods instead of at top of file
        - Current: `import tomllib` inside `test_toml_config_parsing_dict_format` and `test_toml_config_overwrite_defaults_false`
        - Expected: Move `import tomllib` to the top-level imports alongside other standard library imports
        - The import is repeated in two separate test methods. Since `tomllib` is a standard library module (Python 3.11+), it should be imported once at the top of the file.

3. [completed] Fix sys.path usage - `code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py`
   - 3.1. [minor] Line 8: Relative path in `sys.path.insert` for logconfig is fragile
        - Current: `sys.path.insert(0, ".claude/skills/python-development/scripts")`
        - Expected: `sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude/skills/python-development/scripts"))`
        - The relative path only works when tests are run from the project root. Using `__file__` to compute the path makes the import work regardless of the working directory. Note that `conftest.py` has the same issue with its `sys.path.insert(0, "code/excel_ingestion")` but that is outside the scope of this review.

4. [completed] Reduce mock setup duplication - `code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py`
   - 4.1. [minor] Lines 160-163, 198-201, 211-216, 231-236, 264-269, 296-301, 351: Repeated mock engine/connection boilerplate across multiple tests
        - Current: Each test manually builds `mock_engine`, `mock_conn`, and wires `__enter__`/`__exit__` methods
        - Expected: Extract a shared fixture in `conftest.py` (or at module level with `@pytest.fixture`) that returns a `(mock_engine, mock_conn)` tuple
        - The same 4-6 lines of mock setup are repeated in 7 tests. A fixture would reduce duplication and make tests easier to maintain per the unit-tests skill ("Use fixtures - Share common test data via conftest.py").

5. [completed] Add `logging` import to top-level or remove unused reference - `code/excel_ingestion_qpp_cm/unit_tests/test_ingest_excel.py`
   - 5.1. [info] Line 3: `import logging` is used only by `test_insert_rows_logs_error_for_missing_column` (line 308, 322)
        - This is not a violation but worth noting: the `logging` import is used, so it is correctly placed at the top level. No action needed.

## Skills with No Issues

1. Docstrings: No issues found - all test functions have single-line docstrings describing the expected behavior
2. Comments: No issues found - section comments clearly separate test groups, inline comments explain verification logic
3. Logging: No issues found - `setup_logging` is called correctly with appropriate `log_dir` and `log_name` values matching the module path convention
4. Exception Handling: N/A - test files rely on pytest to handle exceptions
5. Executable Scripts: N/A - not an executable script
6. Data Validation: N/A - not a data validation script
7. Unit Tests (structural): No issues found - tests follow Arrange-Act-Assert pattern, use `@pytest.mark.parametrize` for data-driven tests, mock external boundaries (DB engine), and test names are descriptive

## Status & Next Steps

**Current Status**: All code review items implemented. All 33 tests pass.
**Completed**:
1. Code review analysis of test_ingest_excel.py against all python-development skills
2. Added `-> None` return type and fixture parameter type annotations to all 12 test functions (items 1.1, 1.2)
3. Moved `import tomllib` to top-level imports, removed duplicate in-function imports (item 2.1)
4. Updated `sys.path.insert` to use `__file__`-based absolute path (item 3.1)
5. Extracted `mock_db` fixture returning `(mock_engine, mock_conn)` tuple, refactored 7 tests to use it (item 4.1)
6. Item 5.1 confirmed as no-action-needed (logging import already correct)
**Next Steps**:
1. None - all items completed
**Blockers**:
1. None
