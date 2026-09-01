---
name: cr-ingest_parse
goal: Address code quality issues in the Step 1 / parse path of code/file_ingestion/ingest.py (step_parse, _group_files_by_parse_settings, main() parse-config handling) and its tests, to align with python-development skills.
created: 2026-06-18 00:00:00
updated: 2026-06-18 00:00:00
---

## Implementation Plan

1. [completed] Correctness — skip-sentinel ignores requested output_formats — `code/file_ingestion/ingest.py`
   - 1.1. [major] Lines 138-153: The skip-sentinel keys on `.json` alone and never consults the
     resolved `output_formats` set, producing two distinct failure modes:
     - (a) When `output_formats` omits `"json"` (e.g. `["markdown"]`), the sentinel checks a file
       that is never produced, so no file is ever skipped and every run reparses everything.
       Wasteful but safe.
     - (b) When `"json"` IS requested but a prior run's markdown (or other format) export failed
       *after* the `.json` had already been written, the next run sees the `.json`, skips the file,
       and the missing `.md` then breaks `step_collapse` ("Markdown file not found", which only logs
       and continues) and ultimately `step_load` (`FileNotFoundError`). The only recovery is
       `overwrite=true`. This is a silent, hard-to-diagnose partial-output state.
        - Current:
          ```python
          json_path = parsed_dir_path / (Path(file_name).stem + ".json")
          if json_path.exists() and json_path.stat().st_size > 0:
              logger.info(f"Skipping {file_name}: {json_path.name} already exists")
          else:
              to_convert.append(file_name)
          ```
        - Expected: Skip only when every requested format's output exists and is non-empty (derive
          extensions from `file_parser.FORMAT_CONFIG`), or restrict the sentinel to `"json"` only
          when `"json" in output_formats` and otherwise fall back to the markdown output. For example:
          ```python
          from file_parser import FORMAT_CONFIG
          exts = [FORMAT_CONFIG[f]["ext"] for f in output_formats]
          stem = Path(file_name).stem
          outputs = [parsed_dir_path / f"{stem}{ext}" for ext in exts]
          if outputs and all(p.exists() and p.stat().st_size > 0 for p in outputs):
              logger.info(f"Skipping {file_name}: all outputs already exist")
          else:
              to_convert.append(file_name)
          ```

2. [completed] Comments — misleading atomicity claim — `code/file_ingestion/ingest.py`
   - 2.1. [major] Lines 142-144: The comment states "Atomic writes guarantee an existing .json is
     complete," conflating *per-file* atomicity with *per-format-set* completeness. In
     `parse_files_docling` each format is exported in an independent, individually-atomic loop
     iteration (file_parser.py lines 151-165), and a failed format only appends to `failures` and
     continues. A present, complete `.json` therefore does NOT imply the other requested formats
     were written. The comment actively misleads a reader into trusting the json-only sentinel and
     masks finding 1.1.
        - Current: `# Atomic writes guarantee an existing .json is complete, but guard / # against a zero-byte file ...`
        - Expected: Reword to state that per-format atomicity guarantees each individual file is
          either complete or absent, but that the json sentinel alone does not prove the full
          requested format set is present (tie this to the fix in 1.1).

3. [completed] Correctness — cross-group failure aborts later groups — `code/file_ingestion/ingest.py`
   - 3.1. [minor] Lines 158-165: Because `parse_files_docling` raises `RuntimeError` per call, a
     failure in an earlier `(do_ocr, pdf_backend)` group aborts the loop and the remaining groups
     never run. This is a behavior change introduced by the grouping rework: with a single converter,
     every file was attempted in one pass. Now a bad file in the first group can silently prevent an
     entirely-unrelated second group from being parsed at all. Decide whether this fail-fast is
     intended; if not, accumulate failures across groups and raise once after the loop.
        - Current: each `parse_files_docling(...)` call may raise and break out of the `for` loop.
        - Expected: either document the fail-fast behavior in the `step_parse` docstring, or collect
          per-group `RuntimeError`s and raise a combined error after all groups are attempted.

4. [completed] Unit Tests — step_parse orchestration is untested — `code/file_ingestion/unit_tests/test_ingest.py`
   - 4.1. [major] The reworked `step_parse` (skip-sentinel logic and the group-then-convert
     orchestration) has zero tests; only the pure helper `_group_files_by_parse_settings` is covered.
     The two scenarios in finding 1.1 are exactly the untested, high-risk paths. These are
     `tmp_path` + `mocker.patch("ingest.parse_files_docling")` tests, e.g.:
     - skip path: pre-create a non-empty `.json` in `parsed_dir`, call with `overwrite=False`, assert
       `parse_files_docling` was not called for that file.
     - reparse-on-empty-json: pre-create a zero-byte `.json`, assert the file is still converted.
     - grouping dispatch: mixed `do_ocr_map`, assert `parse_files_docling` is called once per group
       with the correct `do_ocr`/`pdf_backend` and file list.
     - `overwrite=True`: assert the sentinel is bypassed and all files are converted.
        - Current: only `TestGroupFilesByParseSettings` exists.
        - Expected: add a `TestStepParse` class covering skip, empty-sentinel, overwrite, and
          per-group dispatch, mocking `parse_files_docling` at the `ingest` boundary.

5. [completed] Maintainability — hard defaults duplicated across modules — `code/file_ingestion/ingest.py`
   - 5.1. [minor] The `do_ocr=False` / `pdf_backend="dlparse"` hard defaults are repeated in
     `main()` (lines 417-418), in `_group_files_by_parse_settings` (line 96), and in the
     `step_parse` docstring (lines 125-127), and again as parameter defaults in
     `parse_files_docling`. In practice `main()` always fully populates both maps, so the
     `.get(..., default)` fallbacks inside `_group_files_by_parse_settings` are a redundant safety
     net (defensible, since `step_parse` is independently callable). Consider centralizing the two
     hard defaults as module-level constants to keep them from drifting apart.
        - Current: literal `False` / `"dlparse"` in multiple locations.
        - Expected: e.g. `DEFAULT_DO_OCR = False` / `DEFAULT_PDF_BACKEND = "dlparse"` referenced from
          a single source of truth.

6. [wontfix] Readability — `or {}` masks an explicit empty dict — `code/file_ingestion/ingest.py`
   - **Resolution:** Declined (suggestion). `or {}` and `is None` are behaviorally identical here; not worth a change.
   - 6.1. [suggestion] Lines 133-134: `do_ocr_map = do_ocr_map or {}` treats an explicitly-passed
     empty dict the same as `None`. Behaviorally identical here, but `if do_ocr_map is None` states
     the intent (handle the unset default) more precisely.

7. [wontfix] Validation timing — invalid pdf_backend not caught fail-fast — `code/file_ingestion/ingest.py`
   - **Resolution:** Declined (suggestion). An invalid backend still raises `ValueError` (now propagated immediately, not accumulated, per finding 3); configs are small and authored, so up-front validation adds little.
   - 7.1. [suggestion] Lines 158-165: An invalid `pdf_backend` is only rejected inside
     `parse_files_docling` when its group is reached, so earlier groups may already have written
     output before a later group's bad backend raises `ValueError`. Minor given configs are small
     and authored, but validating all distinct backends up front (against
     `file_parser.VALID_PDF_BACKENDS`) before the conversion loop would make `step_parse` fail fast.

8. [wontfix] Unit Tests — test names omit the function prefix — `code/file_ingestion/unit_tests/test_ingest.py`
   - **Resolution:** Declined (suggestion). The class-name-as-context convention (`TestStepParse`, `TestGroupFilesByParseSettings`) is kept for consistency across the file.
   - 8.1. [suggestion] The skill specifies `test_<function>_<scenario>_<expected>`; the tests
     (`test_uniform_settings_single_group`, etc.) rely on the class name `TestGroupFilesByParseSettings`
     for the function context instead. Defensible convention rather than a clear violation; noting for
     consistency only.

## Skills with No Issues

1. Type Hints: No issues found — `step_parse`, `_group_files_by_parse_settings`, and the test
   functions all carry modern parameter and return annotations
   (`dict[tuple[bool, str], list[str]]`, `dict[str, bool] | None`, keyword-only `*` separator, `-> None`).
2. Docstrings: No issues found in the parse path — Google-style with Args/Returns/Raises;
   `step_parse` documents the per-file resolution, default fallbacks, and propagated `ValueError`/`RuntimeError`.
3. Logging: No issues found — uses `logconfig` `get_logger`, per-group INFO with `do_ocr`/`pdf_backend`
   context, run-boundary separators in `main()`, f-strings throughout, no `print()`.
4. Exception Handling: No issues found in the parse path — `main()` catches specific
   `(FileNotFoundError, ValueError, RuntimeError)` then a logged generic fallback; `get_engine`
   chains with `raise ... from e`. The cross-group abort (3.1) is a control-flow design point, not an
   exception-handling anti-pattern.
5. Executable Scripts: No issues found — single `--config` argument, deferred logging setup, TOML
   config in `config/`, `main()` with `if __name__ == "__main__"`.
6. Comments: One issue found (2.1, misleading atomicity claim); the section-separator comments and
   the precedence comment at line 416 are otherwise accurate and explain "why".
7. main() parse-config precedence: Verified correct — `parse_do_ocr_default = parse_cfg.get("do_ocr", False)`
   then `d.get("do_ocr", parse_do_ocr_default)` implements document-entry → `[parse]` default → hard
   default exactly as intended; `output_formats` defaults to `["json", "markdown"]`.
8. SQL Development: N/A — the parse path contains no SQL.
9. Data Validation: N/A — `step_parse` is conversion orchestration, not a data-validation step.

## Status & Next Steps

**Current Status**: Addressed — findings 1–5 implemented and committed (`6767eeb`); 6–8 are declined suggestions. Full suite passes (65 tests).
**Completed**:
1. Skip-sentinel now requires every requested `output_format` present and non-empty (1); atomicity comment corrected (2).
2. Cross-group failures accumulate and raise once; `ValueError` propagates immediately (3).
3. `TestStepParse` added (skip, partial/empty reparse, overwrite bypass, per-group dispatch, failure handling) (4).
4. Hard defaults centralized as `DEFAULT_DO_OCR` / `DEFAULT_PDF_BACKEND` (5).
**Next Steps**:
1. None blocking. Findings 6, 7, 8 are deliberately declined suggestions (see each finding's Resolution); revisit only if a need arises.
**Blockers**:
1. None.
**Notes**:
1. `step_collapse`, `step_load`, and `get_engine` were out of scope (prior rework reviewed in `cr_ingest.md`); only the parse path was assessed here.
2. `parse_files_docling`'s own behavior (do_ocr, pdf_backend mapping, atomic export) is covered by `test_file_parser.py` and was treated as a trusted boundary.
