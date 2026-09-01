---
name: cr-test_utils
goal: Address coverage gaps and unit-test standard deviations in code/file_ingestion/unit_tests/test_utils.py to align with the python-development unit-tests sub-skill.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

The suite is clean and passes (13 tests, all green), and every test it does contain is well-formed: clear Arrange-Act-Assert, descriptive docstrings, no shared state, no ordering dependency, no assertions on private internals. The `validate_collection_path` validator is covered well — all the rejection forms called out in the task (uppercase, dash, space, `.ext` leaf, leading / trailing / doubled dot, empty, blank) each have a test, plus three valid-pass-through cases.

The problem is scope, not quality. `_utils.py` exports **three** public functions; the test file imports and exercises **one** (line 5 imports only `validate_collection_path`). `validate_sql_identifier` has **zero** tests and `ensure_schema` — the only function with real behavior (DDL read, placeholder rendering, transactional `conn.execute`, and a documented `FileNotFoundError` / `OSError` / `ValueError` contract) — has **zero** tests. `pytest --cov` confirms:

```
Name                            Stmts   Miss  Cover   Missing
code/file_ingestion/_utils.py      42     25    40%   38-43, 105-139
```

Missing lines 38-43 are the whole body of `validate_sql_identifier`; missing 105-139 are the whole body of `ensure_schema`. To answer the task's explicit question directly: **`ensure_schema`'s placeholder rendering and DDL execution are not tested at all — only one of the two validators is.** Two of three public functions are entirely uncovered, so standard 7 (every public function has tests) is failing for this module.

A secondary, lower-severity observation: within the covered validator, the rejection tests assert only the exception *type* (`pytest.raises(ValueError)`) and never the message via `match=`, so they cannot distinguish a deliberate validation rejection from an incidental `ValueError` raised for any other reason. Standard 5.2 calls for `pytest.raises(Exception, match="msg")`.

## Implementation Plan

1. [completed] Add coverage for `validate_sql_identifier` (module lines 25-43; currently 0%) - `code/file_ingestion/unit_tests/test_utils.py`
   - **RESOLVED:** `validate_sql_identifier` now has direct coverage in `TestValidateSqlIdentifier` — parametrized valid forms returned unchanged (1.1/1.2), parametrized unsafe forms raising `ValueError` with `match="Unsafe SQL identifier"` (1.3), a label-interpolation assertion (1.4), and the trailing-newline rejection case (`"public\n"`).
   - 1.1. [major] Line 5: the import brings in only `validate_collection_path`. `validate_sql_identifier` is a public function with no test, violating standard 7 (every public function should have tests).
        - Current: `from _utils import validate_collection_path`
        - Expected: `from _utils import ensure_schema, validate_collection_path, validate_sql_identifier`
   - 1.2. [major] Missing test: a valid identifier is returned unchanged (module line 43, the `return name` happy path — uncovered per `--cov`). Cover the boundary forms the regex `^[a-z_][a-z0-9_]*$` allows: a leading underscore, and digits after the first char.
        - Expected (parametrized over valid forms, per standard 5.1):
          ```python
          @pytest.mark.parametrize("ident", ["document", "_private", "tbl_2025", "x"])
          def test_validate_sql_identifier_valid_returns_unchanged(self, ident: str) -> None:
              """A safe identifier passes through untouched."""
              assert validate_sql_identifier(ident, "label") == ident
          ```
   - 1.3. [major] Missing test: unsafe identifiers raise `ValueError` (module lines 38-42, the entire rejection path — uncovered). Cover each distinct rejection cause: uppercase, leading digit, dash, space, dot, and empty string. Assert the message via `match=` (standard 5.2) so the test is bound to the validation failure, not any `ValueError`.
        - Expected:
          ```python
          @pytest.mark.parametrize("ident", ["Document", "2025_tbl", "my-table", "my table", "a.b", ""])
          def test_validate_sql_identifier_unsafe_raises(self, ident: str) -> None:
              """An identifier with unsafe characters is rejected."""
              with pytest.raises(ValueError, match="Unsafe SQL identifier"):
                  validate_sql_identifier(ident, "db_schema")
          ```
   - 1.4. [minor] Missing assertion: the `label` argument is interpolated into the error message (module line 40, `f"...for {label}: ..."`). One test should assert the label appears, so the diagnostic contract is verified.
        - Expected: `with pytest.raises(ValueError, match="db_schema"): validate_sql_identifier("Bad", "db_schema")`

2. [completed] Add coverage for `ensure_schema` (module lines 80-139; currently 0%) - `code/file_ingestion/unit_tests/test_utils.py`
   - **RESOLVED:** `ensure_schema` now has direct coverage in `TestEnsureSchema` against a mocked `Engine` (a `_make_mock_engine` helper builds a `MagicMock` whose `begin()` context manager yields a mock conn): placeholder rendering verified by capturing the executed SQL (2.1), single transactional `conn.execute` (2.2), `FileNotFoundError` for a missing DDL path (2.3), unsafe-identifier fail-fast before any DB call (2.4), `SQLAlchemyError` propagation (2.5), defaults landing in rendered SQL (2.6), plus the new stray-placeholder guard raising `ValueError` on `{bogus}`.
   - 2.1. [critical] Missing test: placeholder rendering. The task's central question — whether `{schema_name}` / `{document_table}` / `{content_table}` are substituted correctly (module lines 123-128) — is entirely unverified. This is the function's core logic and the most security-relevant behavior (the validated identifiers are string-substituted into raw SQL, not parameter-bound). Use a fake/mock `Engine` so no real DB is needed; capture the SQL passed to `conn.execute` and assert all three placeholders are replaced and none survive.
        - Expected (sketch — patch the engine boundary per standard 4):
          ```python
          def test_ensure_schema_renders_all_placeholders(self, tmp_path: Path, mocker) -> None:
              """All three placeholders are substituted into the executed DDL."""
              ddl = tmp_path / "schema.sql"
              ddl.write_text("CREATE SCHEMA {schema_name}; "
                             "CREATE TABLE {schema_name}.{document_table} (); "
                             "CREATE TABLE {schema_name}.{content_table} ();")
              engine = mocker.MagicMock()
              conn = engine.begin.return_value.__enter__.return_value
              ensure_schema(engine, "rag", ddl, "doc", "doc_content")
              executed = str(conn.execute.call_args[0][0])  # the text() clause
              assert "rag" in executed and "doc" in executed and "doc_content" in executed
              assert "{schema_name}" not in executed
              assert "{document_table}" not in executed
              assert "{content_table}" not in executed
          ```
   - 2.2. [major] Missing test: DDL execution path. That `conn.execute(text(rendered_sql))` runs inside `engine.begin()` (module lines 130-132) is uncovered. The same mock-engine test (2.1) should assert `engine.begin` was entered and `conn.execute` was called exactly once.
        - Expected: `engine.begin.assert_called_once()` and `conn.execute.assert_called_once()`.
   - 2.3. [major] Missing test: `FileNotFoundError` when `ddl_path` does not exist (module lines 111-113; documented in the `Raises:` contract). No test asserts this.
        - Expected:
          ```python
          def test_ensure_schema_missing_ddl_raises(self, tmp_path: Path, mocker) -> None:
              """A nonexistent DDL path raises FileNotFoundError before any DB call."""
              engine = mocker.MagicMock()
              with pytest.raises(FileNotFoundError, match="Schema SQL template not found"):
                  ensure_schema(engine, "rag", tmp_path / "missing.sql")
              engine.begin.assert_not_called()
          ```
   - 2.4. [major] Missing test: identifier validation rejects an unsafe `db_schema` / table name before the DDL is read or executed (module lines 107-109; documented `Raises: ValueError`). This is the injection guard and should be asserted to fail fast — no file read, no DB call.
        - Expected: pass `db_schema="bad-schema"` (or `"Bad"`), assert `pytest.raises(ValueError, match="Unsafe SQL identifier")`, and assert `engine.begin.assert_not_called()`.
   - 2.5. [minor] Missing test: `SQLAlchemyError` from `conn.execute` is logged and re-raised (module lines 133-135, the documented failure path). Make the mocked `conn.execute` raise `SQLAlchemyError` and assert it propagates.
   - 2.6. [suggestion] Default-argument behavior: `document_table`/`content_table` default to `"document"`/`"document_content"` (module lines 84-85). Add one call omitting them and assert those defaults land in the rendered SQL, so the defaults are pinned.

3. [completed] Strengthen the existing `validate_collection_path` tests - `code/file_ingestion/unit_tests/test_utils.py`
   - **RESOLVED (3.1):** Every `validate_collection_path` rejection test now binds the message via `match="Invalid collection_path"`, and a trailing-newline rejection case (`"a.b\n"`) was added to match the new `fullmatch` behaviour.
   - **NOT APPLIED (3.2):** Collapsing the per-case rejection tests into a single `@pytest.mark.parametrize` was deliberately left as-is — it is explicitly optional and the one-test-per-case form is readable; new test groups do use parametrize where natural.
   - 3.1. [minor] Lines 31, 36, 41, 48, 53, 58, 63, 68, 73, 78: every rejection test uses bare `pytest.raises(ValueError)` with no `match=`, contrary to standard 5.2. A bare `ValueError` could be raised for an unrelated reason and the test would still pass. Bind each to the validation message.
        - Current: `with pytest.raises(ValueError):`
        - Expected: `with pytest.raises(ValueError, match="Invalid collection_path"):`
   - 3.2. [minor] Lines 29-79: the ten rejection cases share one Act/Assert shape and are prime candidates for `@pytest.mark.parametrize` (standard 5.1). Collapsing them into one parametrized test (id-labelled by failure cause) reduces duplication while preserving per-case visibility. Keep the inline comment from lines 46-47 as a case note if collapsed. Optional, since the current one-test-per-case form is also legitimate and very readable.

## Skills with No Issues

1. unit-tests — pytest usage (1): No issues. Uses pytest; no `unittest`.
2. unit-tests — file naming (2.1): No issues. `test_utils.py` matches `_utils.py` (the underscore-prefixed module name).
3. unit-tests — function naming (2.2): No issues for the present tests; they follow `test_<scenario>_<expected>` and the `TestValidateCollectionPath` class names the function under test. (New tests added per the plan should keep naming the function, e.g. `test_validate_sql_identifier_*`, `test_ensure_schema_*`.)
4. unit-tests — single behavior / AAA (3, 3.2): No issues. Each existing test asserts one behavior in a clean two-line Arrange-Act-Assert.
5. unit-tests — fixtures (3.1): N/A for the current file (no shared data needed yet). The plan's `ensure_schema` tests will introduce a `tmp_path` DDL file and a mock engine; a small fixture or `conftest.py` helper would be reasonable but is not required.
6. unit-tests — mock external boundaries (4): N/A for the current file (the only tested function is pure). Becomes relevant for `ensure_schema` (plan item 2): the SQLAlchemy `Engine` is the boundary to mock; do not stand up a real database.
7. unit-tests — DataFrame comparison (5.3): N/A. No DataFrames in this module.
8. unit-tests — parametrize (5.1): **Issue found** — see 3.2 (existing) and 1.2/1.3 (new). The rejection cases are natural parametrize candidates.
9. unit-tests — test exceptions (5.2): **Issue found** — see 3.1. No `match=` anywhere; exception tests assert type only.
10. unit-tests — independence / no private-state asserts (6): No issues. Tests are independent and assert only public return values / raises.
11. unit-tests — comprehensive coverage (7, 7.1, 7.2): **Issues found** — see plan items 1 and 2. Two of three public functions are entirely untested; `--cov` measured 40% (lines 38-43 and 105-139 uncovered).

## Status & Next Steps

**Current Status**: Implemented. All three findings resolved (3.2 parametrize-collapse deliberately not applied, per task scope). `validate_sql_identifier` and `ensure_schema` now have direct coverage, the newline-bypass cases are tested for both validators, and every rejection assertion binds the message via `match=`. Full `file_ingestion` suite green (141 passed).
**Completed**:
1. Read the unit-tests skill, the code-review template/example, and both target files in full.
2. Ran `pytest code/file_ingestion/unit_tests/test_utils.py --cov=_utils --cov-report=term-missing`; confirmed 13 passing tests and missing lines 38-43 (`validate_sql_identifier`) and 105-139 (`ensure_schema`).
3. Mapped each required behavior to its covering test and severity-ranked the gaps.
**Next Steps**:
1. None — implemented. Added `TestValidateSqlIdentifier` (item 1: parametrized valid/unsafe forms, label assertion, newline case) and `TestEnsureSchema` (item 2: placeholder rendering, transactional execution, missing-file, unsafe-identifier fail-fast, `SQLAlchemyError` propagation, defaults, stray-placeholder guard) against a mocked `Engine`, and added `match=` plus the trailing-newline case to the `validate_collection_path` tests (3.1). 3.2 parametrize-collapse deliberately not applied.
**Blockers**:
1. None. `pytest-cov` is available in the project venv; coverage was measured directly.
**Notes**:
1. Item 2.1 is [critical] because placeholder rendering substitutes identifiers into raw SQL (not bound parameters); it is both the function's core behavior and the security-sensitive surface, and it is wholly untested. The two missing-function gaps (items 1 and 2) rank above the style items (3.x), which apply to already-passing tests.
2. No code was modified and nothing was committed, per task scope.
