---
name: cr-test_file_parser
goal: Verify the default-assertion switch (save_as_markdown -> save_as_json) and multi-format tests in code/file_ingestion/unit_tests/test_file_parser.py align with the unit-tests skill.
created: 2026-06-26 00:00:00
updated: 2026-06-26 00:00:00
---

## Summary

Reviewed the current state after the default-path tests were switched to assert on `save_as_json`. The rename `test_default_output_formats_is_json` (lines 143-170) and the two default-path assertions are correct: `test_single_file_success_returns_filename` (line 66) and `test_default_output_formats_is_json` (lines 167-170) now assert `save_as_json.assert_called_once()` and explicitly assert `save_as_html`/`save_as_markdown` were NOT called, which validly pins the new `["json"]` default. The multi-format export tests still validly exercise the retained capability by passing explicit formats including markdown: `test_multiple_output_formats_produces_multiple_files` (lines 187-215, `["markdown","html","json"]`), `test_text_format_uses_export_to_text` (lines 221-250), and the json+markdown test (lines 465-496). Docling is genuinely mocked via `_make_writing_doc`, the cleanup-on-failure and missing-method branches are tested, and the suite measures 100% coverage of `file_parser.py` (verified, 19 tests in this file). One stale-naming finding: a test still calls the `["json","markdown"]` combination the "default" in its name and docstring, which contradicts the new single-format default.

Counts: 0 critical, 0 major, 1 minor, 0 suggestions (1 finding).

## Implementation Plan

1. [completed] Stale "default" naming after the default switch — `code/file_ingestion/unit_tests/test_file_parser.py`
   - 1.1. [minor] Lines 465-473 `test_default_pipeline_json_and_markdown_produced`: the name and docstring call the explicit `["json", "markdown"]` argument "the default", but the default `output_formats` is now `["json"]` only (and was never `["json","markdown"]` — the brief notes it went `["markdown"]` -> `["json"]`). The test body is correct and validly exercises the retained multi-format capability (it passes the formats explicitly at lines 483-488), so the test should be KEPT — only the stale "default" wording is wrong. This contradicts the project's "keep docstrings current" standard now that the default is single-format, and could mislead a future reader into thinking markdown is produced by default.
        - Current: name `test_default_pipeline_json_and_markdown_produced`; docstring `"""The ["json","markdown"] default writes both a .json and a .md file."""`
        - Expected: rename to e.g. `test_json_and_markdown_formats_produce_both_files`; docstring e.g. `"""Requesting ["json","markdown"] writes both a .json and a .md file."""` (drop "default").

## Skills with No Issues

1. Unit Tests (pytest, not unittest): No issues — pytest throughout.
2. Unit Tests (file naming): No issues — `test_file_parser.py` matches `file_parser.py`.
3. Unit Tests (function naming `test_<function>_<scenario>_<expected>`): One stale name (1.1); all others are descriptive and predictable (e.g. `test_default_output_formats_is_json`, `test_missing_save_as_method_raises_runtime_error_and_cleans_temp`).
4. Unit Tests (single behavior / AAA): No issues — each test is focused and clearly arranged into setup/act/verify.
5. Unit Tests (mock external boundaries only, patch where used): No issues — Docling is mocked at the lazy import sites; `_make_writing_doc` (lines 17-34) models Docling's write-then-rename contract so the atomic `os.replace` is genuinely exercised rather than tautologically mocked.
6. Unit Tests (pytest utilities — parametrize, `pytest.raises(match=)`): No issues — `@pytest.mark.parametrize` for backends and `do_ocr`; `pytest.raises(..., match=...)` for every error case (invalid format, empty formats, invalid pdf_backend, missing file, export failure, missing save_as method).
7. Unit Tests (built-in fixtures): No issues — `tmp_path` and `caplog` used; no manual temp handling.
8. Unit Tests (no shared state / no private-attribute assertions): No issues — tests are independent; assertions target return values, on-disk files, and mock call records.
9. Unit Tests (comprehensive coverage): Satisfied — `file_parser.py` measures 100% (verified). The default-`["json"]` path, the multi-format path (incl. markdown), the `text`/`yaml` format paths, the missing-method defensive branch, the cleanup-on-failure branch, and the pdf-only-log `else ""` branch are all exercised. Default-assertion strength: `test_default_output_formats_is_json` correctly adds negative assertions (`save_as_html`/`save_as_markdown` not called), so the switch is pinned, not merely implied.
10. Type Hints: No issues — every test method and `_make_writing_doc` carry full parameter and `-> None` / `-> MagicMock` annotations using modern syntax.
11. Docstrings: One stale docstring (1.1); all others are Google-style and current.
12. Comments: No issues — inline comments are sparse and explanatory (e.g. the whole-dir cleanup assertion rationale at lines 414-417, the force-attribute-absent note at lines 446-447).
13. Logging: N/A — test file; consumes logs via `caplog`.
14. Exception Handling: N/A — exception paths asserted via `pytest.raises`.
15. SQL Development: N/A — no SQL.

## Status & Next Steps

**Current Status**: Review complete. One minor stale-naming finding; the default-assertion switch and multi-format tests are otherwise correct. Suite passes; `file_parser.py` at 100% coverage.
**Completed**:
1. Read the unit-tests skill and the prior `20260622v01_cr_test_file_parser.md` review.
2. Verified the renamed default test and the two switched default-path assertions (`save_as_json`, with negative assertions on markdown/html).
3. Confirmed the multi-format tests still validly exercise the retained capability by passing explicit formats including markdown.
4. Ran `--cov=file_parser --cov-report=term-missing` (100%; `.coverage` removed afterward).
**Next Steps**:
1. Address 1.1 (rename and drop "default" from the json+markdown test); keep the test body unchanged.
**Blockers**:
1. None.
**Notes**:
1. The retained generic multi-format capability is intentional (library boundary); the multi-format tests are correct and were not flagged.
2. No source/test files were modified and nothing was committed.
