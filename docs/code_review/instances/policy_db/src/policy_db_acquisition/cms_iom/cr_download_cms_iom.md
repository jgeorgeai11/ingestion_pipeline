---
name: cr-download-cms-ioms
goal: Address code quality issues identified in code/data_acquisition/cms_iom/download_cms_iom.py to align with python-development skills.
status: active
created: 2026-03-04 00:00:00
updated: 2026-03-04 00:00:00
---

## Implementation Plan

1. [pending] Fix robustness issues - `code/data_acquisition/cms_iom/download_cms_iom.py`
   - 1.1. [minor] Line 239: Case-insensitive exclude matching relies on caller lowercasing patterns rather than being self-contained. If `get_chapter_pdf_links` is called directly with mixed-case patterns, matching silently fails despite the docstring claiming case-insensitive behavior.
        - Current: `if exclude_patterns and any(p in href_lower for p in exclude_patterns)`
        - Expected: `if exclude_patterns and any(p.lower() in href_lower for p in exclude_patterns)`
   - 1.2. [minor] Line 422: Filename extraction from URL does not strip query parameters. A URL like `https://example.com/file.pdf?v=2` would produce filename `file.pdf?v=2`.
        - Current: `filename = pdf_url.split("/")[-1]`
        - Expected: `filename = pdf_url.split("/")[-1].split("?")[0]`

2. [pending] Remove unused parameter - `code/data_acquisition/cms_iom/download_cms_iom.py`
   - 2.1. [minor] Line 126-127: The `request_delay` parameter in `get_manual_pages` is unused within the function body. The docstring acknowledges this ("unused but kept for interface compatibility"), but there is no interface contract requiring it since this is application code, not a library API.
        - Current: `def get_manual_pages(index_url: str, request_delay: float = 1.0) -> list[dict[str, str]]:`
        - Expected: `def get_manual_pages(index_url: str) -> list[dict[str, str]]:`

## Skills with No Issues

1. Type Hints: No issues found -- all functions have parameter and return type annotations using modern syntax
2. Docstrings: No issues found -- all public functions have Google-style docstrings with Args, Returns, and Raises sections
3. Comments: No issues found -- comments explain "why" not "what", and are accurate
4. Logging: No issues found -- uses logconfig correctly, f-strings in log calls, separators at start/end, appropriate log levels
5. Exception Handling: No issues found -- catches specific exceptions (HTTPError, KeyError), provides context in error messages, follows the executable-scripts template pattern for the outer try/except
6. Executable Scripts: No issues found -- uses main() with if __name__ guard, single --config argument, TOML config, deferred logging setup
7. Data Validation: N/A -- this is a download script, not a data validation script
8. Unit Tests: N/A -- unit tests exist at `code/data_acquisition/cms_iom/unit_tests/test_download_cms_iom.py` (reviewed separately)

## Status & Next Steps

**Current Status**: Review complete, 3 minor findings identified
**Completed**:
1. Reviewed code against all python-development core skills
**Next Steps**:
1. Implement fixes from Implementation Plan
**Blockers**:
1. None
**Notes**:
1. Overall the code is well-structured and closely follows the project skill standards
2. All findings are minor -- no critical or major issues detected
