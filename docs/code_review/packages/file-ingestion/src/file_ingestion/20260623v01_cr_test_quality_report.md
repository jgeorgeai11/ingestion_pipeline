---
name: cr-test_quality_report
goal: Address code quality issues identified in code/file_ingestion/unit_tests/test_quality_report.py to align with python-development (unit-tests, type-hints, docstrings) skills.
created: 2026-06-23 12:56:25
updated: 2026-06-23 12:56:25
---

## Summary

This is a strong, well-constructed test suite. It builds synthetic `DoclingDocument`s through the real `add_*` builders and round-trips them through `save_as_json` / `load_from_json` (matching `test_docling_section_parser.py`), so the parsed side is exercised through genuine serialization rather than stubs; the cleaned side uses realistic `CleanedDocument`-shaped dicts. Type hints and Google-style docstrings are present and correct on every helper and test, tests are independent (each builds its own fixture, no shared mutable state), and most assertions pin specific fields rather than truthiness. All 23 tests pass; measured coverage of the six check functions themselves is high (the 56% line figure is dominated by the out-of-scope CLI/reporting layer — `main`, `build_report`, `_collect_flags`, `_render_console`, `check_section_numbers`). The refinements called out in the task — blank-table vs has-body classification, non-paginated coverage n/a, single-digit exclusion in text-health, the `89.50`/`89.5` normalization, dropped-cell and blank-grid-caption table coverage, and the over-fragmentation outlier — are each covered and the assertions are real (non-tautological). The findings below are coverage gaps and assertion-strength improvements; there are no criticals or majors.

## Implementation Plan

1. [completed] Close coverage gaps in the six checks - `code/file_ingestion/unit_tests/test_quality_report.py`
   - 1.1. [completed] RESOLVED: Added `TestPageCoverage::test_caption_less_picture_page_is_image_only` (a page whose only element is a caption-less `PictureItem` classifies `image-only`, content-less, no has-body signal) and `TestPageCoverage::test_captioned_picture_page_is_has_body` (a captioned `PictureItem` is content-bearing so `has-body`, raising the content-loss signal), built with `add_picture(caption=...)` mirroring the existing table/caption tests — exercising both `PictureItem` branches of `_is_content_bearing` and the `image-only` branch of `_classify_pages`.
   - 1.1. [minor] PictureItem / `image-only` page classification is entirely untested. No fixture anywhere adds a `PictureItem`, so the `image-only` branch of `_classify_pages` (`quality_report.py:413,423`) and both picture branches of `_is_content_bearing` (`quality_report.py:365,368`) never execute. The module docstring lists `image-only` as a first-class benign page class, and the has-body invariant turns on captioned-vs-caption-less pictures. Expected: add `test_*_caption_less_picture_page_is_image_only` (caption-less picture as the page's only element classifies `image-only`, no has-body signal) and `test_*_captioned_picture_page_is_has_body` (a captioned picture is content-bearing, so `has-body`), mirroring the existing `TestPageCoverage` table/caption tests.
   - 1.2. [completed] RESOLVED: Added `TestElementTypes::test_text_bearing_unhandled_element_flags_carries_text`, which drives the real `check_element_types` OR-accumulation guard to `carries_text=True`. Empirically confirmed (via inspecting every `DocItem` subclass) that NO Docling type is both unhandled (not a Table/Picture/Text subclass, not in a handled label set) AND text-bearing, so the branch is unreachable via `add_*` builders; the test therefore feeds a minimal stub item/doc through the function's only seam (`iterate_items`) rather than a round-tripped fixture (which would silently drop a faked `text` attribute), and the test docstring records this finding to satisfy the cr's "state explicitly in the suite" fallback.
   - 1.2. [minor] The text-bearing unhandled element path (`carries_text` True) is untested. `TestElementTypes::test_flags_unhandled_type` (lines 234-254) exercises only the text-*less* `KeyValueItem`, and `carries_text` is only ever asserted `False` (line 254). The module calls a text-bearing unhandled element "the real hazard" and `_collect_flags` only surfaces text-bearing unhandled types, so the OR-accumulation guard (`quality_report.py:322`) is never driven to True. Expected: add a test that produces an unhandled element carrying text and asserts `carries_text is True`; if no such element is realistically constructible via the `add_*` builders, state that explicitly in the suite (a docstring note) rather than leaving the discriminating branch silently uncovered.
   - 1.3. [completed] RESOLVED: `TestElementTypes::test_handled_text_not_flagged` now also asserts the per-document `census` rows — `{("SectionHeaderItem", "section_header"): 1, ("TextItem", "text"): 1, ("TableItem", "table"): 1}` — so a regression in census `(type, label, count)` counting is caught alongside the unhandled-list check.
   - 1.3. [suggestion] The element-type `census` list is never asserted. `TestElementTypes` checks only the `unhandled` list; `check_element_types` also returns a `census` of `{type, label, count}` entries (`quality_report.py:324-330`) that feeds the corpus census and the console summary. Expected: in `test_handled_text_not_flagged` (lines 256-264) also assert the `census` contains the expected `(type, label, count)` rows (e.g. the heading, the text, and the table), so a regression in census counting is caught.

2. [completed] Strengthen assertions - `code/file_ingestion/unit_tests/test_quality_report.py`
   - 2.1. [completed] RESOLVED: `TestFragmentation::test_flags_over_fragmented_outlier` now asserts `set(flags) == {"out"}` to pin that ONLY the outlier is flagged and the fences did not mis-fire on the normal docs.
   - 2.1. [minor] `TestFragmentation::test_flags_over_fragmented_outlier` (lines 411-434) asserts only `"out" in flags` and never that the four normal docs are absent. The test is genuinely non-tautological (the outlier's `sections_per_page`=12 clears the collapsed upper fence of 1 while the normals at 1 do not), but it would silently pass if the fences mis-fired and flagged everything. Expected: also assert `set(flags) == {"out"}` to pin that only the outlier is flagged.
   - 2.2. [completed] RESOLVED: `TestTextHealth::test_broken_hyphenation_threshold` now adds a case at exactly 10 broken hyphenations asserting `flagged_count == 0` and `_section_text_health(...)["broken_hyphenations"] == 10`, pinning the off-by-one edge of the strict `> BROKEN_HYPHENATION_THRESHOLD` (10) guard.
   - 2.2. [minor] `TestTextHealth::test_broken_hyphenation_threshold` (lines 473-489) tests 4 (benign) and 11 (flag) but not the boundary. The guard is `> BROKEN_HYPHENATION_THRESHOLD` (10), so the discriminating edge is 10 (no flag) vs 11 (flag). Expected: add a case at exactly 10 broken hyphenations asserting `flagged_count == 0` (and ideally `broken_hyphenations == 10`) to pin the off-by-one boundary, since 4-vs-11 leaves a 6-wide untested band around the fence.
   - 2.3. [completed] RESOLVED: `TestNormalizeTokens::test_decimal_normalization` now asserts `"89.50" not in tokens` and `"92.00" not in tokens`, proving the trailing-zero rewrite happened (rather than emitting both forms).
   - 2.3. [suggestion] `TestNormalizeTokens::test_decimal_normalization` (lines 129-136) asserts `"89.5" in tokens` but never that the raw `"89.50"` is absent, so it does not fully prove the trailing-zero rewrite happened (a bug that emitted both would still pass here). The real guard is `test_number_normalization_no_false_flag`, so this is minor. Expected: add `assert "89.50" not in tokens` and `assert "92.00" not in tokens`.

3. [completed] Naming convention - `code/file_ingestion/unit_tests/test_quality_report.py`
   - 3.1. [completed] RESOLVED (accepted as documented pattern): Added a module-docstring note recording that tests follow `test_<scenario>_<expected>` and rely on the enclosing `Test<Function>` class (one class per check function) for the function-under-test segment, so the target is unambiguous without repeating the function name in every method. Chose documenting the existing consistent convention over renaming, per the cr's first option, to avoid churning the whole suite for a purely stylistic point.
   - 3.1. [minor] Several method names omit the `<function>` segment of the `test_<function>_<scenario>_<expected>` convention, leaning on the enclosing class for the function name (e.g. `test_decimal_normalization`, `test_case_insensitive` lines 129/138; `test_passes_when_covered` line 162; `test_handled_text_not_flagged` line 256). This is a single, consistent stylistic deviation rather than a per-method defect — the class-per-function grouping makes the target unambiguous. Expected: either accept the class-scoped convention as the suite's documented pattern, or align the outliers (e.g. `test_normalize_tokens_decimal_strips_trailing_zero`).

## Additional Coverage Gaps (noted, not required by the task)

1. Under-segmentation direction of Check 6 is untested. `flag_fragmentation` has symmetric reasons for `low_spp` and `high_mws` (`quality_report.py:755-764`); only the over-fragmentation direction is exercised. The task explicitly scoped only the over-fragmented outlier, so this is an additional gap, not a required miss.
2. The empty-`content_text` skip in `check_text_health` (`quality_report.py:609`) and the empty-corpus early return in `flag_fragmentation` (`quality_report.py:732`) are not directly exercised; low value, noted for completeness.

## Skills with No Issues

1. Type Hints: No issues found — every helper and test has parameter and return annotations using modern syntax (`list[list[str]]`, `dict[str, Any]`, `-> None`, `Path`).
2. Docstrings: No issues found — all helpers and tests carry Google-style docstrings; helper docstrings include Args/Returns and the test docstrings document the why (e.g. the blank-table and single-digit refinements).
3. Comments: No issues found — the inline comments explain intent ("Cleaned content is missing 'gamma'", the rstrip-guard note, the fence rationale), not the obvious.
4. Unit Tests: Issues found — see findings 1.x and 2.x (PictureItem/image-only and text-bearing-unhandled gaps; assertion-strength on fragmentation, hyphenation boundary, and normalization). pytest is used correctly, fixtures (`tmp_path`) are appropriate, tests are independent, and serialization is real rather than mocked.
5. Logging: N/A — a test module; no logging expected.
6. Exception Handling: N/A — no exception-raising paths are under test in this file (the module's per-document error wrapping lives in `analyze_document`, out of scope).
7. Executable Scripts: N/A — not an executable script.
8. Data Validation: N/A — covered by `test_data_val_cleaned_json.py`, not this file.
9. SQL (best-practices / dbt): N/A — no SQL in this file.

## Status & Next Steps

**Current Status**: Review complete, pending implementation. All 23 tests pass; findings validated against `quality_report.py` and confirmed via a `--cov-report=term-missing` run.
**Completed**:
1. Read the code-review and python-development skills (unit-tests, type-hints, docstrings, comments) in full.
2. Reviewed the test file against the module under test and the six checks plus the six named refinements.
3. Ran the suite (23 passed) and a coverage run; empirically confirmed the PictureItem/`image-only` and text-bearing-`carries_text` gaps.
**Next Steps**:
1. Add the `image-only` / captioned-picture page-coverage tests (1.1).
2. Add or document the text-bearing unhandled-element test (1.2).
3. Strengthen the fragmentation, hyphenation-boundary, and normalization assertions (2.1-2.3).
**Blockers**:
1. None.
**Notes**:
1. The CLI/reporting layer (`main`, `build_report`, `_collect_flags`, `_render_console`, `check_section_numbers`) is uncovered by this file but is outside its stated scope (per-check behavior); flagged only as context for the 56% line figure.
