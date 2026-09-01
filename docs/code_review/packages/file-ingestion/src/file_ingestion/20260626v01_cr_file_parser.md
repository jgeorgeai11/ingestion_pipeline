---
name: cr-file_parser
goal: Verify the JSON-default switch and supporting format/atomicity logic in code/file_ingestion/file_parser.py align with python-development skills.
created: 2026-06-26 00:00:00
updated: 2026-06-26 00:00:00
---

## Summary

Reviewed the current state after the default `output_formats` switch from `["markdown"]` to `["json"]`. The change is consistent end-to-end: the module docstring (lines 1-9) states JSON is authoritative and markdown/html/yaml remain available on request, the parameter default is `["json"]` (lines 136-137), the function docstring matches ("Defaults to `["json"]` when None", line 89), empty-list rejection (lines 141-142) and unknown-format rejection via `FORMAT_CONFIG` (lines 145-147) both fire before any conversion, and the per-format atomic export loop (lines 193-211) plus `_export_atomic` (lines 222-272) are unchanged and correct. The generic multi-format capability is intentionally retained at this library boundary and is not flagged. Prior findings (1.1 `FormatConfig` TypedDict, 2.1 per-output atomicity docstring note, 3.1 neutral `.tmp` temp suffix) remain correctly applied. Measured coverage of `file_parser.py` is 100% (verified). No critical, major, minor, or suggestion findings.

Counts: 0 critical, 0 major, 0 minor, 0 suggestions (clean).

## Implementation Plan

No findings. The reviewed change and its supporting logic are correct and skill-conformant.

Verification performed (no change required):

1. [verified] Default switch consistency — `code/file_ingestion/file_parser.py`
   - Module docstring (lines 3-6) describes Docling JSON as the authoritative, lossless, only-format-the-clean-step-reads output, with markdown/html/yaml "available on request but not produced by default" — consistent with the new behaviour.
   - The parameter default resolves to `["json"]` (lines 136-137), and the `output_formats` docstring (line 89) says "Defaults to `["json"]` when None. Must be non-empty." — matches the code.
   - `FORMAT_CONFIG` (lines 42-50) still carries `json` with `save_as_json`/`.json`; markdown/html/yaml/doctags/text remain present so they validate on explicit request.

2. [verified] Format validation — `code/file_ingestion/file_parser.py`
   - Empty list rejected with `ValueError("output_formats must contain at least one format")` (lines 141-142), with a comment explaining the rationale (an empty list would convert every file but write nothing, then falsely report success).
   - Unknown formats rejected via `invalid = [f for f in output_formats if f not in FORMAT_CONFIG]` then `ValueError` (lines 145-147). Both checks run before the `DocumentConverter` is built, so no conversion work precedes a config error. The `Raises` section (lines 99-102) documents both `ValueError` cases and the accumulated `RuntimeError`.

3. [verified] Per-format atomic export loop — `code/file_ingestion/file_parser.py`
   - The loop (lines 193-211) calls `_export_atomic` per format, accumulates per-format failures into `failures`, sets `file_had_export_failure`, and only appends to `successes` when no format failed (lines 209-210) — matching the per-output-file atomicity caveat documented at lines 77-82. `_export_atomic` writes to a neutral `.tmp` temp file in the output dir then `os.replace`s atomically (lines 250-268) and unlinks the temp on any failure (lines 269-272); the `text` special-case and the "method is None" defensive guard (lines 257-266) are intact.

## Skills with No Issues

1. Type Hints: No issues — `parse_files_docling` and `_export_atomic` carry full modern annotations; `FormatConfig` TypedDict keeps `cfg["ext"]` a concrete `str` at the `mkstemp`/f-string sites; the lazy `DoclingDocument` type is supplied via `TYPE_CHECKING` (lines 20-23).
2. Docstrings: No issues — Google-style with Args/Returns/Raises/Note; the default value, the empty/unknown-format and pdf_backend `ValueError`s, the accumulated `RuntimeError`, and the per-output (not per-source) atomicity caveat are all documented and match the code.
3. Comments: No issues — comments explain "why" (lazy Docling import, single-source allow-list with drift assert, same-filesystem temp for atomic replace, neutral `.tmp` suffix, empty-list rejection rationale, pdf-only log-label rationale). None stale after the default switch.
4. Logging: No issues — `logconfig.get_logger`, f-strings, appropriate levels, no `print`, no Entering/Exiting noise; `_export_atomic` stays silent so the caller owns per-export log lines.
5. Exception Handling: No issues — no bare `except`; messages carry file/format/error context; the resilient broad `except Exception` (lines 183, 199) logs-records-continues; `_export_atomic` re-raises unchanged after cleanup; the aggregate `RuntimeError` (lines 215-217) intentionally summarizes multiple failures without `from e`; the drift `assert` (lines 122-125) is a developer invariant on a module-internal mapping.
6. Executable Scripts: N/A — library module; `ingest.py` owns the entry point.
7. Data Validation: N/A — not a data-validation script.
8. Unit Tests: N/A for this file — tests live in `unit_tests/test_file_parser.py` (reviewed separately); `file_parser.py` measures 100% coverage.
9. SQL Development: N/A — no SQL.

## Status & Next Steps

**Current Status**: Complete — clean. The JSON-default switch and all supporting validation/atomicity logic are correct and consistent across docstrings, defaults, and code. No findings.
**Completed**:
1. Read the python-development core skills and the prior `20260622v01_cr_file_parser.md` review.
2. Verified the default (`["json"]`), empty-list and unknown-format rejection, the per-format atomic export loop, and `_export_atomic` against the source.
3. Confirmed the module/function docstrings and comments are consistent with the new default.
4. Ran the suite with `--cov=file_parser --cov-report=term-missing` (100% coverage; `.coverage` removed afterward).
**Next Steps**:
1. None.
**Blockers**:
1. None.
**Notes**:
1. The generic multi-format capability is intentionally retained here (library boundary) per the brief and is not flagged as dead code.
2. No source/test files were modified and nothing was committed.
