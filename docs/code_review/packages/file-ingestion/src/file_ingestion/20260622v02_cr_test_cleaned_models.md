---
name: cr-test_cleaned_models
goal: Address test-quality observations identified in code/file_ingestion/unit_tests/test_cleaned_models.py to align with python-development (unit-tests) skill.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

`test_cleaned_models.py` directly pins the shared `Section` / `Document` / `CleanedDocument` schema after the restructure to the `Document` envelope (`document={n_parsed_sections, binary_hash}` + `sections`). Coverage of the restructured contract is broad and largely strong: positive cases for Section (valid, heading-only, content-only, null pages) and CleanedDocument; negative cases for every Section invariant (word_count mismatch, page ordering, fully-empty, page < 1, negative word_count); the new `Document` sub-model (negative `binary_hash`, extra-key forbid, strict-bool rejection for both fields, valid `2^64-1`); and the new envelope-level cases (count mismatch, zero sections, non-contiguous sort_order, `n_parsed_sections` floor, top-level extra-key forbid, field order). Most negative tests use `pytest.raises(match=...)`, and the message-pinning on the model-validator cases (`"word_count is 99, expected 7"`, the count-mismatch regex, `"sort_order not 1-based contiguous"`) is genuinely strong. The file follows the unit-tests skill well: pytest, clear class grouping, AAA structure, a documented helper, and descriptive `test_<thing>_<scenario>_<expected>` names. 24 tests pass. The gaps are completeness gaps in the test, not defects: the load-bearing Section key order is never asserted, two tests are slightly mis-placed or under-reach their named branch, and a few invariant branches are pinned by only one representative input.

**Counts by severity:** critical: 0, major: 0, minor: 3, suggestion: 5.

**Top issue (minor):** the field-order test (lines 160-174) pins the top-level and `document` key order but never asserts the Section key order, even though the module docstring (cleaned_models.py lines 13-15, 25-27) declares Section field order equally load-bearing for the on-disk JSON — the largest coverage gap.

**Coverage-gap note:** the "uint64 boundary" is only proved on the passing side (`2^64-1` validates, line 235); the rejecting side cannot be tested because the model puts no upper bound on `binary_hash` (`Field(ge=0)` only), so `2^64` and beyond validate. This is a model limitation that the test honestly cannot close — flagged here, not fixable in the test.

## Implementation Plan

1. [pending] Field-order coverage - `code/file_ingestion/unit_tests/test_cleaned_models.py`
   - 1.1. [minor] Lines 160-174: `test_field_order_is_document_then_sections` asserts `list(dumped.keys()) == ["document", "sections"]` and `list(dumped["document"].keys()) == ["n_parsed_sections", "binary_hash"]`, but never asserts the Section key order. The module docstring (cleaned_models.py lines 13-15) states "Field declaration order is load-bearing: model_dump() preserves it, and the clean step writes that dict as JSON" and the Section docstring (lines 25-27) repeats it for Section. Sections are dumped to JSON in the same payload, so their key order (`sort_order, heading_text, content_text, word_count, page_start, page_end`) is equally load-bearing and pinned nowhere. A reorder of Section fields would silently change the on-disk JSON and no test would fail.
        - Current: (no assertion on `dumped["sections"][0]` keys)
        - Expected: add `assert list(dumped["sections"][0].keys()) == ["sort_order", "heading_text", "content_text", "word_count", "page_start", "page_end"]`

2. [pending] Test placement / branch reach - `code/file_ingestion/unit_tests/test_cleaned_models.py`
   - 2.1. [minor] Lines 210-216: `test_n_parsed_sections_below_one_rejected` lives under `TestCleanedDocumentInvariants` and is documented as an envelope invariant, but the `ge=1` bound fires while constructing the inner `Document(n_parsed_sections=0, binary_hash=1)` — i.e. it raises before `CleanedDocument._check_invariants` ever runs. The `sections=[_valid_section()]` argument is inert; the same error is produced regardless of the section list. This duplicates the floor already covered at the Document level conceptually and is mis-placed. Either move it to `TestDocumentSubModel` and construct `Document(n_parsed_sections=0, binary_hash=1)` directly (clearer intent), or keep it here but note in the docstring that the bound is enforced on the sub-model, not the envelope.
        - Current: asserts via `CleanedDocument(document=Document(n_parsed_sections=0, ...), sections=[_valid_section()])`
        - Expected: assert via `Document(n_parsed_sections=0, binary_hash=1)` under `TestDocumentSubModel`
   - 2.2. [completed] RESOLVED 2026-06-22: The model's `CleanedDocument._check_invariants` was reordered (separate cleaned_models fix) so the non-empty `sections` check runs before the count-equality check, making the `"has zero sections"` branch reachable; `test_zero_sections_rejected` now constructs `Document(n_parsed_sections=1, binary_hash=1)` with `sections=[]` and pins the now-reachable branch via `pytest.raises(ValidationError, match="zero sections")`; docstring updated to describe the non-empty path; full suite green (186 passed).
   - 2.2. [minor, completed] Lines 190-200: `test_zero_sections_rejected` proves rejection through the count-equality path, not the named empty-list branch. With `Document(n_parsed_sections=1)` and `sections=[]`, `n_parsed_sections (1) != len(sections) (0)` fires first; the model's dedicated `if not self.sections: raise ValueError("has zero sections")` line (cleaned_models.py line 167) is never reached and is in fact unreachable given `n_parsed_sections` is `ge=1` (any valid `Document` forces `n_parsed_sections >= 1`, which can never equal `len([]) == 0`). The test docstring honestly admits "the exact message varies, hence no match=", so this is a "test cannot reach its named branch" note rather than a hidden defect. The bare `pytest.raises(ValidationError)` is acceptably honest here, but the test name implies it exercises the zero-section guard, which it does not. (The dead model branch is a model observation — out of scope to fix here — noted only to explain why the test cannot pin it.)

3. [pending] Assertion strength / branch representativeness - `code/file_ingestion/unit_tests/test_cleaned_models.py`
   - 3.1. [suggestion] Lines 202-208: contiguity is exercised only via the `[1, 3]` gap. Three other inputs hit the same `actual_orders != expected_orders` branch but distinct real-world corruption modes are unpinned: permutation `[2, 1]` (out of order), duplicate `[1, 1]` (repeated position), and non-1-start `[2, 3]` (off-by-one base). A `@pytest.mark.parametrize` over these would prove the invariant rejects each, not just gaps.
        - Current: single `sections=[_valid_section(sort_order=1), _valid_section(sort_order=3)]`
        - Expected: parametrize sort_order pairs `[(1, 3), (2, 1), (1, 1), (2, 3)]`, all expecting `match="sort_order not 1-based contiguous"`
   - 3.2. [suggestion] Lines 240-243 / 252-255: the `Document` lower-bound and strict cases pin only the rejecting side (`binary_hash=-1`, `binary_hash=True`). The inclusive boundary `binary_hash=0` — the documented floor (`ge=0`) and a realistic value (a source whose low-64 sha256 bits are all zero) — is asserted to *pass* nowhere. `test_valid_document_envelope_passes` (line 235) uses `2^64-1` but not `0`. Add an assertion that `Document(n_parsed_sections=1, binary_hash=0)` validates to pin the inclusive lower edge.
   - 3.3. [suggestion] Lines 87-115, 121-143, 240-260: the model-validator cases use strong message-text matches (`"word_count is 99, expected 7"`, the count-mismatch regex), but the field-bound and strict-bool cases match only the bare field name (`match="word_count"`, `match="binary_hash"`, `match="sort_order"`). `match=` runs `re.search` over the full stringified `ValidationError`, which includes the field `loc` line, so these do pin the offending field — but they do not distinguish a type error from a bound error from an invariant error on that field. For the strict-bool tests in particular, matching the pydantic error type would be stronger, e.g. `match=r"word_count\n\s*Input should be a valid integer"` (or asserting `exc_info.value.errors()[0]["type"] == "int_type"`), which proves the bool was rejected by strict typing specifically and not by some unrelated failure on the same field. This is a deliberate-tradeoff note: the current matches are reasonable; the model-validator cases (which have no field `loc`, only a top-level `value_error`) correctly rely on message text and should keep doing so.

4. [pending] Boundary completeness (model-limited) - `code/file_ingestion/unit_tests/test_cleaned_models.py`
   - 4.1. [suggestion] Line 235: `test_valid_document_envelope_passes` pins that `2^64-1` validates, which reads as a uint64 upper-boundary check, but it is one-sided. There is no test that `2^64` (or any larger value) is rejected, and there cannot be one: `binary_hash` is `Field(ge=0)` with no `le`/`lt`, so `18446744073709551616` and beyond validate cleanly. The "uint64" framing in the docstring is aspirational relative to what the schema enforces. Flagging the gap honestly: if a true uint64 ceiling is intended, that belongs in the model (`Field(ge=0, le=2**64 - 1)`); until then the test should not imply a ceiling it cannot prove. (Model change out of scope here.)
   - 4.2. [suggestion] Lines 95-100: `test_fully_empty_section_rejected` passes `word_count=0`, so the both-None guard (`section has neither heading nor content`) is what fires. Good. Worth one companion case confirming the guard precedes the word_count check — e.g. both-None with a *non-zero* `word_count` (which would also fail the count invariant) still raises the "neither heading nor content" message — to pin the validator's check ordering as deliberate. Optional.

## Skills with No Issues

1. Unit Tests — Use pytest: No issues — `pytest` + `pytest.raises`, no `unittest`.
2. Unit Tests — Naming: No issues — files/functions follow `test_<thing>_<scenario>_<expected>`; classes group by subject (`TestSectionValid`, `TestSectionInvariants`, `TestSectionStrictMode`, `TestCleanedDocumentValid`, `TestCleanedDocumentInvariants`, `TestDocumentSubModel`).
3. Unit Tests — Single behavior / AAA: No issues — each test targets one invariant with clear arrange/act/assert; the `_valid_section` helper keeps arrange minimal and self-consistent (computes `word_count` from text by default).
4. Unit Tests — Test exceptions: Mostly strong; refinements at 3.3 (bare field-name matches) and 2.2 (bare `raises` whose named branch is unreachable). Model-validator message matches are exemplary.
5. Unit Tests — Behavior independence: No issues — no shared state, no order dependence, no assertions on private attributes (the field-order test reads `model_dump()`, a public surface, not internals).
6. Unit Tests — Parametrize: Not used; opportunities noted at 3.1 (contiguity variants) — current explicit tests are still correct.
7. Unit Tests — Mock boundaries: N/A — pure in-memory schema, no external boundaries to mock.
8. Unit Tests — Comprehensive coverage: Strong on the restructured schema; gaps at 1.1 (Section key order), 3.1/3.2 (branch representativeness, inclusive `binary_hash=0`), 4.1 (uint64 ceiling, model-limited).
9. Type Hints: No issues — helper and every test annotated (`-> None`, `-> Section`, modern `str | None` / `int | None` unions).
10. Docstrings: No issues — module and helper have Google-style docstrings; per-test one-line docstrings state the behavior. Note: the `(2.1)` / `(2.2)` / `(1.1)` / `(1.2)` references in test docstrings (lines 96, 103, 108, 191, 211) point at the prior `cr_cleaned_models.md` finding numbers; harmless but coupling test docs to a review doc is mildly brittle if those numbers drift.
11. Logging / Exception Handling / Executable Scripts / Data Validation / SQL / dbt: N/A — declarative test module, no runtime/I/O/CLI/SQL.

## Status & Next Steps

**Current Status**: Review complete; 24/24 tests pass; findings are completeness/placement refinements, no failing or incorrect tests.
**Completed**:
1. Read code-review and python-development (unit-tests) skills in full.
2. Read the current `test_cleaned_models.py` and the schema it pins (`cleaned_models.py`), plus the prior `20260622v01_cr_cleaned_models.md` (model review; no prior *test*-file review exists — the test file was created under v01's 3.1).
3. Ran the test file (24 passed) and empirically probed error paths: bool/negative/bound errors include the field `loc`; the `n_parsed_sections=0` case raises at `Document` construction before the envelope validator; `zero_sections` raises via count-mismatch not the empty-list branch; Section dump key order is `sort_order..page_end`; `binary_hash=0` and `2^64-1` both validate.
4. Wrote this review.
**Next Steps**:
1. Add the Section key-order assertion (1.1) — the one finding with real regression-catching value.
2. Optionally relocate 2.1, add `binary_hash=0` (3.2), and parametrize contiguity (3.1).
**Blockers**:
1. None.
**Notes**:
1. No code modified; no commit made.
2. 4.1 and the dead `"has zero sections"` branch (model line 167) are model observations included only to explain why the corresponding test cases cannot fully pin their named behavior — both are out of scope (test-file review).
