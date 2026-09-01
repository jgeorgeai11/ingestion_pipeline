---
name: cr-test_download_cms_iom
goal: Address code quality issues identified in code/data_acquisition/cms_iom/unit_tests/test_download_cms_iom.py to align with python-development skills.
status: active
created: 2026-03-04 00:00:00
updated: 2026-03-04 00:00:00
---

## Implementation Plan

1. [pending] Fix type hint issues - `code/data_acquisition/cms_iom/unit_tests/test_download_cms_iom.py`
   - 1.1. [major] Lines 177, 202, 216, 242, 309, 329, 350, 370, 400, 413, 431: Incorrect type annotation for `mocker` parameter
        - Current: `mocker: pytest.fixture`
        - Expected: `mocker: MockerFixture` (with `from pytest_mock import MockerFixture` at top of file)
        - `pytest.fixture` is a decorator, not a type. The correct type for the pytest-mock `mocker` fixture is `pytest_mock.MockerFixture`.

2. [pending] Fix import placement - `code/data_acquisition/cms_iom/unit_tests/test_download_cms_iom.py`
   - 2.1. [minor] Lines 268, 281, 289: `from bs4 import BeautifulSoup` imported inside test methods instead of at top of file
        - Current: `from bs4 import BeautifulSoup` inside `test_extracts_titles_from_table`, `test_returns_empty_when_no_table`, and `test_strips_title_prefix`
        - Expected: Move `from bs4 import BeautifulSoup` to the top-level imports alongside other third-party imports
        - The import is repeated in three separate test methods. Since `BeautifulSoup` is already a project dependency used by the source module, it should be imported once at the top of the file.

3. [pending] Fix sys.path usage - `code/data_acquisition/cms_iom/unit_tests/test_download_cms_iom.py`
   - 3.1. [minor] Line 9: Relative path in `sys.path.insert` is fragile
        - Current: `sys.path.insert(0, "code/data_acquisition/cms_iom")`
        - Expected: `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`
        - The relative path only works when tests are run from the project root. Using `__file__` to compute the path makes the import work regardless of the working directory.

## Skills with No Issues

1. Docstrings: No issues found
2. Comments: No issues found
3. Logging: N/A - test files do not require logging setup
4. Exception Handling: N/A - test files rely on pytest to handle exceptions
5. Executable Scripts: N/A - not an executable script
6. Data Validation: N/A - not a data validation script

## Status & Next Steps

**Current Status**: Review complete, pending implementation
**Completed**:
1. Code review analysis of test_download_cms_iom.py against all python-development skills
**Next Steps**:
1. Fix `mocker` type annotations across all test methods (item 1.1)
2. Move `BeautifulSoup` import to top of file (item 2.1)
3. Update `sys.path.insert` to use `__file__`-based path (item 3.1)
**Blockers**:
1. None
**Notes**:
1. The test file is well-structured overall, with good use of pytest classes, parametrize, fixtures, and the Arrange-Act-Assert pattern
2. Test naming is descriptive and test coverage spans core functionality including edge cases and deduplication
