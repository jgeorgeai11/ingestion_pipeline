---
name: cr-test_data_val_cleaned_json
goal: Address code quality issues identified in code/file_ingestion/unit_tests/test_data_val_cleaned_json.py to align with python-development (unit-tests) skill standards.
created: 2026-06-22 18:16:39
updated: 2026-06-22 18:16:39
---

## Summary

`test_data_val_cleaned_json.py` is a strong, well-organised suite for `validate_cleaned_file`. The migration to the new `document` envelope is clean: every fixture builds the two-key shape through `_make_payload` (or constructs the `document` block explicitly for the malformed cases), and there are no stale flat-shape fixtures left behind. The v01 refactor held — failure tests pin the schema-owned field name in the returned FAIL string (`any("<field>" in f for f in result)`) rather than matching Pydantic prose, and I verified empirically that each new provenance case fires on the intended field: missing `binary_hash` → `document.binary_hash: Field required`, negative `binary_hash` → `document.binary_hash: Input should be greater than or equal to 0`, extra document-block key → `document.surprise: Extra inputs are not permitted`. The valid-passes and null-handling happy paths correctly assert `result == []`, and the OSError read-failure test is well-pinned (`len(result) == 1` plus `"could not read JSON"`). All 25 tests pass (`uv run pytest ... -q` → 25 passed). Coverage of `cleaned_models.py` is 95% (lines 76 and 167 uncovered).

The headline issue is a single reversion to the v01 weak-assertion pattern: the non-UTF-8 read-path test still asserts bare `assert result`, looser than its malformed-JSON sibling (which asserts `len == 1`). A secondary, subtler recurrence of the same concern: `test_..._missing_sections_key_caught` asserts `any("sections" in f ...)`, but `"sections"` is a substring of `n_parsed_sections` / `len(sections)` / `has zero sections`, so the token is present-but-not-discriminating — a regression that tripped the count invariant on that payload would still pass. Coverage gaps: the `Section` "neither heading nor content" invariant (`cleaned_models.py:76`, reachable and untested), and a type-guard for the *new* `binary_hash` field (the suite guards bool-as-int for `n_parsed_sections` and `sort_order` but not `binary_hash`).

## Implementation Plan

1. [pending] Tighten the one reverted weak assertion and the non-discriminating tokens - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - 1.1. [completed] RESOLVED 2026-06-22: Tightened `test_validate_cleaned_file_non_utf8_file_reports_failure` from bare `assert result` to `assert len(result) == 1` (matching its malformed-JSON sibling); fixture kept (the `\xff\xfe...` bytes still exercise the read_bytes path); full suite green (186 passed).
   - 1.1. [major, completed] Lines 159-166: `test_..._non_utf8_file_reports_failure` asserts bare `assert result` — the exact v01 pattern the prior review removed elsewhere. The fixture is correct (the `\xff\xfe...` bytes genuinely exercise the read path: they *would* raise `UnicodeDecodeError` under `read_text`, which is the crash the source fix at `data_val_cleaned_json.py:60-68` protects against). The defect is purely the loose assertion: `assert result` proves *some* failure, not that the file was reported as exactly one FAIL rather than raised. Match the malformed-JSON sibling (line 157).
        - Current: `assert result`
        - Expected: `assert len(result) == 1`
   - 1.2. [completed] RESOLVED 2026-06-22: Pinned `test_validate_cleaned_file_missing_sections_key_caught` to `assert any("sections: Field required" in f for f in result)` (verified the validator emits loc `sections` / msg `Field required`), so the non-discriminating `"sections"` substring no longer passes on a count-mismatch regression; full suite green (186 passed).
   - 1.2. [minor, completed] Lines 305-314: `test_..._missing_sections_key_caught` asserts `any("sections" in f for f in result)`. `"sections"` is a substring of `n_parsed_sections`, `len(sections)`, and `has zero sections`, so the token is present-but-not-discriminating — if a regression made this payload trip the count invariant (`n_parsed_sections (1) != len(sections) (0)`) instead of the missing-key error, the test would still pass falsely. This is the v01 "pin the RIGHT invariant" concern recurring at a subtler level. The actual message is `sections: Field required` (verified); pin the discriminating tail.
        - Current: `assert any("sections" in f for f in result)`
        - Expected: `assert any("sections: Field required" in f for f in result)`
   - 1.3. [suggestion] Lines 241-249, 251-259, 293-303: the three `extra="forbid"` tests all assert `any("surprise" in f ...)`. Each payload isolates one level today, but the shared token cannot tell which `loc` fired (top-level `surprise`, `document.surprise`, or `sections.0.surprise` — all verified distinct), so a `loc` regression would not be caught. Optionally pin each to its loc prefix (e.g. `"document.surprise"` for the document-block case at line 303) so the three tests cannot pass each other's failure.

2. [pending] Close the remaining coverage gaps (all confirmed reachable + untested) - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - 2.1. [minor] Coverage gap: `cleaned_models.py:76` — the `Section` "section has neither heading nor content" invariant — is the only reachable-but-uncovered branch (pytest-cov reports `cleaned_models.py 95% ... 76, 167`; line 167 is correctly documented as unreachable in the empty-sections test docstring). A section with `heading_text=None` and `content_text=None` and `word_count=0` fires it: verified `→ sections.0: Value error, section has neither heading nor content`. Add `test_validate_cleaned_file_section_neither_heading_nor_content_caught` asserting `any("neither heading nor content" in f for f in result)` (the loc is bare `sections.0`, so the field-name approach does not apply — assert on the invariant phrase).
   - 2.2. [minor] Coverage gap: the new `binary_hash` field has no type-guard test. The suite guards bool-as-int for `n_parsed_sections` (line 354) and `sort_order` (line 364) under `strict=True`, but not for `binary_hash`. A bool or string `binary_hash` is rejected (verified: both `→ document.binary_hash: Input should be a valid integer`). Add `test_validate_cleaned_file_non_int_binary_hash_caught` asserting `any("binary_hash" in f for f in result)` so the new provenance field gets the same type-guard coverage as its sibling int fields.
   - 2.3. [suggestion] Coverage gap: a payload missing the top-level `document` key entirely is untested (the suite tests a missing key *inside* the document block via 2.1's sibling, and a missing top-level `sections`, but not a missing `document`). Verified `→ document: Field required`. A realistic stale/corrupt-file mode; cheap to add alongside the existing `test_..._missing_sections_key_caught`.

3. [pending] Optional structural improvement (carried from v01, still applicable) - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - 3.1. [suggestion] Lines 177-383: the failure and type-guard tests are near-identical (build payload → write → assert the field token). Per unit-tests 5.1, these are candidates for `@pytest.mark.parametrize` over `(payload_builder, expected_loc_substring)`, which would make adding a case one line and keep each case pinned to its field. As in v01, the explicit-test form is also defensible (each docstring names its intended invariant); noted as an option, not a defect.

## Skills with No Issues

1. unit-tests — pytest, not unittest: No issues (plain pytest classes/functions).
2. unit-tests — file naming `test_<module>.py`: No issues (`test_data_val_cleaned_json.py` matches `data_val_cleaned_json.py`).
3. unit-tests — function naming `test_<function>_<scenario>_<expected>`: No issues (all names follow the pattern).
4. unit-tests — Arrange-Act-Assert: No issues (each test arranges a fixture, acts via one `validate_cleaned_file` call, asserts on the return list).
5. unit-tests — `tmp_path` usage: No issues (uses the built-in `tmp_path`; no manual temp dirs or cleanup).
6. unit-tests — single behavior per test: No issues (each test targets one invariant/path; the negative-word_count and non-int-page_start cases deliberately isolate the boundary/type failure from the ordering/empty invariants, per their inline comments).
7. unit-tests — independence / no shared state: No issues (`_make_section`/`_make_payload`/`_valid_payload` return fresh dicts per call; no ordering dependence).
8. unit-tests — no asserting on private/internal state: No issues (asserts only on the public return list, never on schema internals).
9. unit-tests — mock external boundaries only: N/A — tests use real files via `tmp_path` rather than mocking the filesystem, which is appropriate; the OSError branch is exercised with a real directory path rather than a patch.
10. unit-tests — comprehensive coverage / all paths: Mostly met; see Plan 2 for the three remaining reachable gaps (line 76, `binary_hash` type-guard, missing `document` key).
11. Type Hints: No issues (helpers and tests fully annotated; `# type: ignore[arg-type]` used intentionally for deliberate bad inputs at lines 125, 211, 378).
12. Docstrings: No issues (module, classes, helpers, and every test carry docstrings; the failure-class docstring at lines 133-141 accurately explains the field-loc assertion strategy, and the empty-sections docstring correctly explains why the count check fires first).
13. Comments: No issues (the inline notes isolating each failure cause — e.g. lines 169-171, 209-210, 376-377 — are accurate and earn their place).
14. Logging / Exception Handling / Executable Scripts / Data Validation / SQL: N/A — this is a test module.

## Status & Next Steps

**Current Status**: Review complete. The envelope migration is correct and the v01 strengthening largely held; one weak assertion reverted (non-UTF-8, `assert result`) and one new non-discriminating token (`"sections"`) recur the v01 concern. Three reachable coverage gaps remain. Full file is green (25 passed); `cleaned_models.py` at 95% (only line 76 reachable-but-uncovered).
**Completed**:
1. Read the code-review and python-development (unit-tests) skills in full, plus the v01 review for context.
2. Read the current test file, `data_val_cleaned_json.py`, and `cleaned_models.py`.
3. Ran the suite (25 passed) and empirically verified every new provenance case's FAIL message (missing/negative/extra-key `binary_hash`), the non-UTF-8 message (`<document>: Invalid JSON`), the empty-sections ordering, and the three reachable gaps (line 76 neither-heading-nor-content, bool/string `binary_hash`, missing top-level `document`).
4. Confirmed via pytest-cov that `cleaned_models.py` lines 76 and 167 are the only uncovered branches (167 is documented-unreachable; 76 is a real gap).
**Next Steps**:
1. Tighten the non-UTF-8 assertion to `len(result) == 1` and the `"sections"` token to `"sections: Field required"` (Plan 1.1, 1.2).
2. Add the three missing-coverage tests (Plan 2): line-76 invariant, `binary_hash` type-guard, missing `document` key.
3. Optionally pin the three `extra="forbid"` tests to their loc and/or parametrize the failure cases (Plan 1.3, 3.1).
**Blockers**:
1. None.
**Notes**:
1. The migration introduced no new weaknesses of its own — the new `document`-block tests are all correctly pinned. The two assertion issues are an old-pattern reversion (1.1) and a substring subtlety (1.2), not migration artefacts.
