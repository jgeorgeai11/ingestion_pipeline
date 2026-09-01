---
name: cr-test_ingest
goal: Assess test_ingest.py's coverage of the new resilient (accumulate-and-report) contract and the provenance (source_binary_hash / (sections, hash) tuple) wiring, plus mock realism, assertion strength, and conventions against the python-development unit-tests skill.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

This is a strong behavioural suite. All 50 tests pass and measured `ingest.py` coverage is 98% (only `get_engine`'s post-env `URL.create`/`create_engine` wiring and the `__main__` guard are uncovered — both acceptable misses). The resilient contract is well exercised at the step level: `step_parse`, `step_clean`, and `step_load` each have a mixed-batch test proving one file's failure does not abort its siblings, an accumulate-and-record test asserting the exact `{"file", "stage", "reason"}` failure shape, and a `(ok_files, failures)` return-tuple assertion. The provenance wiring is also well covered: `step_clean`'s happy path asserts the `(sections, binary_hash)` tuple is unpacked and threaded with `mock_to_record.assert_called_once_with(["sec1", "sec2"], 42)`, the zero-section guard proves the tuple is unpacked before the `if not sections` check, and `step_load` asserts both that `source_binary_hash` appears in the insert SQL text AND that the bound value equals the fixture's `FIXTURE_BINARY_HASH` (a realistic uint64 above the signed-bigint range, matching `Document.binary_hash`'s `ge=0`). The engine/connection mock (`_make_engine`) realistically models the single `engine.begin()` transaction and `fetchone()`-driven skip/overwrite branch, and `test_multi_file_batch_skips_existing_then_inserts_next` correctly drives two independent per-document transactions via `__enter__.side_effect`. Fixtures (`_cleaned_doc`, `_write_cleaned`) build payloads that validate against the real `CleanedDocument` envelope.

The gaps are concentrated in `main()`'s aggregation layer — the seam where the new resilient contract actually pays off. The per-step accumulate-and-report behaviour is thoroughly proven, but main()'s threading of survivors stage-to-stage and its aggregation of failures across stages are under-proven: only a clean-stage failure is fed through main(); the load stage's failure-list aggregation and its `loaded_n/cleaned_n` summary line are never exercised end-to-end, and no test asserts that one step's `ok_files` is the next step's input (survivor threading). One test carries a confusing `del mock_setup` no-op, and a couple of step-level survivor-counting assertions (skipped-file-counts-as-ok) are implicit rather than pinned.

Counts: 0 critical, 0 major, 3 minor, 4 suggestions (7 findings).

## Implementation Plan

1. [pending] Resilient-contract coverage gaps in `main()` aggregation — `code/file_ingestion/unit_tests/test_ingest.py`
   - 1.1. [minor] Lines 1286-1323 `test_some_files_fail_exits_nonzero_with_summary`: this is the ONLY test that threads a recorded failure through `main()`, and it does so only at the CLEAN stage (with `run_load=False`). The load stage's failure aggregation (`all_failures.extend(load_failures)`, ingest.py:751) and the load arm of the summary line (`loaded_n/cleaned_n`, ingest.py:760-764) are never exercised end-to-end. A load-stage `step_load` returning `([], [{...stage:"load"...}])` would be aborted-by-exception under the old contract but should now produce a summary + exit 1; this exact branch is unproven at the main() level. Add a sibling test with `run_load=True`, mocking `get_engine`/`ensure_schema` and `step_load` to return a failure, asserting exit 1 and that `error_messages` contains `"load: <file>"`.
   - 1.2. [minor] Lines 1251-1284 `test_all_steps_enabled_happy_path_runs_each_step`: this asserts each step is called once but does NOT assert that survivors thread between stages — i.e. that `step_parse`'s `ok_files` (`["a.pdf"]`) is passed as `step_clean`'s `file_paths`, and `step_clean`'s `ok_files` as `step_load`'s `cleaned_ok` argument. Survivor threading (ingest.py:722-754) is the core of the resilient design and is currently unproven; the test would still pass if main() re-passed the original `file_paths` to every stage (defeating the drop-and-continue behaviour). Strengthen by asserting `mock_clean.call_args.args` / `kwargs` carry the parse survivors and `mock_load`'s positional `cleaned_ok` carries the clean survivors. A higher-value variant: have `step_parse` return `(["a.pdf"], [])` but `step_clean` drop it (`([], [failure])`) and assert `step_load` is called with an EMPTY survivor list — proving a dropped file does not reach load.
   - 1.3. [suggestion] Lines 1223-1249 `test_all_steps_disabled_skips_and_succeeds`: covers the all-skipped no-op, but the skipped-step PASSTHROUGH branches (`parsed_ok = file_paths`, `cleaned_ok = parsed_ok`, `loaded_ok = cleaned_ok`, ingest.py:729/739/754) where only SOME steps are disabled are not directly asserted. With a skipped parse but enabled clean, main() must pass the original `file_paths` into `step_clean`; no test pins that a skipped step forwards its input list unchanged.

2. [pending] Step-level survivor-counting assertions — `code/file_ingestion/unit_tests/test_ingest.py`
   - 2.1. [minor] Lines 783-804 `test_existing_path_skipped_without_overwrite`: asserts no inserts/deletes run for an already-ingested document, but does NOT assert the skipped file is RETURNED as a survivor. The resilient contract counts an already-ingested document as `ok` (ingest.py:544 `loaded_ok.append(file_name)` in the skip branch). The test discards the return value entirely; it would pass even if the skip branch wrongly recorded a failure or omitted the file. Capture `ok_files, failures = step_load(...)` and assert `ok_files == ["ge101c01.pdf"]` and `failures == []`.
        - Current: `step_load(...)` (return value unused)
        - Expected: `ok_files, failures = step_load(...)` then `assert ok_files == ["ge101c01.pdf"]` and `assert failures == []`
   - 2.2. [suggestion] Lines 978-1029 `test_multi_file_batch_skips_existing_then_inserts_next`: realistically drives two transactions and asserts the SQL per file, but likewise drops the return value. Since this is the canonical mixed skip/insert batch, asserting `ok_files == ["first.pdf", "second.pdf"]` (skip AND insert both count as survivors, with `failures == []`) would pin that a skip is not miscounted as a loss.

3. [pending] Test hygiene / assertion clarity — `code/file_ingestion/unit_tests/test_ingest.py`
   - 3.1. [minor] Line 1314 `del mock_setup` in `test_some_files_fail_exits_nonzero_with_summary`: `mock_setup` is assigned at line 1298 (`mocker.patch("ingest.setup_logging")`) solely to suppress real log-file creation, then `del`'d with an explanatory comment. The `del` is a no-op that reads as if it were load-bearing; the patch already takes effect at call time. Drop the binding name (use `mocker.patch("ingest.setup_logging")` without assignment) and remove the `del` line — the comment's intent ("setup_logging is mocked") is already self-evident from the patch.
        - Current: `mock_setup = mocker.patch("ingest.setup_logging")` ... `del mock_setup`
        - Expected: `mocker.patch("ingest.setup_logging")` (no binding, no `del`)
   - 3.2. [suggestion] Lines 837-865 `test_malformed_cleaned_json_records_failure`: strong assertions (validation precedes any DB touch; `engine.begin`/`connect` not called). One refinement for the resilient contract: it asserts `failures[0]["stage"] == "load"` and `["file"]` but not that the `reason` reflects the validation error (e.g. contains `"n_parsed_sections"` or `"validation"`). Pinning a substring of the pydantic error in `reason` would prove the recorded reason is the real cause, not an unrelated message — matching the rigour applied to `step_clean`'s reason assertions (e.g. `"No sections parsed" in failures[0]["reason"]` at line 571).

## Skills with No Issues

1. Unit Tests skill: Issues found — see 1.x (comprehensive coverage 7 / cover-all-paths 7.1 at the main() aggregation seam), 2.x (survivor-count assertion strength), 3.x (hygiene). The resilient/provenance step-level contract itself is well covered.
2. Type Hints skill: No issues — all tests and helpers carry parameter and return annotations (e.g. `_make_engine(existing: bool) -> tuple[MagicMock, MagicMock]`, `_cleaned_doc(...) -> dict`, the `fake_parse` closures fully annotated).
3. Docstrings skill: No issues — every test and helper has a clear docstring; helpers use Args/Returns where applicable.
4. Comments skill: No issues — inline comments accurately scope what each mock proves (e.g. the tuple-unpack rationale at lines 551-552, the validate-before-begin note at 857-858). The previously-overstated unknown-format comment (v01 finding 3.2) remains correctly trimmed and paired with its propagation sibling.
5. Logging skill: N/A — test module; main()'s summary lines are asserted via a patched `ingest.logger` mock (lines 1299, 1315-1323, 1335-1345), which is a reasonable approach for verifying the summary text.
6. Exception Handling skill: No issues — tests correctly use `pytest.raises(..., match=...)` for the config-error paths that still propagate (`ValueError` in `step_parse` at 416/469, `SystemExit` in main()). The shift from raise-on-first to record-and-continue is correctly reflected: former `*_raises_*` tests are now `*_records_failure` asserting the failure dict rather than `pytest.raises`.
7. Data Validation skill: N/A in the test file; the `CleanedDocument` contract is exercised via the malformed-JSON failure test (837-865) and the fixtures validate against the real model.
8. Executable Scripts skill: N/A for the test file; `main()`/`get_engine` are the tested entry points and are well covered except the aggregation seam (1.x) and the post-env engine wiring.
9. SQL best-practices skill: N/A — no SQL authored in tests. The lowercase/trailing-space SQL-text matching (e.g. `"insert into cms_iom.document "`) remains brittle to benign reformatting (carried over from v01 finding 5.2, intentionally not actioned); still acceptable given there is no integration layer, noted for awareness only.

## Status & Next Steps

**Current Status**: Review complete. Full file re-run green (50 passed); `ingest.py` coverage 98% (misses: `get_engine` URL/create_engine wiring at 73-75/86-94 and the `__main__` guard at 791). The new resilient and provenance contracts are well covered at the per-step level; the residual gaps are in main()'s cross-stage aggregation and survivor threading.
**Completed**:
1. Read code-review and python-development (unit-tests) skills in full, plus the v01 review for context.
2. Read `test_ingest.py`, `ingest.py`, and `cleaned_models.py`; verified the fixtures validate against the real `CleanedDocument` envelope and that `FIXTURE_BINARY_HASH` is a realistic uint64 (matching `binary_hash` `ge=0`).
3. Ran `pytest ... --cov=ingest --cov-report=term-missing` (50 passed, 98%).
4. Verified the provenance assertions (the `(sections, hash)` tuple wiring in `step_clean`, the `source_binary_hash` text + bound-value assertion in `step_load`) and the per-step accumulate-and-report behaviour against the source.
**Next Steps**:
1. Address findings 1.1-1.2 (main() load-stage aggregation + survivor threading) and 2.1 (skip-counts-as-survivor) to fully prove the resilient contract end-to-end; the rest are optional hardening.
**Blockers**:
1. None.
**Notes**:
1. Tests must run from the repo root — `conftest.py` does `sys.path.insert(0, "code/file_ingestion")` (a cwd-relative path). Out of scope for this file but worth carrying forward from v01.
2. The advisor tool was rate-limited this pass and could not be consulted; findings were validated directly against the source instead. </content> </invoke>
