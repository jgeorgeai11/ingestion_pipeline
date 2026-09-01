---
name: cr-test_generate_embeddings
goal: Address code quality issues identified in code/embedding_generation/unit_tests/test_generate_embeddings.py (and conftest.py) to align with the python-development unit-tests skill.
created: 2026-06-25
updated: 2026-06-25
---

## Implementation Plan

1. [completed] The real SELECT/INSERT SQL is never exercised; tests re-implement it inline - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 1.1. [major] Lines 513-585 (`TestDynamicSqlGeneration`): All three tests build their own `select_sql` / `insert_sql` strings inside the test body and then assert against those local strings. The source's actual SQL construction (`generate_embeddings.py:506-521`, `604-610`) is never called, so these tests are tautological — they verify the test's own f-string, not the production query, and would still pass if the source query were broken.
        - Rationale: The unit-tests skill (3.2 AAA, and the prior review's own suggestion 5.1) wants assertions on real behavior. The `TestChunkingWiring` / `TestContextualChunkHeaders` classes already drive the real `generate_embeddings` with a stubbed engine and capture bound params via a `side_effect` — the same harness can capture the SELECT string passed to `select_conn.execute` (its first positional arg's `.text`) and the INSERT string, then assert on the production SQL. As written, the select/insert structure is effectively untested.
        - Current: `select_sql = (f"select {pk_cols_csv}, ..." ...)` rebuilt in the test, then `assert "select src.id, ..." in select_sql`.
        - Expected: Invoke `generate_embeddings(...)` with mocks (reuse `_setup_generate_embeddings_mocks`), capture the SQL handed to `conn.execute` from the source, and assert the join/`is null`/`order by` and the insert column list / `cast(:embedding as vector)` against that captured production string.
   - 1.2. [major] The new `::text` cast on the source_filter is only tested in isolation (`TestBuildSourceFilterClause`), never in the real SELECT path. `generate_embeddings.py:511-519` injects the aliased filter (`and src.col::text like ...`) into the live SELECT and binds `filter_params`; no test runs `generate_embeddings` with a non-None `source_filter` and asserts the clause reaches the executed SELECT or that `filter_params` are bound. The integration of the `::text` filter (the headline change in this revision) is uncovered end-to-end.
        - Rationale: `_build_source_filter_clause` is well unit-tested, but the wiring at line 511-519 (alias prefix + `select_filter` concatenation + param binding) is a distinct branch with no coverage.
        - Current: No `generate_embeddings` invocation passes `source_filter`.
        - Expected: A test passing `source_filter={"module_name": "Pub%"}` that asserts `src.module_name::text like :sf_module_name_0` appears in the captured SELECT and `sf_module_name_0` is in the bound params.

2. [completed] `main()` is entirely untested - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 2.1. [major] `generate_embeddings.py:638-761` (`main()`) has zero coverage (coverage reports 641-761, 765 missing). The resilient per-table loop — `embed_columns`-missing skip (708-711), per-table `try/except (ConfigurationError, ValueError, SQLAlchemyError)` that records `failed_tables` and continues (730-749), the CLI `--overwrite` override of the TOML default (694-695), the `sys.exit(1)` on any failed table (756-759), and the missing-config / bad-TOML / missing-field exits (660-685) — are all unverified. The prior review (item 3.6) flagged this and it remains open.
        - Rationale: `main()` is the entry point and owns the no-PK / per-table failure resilience the task calls out. Its behavior (one bad table must not abort the run, but must force a non-zero exit) is exactly the kind of branch unit tests should pin. The no-PK path specifically surfaces here: a table whose `_detect_primary_keys` raises `ConfigurationError` must be recorded in `failed_tables` and not crash the other tables.
        - Current: No `TestMain` class; `main()` never invoked.
        - Expected: Tests driving `main()` with a temp TOML (`tmp_path`) and `generate_embeddings` patched: (a) two tables where one raises `ConfigurationError` → the other still processed, `failed_tables` non-empty, `SystemExit` raised; (b) a table missing `embed_columns` is skipped; (c) `--overwrite` CLI flag overrides the TOML default passed through to `generate_embeddings`; (d) missing config file / missing required field → `SystemExit(1)`.

3. [completed] `_resolve_pg_column_type` is mocked away, never exercised - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 3.1. [minor] Lines 374-403 (`test_create_embedding_table_nulltype_pk_resolves_pg_type`) patches `generate_embeddings._resolve_pg_column_type` to return `"ltree"`, so the function's own body (`generate_embeddings.py:214-227`, the `pg_attribute`/`format_type` query and `scalar_one()`) is never run (coverage: 214-225 missing). The test proves the DDL uses the resolved type but not that resolution works.
        - Rationale: The NullType→canonical-PG-type fallback is the reason this branch exists (custom `ltree` PKs). A small test that drives `_resolve_pg_column_type` directly with a mocked `engine.connect().execute().scalar_one()` would cover the query-build + return path cheaply.
        - Current: The function is replaced wholesale by `mocker.patch(... return_value="ltree")`.
        - Expected: Add `test_resolve_pg_column_type_returns_canonical_type` mocking the connection's `scalar_one()` to assert the bound `{schema, tbl, col}` params and the returned string.

4. [completed] Uncovered resilience branches inside generate_embeddings - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 4.1. [minor] `generate_embeddings.py:460-467` (source_filter-references-unknown-column → `ConfigurationError`) is uncovered (461-465 missing). No test feeds a `source_filter` whose key is absent from the mocked `get_columns`, so the `invalid_cols` guard and its error message are unverified.
        - Rationale: This is the one place `generate_embeddings` raises `ConfigurationError` for bad filter config; it pairs naturally with the finding-1.2 source_filter integration test.
        - Current: Not tested.
        - Expected: With `insp.get_columns` mocked to a known set, pass `source_filter={"not_a_col": "x%"}` and assert `pytest.raises(ConfigurationError, match="columns not on")`.
   - 4.2. [minor] The overwrite DELETE path (`generate_embeddings.py:485-498`, uncovered 486-495) and the no-rows early return (`527-529`, uncovered 528-529) have no tests. Overwrite scoping by `filter_params` and the `return 0` short-circuit when `fetchall()` is empty are both observable, mockable behaviors.
        - Rationale: Overwrite is a documented mode that mutates data (DELETE); the no-rows path is the idempotent re-run guarantee. Both are reachable with the existing `_setup_generate_embeddings_mocks` harness (set `fetchall` to `[]` for the no-rows case; pass `overwrite=True` and assert the DELETE SQL + rowcount logging for the other).
        - Current: Not tested (`overwrite=False` everywhere; `fetchall` always returns one row).
        - Expected: `test_overwrite_deletes_then_regenerates` and `test_no_rows_returns_zero`.

5. [completed] `get_tokenizer` `_first_module` fallback is uncovered - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 5.1. [minor] `_utils.py:49-51` — when `model.tokenizer` is absent, `get_tokenizer` falls back to `model._first_module().tokenizer`; coverage reports `_utils.py:51` missing. `TestTokenCounter` only covers the `model.tokenizer`-present path.
        - Rationale: The fallback exists for older/custom SentenceTransformer layouts; a one-line test with a model mock lacking `.tokenizer` but exposing `_first_module().tokenizer` would close it.
        - Current: Only the happy path (`mock_model.tokenizer = ...`) is tested.
        - Expected: `test_get_tokenizer_falls_back_to_first_module` using `mocker.MagicMock(spec=...)` (or `del mock_model.tokenizer`) so `getattr(model, "tokenizer", None)` is None.

6. [completed] Signature-introspection tests assert API shape, not behavior - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 6.1. [suggestion] Lines 459-510 (`TestConfigParsing`): These use `inspect.signature` to assert parameter names exist / are absent (e.g. `source_table_pks` removed, `max_tokens` present). They pin the contract but pass even if the body is broken. The removal of `source_table_pks` is better proven behaviorally by the `TestChunkingWiring` / header tests that already call `generate_embeddings` without it.
        - Rationale: Skill 6 ("don't assert on internal state") and AAA favor behavioral assertions. The signature tests are cheap regression canaries, so keep them, but they should not be the only evidence for the removed-param and token-budget changes.
        - Current: `assert "source_table_pks" not in sig.parameters`, etc.
        - Expected: Retain as lightweight canaries; ensure the behavioral coverage in findings 1-2 carries the real assurance.

## Skills with No Issues

1. unit-tests — Naming: No issues found. Method names now follow `test_<function>_<scenario>_<expected>` (the prior review's 1.1-1.4 naming gaps are resolved); helpers `_setup_generate_embeddings_mocks` are clearly non-test.
2. unit-tests — `mocker` over `unittest.mock`: No issues found. The file uses the `pytest-mock` `mocker` fixture and `monkeypatch` throughout; no `unittest.mock` imports remain (prior review item 2 resolved).
3. unit-tests — Mock external boundaries only / heavy model mocked: No issues found. `SentenceTransformer` and `torch` are patched; `model.encode` returns `np.zeros(...)`; the tokenizer is a `MagicMock` returning `input_ids`. No real model is ever loaded, which is correct for a heavy dependency. `inspect`, the engine, and connections are all mocked at the boundary.
4. unit-tests — `_get_engine` realism: No issues found. `test_get_engine_returns_engine_with_env_vars` and `test_get_engine_percent_encodes_credentials` assert against the real `URL.create` output (`.url.host/port/username/database`, `render_as_string`), correctly proving percent-encoding of reserved characters — a real check kept cheap rather than over-mocked.
5. unit-tests — DDL assertion strength: No issues found. `TestCreateEmbeddingTable` asserts the mirrored composite PK (`primary key (..., chunk_number)`), the cascade FK (`foreign key (...) references ...`), `vector(dim)`, the generated `chunk_tsv` tsvector column, the GIN index, and the NullType→`ltree` fallback in the DDL string — strong, specific assertions (the resolution function body is the only gap; see finding 3.1).
6. unit-tests — `_build_embed_text` newline-KV: No issues found. Tests assert the `col: value\ncol: value` newline-joined format, None-skipping, and missing-column-as-empty-with-prefix — matching `generate_embeddings.py:325-330`.
7. unit-tests — Contextual-header budgeting: No issues found. `TestContextualChunkHeaders` verifies every-chunk prefixing (incl. the tail chunk_number=2), header-reserved body budget (`max_tokens - header_cost`), the overlap-floor guard (`overlap_tokens + 1`), `word_count` left as the body count, and the empty-`header_columns` backward-compat canary — assertions on both stored `chunk_text` and the `model.encode` input.
8. unit-tests — `pytest.raises(match=...)`: No issues found. All error-path tests use `match=` substrings tied to the source messages.
9. unit-tests — Order independence: Mostly fine. `TestGetEmbeddingModel` mutates module globals (`_model`, `_model_name`) but resets them at the start of each test, so order independence holds; no shared-state coupling between tests.
10. type-hints — No issues found. Test methods are annotated `-> None`; the `mocker`/`monkeypatch` fixtures follow the conventional un-annotated pytest-fixture style used across the repo.
11. docstrings — No issues found. Module, classes, helper, and every test method carry descriptive docstrings.
12. comments — No issues found. The mock-wiring comments in `_setup_generate_embeddings_mocks` explain column-order intent (the "why"), which is genuinely non-obvious.
13. logging — N/A: test module.
14. exception-handling — N/A: tests assert on exceptions rather than handling them.
15. executable-scripts — N/A for the test file itself; note finding 2.1 is that the *source* entry point `main()` is untested.
16. data-validation — N/A: not a `data_val_` script.
17. conftest.py — No issues found. The lone `conftest.py` only inserts the `embedding_generation` package dir onto `sys.path` so the source modules import by bare name; it defines no fixtures and has nothing to review beyond that. No findings fold in from it.

## Status & Next Steps

**Current Status**: First per-file review of `test_generate_embeddings.py` (and `conftest.py`). Suite is part of the 66 passing tests; `generate_embeddings.py` coverage is 68% with the entire `main()` body, the source_filter integration / overwrite / no-rows branches, and `_resolve_pg_column_type`'s body uncovered. The revision's named changes (newline-KV embed text, `URL.create` percent-encoding, removed `source_table_pks`, the re-exported `fullmatch` validator, DDL with tsv/GIN) are well covered; the gaps are in untested branches, not in the aligned changes.
**Completed**:
1. Reviewed against all python-development core skills and the unit-tests skill.
2. Ran `pytest --cov=generate_embeddings --cov=_utils --cov-report=term-missing`; mapped uncovered lines (141, 214-225, 461-465, 486-495, 528-529, 641-761, 765 in generate_embeddings.py; 51 in _utils.py) to specific findings.
3. Verified each finding against `generate_embeddings.py`, `_utils.py`, and the test file on disk — confirmed `TestDynamicSqlGeneration` rebuilds SQL inline rather than asserting the production query, and that no `generate_embeddings` call passes `source_filter`.
**Next Steps**:
1. Add behavioral SELECT/INSERT assertions and a source_filter integration test (1.1, 1.2).
2. Add a `TestMain` covering resilient per-table failure, no-PK, `--overwrite` override, and config-error exits (2.1).
3. Close the cheaper branch gaps: `_resolve_pg_column_type` body (3.1), invalid-filter-column / overwrite / no-rows (4.1-4.2), and the `get_tokenizer` fallback (5.1).
**Blockers**:
1. None.
**Notes**:
1. Review-only: no test or source file was modified; the `.coverage` artifact was deleted after measuring.
2. The advisor was rate-limited this turn; findings are grounded directly in the source and the coverage report.
