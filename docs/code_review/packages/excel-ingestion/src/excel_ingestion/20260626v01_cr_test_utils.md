---
name: cr-test_utils
goal: Re-review code/excel_ingestion/unit_tests/test_utils.py against the unit-tests skill; confirm prior-round findings are resolved and the current state is clean.
created: 2026-06-26
updated: 2026-06-26
---

## Implementation Plan

1. [pending] No actionable findings - `code/excel_ingestion/unit_tests/test_utils.py`
   - 1.1. [suggestion] None. The file is at 100% line coverage of `_utils.py` and the prior-round findings are resolved (see "Skills with No Issues"). The configurable consolidated-table change does not touch `_utils`, so there is no new surface here. No edits recommended.

## Skills with No Issues

1. unit-tests (prior 2026-06-24 findings all resolved): No issues — 2.1 the newline-join / order sensitivity of `compute_source_hash` is now pinned by `test_compute_source_hash_newline_join_is_load_bearing` (line 276: `["A","B"] != ["B","A"]` and `["A","B"] != ["AB"]`) and `test_compute_source_hash_changes_with_content` (line 271); 3.1 the cap-boundary `rstrip('_')` by `test_normalize_column_name_cap_boundary_strips_trailing_underscore` (line 74, input `"a"*58 + " x"` forces the cut at 63 to land on the `_`, asserting the result is `"col_" + "a"*58` with no trailing underscore). 1.1 (the `_suffixed` truncation-collision rstrip) was consciously skipped as marginal and remains covered indirectly by `test_deduplicate_columns_truncation_collision_stays_unique`.
2. unit-tests (line coverage): No issues — `_utils.py` is at 100% line coverage (verified via `--cov=_utils --cov-report=term-missing`, no missing lines).
3. unit-tests (parametrize): No issues — `normalize_column_name` (snake/prefix + empty fallback), `get_engine` missing-env, and `make_collection_path` derivation are all well parametrized over representative inputs.
4. unit-tests (assertion strength): No issues — `test_get_engine_percent_encodes_credentials` pins the encoded `%40`, that the raw reserved string is absent, and that the username round-trips after decode; the dedup tests assert exact lists; the cap test asserts exact lengths and the no-trailing-underscore invariant.
5. unit-tests (pytest.raises match): No issues — empirically confirmed by the all-passing live run; "Unsafe SQL identifier", "Missing Postgres environment variable", "Invalid collection_path" all exist in the source / re-exported validators.
6. unit-tests (mock boundaries / no over-mocking): No issues — `get_engine` env tests use `monkeypatch` (the correct boundary) and build but never connect the engine, so no DB is touched; pure functions need no mocking.
7. unit-tests (naming / AAA / order independence): No issues — predictable names; single-behavior, cleanly phased tests; all tests are pure or use `monkeypatch`, no shared mutable state.
8. unit-tests (regression coverage): No issues — `test_deduplicate_columns_suffix_does_not_resurrect_collision` and `test_deduplicate_columns_input_matching_prior_suffix` pin the seen-set / counts-seeding edge cases the `deduplicate_columns` docstring warns about.
9. type-hints: No issues — all test and parametrized signatures annotated.
10. docstrings: No issues — every test has a one-line behavior docstring.
11. logging: No issues — `setup_logging` configured to `logs/excel_ingestion/unit_tests`.
12. sql-development: N/A — no SQL in this file.

## Status & Next Steps

**Current Status**: Reviewed (review-only, no edits). Clean. 100% line coverage of `_utils.py`; all prior-round findings are resolved in the current file. The configurable consolidated-table change does not touch `_utils`, so no new surface to review.
**Completed**:
1. Re-verified every prior-round finding against the current test file and source.
2. Ran `pytest --cov=_utils --cov-report=term-missing` (then deleted `.coverage`): no missing lines.
3. Confirmed assertion strength (encoding round-trip, exact dedup lists, cap boundary) and mock realism (monkeypatch-only, no DB touched).
**Next Steps**:
1. None.
**Blockers**:
1. None.
**Notes**:
1. This remains the strongest of the four test files: comprehensive happy/edge/error coverage, good parametrization, and the truncation/dedup edge cases pinned. All prior findings were suggestion/minor and are addressed.
