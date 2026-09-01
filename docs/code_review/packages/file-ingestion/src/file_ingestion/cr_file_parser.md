---
name: cr-file_parser
goal: Address code quality issues identified in code/file_ingestion/file_parser.py (and its test file) to align with python-development skills.
status: completed
created: 2026-06-18 00:00:00
updated: 2026-06-18 00:00:00
---

## Implementation Plan

1. [completed] Fix dead/duplicated backend constant - `code/file_ingestion/file_parser.py`
   - 1.1. [major] Lines 32-34 / 84-92 / 62: `VALID_PDF_BACKENDS` is defined at module scope but never referenced. Backend validation is driven instead by the keys of the locally-built `pdf_backends` dict (lines 84-92), and the docstring (line 62) tells callers the valid set is `VALID_PDF_BACKENDS`. There are now two independent sources of truth for the allow-list that can silently drift; the docstring already points at the constant the code ignores.
        - Current:
          ```python
          VALID_PDF_BACKENDS: tuple[str, ...] = ("pypdfium2", "dlparse", "dlparse_v2")
          # ...
          if pdf_backend not in pdf_backends:
              raise ValueError(
                  f"Unsupported pdf_backend={pdf_backend!r}. Valid: {list(pdf_backends.keys())}"
              )
          ```
        - Expected: Drive validation from the single dict, e.g. validate against `pdf_backends.keys()` and either delete `VALID_PDF_BACKENDS` or assert it equals `set(pdf_backends)` so the two cannot diverge. (Keeping the constant only as documentation while the code ignores it is the failure mode to remove.)

2. [completed] Type hints - `code/file_ingestion/file_parser.py`
   - 2.1. [suggestion] Line 181: `_export_atomic` types the document as `doc: object`, but the body calls `doc.export_to_text()` and `getattr(doc, ...)`. `object` is imprecise and defeats type checking on those calls. The lazy-import constraint (avoid importing Docling at module load) can be preserved with a `TYPE_CHECKING` import of the Docling document type.
        - Current: `doc: object,`
        - Expected:
          ```python
          from typing import TYPE_CHECKING
          if TYPE_CHECKING:
              from docling_core.types.doc import DoclingDocument
          # ...
          def _export_atomic(doc: "DoclingDocument", ...) -> None:
          ```

3. [completed] Edge cases / risks - `code/file_ingestion/file_parser.py`
   - **Resolution:** 3.1 (empty `output_formats`) now raises `ValueError`. 3.2 (per-format atomicity) addressed at the orchestration layer instead — the `ingest.py` skip-sentinel now requires *all* requested formats present (cr_ingest_parse #1), so a partial-output file is reparsed rather than skipped; per-format cleanup in `file_parser` left as-is by design.
   - 3.1. [minor] Lines 99-105 / 167-169: An empty `output_formats=[]` passes validation (the `invalid` comprehension is empty), the format loop never runs, `file_had_export_failure` stays `False`, and the file is appended to `successes` and logged as `Finished ... 0 format(s)` despite producing no output. Consider rejecting an empty `output_formats` with a `ValueError`, since a converted-but-unwritten file is almost certainly a caller mistake.
        - Current: `invalid = [f for f in output_formats if f not in FORMAT_CONFIG]` (no empty-list guard)
        - Expected: Add `if not output_formats: raise ValueError("output_formats must contain at least one format")` after the `None` default is applied.
   - 3.2. [suggestion] Lines 150-168: Atomicity is per-`(file, format)`, not per-file. If a file requests two formats and the second export fails, the first format's `os.replace` has already landed a real file on disk, yet the file is recorded as a failure and omitted from `successes`. The module's own skip-if-exists rationale (docstring lines 48-52) means a later re-run could see that orphaned first-format file and treat that file as already complete, skipping the missing second format. Consider documenting this limitation, or cleaning up already-written outputs for a file when any of its formats fail.

4. [completed] Test coverage gaps - `code/file_ingestion/unit_tests/test_file_parser.py`
   - **Resolution:** 4.1 (`.docx` path) and 4.2 (export-failure / temp-cleanup) tests added. 4.3 (missing-method branch) left uncovered — unreachable with the current `MagicMock` style, noted not fixed. 4.4 (`pytest-cov`) not done — adding a dev dependency was out of the two-file scope.
   - 4.1. [major] No test exercises a non-PDF input. Every test writes a `.pdf` source file, so the `.docx` / `WordFormatOption` path and the `else ""` branch of the pdf-only log label (file_parser.py lines 116, 133-137) are entirely uncovered. The review context flags the pdf-only-label behavior specifically, so its absence from the suite is a real gap.
        - Expected: Add a test with a `.docx` source asserting (a) success and (b) that the per-file "Converting" log omits the `pdf-only:` suffix (use `caplog`).
   - 4.2. [major] No test exercises an export failure / temp-file cleanup. `test_multiple_files_one_fails_raises_runtime_error` (lines 111-136) fails at *conversion*, not export, so `_export_atomic`'s `except` cleanup branch (file_parser.py lines 225-228) and the `file_had_export_failure` path (lines 157-168) are uncovered.
        - Expected: Add a test where a `save_as_*` mock raises, asserting the file is in the `RuntimeError` message, is NOT in the returned successes, and no temp file (`.<stem>.*`) lingers in `output_dir`.
   - 4.3. [minor] The missing-method `ValueError` branch (file_parser.py lines 218-222) is unreachable with the current mocks: a plain `MagicMock` auto-creates a truthy attribute for any `getattr`, so `method is None` is never true. Either accept this branch is untestable via the mock and note it, or add a test using a doc object that genuinely lacks the configured method (e.g., `MagicMock(spec=...)` or `del doc.save_as_json`) to cover it.
   - 4.4. [minor] Coverage cannot currently be validated (unit-tests skill 7.2 requires `pytest --cov ... --cov-report=term-missing`). `pytest-cov`/`coverage` are not installed in this environment, so the "comprehensive coverage" claim is unsubstantiated. Add `pytest-cov` to the dev dependencies and run it to confirm the branches above.

## Skills with No Issues

1. Type Hints: One suggestion (3 / 2.1, `doc: object`); all public signatures otherwise use modern syntax (`list[str]`, `str | Path`, `str | None`).
2. Docstrings: No issues found - Google-style with Args/Returns/Raises/Note; the `Raises` sections match the actual `ValueError`/`RuntimeError` raised.
3. Comments: No issues found - comments explain "why" (lazy Docling import, same-filesystem temp file for atomic `os.replace`, pdf-only settings rationale).
4. Logging: No issues found - uses `logconfig.get_logger`, f-strings, appropriate levels (debug/info/warning/error), no print, no "Entering/Exiting" noise; `_export_atomic` correctly stays silent to avoid duplicate logging (caller logs export outcome).
5. Exception Handling: No issues found - specific messages with context, no bare `except`. The broad `except Exception` at lines 141/157 is the correct resilient-batch pattern (accumulate failures, continue); the final `RuntimeError` not chaining `from e` is inherent to aggregating multiple failures, not a violation.
6. Executable Scripts: N/A - library module, no `main()` / `--config` entry point.
7. Data Validation: N/A - not a data-validation script.
8. SQL Development: N/A - no SQL in this file.
9. Unit Tests: Issues in section 4 (coverage gaps). Strengths: pytest (not unittest), correct `test_<fn>_<scenario>_<expected>` naming, `tmp_path` for filesystem isolation, `@pytest.mark.parametrize` for `do_ocr` and backend mapping, `pytest.raises(..., match=...)` for both error types, and the `_make_writing_doc` helper correctly models the write-then-rename contract. Patching the Docling module attributes is valid despite the function's local `from ... import` (the local import re-reads the patched attribute at call time).

## Status & Next Steps

**Current Status**: Addressed — fixes implemented and committed (`6767eeb`); 18 tests pass.
**Completed**:
1. Findings 1, 2, 3.1, 4.1, 4.2 implemented in `file_parser.py` / `test_file_parser.py`.
2. `VALID_PDF_BACKENDS` is now the single source of truth (assert guards drift); empty `output_formats` rejected; `doc` typed via `TYPE_CHECKING`; `.docx` and export-failure tests added.
3. Suite run: 18 passed.
**Next Steps**:
1. None blocking. Optional follow-ups deliberately deferred: 4.4 (add `pytest-cov` dev dep + run term-missing coverage) and 3.2 per-format cleanup (mitigated by the ingest skip-sentinel fix).
**Blockers**:
1. None.
**Notes**:
1. 4.3 (missing-method branch) is unreachable under the current `MagicMock` style and was left uncovered by design.
