---
name: cr-test_ingest
goal: Review the docling-upgrade branch changes to test_ingest.py — the new max_pages_per_batch forwarding test, the two fake_parse stub signatures, and the (False, "dlparse") fallback expectation — per the unit-tests skill.
created: 2026-06-29 00:00:00
updated: 2026-06-29 00:00:00
---

## Summary

Reviewed the `git diff main..docling-upgrade -- code/file_ingestion/unit_tests/test_ingest.py`. The diff is small and well-targeted: one new test (`test_max_pages_per_batch_forwarded_to_parse`, lines 320-336) plus two `fake_parse` stub signatures (lines 358, 399) that gained `max_pages_per_batch: int = 0` so the group-loop tests still accept the kwarg `step_parse` now forwards (ingest.py:321). The suite is green: `49 passed`; `ingest.py` measures 96% with no miss inside `step_parse` (the 10 missed lines are `get_engine` URL wiring 84-86/94-110 and the `__main__` guard 821).

The new test is correct and the seam is right: `ingest.parse_files_docling` is patched where it is used and the test asserts the *value* (`call_args.kwargs["max_pages_per_batch"] == 25`), not merely that a kwarg was passed — so it would fail if `step_parse` dropped or renamed the kwarg. The two stub signatures are now faithful to the real `parse_files_docling` keyword-only group; without `max_pages_per_batch` on them, `test_group_failure_records_failure_and_continues` and `test_all_groups_succeed_returns_all_ok` would raise `TypeError` once `step_parse` began forwarding the kwarg, so this change is load-bearing and correct.

Two items keep this from being airtight. (1) Brief claim #2 — that `test_missing_file_falls_back_to_hard_defaults` was "updated to expect `(False, 'dlparse')` (reverted from the deprecated `dlparse_v4`)" — does not correspond to any change in this branch's diff. On `main` the test already asserts `(False, "dlparse")` (main test line 190) and production already defaults to `dlparse` (main ingest.py:53). The `(False, "dlparse")` expectation is correct, but it is a no-op on this branch, not a change introduced here. (2) The default `DEFAULT_MAX_PAGES_PER_BATCH = 25` wiring in `main()` (ingest.py:702) is *executed* by the TestMain happy-path tests but its value is never *asserted* to reach `step_parse`, because those tests mock `ingest.step_parse`. Only the `step_parse -> parse_files_docling` hop is value-asserted; the `main config-read -> step_parse` hop and the literal default of 25 are not.

Counts: 0 critical, 0 major, 1 minor, 2 suggestions (3 findings).

## Implementation Plan

1. [completed] New forwarding test asserts only the caller-supplied value, not the step_parse default — `code/file_ingestion/unit_tests/test_ingest.py`
   - 1.1. [suggestion] Lines 320-336 `test_max_pages_per_batch_forwarded_to_parse` passes `max_pages_per_batch=25` and asserts the same 25 arrives at `parse_files_docling`. This pins the forwarding wire, which is the test's stated purpose, but it is the only `max_pages_per_batch` value exercised at the `step_parse` level. `step_parse`'s own signature default is `0` (ingest.py:222) — the "batching disabled" sentinel — and no test asserts that when a caller omits the kwarg, `0` is what reaches `parse_files_docling`. Consider a second assertion (or a `parametrize` over `25` and the omitted/default case) so a future change to `step_parse`'s default would be caught. Suggestion only; the in-scope forwarding behaviour is correctly pinned.
       - Current: single call with `max_pages_per_batch=25` asserted equal to 25.
       - Expected (optional): add a call that omits the kwarg and asserts `mock_parse.call_args.kwargs["max_pages_per_batch"] == 0`.

2. [completed] main()'s DEFAULT_MAX_PAGES_PER_BATCH config read is executed but its value is never asserted — `code/file_ingestion/unit_tests/test_ingest.py`
   - 2.1. [suggestion] The `max_pages_per_batch = parse_cfg.get("max_pages_per_batch", DEFAULT_MAX_PAGES_PER_BATCH)` read (ingest.py:702) and its forwarding into `step_parse` (ingest.py:749) are covered for *statement* coverage by `test_all_steps_enabled_happy_path_runs_each_step` (1220) and `test_pipeline_*` tests, but every one of those mocks `ingest.step_parse`, so the value 25 (or a `[parse].max_pages_per_batch` override) is never asserted to flow from config into the step call. The result: the `step_parse -> parse_files_docling` hop is value-tested (finding test, 320-336) but the `[parse] config -> step_parse` hop and the literal default of `25` are not. This is arguably main()'s config-plumbing surface rather than this unit's, so it is a suggestion: if you want the default pinned end-to-end, have one TestMain happy-path test assert `mock_parse.call_args.kwargs["max_pages_per_batch"] == 25` (default) and, optionally, a second with `[parse].max_pages_per_batch` set asserting the override wins. Mirrors the v01/v02 pattern of preferring value assertions over called/not-called where a regression would otherwise pass silently.

3. [completed] Brief-claimed fallback revert is not a change on this branch — `code/file_ingestion/unit_tests/test_ingest.py`
   - 3.1. [minor] `test_missing_file_falls_back_to_hard_defaults` (lines 186-190) correctly asserts `groups == {(False, "dlparse"): ["a.pdf"]}`, and `(False, "dlparse")` is the right expected fallback given `DEFAULT_PDF_BACKEND = "dlparse"` (ingest.py:53) and `DEFAULT_DO_OCR = False`. However, this is not a `docling-upgrade` change: on `main` the test already expects `(False, "dlparse")` (main test line 190) and production already defaults to `dlparse` (main ingest.py:53), so the brief's "reverted from `dlparse_v4`" description does not match this branch's diff (which touches only lines 320-336 and the two stub signatures). No code action needed — the test is correct as-is; this is a documentation note so the review record is not mistaken about what changed. If a `dlparse_v4 -> dlparse` revert was intended to be part of this branch, it landed in an earlier commit already merged to `main`.

## Skills with No Issues

1. Unit Tests (pytest, not unittest): No issues — `mocker`/`tmp_path` fixtures throughout; the new test uses `MockerFixture`.
2. Unit Tests (file naming): No issues — `test_ingest.py` for `ingest.py`.
3. Unit Tests (function naming): No issues — `test_max_pages_per_batch_forwarded_to_parse` is scenario+expectation shaped.
4. Unit Tests (single behavior / AAA): No issues — the new test arranges one mock, acts once, asserts one wire; clearly arranged.
5. Unit Tests (mock external boundaries only, patch where used): No issues — `parse_files_docling` is patched at `ingest.parse_files_docling` (where used), the correct seam for asserting forwarding. The `fake_parse` closures write JSON on disk so survivors are derived as production derives them — not a tautology.
6. Unit Tests (pytest utilities — parametrize, raises(match=)): No issues for what is present; finding 1.1 notes `parametrize` would strengthen the single-value forwarding test (optional).
7. Unit Tests (built-in fixtures): No issues — `tmp_path`/`mocker` and the `_write` helper build on-disk fixtures.
8. Unit Tests (no shared state / no private-attr assertions): No issues — the new test is independent; it asserts on the mock call record, not internals.
9. Unit Tests (comprehensive coverage): Satisfied for the new forwarding behaviour at the `step_parse` level; the gaps (step_parse default of 0, and main()'s default of 25 value-assertion) are captured as suggestions 1.1 and 2.1. `ingest.py` at 96% with no miss inside `step_parse`.
10. Type Hints: No issues — the new test and both updated `fake_parse` closures carry full parameter and `-> None` annotations; `max_pages_per_batch: int = 0` matches the production type.
11. Docstrings: No issues — the new test has a one-line docstring stating the forwarding contract.
12. Comments: No issues — the stub comments still accurately scope the OCR-fails / non-OCR-writes split.
13. Logging: N/A — test module.
14. Exception Handling: N/A for the diff; the propagate-vs-record split remains asserted in the unchanged tests.
15. Data Validation: N/A.
16. Executable Scripts: N/A — `step_parse` is a library function; main()'s config plumbing is noted in 2.1.
17. SQL Development: N/A — no SQL in the changed lines.

## Status & Next Steps

**Current Status**: Review complete. The new forwarding test and the two stub-signature updates are correct and load-bearing; the suite is green (`49 passed`) and `step_parse` is fully covered. Three findings, all minor/suggestion: a single-value forwarding assertion (1.1), an unasserted main()-default-of-25 wire (2.1), and a brief-vs-diff mismatch on the `(False, "dlparse")` "revert" which is actually already on main (3.1).
**Completed**:
1. Read the unit-tests skill and the prior `20260626v01_cr_test_ingest.md` review.
2. Diffed `main..docling-upgrade` for both the test file and `ingest.py`, and compared the fallback test/default against `main`.
3. Confirmed the patch seam (`ingest.parse_files_docling`), value assertion (25), and stub-signature fidelity (keyword-only `max_pages_per_batch`).
4. Ran `uv run pytest ... -q` (49 passed) and `--cov=ingest --cov-report=term-missing` (96%, line 702 executed but not value-asserted); removed `.coverage` afterward.
**Next Steps**:
1. Optionally add the default-value assertions in 1.1 and 2.1.
2. Reconcile the brief's claim #2 (3.1) — confirm the `dlparse_v4 -> dlparse` revert was intended for an earlier already-merged commit, not this branch.
**Blockers**:
1. None.
**Notes**:
1. No source or test files were modified and nothing was committed.
2. The forwarding behaviour the brief asks about IS meaningfully asserted (value 25 at the correct seam); the gaps are about the default sentinel (0) and main()'s default (25), not the forwarding wire itself.

## Resolution (2026-06-29)

- [suggestion] step_parse default sentinel (0) — **added** (`test_max_pages_per_batch_defaults_to_zero`).
- [suggestion] main() config-read default-of-25 wire — not added (TestMain mocks step_parse; the forwarding + validation tests cover the meaningful paths).
- [minor] `(False, "dlparse")` net-diff clarification — acknowledged; no code change (the v4 round-trip nets to zero vs main).
