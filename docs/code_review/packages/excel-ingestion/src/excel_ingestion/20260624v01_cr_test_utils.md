---
name: cr-test_utils
goal: Address code quality and coverage gaps in code/excel_ingestion/unit_tests/test_utils.py to align with the unit-tests skill.
created: 2026-06-24
updated: 2026-06-24
---

## Implementation Plan

1. [skipped] Coverage gaps - `code/excel_ingestion/unit_tests/test_utils.py`
   - 1.1. [suggestion] No test pins the `_suffixed` truncation-collision branch through `deduplicate_columns`. `test_deduplicate_columns_truncation_collision_stays_unique` (line 101) uses an all-`a` base, so trimming room for the `_2` suffix never has to strip a trailing underscore. A base whose tail char near the cap is non-alphanumeric-derived (i.e. ends in `_` after trimming) would exercise the `rstrip('_')` inside `_suffixed`. Add a case where the trimmed base ends in `_` to confirm the suffix never re-collides.
        - Rationale: unit-tests 7.1 (boundary values) — the cap-minus-suffix boundary in `_utils._suffixed` (line 161) is the subtle case the docstring warns about, but the existing test's input does not force the `rstrip` to fire.

2. [completed] Assertion strength - `code/excel_ingestion/unit_tests/test_utils.py`
   - 2.1. [minor] Line 234-238 `test_compute_source_hash_deterministic_and_in_uint64_range` asserts only range + determinism. It does not pin that the join is newline-delimited (the documented `"\n".join` contract). A wrong delimiter (e.g. `""` join) would still be deterministic and in range, so the test passes.
        - Current: `assert 0 <= h < 2**64` / `assert compute_source_hash(["Code: A1", "Code: B2"]) == h`
        - Expected: also assert the order/delimiter sensitivity, e.g. `assert compute_source_hash(["A", "B"]) != compute_source_hash(["B", "A"])` and that `compute_source_hash(["A", "B"]) != compute_source_hash(["AB"])` (the newline separator is load-bearing for the change-detection signal).
        - Rationale: unit-tests 7 — assertions should pin the documented behavior so a wrong implementation cannot pass.

3. [completed] Boundary coverage - `code/excel_ingestion/unit_tests/test_utils.py`
   - 3.1. [suggestion] `test_normalize_column_name_caps_long_header` (line 59) checks `len <= MAX_IDENTIFIER_LENGTH` and no trailing underscore, but not the exact-63 boundary where the cut lands on an underscore. Add a header that, once `col_`-prefixed and cut at 63, would end in `_` to prove the `rstrip('_')` at `_utils.py:141` fires at the boundary.
        - Rationale: unit-tests 7.1 — the silent-truncation boundary is the documented reason this cap exists; the off-by-one at the cut point is the case most likely to regress.

## Skills with No Issues

1. unit-tests (naming): No issues — files/functions follow `test_<function>_<scenario>_<expected>`.
2. unit-tests (parametrize): No issues — `normalize_column_name`, `get_engine` missing-env, and `make_collection_path` are well parametrized.
3. unit-tests (pytest.raises match): No issues — every `raises` uses `match=` with a substring that exists in the source message (verified against `file_ingestion/_utils.py` "Unsafe SQL identifier" / "Invalid collection_path" and `get_engine` "Missing Postgres environment variable").
4. unit-tests (Arrange-Act-Assert): No issues — tests are single-behavior and cleanly phased.
5. unit-tests (order independence / no shared state): No issues — all tests are pure or use `monkeypatch`; no shared mutable state.
6. unit-tests (mock boundaries): No issues — `get_engine` env tests use `monkeypatch` (the correct boundary); the engine is built but not connected, so no DB is touched.
7. type-hints: No issues — all test functions and parametrized signatures are annotated.
8. docstrings: No issues — every test has a one-line behavior docstring.
9. logging: No issues — `setup_logging` is configured to the unit_tests log dir.
10. sql-development: N/A — no SQL in this file.

## Status & Next Steps

**Current Status**: RESOLVED. 2.1: added a newline-join/order sensitivity test for compute_source_hash. 3.1: added a cap-boundary test that forces the rstrip at the 63-char cut. 1.1 skipped (the _suffixed truncation-collision is already covered; forcing the specific rstrip is marginal). Suite green.
**Completed**:
1. Reviewed all 17 tests against the unit-tests skill and the `_utils.py` source.
2. Verified every `match=` string against the canonical `file_ingestion/_utils.py` validators.
3. Ran coverage: `_utils.py` is at 100% line coverage; findings here are assertion-strength / boundary depth, not line gaps.
**Next Steps**:
1. Strengthen the hash and cap-boundary assertions.
**Blockers**:
1. None.
**Notes**:
1. This is the strongest of the four test files: comprehensive happy/edge/error coverage, good parametrization, and the truncation/dedup edge cases the rework cares about are present. All findings are suggestion/minor.
