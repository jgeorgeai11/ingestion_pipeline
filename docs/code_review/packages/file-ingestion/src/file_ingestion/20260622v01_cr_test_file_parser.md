---
name: cr-test_file_parser
goal: Assess the test suite in code/file_ingestion/unit_tests/test_file_parser.py for coverage, mocking discipline, assertion strength, and conformance to the unit-tests skill.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

This is a strong, well-disciplined test suite. Docling is genuinely mocked — no real document conversion runs in the unit tests — and the suite measures 99% statement coverage on `file_parser.py` (verified: `18 passed`, only line 241 uncovered). Every test method has typed parameters, a `-> None` return annotation, and a focused docstring, and follows Arrange-Act-Assert. The atomic temp-then-replace path, the cleanup-on-failure path, the failure-accumulation behavior, the `text` special case, the PDF backend allow-list, and the pdf-only log line are all exercised. No critical or major issues were found. Findings are limited to one genuine coverage gap (the `_export_atomic` "method is None" defensive branch plus the untested `yaml`/`doctags` formats) and a few minor/suggestion items around an inert mock and one over-precise assertion.

## Implementation Plan

1. [completed] Coverage gaps - `code/file_ingestion/unit_tests/test_file_parser.py` — RESOLVED: added `test_missing_save_as_method_raises_runtime_error_and_cleans_temp` (sets `mock_doc.save_as_yaml = None`, requests `["yaml"]`, asserts RuntimeError and an empty output dir); covers `_export_atomic` "method is None" branch and the yaml FORMAT_CONFIG path; `file_parser.py` now 100%.
   - 1.1. [minor] `_export_atomic` line 241 (`file_parser.py:239-243`) is the only uncovered statement. This is the defensive guard that raises `ValueError` when the configured `save_as` method is missing on the document (`method = getattr(doc, cfg["method"], None); if method is None: raise ...`). It is unreachable with the current mocks because a `MagicMock` auto-creates any attribute, so `getattr` never returns `None`. The related `yaml` and `doctags` entries in `FORMAT_CONFIG` also have no tests.
        - Current: no test forces a configured method to be absent on the document.
        - Expected (suggested, do not implement here): add a test that sets the attribute to `None` on the mock document and requests that format, e.g. `mock_doc.save_as_yaml = None`, call with `output_formats=["yaml"]`, and assert a `RuntimeError` is raised (the `ValueError` is caught per-format and re-raised as the accumulated `RuntimeError`) with no lingering temp file. This closes line 241 and adds coverage for a non-markdown/json/html format path.

2. [skipped] Inert / unnecessary mock - `code/file_ingestion/unit_tests/test_file_parser.py` — SKIPPED: the inert backend patch is intentionally left in place; removing it from ~10 tests is churn for no behavioral gain.
   - 2.1. [suggestion] Lines 40, 82, 108, 139, 183, 217, 253, 337, 381, 419: the `@patch("docling.backend.docling_parse_backend.DoclingParseDocumentBackend", new_callable=MagicMock)` decorator (param `mock_backend`) is applied to nearly every test but is never asserted on. Because `PdfFormatOption` is itself mocked in those tests, the backend class is only ever passed into a mock and never instantiated, so this patch has no observable effect — it functions as an inert default. It is harmless, but removing it (or, conversely, asserting on it) would reduce decorator noise. Note the contrast with `test_pdf_backend_maps_to_correct_class` (lines 280-313), which deliberately does NOT mock the backend classes so it can assert `call_args.kwargs["backend"] is expected_cls` against the real classes — that is the correct pattern and should be left as-is.

3. [completed] Over-precise assertion - `code/file_ingestion/unit_tests/test_file_parser.py` — RESOLVED: replaced `assert converting_lines == ["Converting memo.docx"]` with `assert len(converting_lines) == 1`, keeping the `"pdf-only" not in line` behavioral check and decoupling from the exact "Converting " phrasing.
   - 3.1. [suggestion] Line 378: `assert converting_lines == ["Converting memo.docx"]` asserts the full log string by exact equality, coupling the test to the precise "Converting " phrasing. The behavioral intent (the pdf-only suffix is omitted for non-PDF inputs) is already and correctly captured by line 379 (`assert all("pdf-only" not in line ...)`), which is exactly what the review brief asks for. Consider relaxing the equality check to assert the count of converting lines and the absence of the suffix, leaving the exact label unasserted.
        - Current: `assert converting_lines == ["Converting memo.docx"]`
        - Expected: keep the `"pdf-only" not in line` check; optionally assert `len(converting_lines) == 1` rather than exact-string equality.

4. [completed] Internal-detail assertion - `code/file_ingestion/unit_tests/test_file_parser.py` — RESOLVED: replaced `assert list(output_dir.glob(".doc.*")) == []` with `assert list(output_dir.iterdir()) == []` (export fails before any final file is written, so the dir is empty), removing the hard-coded temp-prefix coupling.
   - 4.1. [suggestion] Line 417: `assert list(output_dir.glob(".doc.*")) == []` verifies cleanup by matching the temp-file naming convention (the `.{stem}.` prefix from `_export_atomic`, `file_parser.py:228`). This couples the test to an internal temp-file naming detail. The intent is sound and the cleanup path is worth testing; a slightly more robust phrasing asserts that `output_dir` contains no files at all after the failed export (no final output and no temp residue), which does not hard-code the prefix.

## Skills with No Issues

1. Unit Tests skill (pytest, not unittest): No issues found — uses pytest throughout.
2. Unit Tests skill (file naming): No issues found — `test_file_parser.py` matches `file_parser.py`.
3. Unit Tests skill (function naming `test_<function>_<scenario>_<expected>`): No issues found — names are descriptive and predictable (e.g. `test_missing_file_raises_runtime_error`, `test_export_failure_cleans_temp_file_and_raises`).
4. Unit Tests skill (single behavior / AAA): No issues found — each test is focused and clearly arranged.
5. Unit Tests skill (mock external boundaries only, patch where used): No issues found — Docling is mocked at the import sites the lazy `from docling... import ...` statements resolve against; verified empirically that no real conversion runs and the patches take effect.
6. Unit Tests skill (pytest utilities — parametrize, `pytest.raises(match=...)`): No issues found — `@pytest.mark.parametrize` is used for backends and `do_ocr`; `pytest.raises(..., match=...)` is used for all error cases.
7. Unit Tests skill (built-in fixtures): No issues found — `tmp_path` and `caplog` used appropriately; no manual temp-dir handling.
8. Unit Tests skill (no shared state / no private-attribute assertions): No issues found — tests are independent; assertions target public return values, files on disk, and mock call records, not private internals.
9. Unit Tests skill (comprehensive coverage): Mostly satisfied — 99% measured; one defensive branch and the `yaml`/`doctags` formats remain uncovered (see 1.1).
10. Type Hints skill: No issues found — every test method and the `_make_writing_doc` helper carry full parameter and return annotations using modern syntax.
11. Docstrings skill: No issues found — module, helper, and every test method have Google-style docstrings; `_make_writing_doc` documents the "why" of its write-then-rename design.
12. Comments skill: No issues found — inline comments are sparse and explanatory.
13. Logging skill: N/A — test file; it consumes logs via `caplog` rather than configuring logging.
14. Exception Handling skill: N/A — test file; exception paths are asserted via `pytest.raises`.
15. SQL skills: N/A — no SQL.

## Notable Strengths

1. `_make_writing_doc` (lines 17-34) correctly models Docling's contract: the `save_as_*` side effects write the temp file they are handed, so the subsequent real `os.replace` in `_export_atomic` succeeds. This makes the file-existence and content assertions (e.g. lines 249-250, 453-454) genuine tests of the atomic rename code, not tautologies on the mock.
2. The atomicity / cleanup-on-failure path is actually tested (`test_export_failure_cleans_temp_file_and_raises`, lines 385-417): it forces a post-conversion export failure, asserts the file is reported as a failure (RuntimeError, not in successes), the final output is absent, and no temp file lingers.
3. Both error preconditions requested in the brief are covered: missing source file (lines 69-80) and unsupported output format (lines 172-181), plus empty `output_formats` (lines 326-335) and invalid `pdf_backend` (lines 315-324).
4. The PDF backend allow-list is well-tested at the public boundary: `test_pdf_backend_maps_to_correct_class` parametrizes all three valid names against the real backend classes, and the invalid-name rejection is covered. The internal `assert set(pdf_backends) == set(VALID_PDF_BACKENDS)` drift guard (`file_parser.py:101-104`) is a defensive invariant that runs on every call; it is not separately unit-tested, which is acceptable for a same-function invariant.
5. The pdf-only log line is verified in both directions: PDF inputs implicitly through other tests, and the `.docx` test (lines 342-379) explicitly confirms the `else ""` branch omits the suffix.

## Status & Next Steps

**Current Status**: Findings applied. Suite passes (19/19) at 100% coverage of `file_parser.py`.
**Completed**:
1. Read code-review and python-development (unit-tests, type-hints, docstrings) skills in full.
2. Read the test file and the module under test.
3. Ran the suite with `pytest --cov=file_parser --cov-report=term-missing` to verify mocking is real and to locate uncovered lines.
4. Applied findings 1.1, 3.1, 4.1 (test-only); skipped finding 2.1. Suite now 19/19, `file_parser.py` at 100% coverage.
**Next Steps**:
1. None — findings 1, 3, 4 applied; finding 2 intentionally skipped.
**Blockers**:
1. None.
**Notes**:
1. No code was modified and nothing was committed, per instructions.
2. Filename uses the user-specified `20260622v01_cr_test_file_parser.md` rather than the skill default `cr_test_file_parser.md`, per the explicit instruction.
