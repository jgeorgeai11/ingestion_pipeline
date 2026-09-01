---
name: cr-test_ingest
goal: Assess the test suite in code/file_ingestion/unit_tests/test_ingest.py for behavioral coverage, mocking quality, assertion strength, and conventions against the python-development unit-tests skill.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
resolved: 2026-06-22 00:00:00
---

## Summary

`test_ingest.py` is a strong, well-organised behavioural suite for the four pure/orchestration helpers it targets (`filter_valid_documents`, `_group_files_by_parse_settings`, `step_parse`, `step_clean`, `step_load`). All 27 tests pass. Naming follows `test_<function>_<scenario>_<expected>`, tests are grouped into clear `Test*` classes, fixtures use `tmp_path`, and the engine/connection mocks are realistic (they model the two distinct context managers `connect()`/`begin()` separately). Assertions are mostly strong — they check dispatched SQL, bound parameters, call counts, and ordering rather than merely "no exception".

The headline gap is coverage: `main()` (lines 490-604) and `get_engine()` (lines 69-82) have **zero tests** — measured coverage is 61%, with every uncovered line living in those two functions. The task brief explicitly calls out "main() error handling" as a behaviour to assess; it is currently untested (config-missing, TOML-decode failure, missing config key, ValueError, and the broad-except pipeline-failure paths all unexercised). Several documented exception paths on the tested functions are also unexercised (`step_load` `KeyError` on a file missing from `collection_paths`; `step_clean`/`step_load` `OSError`). A handful of assertions are looser than they could be, and a couple of inline comments imply coverage that the mocked tests do not provide.

Counts: 0 critical, 1 major, 4 minor, 6 suggestions (11 findings).

## Implementation Plan

1. [completed] Coverage gaps — `code/file_ingestion/unit_tests/test_ingest.py`
   - 1.1. [completed] RESOLVED: Added `TestMain` (config-not-found, malformed-TOML, missing-required-field, pipeline known-error ValueError, pipeline unexpected TypeError broad-except, all-steps-disabled gating, all-steps-enabled happy path incl. load block) and `TestGetEngine` (missing POSTGRES_HOST -> ValueError). The all-invalid-config -> exit 1 main() branch was already covered by the prior pass (`TestMainAllInvalidConfig`). ingest.py coverage now 97% (remaining misses are get_engine's post-env URL.create/create_engine wiring and the `__main__` guard). No tests for `main()` (ingest.py:487-604) or `get_engine()` (ingest.py:54-82). Measured coverage is 61%; all 82 uncovered statements are in these two functions (`ingest.py` lines 69-82, 490-604, 608). The brief specifically asks for `main()` error handling. None of these branches are exercised:
        - config file not found -> `sys.exit(1)` (ingest.py:504-506)
        - `tomllib.TOMLDecodeError` / `OSError` reading config -> `sys.exit(1)` (ingest.py:512-514)
        - `KeyError` missing config field -> `sys.exit(1)` (ingest.py:555-557)
        - `ValueError` invalid config value -> `sys.exit(1)` (ingest.py:558-563)
        - run-flag gating (`run_parse`/`run_clean`/`run_load` False -> "Skipped")
        - the pipeline-failure `except (FileNotFoundError, OSError, ValueError, RuntimeError)` and broad `except Exception` paths -> `sys.exit(1)` (ingest.py:597-604)
        - `get_engine` missing-env-var -> `ValueError` (ingest.py:74-79)
        - Expected: add a `TestMain` class driving `main()` with a written TOML config under `tmp_path`, `monkeypatch.setattr(sys, "argv", ...)`, `step_*`/`get_engine`/`ensure_schema` mocked, and `pytest.raises(SystemExit)` asserting `exc.value.code == 1` per error branch; add a `get_engine` env-var test using `monkeypatch.delenv(...)` and `pytest.raises(ValueError, match="Missing Postgres environment variable")`.

2. [completed] Assertion strength — `code/file_ingestion/unit_tests/test_ingest.py`
   - 2.1. [completed] RESOLVED: Added `engine.begin.assert_not_called()` and `engine.connect.assert_not_called()` to `test_malformed_cleaned_json_raises_validation_error`; `begin` is the load-bearing assertion (post fix-3.1 the code uses `engine.begin()` exclusively, preceded by `model_validate_json`), pinning "rejected before any DB touch". Line 595-616 `test_malformed_cleaned_json_raises_validation_error`: asserts `txn_conn.execute.assert_not_called()` but not that the existence check never ran. Because `model_validate_json` (ingest.py:408) precedes `engine.connect()` (ingest.py:413), the intended guarantee is "rejected before *any* DB touch". The current assertion would still pass if a connect/check query had fired.
   - 2.2. [completed] RESOLVED: Added `assert list(cleaned_dir.glob("*.json")) == []` to `test_zero_section_parse_raises_and_writes_nothing` to pin "writes nothing" literally. Line 365-385 `test_zero_section_parse_raises_and_writes_nothing`: good that it asserts `sections_to_record` is not called and no file written, but it does not assert the cleaned output dir stays empty of *other* files.

3. [completed] Comment / scope precision — `code/file_ingestion/unit_tests/test_ingest.py`
   - 3.1. [skipped] Intentionally not done (reviewed and deliberately skipped per the implementation brief; optional extra-assertion only). Line 387-408 `test_existing_nonempty_output_is_skipped`: the test correctly pins the skip-before-source-existence ordering — leaving the parsed source absent is itself the assertion (a wrong ordering would raise `FileNotFoundError` and the test would error). This is fine as written. Optional only: make the implicit ordering assertion explicit by also asserting `cleaned_dir/a.json` content is unchanged after the call.
   - 3.2. [completed] RESOLVED: Trimmed the overstated comment in `test_unknown_output_format_does_not_skip` (it now states it covers only the skip-suppression half) and added sibling `test_unknown_output_format_propagates_value_error` where the mocked `parse_files_docling` raises `ValueError` and `step_parse` lets it propagate (not swallowed by the per-group RuntimeError accumulation). Line 293-311 `test_unknown_output_format_does_not_skip`: this asserts the file is reparsed (correct), but the comment says the downstream `ValueError` from `parse_files_docling` "surfaces the bad config" — `parse_files_docling` is mocked here, so that surfacing is *not* tested. The test verifies only the skip-suppression half.

4. [completed] Missing edge cases on tested functions — `code/file_ingestion/unit_tests/test_ingest.py`
   - 4.1. [completed] RESOLVED: Added `test_missing_collection_path_entry_raises_key_error` passing `collection_paths={}` for a present file and asserting `pytest.raises(KeyError)`. `step_load` documents `KeyError` "If a file has no entry in `collection_paths`" (ingest.py:384, 402) — untested.
   - 4.2. [completed] RESOLVED: Added `test_multi_file_batch_skips_existing_then_inserts_next` — a two-file batch (file 1 existing -> skip, file 2 absent -> insert) using two distinct transaction connections via `engine.begin().__enter__.side_effect`; asserts `engine.begin.call_count == 2` (independent transactions) and that the loop does not abort early (file 2 inserts one doc + two content rows). `step_load` multi-file behaviour is untested: every test passes exactly one file.
   - 4.3. [skipped] Intentionally not done (reviewed and deliberately skipped per the implementation brief; thin pass-through, low priority). `step_clean` / `step_load` `OSError` on read/write (documented at ingest.py:294, and the `write_text` at ingest.py:332) is untested.
   - 4.4. [completed] RESOLVED: Added `test_non_string_path_coerced_then_skipped` using a non-string `collection_path` of `-1` (a positive int like `123` coerces to a VALID ltree label and would be kept; `-1` coerces to `"-1"` whose hyphen is rejected), asserting it is `str()`-coerced, validated, and skipped. The all-invalid -> `([], {})` case was already covered by the prior pass (`test_all_invalid_returns_empty`). `filter_valid_documents` has no test for an all-invalid input returning `([], {})`, nor for a `collection_path` that is a non-string (e.g. an int), which `str(raw_path)` coerces — worth a parametrized case to lock the coercion/skip behaviour.

5. [completed] Conventions — `code/file_ingestion/unit_tests/test_ingest.py`
   - 5.1. [skipped] Intentionally not done (reviewed and deliberately skipped per the implementation brief; a convention nudge, not a defect — the module-level helper approach is readable and acceptable). Shared helpers `_write` (line 133), `_cleaned_doc` (437), `_write_cleaned` (456), `_make_engine` (464), `_statements` (495) are module-level functions rather than `conftest.py` fixtures. The unit-tests skill (3.1) prefers sharing common test data via fixtures. `_make_engine`/`_cleaned_doc` in particular are good fixture candidates (`make_engine` as a factory fixture). Current approach is readable and acceptable; flag only as a convention nudge, not a defect.
   - 5.2. [skipped] Intentionally not done (reviewed and deliberately skipped per the implementation brief; acceptable given there is no fuller integration layer). The SQL-matching assertions rely on the source emitting lowercase keywords and a trailing space (e.g. `"insert into cms_iom.document "` to disambiguate from `document_content`). This is brittle against benign reformatting of the SQL string (case, whitespace) even though current behaviour is unchanged. It leans toward asserting the literal SQL text rather than behaviour. Acceptable given there is no fuller integration layer, but a normalised match (e.g. casefold + collapse whitespace, or matching the bound params + a looser `"insert into"` + table token check) would be more robust.

## Skills with No Issues

1. Unit Tests skill: Issues found — see 1.1 (comprehensive coverage 7 / 7.2), 2.x (assertion strength), 4.x (cover-all-paths 7.1), 5.1 (fixtures 3.1).
2. Type Hints skill: No issues — all test functions and helpers carry parameter and `-> None`/return annotations (e.g. `_make_engine(...) -> tuple[MagicMock, MagicMock]`).
3. Docstrings skill: No issues — every test and helper has a clear docstring; helpers use Args/Returns.
4. Comments skill: Issues found — see 3.2 (one comment overstates coverage). Otherwise inline comments explain intent well (e.g. the partial-output rationale at lines 164-168).
5. Logging skill: N/A — test module; no logging expected (uses pytest assertions, not `caplog`, which is fine here).
6. Exception Handling skill: No issues in the tests themselves; they correctly use `pytest.raises(..., match=...)` for `FileNotFoundError`, `ValueError`, `RuntimeError`, and `pydantic.ValidationError`. Coverage of the *module's* exception paths is incomplete (see 1.1, 4).
7. Data Validation skill: N/A — covered by `CleanedDocument` and exercised via 595-616.
8. Executable Scripts skill: N/A for the test file; but the script's `main()`/`get_engine` are the untested portion (1.1).
9. SQL best-practices skill: N/A — no SQL authored in tests; assertions match the module's SQL text (see 5.2 on brittleness).

## Status & Next Steps

**Current Status**: All actioned findings completed. Full `unit_tests/` suite green (160 passed, was 148). ingest.py coverage now 97% (remaining misses: get_engine's post-env `URL.create`/`create_engine` wiring and the `__main__` guard). The review's headline 61% predated a prior remediation pass that had already raised the baseline to 72% before this round of work. Findings 3.1, 4.3, 5.1, 5.2 were reviewed and intentionally not done per the implementation brief.
**Completed**:
1. Read code-review and python-development (unit-tests, exception-handling) skills in full.
2. Read `test_ingest.py`, `ingest.py`, `cleaned_models.py`, and `conftest.py`.
3. Ran the suite with `--cov=ingest --cov-report=term-missing` (from repo root, per conftest path setup).
4. Verified specific assertion correctness (SQL text casing/spacing, kwargs-vs-args dispatch, validate-before-connect ordering).
5. Added `TestMain` (7 tests) and `TestGetEngine` (1 test) closing 1.1; tightened 2.1/2.2; added 3.2 sibling + comment trim; added 4.1/4.2/4.4 edge cases.
**Next Steps**:
1. None — all actioned findings completed.
**Blockers**:
1. None.
**Notes**:
1. Prior-pass coverage already present and not duplicated this round: the all-invalid main() -> exit 1 test (`TestMainAllInvalidConfig`), the duplicate-`file` ValueError tests (`test_duplicate_file_raises`, `test_duplicate_file_raises_even_when_one_path_invalid`), the all-invalid `filter_valid_documents` -> `([], {})` test (`test_all_invalid_returns_empty`), and the `step_load` missing-title KeyError test (`test_missing_title_raises_key_error`).
**Notes**:
1. Tests must run from the repo root — `conftest.py` does `sys.path.insert(0, "code/file_ingestion")` (a cwd-relative path). Running pytest from inside `unit_tests/` fails import collection. This is a conftest fragility worth noting but is out of scope for this file. </content>
