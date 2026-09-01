---
name: cr-data_val_cleaned_json
goal: Address code quality issues identified in code/file_ingestion/data_validation/data_val_cleaned_json.py to align with python-development core skills (type hints, docstrings, comments, logging, exception handling, executable-scripts, data-validation).
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

The file has been rewritten since the prior review (`20260618v01`): it no longer hand-rolls field extraction and checks, and instead delegates wholly to the `CleanedDocument` Pydantic model via `model_validate_json`. That is the right design and it eliminates every finding from the previous review — the bool-as-int and `AttributeError`-on-`.split()` crashes can no longer occur, because `strict=True` + `extra="forbid"` reject those at field-validation time, before the `model_validator(mode="after")` invariants run. The error-to-message conversion, the `<document>` loc fallback, the config resolution, and the exit-code logic are all correct.

One real defect remains: the read path catches only `OSError`, but a non-UTF-8 file raises `UnicodeDecodeError` (a `ValueError`), which is uncaught and — since `main()`'s per-file loop has no surrounding handler — crashes the whole run with a traceback instead of producing a `FAIL` message. For a validator whose purpose is to handle malformed input gracefully, this is the top issue. Severity counts: 1 major, 1 minor, 3 suggestions.

## Implementation Plan

1. [completed] Fix read-path completeness - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - RESOLVED: read path now uses `read_bytes()` under `except OSError`, and the bytes are passed to `CleanedDocument.model_validate_json(...)`, which folds a non-UTF-8 decode failure into the existing `except pydantic.ValidationError` → FAIL path (verified: a `b"\xff\xfe..."` file returns a non-empty FAIL list and does not raise).
   - 1.1. [major] Lines 60-62: The read path catches only `OSError`, but decoding a non-UTF-8 / corrupt file raises `UnicodeDecodeError`, which is a subclass of `ValueError` (verified: `issubclass(UnicodeDecodeError, OSError)` is `False`). That exception is uncaught here, and `main()` does not wrap the `validate_cleaned_file` call (lines 135-138), so a single bad-encoding file crashes the entire validation run with a traceback and aborts the remaining files — the opposite of the per-file FAIL-message behaviour the module promises. Preferred fix: read bytes and let Pydantic fold the decode error into the existing `ValidationError` path.
        - Current:
          ```python
          try:
              text = json_path.read_text(encoding="utf-8")
          except OSError as e:
              return [f"FAIL: {name}: could not read JSON: {e}"]
          try:
              document = CleanedDocument.model_validate_json(text)
          ```
        - Expected (one option):
          ```python
          try:
              raw = json_path.read_bytes()
          except OSError as e:
              return [f"FAIL: {name}: could not read JSON: {e}"]
          try:
              document = CleanedDocument.model_validate_json(raw)
          ```
          `model_validate_json` accepts `bytes` and reports a decode failure as a `ValidationError`, so it is captured by the existing `except`. (Alternative: keep `read_text` but catch `(OSError, UnicodeDecodeError)`.)

2. [completed] Correct an inaccurate docstring claim - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - RESOLVED: the "non-trivial document" qualifier is gone; the rewritten docstring summarises the checks (including "at least one section") and points to `cleaned_models.CleanedDocument` for the authoritative rules, with no triviality exception.
   - 2.1. [minor] Line 15: The module docstring lists "At least one section per non-trivial document." There is no triviality exception in the schema: `cleaned_models.py:124-125` raises `"has zero sections"` unconditionally for any empty `sections` list. The "per non-trivial document" qualifier misstates the invariant and should be removed.
        - Current: `- At least one section per non-trivial document.`
        - Expected: `- At least one section (an empty cleaned document is a cleaning failure).`

3. [completed] Reduce docstring/model drift - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - RESOLVED: the per-rule enumeration is replaced with a brief summary that points to `cleaned_models.CleanedDocument` as the authoritative invariant list, keeping the high-level "validates against the shared schema" framing.
   - 3.1. [suggestion] Lines 8-16: The module docstring re-lists each invariant enforced by `CleanedDocument`/`Section`. Since the validator delegates wholly to that model, this duplicated spec will drift from the schema over time — finding 2.1 is an instance of that drift already occurring. Consider summarising the checks and pointing to `cleaned_models.CleanedDocument` as the authoritative list, rather than restating each rule.

4. [completed] Make the cleaned_models import less CWD-dependent - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - RESOLVED: the insert is now `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` (parents[1] == code/file_ingestion), anchored to the file location; the `.claude/skills/.../scripts` insert is left as-is per repo convention.
   - 4.1. [suggestion] Line 32: `sys.path.insert(0, "code/file_ingestion")` is a relative path, so the import only resolves when the script is launched from the repo root. The accompanying comment justifying *why* the schema is imported from the package root (to avoid the heavy `docling_core` dependency) is good and accurate; the concern is solely the path's CWD-dependence. Anchoring it to this file's location would make it robust:
        - Current: `sys.path.insert(0, "code/file_ingestion")`
        - Expected: `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` (`parents[1]` is `code/file_ingestion`). The `.claude/skills/.../scripts` insert at line 35 follows the documented repo convention and is left as-is.

5. [pending] Symmetrise PASS/FAIL logging - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - INTENTIONALLY LEFT AS-IS: the PASS-inside-helper / FAIL-in-main split is harmless and was left unchanged by request.
   - 5.1. [suggestion] Lines 77 vs 140-143: PASS is logged inside `validate_cleaned_file`, while FAIL messages are only returned and logged later in `main()`. The split is harmless but slightly asymmetric; consider returning the PASS state too (or logging both in the same place) so the helper's logging responsibility is consistent. Low priority.

## Skills with No Issues

1. Type Hints: No issues found - `validate_cleaned_file(json_path: Path) -> list[str]` and `main() -> None` are fully annotated; `all_failures: list[str]` uses modern syntax.
2. Docstrings (structure): No issues found - module docstring plus Google-style docstrings on both functions with Args/Returns; the only docstring problem is the factual claim flagged in 2.1, not missing or malformed sections.
3. Comments: No issues found - comments explain the "why" (lines 29-31 justify importing the schema from the package root; lines 64-65 explain the single-except covering parse and shape; lines 70-71 explain the `<document>` loc fallback; lines 127-128 explain the directory-level diagnostic). All accurate.
4. Logging: No issues found - uses `logconfig` (`setup_logging`/`get_logger`), no `print()`, f-strings throughout, `"=" * 60` run separators at start/end, log dir mirrors script location (`logs/file_ingestion/data_validation`).
5. Exception Handling: No issues found in the handlers present - config read catches `(tomllib.TOMLDecodeError, OSError)` (line 110), config-field access catches `KeyError` (line 119), and the schema check catches `pydantic.ValidationError` (line 68); all specific, all with context. (The read-path gap in 1.1 is a missing branch, not a misused handler.)
6. Executable Scripts: No issues found - `main()` with `if __name__ == "__main__"`, single required `--config` argument, logging deferred until after argparse, `sys.exit(1)` on every failure path and clean exit on success.
7. Data Validation: No issues found - correctly named `data_val_` and located under `data_validation/` alongside the validated module.
8. Unit Tests: N/A - target is the validator script itself; no test file under review.
9. SQL: N/A - this file contains no SQL.

## Status & Next Steps

**Current Status**: Implementation complete. Findings 1, 2, 3, 4 resolved; finding 5.1 intentionally left as-is. Full suite green (148 passed), real config exits 0, non-UTF-8 sanity confirmed (FAIL, no crash).
**Completed**:
1. Read the file in full plus `cleaned_models.py`; confirmed the validator delegates wholly to `CleanedDocument`.
2. Verified the error-to-message conversion and `<document>` loc fallback: `model_validator(mode="after")` invariants and malformed-JSON (`json_invalid`) both produce empty `loc` → `<document>`; section-level field errors produce `sections.N.<field>`. Correct.
3. Verified config resolution and exit codes: missing config, TOML decode error, missing `[clean].cleaned_dir` / `[module].documents` key, empty `stems`, and missing `cleaned_dir` all `sys.exit(1)` with distinct messages; success exits 0. All 35 configs carry `[module].documents` with `file` keys.
4. Verified the read-path defect empirically (`UnicodeDecodeError` is not an `OSError`) and confirmed `main()`'s loop has no surrounding try/except.
5. Confirmed the prior review (`20260618v01`) targets a superseded hand-rolled implementation; its findings no longer apply.
**Next Steps**:
1. Fix the read path so a non-UTF-8 file becomes a FAIL message rather than a run-crashing traceback (1.1).
2. Correct the "non-trivial document" docstring claim (2.1).
3. Optionally reduce docstring/model drift, anchor the import path, and symmetrise logging (3.1, 4.1, 5.1).
**Blockers**:
1. None.
**Notes**:
1. Delegating wholly to the Pydantic model is the right design: `strict=True` + `extra="forbid"` reject bool-as-int, number-as-string, and unknown/missing keys at field validation (before the `model_validator`), which is exactly why the prior review's crash and bool findings cannot recur. The one structural caveat is that the model cannot see the read path — hence finding 1.1 — and re-documenting its invariants in the validator docstring invites drift (finding 2.1 is that drift).
2. Nothing material is left unchecked beyond the read-path encoding case: the schema covers shape, types, `n_parsed_sections == len(sections)`, contiguous 1-based `sort_order`, `word_count`, `page_start <= page_end`, and non-empty sections.
