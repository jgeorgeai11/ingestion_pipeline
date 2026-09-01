---
name: cr-test_data_val_cleaned_json
goal: Address code quality issues identified in code/file_ingestion/unit_tests/test_data_val_cleaned_json.py to align with python-development (unit-tests) skill standards.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

`test_data_val_cleaned_json.py` is a well-structured, focused suite for `validate_cleaned_file`. It groups tests into valid / failure / type-guard classes, uses local builders (`_make_section`, `_write_file`, `_valid_payload`) to keep cases readable, and relies on the `tmp_path` built-in fixture correctly. All 15 tests pass (`uv run pytest ... -q` → 15 passed). The refactor note to stop asserting exact Pydantic message strings was followed: failure tests no longer match schema wording, which is the right call.

The main weakness is the swing from "match exact message" to "assert the list is merely truthy". `assert result` confirms *some* failure was returned but not that the *intended* invariant fired. For tests where the constructed payload violates only one invariant this is harmless, but it leaves the suite unable to catch a regression where the wrong check trips (e.g. a payload meant to test sort_order that instead fails on count). A stable middle ground exists: assert on the field `loc` (field names like `sort_order`, `word_count`, `page_start`), which the validator emits and which is owned by the schema's field names — not by Pydantic's prose. Secondary gaps: missing-key, `extra="forbid"`, negative `word_count` (the `ge=0` boundary), and the `OSError` read-failure branch are untested; the many near-identical failure tests are good parametrize candidates.

## Implementation Plan

1. [completed] Strengthen failure assertions to confirm the *intended* invariant - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - RESOLVED: every weak `assert result` now checks the schema-owned field name in the returned FAIL string (`any("<field>" in f for f in result)`) — sort_order, word_count, page_start, n_parsed_sections, content_text, heading_text, and the bool type-guards — verified empirically that each case fires for exactly one reason with the discriminating token present. The 1.5 page_start case now uses `page_start="1"` (not `"5"`) so the type rejection is isolated from the ordering invariant.
   - 1.1. [major] Lines 144-146: `test_..._non_contiguous_sort_order_caught` asserts only `assert result`. The payload (`n_parsed=3`, sort 1,2,4) does isolate the sort_order error today (verified), but the assertion does not pin that. Assert on the stable field name in the emitted message instead.
        - Current: `assert result`
        - Expected: `assert any("sort_order" in f for f in result)`
   - 1.2. [major] Lines 153-155: `test_..._word_count_mismatch_caught` — same issue; tie the assertion to the field.
        - Current: `assert result`
        - Expected: `assert any("word_count" in f for f in result)`
   - 1.3. [major] Lines 162-164: `test_..._page_start_after_end_caught` — distinguish from word_count by checking the field/loc.
        - Current: `assert result`
        - Expected: `assert any("page_start" in f for f in result)`
   - 1.4. [minor] Lines 133-135: `test_..._n_parsed_mismatch_caught` — confirm the count check fired, not an unrelated one.
        - Current: `assert result`
        - Expected: `assert any("n_parsed_sections" in f for f in result)`
   - 1.5. [minor] Lines 222-229: `test_..._non_int_page_start_caught` uses `page_start="5", page_end=2`. With `page_start="5"` rejected by strict typing, the docstring claim "flagged rather than silently skipped" is true, but the assertion can't tell a type rejection from the unrelated `page_start>page_end` ordering this payload also implies. Use a non-ordering value (e.g. `page_start="1"`) and assert on `page_start`.
        - Current: `section = _make_section(1, page_start="5", page_end=2)` then `assert result`
        - Expected: `section = _make_section(1, page_start="1", page_end=2)` then `assert any("page_start" in f for f in result)`

2. [completed] Cover untested schema constraints (all verified to fire today, none exercised) - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - RESOLVED: added tests for negative word_count (ge=0 boundary, heading-only so the boundary is isolated), extra='forbid' at both document and section level, a missing required key at both document (`sections`) and section (`word_count`) level, the non-UTF-8 read path (becomes a FAIL, not a crash — covers source fix 1.1), and the genuine OSError read-failure branch (a directory path that exists but cannot be read).
   - 2.1. [major] Coverage gap: `Section.word_count = Field(ge=0)` boundary is untested. A negative `word_count` raises `greater_than_equal`. Add `test_validate_cleaned_file_negative_word_count_caught` (e.g. `heading_text=None, content_text=None, word_count=-1`).
   - 2.2. [minor] Coverage gap: `model_config = ConfigDict(extra="forbid")` is untested at both levels. An unexpected top-level key and an unexpected section key both raise `extra_forbidden`. Add a test that an extra key is rejected.
   - 2.3. [minor] Coverage gap: a missing required key (e.g. payload without `sections`, or a section without `word_count`) raises `missing`. Add a test; this is a realistic "stale/corrupt file" failure mode.
   - 2.4. [suggestion] Coverage gap: the `except OSError` read-failure branch (`data_val_cleaned_json.py:61-62`) is unreachable by the current tests because `exists()` passing implies a readable file. Optionally cover via a path that exists but raises on read (e.g. a directory passed as `json_path`, or `mocker.patch` on `Path.read_text`).

3. [completed] Reduce duplication and tighten the malformed-JSON case - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - RESOLVED: the malformed-JSON test now asserts `len(result) == 1` (locks in "reported, not raised"). Parametrize (3.1) was left as explicit tests: each failure case names its own intended field token in its assertion and docstring, which reads more clearly than a parametrized table here.
   - 3.1. [suggestion] Lines 127-172, 178-229: The failure/type-guard tests are near-identical (build payload → write → assert). Per unit-tests 5.1, consolidate with `@pytest.mark.parametrize` over `(payload_builder, expected_loc_substring)` so adding a case is one line and each case still pins the intended field.
   - 3.2. [minor] Lines 118-125: `test_..._malformed_json_reports_failure` asserts only `assert result`. The malformed-input path emits a Pydantic `json_invalid` error (verified), distinct from a schema violation. Assert it is reported as exactly one failure to lock in "reported, not raised" behavior.
        - Current: `assert result`
        - Expected: `assert len(result) == 1`

4. [completed] Minor clarity items - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - RESOLVED: 4.1 needed no change (the null-handling happy-path tests already assert `result == []`). 4.2 applied — both bool type-guard tests now assert on the field loc (`n_parsed_sections`, `sort_order`) so they keep their meaning if strictness is ever relaxed.
   - 4.1. [suggestion] Lines 82-91 / 93-100: The two null-field happy-path tests live in `TestValidateCleanedFileValid` alongside the headline pass test, which is correct, but consider asserting the message is empty via `assert result == []` consistently (already done) — no change needed; noting only that the class docstring "Happy-path tests." could name the null-handling guarantee these two encode.
   - 4.2. [suggestion] Lines 202-220: `test_..._bool_n_parsed_rejected` and `test_..._bool_sort_order_rejected` are valuable (bool-as-int is a classic trap) and currently rely on `strict=True`. Consider asserting on the field `loc` so they keep their meaning if the schema's strictness is ever relaxed.

## Skills with No Issues

1. unit-tests — pytest, not unittest: No issues (uses plain pytest classes/functions).
2. unit-tests — file naming `test_<module>.py`: No issues (`test_data_val_cleaned_json.py` matches `data_val_cleaned_json.py`).
3. unit-tests — function naming `test_<function>_<scenario>_<expected>`: No issues (all names follow the pattern).
4. unit-tests — Arrange-Act-Assert: No issues (each test is cleanly arranged, acts via one call, asserts once).
5. unit-tests — tmp_path usage: No issues (uses the built-in `tmp_path`; no manual temp dirs or cleanup).
6. unit-tests — single behavior per test: No issues (each test targets one invariant/path).
7. unit-tests — independence / no shared state: No issues (builders return fresh dicts per call; no ordering dependence).
8. unit-tests — no asserting on private/internal state: No issues (asserts only on the public return list).
9. unit-tests — mock external boundaries only: N/A — tests use real files via `tmp_path` rather than mocking the filesystem, which is appropriate here.
10. Type Hints: No issues (helpers and tests are fully annotated; `# type: ignore[arg-type]` used intentionally for deliberate bad inputs).
11. Docstrings: No issues (module, classes, helpers, and tests all carry docstrings).
12. Comments: No issues.
13. Logging / Exception Handling / Executable Scripts / Data Validation / SQL: N/A — this is a test module.

## Status & Next Steps

**Current Status**: Implementation complete. The weak `assert result` assertions now check the field loc/message token (confirming the intended invariant fired), and the missing cases (ge=0 negative word_count, extra='forbid' at both levels, missing required key, non-UTF-8 read path, and the genuine OSError read-failure branch) are covered. Full suite green (148 passed); the test file alone is 22 passed.
**Completed**:
1. Read code-review and unit-tests skills in full.
2. Read the test file, `data_val_cleaned_json.py`, and `cleaned_models.py`.
3. Ran the suite (15 passed) and empirically verified: malformed JSON → `json_invalid` ValidationError; non-contiguous payload isolates the sort_order error; missing-key/extra-key/negative-word_count all raise but are untested.
**Next Steps**:
1. Tie failure assertions to the field `loc` (Plan 1) so tests confirm the right invariant.
2. Add the four missing-coverage tests (Plan 2).
3. Optionally parametrize the failure/type-guard cases (Plan 3.1).
**Blockers**:
1. None.
**Notes**:
1. Loosening exact-message assertions was correct; the remaining risk is `assert result` being *too* loose. Asserting on field names (not Pydantic prose) keeps tests robust to schema-message changes while still pinning the cause.
