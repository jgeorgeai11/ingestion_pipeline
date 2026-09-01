---
name: cr-test_file_parser
goal: Review the docling-upgrade test changes in code/file_ingestion/unit_tests/test_file_parser.py (new TestPageRangeSlices/TestPdfPageCount/TestConvertDocument classes and the v2/v4 backend removal) against the unit-tests skill.
created: 2026-06-29 00:00:00
updated: 2026-06-29 00:00:00
---

## Summary

Reviewed the `docling-upgrade` diff (`git diff main..docling-upgrade -- code/file_ingestion/unit_tests/test_file_parser.py`): three new test classes covering the page-batching path (`_page_range_slices`, `_pdf_page_count`, `_convert_document`) and the removal of the deprecated `dlparse_v2`/`dlparse_v4` backend-parametrize cases and their import.

The new tests are well constructed: the slice-math cases pin all four shapes (exact multiple, remainder, single-page tail, fewer-than-batch); `test_large_pdf_parses_in_slices_and_concatenates` is genuinely strong — it asserts the exact slice `page_range` tuples `[(1,30),(31,60),(61,70)]`, that `concatenate` is called once with the slice docs, AND that the return is the merged doc (not merely "it runs"). The mocks are faithful to the `converter.convert(...).document` shape, and the patch sites are correct: `patch("file_parser._pdf_page_count")` patches where used, and `patch("docling_core.types.doc.DoclingDocument")` correctly patches the definition site because production re-imports that name function-locally at call time (`file_parser.py:300`), so the name resolves from that module.

The one real gap is the **origin-restoration behavior** (`file_parser.py:315-316`), which fixed a real bug where the clean step rejects a doc with no `origin.binary_hash`. In `test_large_pdf...` the slice docs are bare `MagicMock()`, so `docs[0].origin` is a truthy auto-child: the positive branch *executes* (it would show as line-covered) but its effect — `merged.origin = docs[0].origin` — is never asserted, and the `origin is None` branch is never exercised at all. Line coverage here does not imply behavioral coverage. Smaller gaps: `_pdf_page_count`'s success path is never run (always patched), and the `n_pages <= max_pages_per_batch` boundary (`== threshold`, just-over) is untested.

Counts: 0 critical, 1 major, 3 minor, 2 suggestions (6 findings).

## Implementation Plan

1. [pending] Origin-restoration behavior is not asserted — `code/file_ingestion/unit_tests/test_file_parser.py`
   - 1.1. [major] Lines 580-592 `test_large_pdf_parses_in_slices_and_concatenates`: production `_convert_document` restores `merged.origin = docs[0].origin` after `concatenate` (`file_parser.py:315-316`) — a documented fix for the clean step rejecting a doc with no `origin.binary_hash`. The test's `slice_docs` are bare `MagicMock()`, so `docs[0].origin` is a truthy auto-child and the assignment branch runs (line-covered) but its *effect* is never checked: nobody asserts `merged.origin`, and `merged` is a `MagicMock` so the assignment is silently absorbed. The `origin is None` branch is never exercised. This is the exact behavior the production comment says fixes a real bug; it should be pinned, not left to indirect/no coverage.
        - Current: `slice_docs = [MagicMock(name="d1"), MagicMock(name="d2"), MagicMock(name="d3")]` ... `assert out is merged` (no `merged.origin` assertion)
        - Expected: add an assertion in this test (or a focused test) setting a sentinel origin, e.g. `slice_docs[0].origin = sentinel` then `assert merged.origin is sentinel`; and a second case `slice_docs[0].origin = None` asserting `merged.origin` is left untouched (the assignment is skipped).

2. [pending] Coverage gaps in the new batching tests — `code/file_ingestion/unit_tests/test_file_parser.py`
   - 2.1. [minor] Lines 518-526 `TestPdfPageCount`: only the failure path (`pdfplumber.open` raising -> `None`) is tested. The success path — `len(pdf.pages)` returning a real count — is never exercised here, and in `TestConvertDocument` `_pdf_page_count` is always patched, so the real return value is never asserted anywhere.
        - Current: only `test_unreadable_pdf_returns_none`
        - Expected: add `test_pdf_page_count_returns_page_count` patching `pdfplumber.open` to yield a context manager whose `.pages` has known length, asserting the int is returned.
   - 2.2. [minor] Lines 558-564 `test_pdf_within_threshold_parses_single_pass`: uses `n_pages=10 < batch=30`, leaving the `n_pages <= max_pages_per_batch` boundary (`file_parser.py:297`) untested. The off-by-one risk lives exactly at `n_pages == batch` (must stay single-pass) and `n_pages == batch + 1` (must batch). The large-PDF test uses 70, far from the boundary.
        - Current: `patch("file_parser._pdf_page_count", return_value=10)` only
        - Expected: add `return_value=30` asserting single-pass (`convert` called once, no `page_range`), and ideally `return_value=31` asserting it batches.
   - 2.3. [minor] Lines 528-592 `TestConvertDocument`: no case exercises `batch == 1` (every page its own slice) at the `_convert_document` level; `_page_range_slices` is also only tested with `batch=30`. Low risk given the slice-math tests, but the smallest batch is a natural boundary.
        - Current: `max_pages_per_batch` is only ever `0` or `30` in `TestConvertDocument`; `_page_range_slices` only `batch=30`
        - Expected: add a `_page_range_slices(3, 1) == [(1,1),(2,2),(3,3)]` case (cheap, no mocks).

3. [pending] Documented failure-propagation contract is unverified — `code/file_ingestion/unit_tests/test_file_parser.py`
   - 3.1. [suggestion] Lines 528-592 `TestConvertDocument`: the production docstring states "Any `convert` failure (including in a slice) propagates to the caller" (`file_parser.py:283`). No test asserts that a slice `convert` raising propagates out of `_convert_document` (and that `concatenate` is not reached).
        - Current: no failing-slice test
        - Expected: add a test with `conv.convert.side_effect = [MagicMock(document=...), Exception("boom")]` and `pytest.raises(Exception, match="boom")`, asserting `concatenate` was not called.
   - 3.2. [suggestion] Lines 518-526 `TestPdfPageCount`: the failure path asserts the return is `None` but not that the warning is logged (`file_parser.py:267-269`), the user-visible signal that a file silently fell back to single-pass. Optional given the behavioral return is what callers branch on.
        - Current: `assert _pdf_page_count(pdf) is None`
        - Expected (optional): add `caplog` and assert a `WARNING` mentioning the file name.

## Skills with No Issues

1. Unit Tests (pytest, not unittest): No issues — pytest throughout; new classes use plain `assert` and `tmp_path`.
2. Unit Tests (file naming): No issues — `test_file_parser.py` matches `file_parser.py`.
3. Unit Tests (function naming `test_<function>_<scenario>_<expected>`): No issues — new names are descriptive and predictable (`test_zero_disables_batching_without_reading_pages`, `test_unreadable_page_count_falls_back_single_pass`, `test_large_pdf_parses_in_slices_and_concatenates`).
4. Unit Tests (single behavior / AAA): No issues — each new test is focused and cleanly arranged.
5. Unit Tests (mock external boundaries only, patch where used): No issues found in the new tests — `patch("file_parser._pdf_page_count")` patches at the use site; `patch("docling_core.types.doc.DoclingDocument")` correctly patches the definition site because production imports the name function-locally at call time (`file_parser.py:300`); `pdfplumber.open` is patched at its use site. The `_converter_returning` helper and `conv.convert.side_effect` model the real `.convert(...).document` shape — no over-mocking of the logic under test (slice math and branching run for real).
6. Unit Tests (pytest utilities — parametrize, `pytest.raises(match=)`): No issues — the backend `@pytest.mark.parametrize` is correctly trimmed to the two supported backends; existing error cases use `pytest.raises(..., match=...)`. (See 3.1 for an optional added `pytest.raises` case.)
7. Unit Tests (built-in fixtures): No issues — `tmp_path` used for all file-touching tests; no manual temp handling.
8. Unit Tests (no shared state / no private-attribute assertions): No issues — tests are independent; assertions target return values and mock call records (`call_args_list`, `assert_called_once_with`). The new tests legitimately call module-private helpers (`_convert_document` etc.), which is unit testing of the functions themselves, not asserting on internal state of a public call.
9. Unit Tests (comprehensive coverage): Gaps noted in 1.1, 2.1, 2.2, 2.3, 3.1 — origin-restoration effect, `_pdf_page_count` success path, the `== threshold` boundary, `batch == 1`, and slice-failure propagation.
10. Type Hints: No issues — every new test method and the `_converter_returning` static helper carry full parameter annotations and `-> None` / `-> MagicMock` returns.
11. Docstrings: No issues — new class and test docstrings are concise and accurate (e.g. the `# 70 pages @ 30 -> ...` inline note matches the asserted ranges).
12. Comments: No issues — sparse and explanatory.
13. Logging: N/A — test file; one optional `caplog` suggestion (3.2).
14. Exception Handling: N/A — exception path asserted via `pytest.raises` in existing tests; one suggested addition (3.1).
15. SQL Development: N/A — no SQL.

## Status & Next Steps

**Current Status**: Review complete (static analysis). One [major] coverage gap (origin restoration), three [minor], two [suggestion]. The slice-math and concatenate assertions are strong; mocks and patch sites are faithful. The deprecated `dlparse_v2`/`dlparse_v4` parametrize cases and their import were cleanly removed.
**Completed**:
1. Read the unit-tests skill, the prior `20260626v01_cr_test_file_parser.md` review, and the production `_convert_document`/`_pdf_page_count`/`_page_range_slices` (`file_parser.py:229-317`).
2. Reviewed the `main..docling-upgrade` diff for the test file and verified mock fidelity and patch sites against production.
3. Confirmed `test_large_pdf...` asserts the exact slice `page_range` tuples and `concatenate.assert_called_once_with(slice_docs)`.
**Next Steps**:
1. Address 1.1 (assert the origin-restoration effect, including the `origin is None` branch).
2. Address 2.1-2.3 (page-count success path, `== threshold` boundary, `batch == 1`).
3. Optionally address 3.1-3.2 (slice-failure propagation, fallback warning).
**Blockers**:
1. Could not execute `uv run pytest ... -q` — Bash was denied by the sandbox (twice). Findings are from static analysis; the pytest run and `--cov` were NOT executed this session. Prior CR's "suite passes / 100% coverage" claim was not re-verified here.
**Notes**:
1. Prior finding 1.1 (stale `test_default_pipeline_json_and_markdown_produced` "default" naming) appears addressed — that name no longer exists in the file; it is outside this diff's scope and not re-litigated.
2. No source or test files were modified and nothing was committed.

## Resolution (2026-06-29)

- [major] origin-restoration behaviorally tested — **fixed**: `test_origin_restored_from_first_slice` (asserts a sentinel origin propagates) + `test_origin_none_left_untouched` (None branch leaves merged.origin untouched).
- [minor] `_pdf_page_count` success path — **added** (`test_returns_page_count`).
- [minor] `== threshold` boundary — **added** (`test_pdf_at_exact_threshold_parses_single_pass`).
- [minor] `batch == 1` — **added** (`test_batch_size_one`).
- [suggestion] slice-convert raising propagates — **added** (`test_slice_convert_failure_propagates`).
- [suggestion] fallback warning-log assertion — not added (low value; the None-return is already covered).
