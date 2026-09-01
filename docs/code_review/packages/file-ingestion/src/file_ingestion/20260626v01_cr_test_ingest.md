---
name: cr-test_ingest
goal: Verify the reworked TestStepParse suite covers the JSON-only step_parse contract and leaves no gap from the three deleted tests, per the unit-tests skill.
created: 2026-06-26 00:00:00
updated: 2026-06-26 00:00:00
---

## Summary

Reviewed the reworked `TestStepParse` (lines 199-388) for the JSON-only `step_parse` (the `output_formats` parameter was removed; `step_parse` now hardwires `["json"]` at ingest.py:292). All eight behaviours the brief lists are covered: skip-on-parsed-json-present (`test_parsed_json_present_skips_file`, 202), empty-json-reparse (`test_empty_output_reparses_file`, 219), overwrite-bypass (`test_overwrite_bypasses_skip_sentinel`, 235), per-group dispatch (`test_mixed_batch_dispatches_one_call_per_group`, 253), RuntimeError-accumulate-and-continue (`test_group_failure_records_failure_and_continues`, 277), ValueError(pdf_backend)-propagate (`test_value_error_propagates_immediately`, 369), already-parsed-counts-as-ok (`test_already_parsed_file_counts_as_ok`, 351), and all-groups-succeed (`test_all_groups_succeed_returns_all_ok`, 322). The three deleted tests leave no coverage gap (argued explicitly below). Mock realism is good: `fake_parse` closures write the JSON on disk so survivors are determined the same way `step_parse` does, and assertions pin both `ok_files` and the exact failure dict shape. `ingest.py` measures 98% (the 6 misses are `get_engine` URL wiring at 73-75/86-94 and the `__main__` guard at 781 — none in `step_parse`). Findings are a formatting artifact on five call sites and a narrow return-value assertion gap on the reparse path.

Counts: 0 critical, 0 major, 1 minor, 1 suggestion (2 findings).

### No coverage gap from the three deletions

- The two deleted unknown-output-format tests: that validation moved to `file_parser` and IS tested there — `test_invalid_output_format_raises_value_error` (test_file_parser.py:172) and `test_empty_output_formats_raises_value_error` (test_file_parser.py:326). `step_parse` no longer accepts `output_formats`, so the validation cannot be reached through it and there is nothing left to cover at the `ingest` level.
- The deleted multi-format partial-output test: the scenario is structurally gone because `step_parse` is hardwired to `["json"]` (ingest.py:292). A single-format step cannot produce a partial multi-format output, so there is no behaviour to cover. The single-format export-failure path (a group raising `RuntimeError`) is still covered by `test_group_failure_records_failure_and_continues` (277).

## Implementation Plan

1. [completed] Formatting artifact: two kwargs run together on one line — `code/file_ingestion/unit_tests/test_ingest.py`
   - 1.1. [minor] Lines 230, 262, 310, 345, 362: each places `parsed_dir=str(...)` and the following keyword argument on the same physical line separated by a run of spaces, e.g. `parsed_dir=str(parsed_dir),            overwrite=False,`. This is a stray editing artifact (the other call sites in the same class put each kwarg on its own line, e.g. lines 211-214). It is purely cosmetic — the tests pass — but it reads as accidental and would not survive a formatter; align it with the one-kwarg-per-line style used elsewhere in the class.
        - Current: `            parsed_dir=str(parsed_dir),            overwrite=False,`
        - Expected: `            parsed_dir=str(parsed_dir),\n            overwrite=False,`

2. [completed] Reparse-path tests discard the return value — `code/file_ingestion/unit_tests/test_ingest.py`
   - 2.1. [suggestion] Lines 202-217 `test_parsed_json_present_skips_file` and lines 219-233 `test_empty_output_reparses_file` assert only on `mock_parse` (called / not-called) and discard `step_parse`'s `(ok_files, failures)` return. The skip path's return IS pinned elsewhere by `test_already_parsed_file_counts_as_ok` (351, asserts `ok_files == ["a.pdf"]`, `failures == []`), so the gap is narrow: the empty-output reparse path's return is never asserted. Because the mock `parse_files_docling` writes nothing, after reparse the JSON is still absent, so `test_empty_output_reparses_file` would currently return `(["a.pdf"] would-be empty, [failure])` — capturing `ok_files, failures` and asserting the reparsed-but-unwritten file lands in `failures` (or having the mock write the JSON and asserting it lands in `ok_files`) would pin the survivor computation on the reparse branch, matching the rigour of the group-failure and all-succeed tests. This mirrors the v01 "discards return value" pattern (v01 finding 2.1); suggestion only.

## Skills with No Issues

1. Unit Tests (pytest, not unittest): No issues — pytest with `mocker`/`tmp_path` throughout.
2. Unit Tests (file naming): No issues — `test_ingest.py` for `ingest.py`.
3. Unit Tests (function naming): No issues — names are descriptive and scenario/expected-shaped (e.g. `test_group_failure_records_failure_and_continues`, `test_value_error_propagates_immediately`).
4. Unit Tests (single behavior / AAA): No issues — each `TestStepParse` test targets one behaviour and is clearly arranged.
5. Unit Tests (mock external boundaries only, patch where used): No issues — `parse_files_docling` is patched at `ingest.parse_files_docling` (where used). The `fake_parse` closures realistically write JSON on disk so survivors are derived the same way the production code does — not a tautology.
6. Unit Tests (pytest utilities — parametrize, `pytest.raises(match=)`): No issues — `test_value_error_propagates_immediately` uses `pytest.raises(ValueError, match="bad pdf_backend")`; record-and-continue paths correctly assert the failure dict rather than raising.
7. Unit Tests (built-in fixtures): No issues — `tmp_path` and `mocker` used; the `_write` helper (line 193) builds on-disk JSON fixtures.
8. Unit Tests (no shared state / no private-attribute assertions): No issues — tests are independent; assertions target return tuples, the failure dict shape, and mock call records, not private internals.
9. Unit Tests (comprehensive coverage): Satisfied for `step_parse` — all eight brief-listed behaviours covered; `ingest.py` at 98% with the only misses outside `step_parse` (`get_engine` wiring 73-75/86-94, `__main__` guard 781). The three deletions leave no gap (argued above). One branch nuance: the `reason = "; ".join(group_failures) or "parsed JSON output missing or empty"` fallback (ingest.py:305) is never exercised with an empty `group_failures` AND a missing JSON — every failing test routes through a group `RuntimeError`; statement coverage is 100%, so this is a residual branch nuance, not a gap.
10. Type Hints: No issues — tests and the `fake_parse` closures carry full parameter and `-> None` annotations.
11. Docstrings: No issues — every test has a clear docstring; the group-failure and all-succeed tests document the survivors-from-disk rationale.
12. Comments: No issues — inline comments accurately scope what each mock proves (e.g. the OCR-group-fails / non-OCR-group-writes split at lines 298-303).
13. Logging: N/A — test module.
14. Exception Handling: N/A in tests; the propagate-vs-record split is asserted via `pytest.raises` and failure-dict assertions respectively.
15. Data Validation: N/A — no data-validation code in this class.
16. Executable Scripts: N/A — `step_parse` is a library function, well covered here.
17. SQL Development: N/A — no SQL in `TestStepParse`.

## Status & Next Steps

**Current Status**: Review complete. `TestStepParse` covers all eight required behaviours and the deletions leave no gap. Suite green; `ingest.py` at 98% (misses outside `step_parse`). Two findings: a cosmetic two-kwargs-per-line artifact (minor) and a narrow reparse-return assertion gap (suggestion).
**Completed**:
1. Read the unit-tests skill and the prior `20260622v02_cr_test_ingest.md` review.
2. Read the current `step_parse` (ingest.py:198-318) and the reworked `TestStepParse`.
3. Mapped each of the eight brief-listed behaviours to a test and confirmed the three deletions leave no gap.
4. Ran `--cov=ingest --cov=file_parser --cov-report=term-missing` (66 passed, ingest 98%; `.coverage` removed afterward) and confirmed the 6 missed lines are all outside `step_parse`.
**Next Steps**:
1. Optionally fix 1.1 (line formatting) and 2.1 (reparse-path return assertion).
**Blockers**:
1. None.
**Notes**:
1. The brief's removal scope (the `output_formats` param and the three tests) is correct: unknown-format validation now lives only at the `file_parser` level (and is tested there).
2. No source/test files were modified and nothing was committed.
