---
name: 20260818v01_add_env_file_flag
goal: Add an optional `--env-file` argument to every entry-point script that connects to PostgreSQL, backed by a shared environment-loading helper in `code/lib/`. This lets a run name the credentials it needs instead of depending on the working directory, which is required now that `archive_db_main` sits on a different server from `policy_db` and `briefs_db`.
created: 2026-08-18 13:03:15
updated: 2026-08-18 13:32:56
---

## Implementation Plan

### Phase 1 — Shared helper

1. [completed] Create the environment-loading helper - `code/lib/env.py`
   - 1.1. Function `load_env(env_file: str | Path | None) -> None`
   - 1.2. When `env_file` is None, load environment variables from the default dotenv search, and leave any variable already present in the environment unchanged
   - 1.3. When `env_file` names a path that does not exist, log an error naming the path and raise `FileNotFoundError`
   - 1.4. When `env_file` names an existing path, load that file's variables and let them take precedence over variables already present in the environment
   - 1.5. Log at INFO which file was loaded, or that the default search was used; never log variable values

2. [completed] Create and run unit tests for the helper - `code/lib/unit_tests/test_env.py`
   - 2.1. Test `env_file=None` leaves an already-set `POSTGRES_HOST` untouched
   - 2.2. Test a named file's value replaces an already-set `POSTGRES_HOST`
   - 2.3. Test a nonexistent path raises `FileNotFoundError` whose message contains the path
   - 2.4. Test a named file sets all four `POSTGRES_*` variables, using a dotenv file written to `tmp_path`
   - 2.5. Test the INFO log names the loaded file, and that no log record contains a variable value
   - 2.6. Run tests with pytest and verify all pass

### Phase 2 — Wire the flag into the entry points

3. [completed] Add `--env-file` to embedding generation - `code/embedding_generation/generate_embeddings.py`
   - 3.1. Add an optional `--env-file` argument defaulting to None, described as the path to a dotenv file supplying the `POSTGRES_*` variables
   - 3.2. Replace the module-scope environment load so that importing the module no longer mutates the process environment
   - 3.3. Resolve the environment inside `main()` from the parsed `--env-file` value, after logging is configured so the helper's INFO record is captured
   - 3.4. Exit 1 when the helper raises `FileNotFoundError`, logging the offending path

4. [completed] Add `--env-file` to embedding validation - `code/embedding_generation/data_validation/data_val_embeddings.py`
   - 4.1. Add the same optional `--env-file` argument and help text as task 3
   - 4.2. Replace the module-scope environment load so import no longer mutates the process environment
   - 4.3. Resolve the environment inside `main()` after logging is configured, exiting 1 on `FileNotFoundError`

5. [completed] Add `--env-file` to file ingestion - `code/file_ingestion/ingest.py`
   - 5.1. Add the same optional `--env-file` argument and help text as task 3
   - 5.2. Replace the module-scope environment load so import no longer mutates the process environment
   - 5.3. Resolve the environment inside `main()` after logging is configured, exiting 1 on `FileNotFoundError`

6. [completed] Add `--env-file` to loaded-document validation - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 6.1. Add the same optional `--env-file` argument and help text as task 3
   - 6.2. Replace the module-scope environment load so import no longer mutates the process environment
   - 6.3. Resolve the environment inside `main()` after logging is configured, exiting 1 on `FileNotFoundError`

7. [completed] Add `--env-file` to excel ingestion - `code/excel_ingestion/ingest_excel.py`
   - 7.1. Add the same optional `--env-file` argument and help text as task 3
   - 7.2. Replace the module-scope environment load so import no longer mutates the process environment
   - 7.3. Resolve the environment inside `main()` after logging is configured, exiting 1 on `FileNotFoundError`

8. [completed] Add `--env-file` to excel output validation - `code/excel_ingestion/data_validation/data_val_excel_outputs.py`
   - 8.1. Add the same optional `--env-file` argument and help text as task 3
   - 8.2. Replace the module-scope environment load so import no longer mutates the process environment
   - 8.3. Resolve the environment inside `main()` after logging is configured, exiting 1 on `FileNotFoundError`

### Phase 3 — Update existing tests

9. [completed] Update embedding generation tests - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 9.1. Add a test asserting that importing the module leaves `POSTGRES_*` unchanged in the process environment
   - 9.2. Add a test asserting `main()` exits 1 when `--env-file` names a nonexistent path
   - 9.3. Confirm the existing `TestGetEngine` cases pass unchanged

10. [completed] Update file ingestion tests - `code/file_ingestion/unit_tests/test_ingest.py`
    - 10.1. Add a test asserting that importing the module leaves `POSTGRES_*` unchanged
    - 10.2. Confirm the existing missing-env-var and non-integer-port cases pass unchanged

11. [completed] Update excel ingestion tests - `code/excel_ingestion/unit_tests/test_ingest_excel.py`
    - 11.1. Add a test asserting that importing the module leaves `POSTGRES_*` unchanged
    - 11.2. Confirm the `ephemeral_schema` fixture in `code/excel_ingestion/unit_tests/conftest.py` still resolves credentials; it performs its own environment load and must keep working

### Phase 4 — Verify against both servers

12. [completed] Run the full test suite - `code/`
    - 12.1. Run `uv run pytest` across the repo
    - 12.2. Verify no regressions in the modules not touched by this activity

13. [completed] Smoke-run an entry point against both databases - `code/embedding_generation/data_validation/data_val_embeddings.py`
    - 13.1. Run with no `--env-file` from the repo root and confirm it resolves to `localhost`, matching today's behavior
    - 13.2. Run with `--env-file .env.archivedb` and confirm it resolves to the remote host named in that file, not `localhost`
    - 13.3. Run with `--env-file` naming a nonexistent path and confirm exit code 1 with the path in the log
    - 13.4. Repeat 13.1 and 13.2 with `POSTGRES_HOST` pre-set in the shell to a third value, confirming the named file wins and the default search does not

### Phase 5 — Record the outcome

14. [completed] Update the deferred review finding - `docs/code_review/embedding_generation/cr_20260816v01_generate_embeddings.md`
    - 14.1. Change finding 6.3's Resolution from Deferred to resolved, citing this activity
    - 14.2. Note that the import-time environment mutation is removed from all six modules, not only `generate_embeddings.py`

## Key Data Decisions and Considerations

1. One shared helper in `code/lib/env.py` rather than the same block repeated in six files — every affected module already inserts `code/lib` on `sys.path` and imports from it, so this matches the established pattern and keeps the precedence and fail-fast semantics defined in exactly one place.
2. A named `--env-file` takes precedence over variables already in the environment, while the default search does not — naming a file is an explicit statement of intent and should beat a stale `POSTGRES_HOST` left in the shell, whereas the no-flag path must behave exactly as it does today so no existing invocation changes.
3. Fail fast on a missing `--env-file` path — the underlying loader returns a falsy result for a nonexistent file rather than raising, so a typo would silently fall through to whatever credentials the environment already holds. That is the same class of bug this activity exists to remove, so the existence check must be explicit.
4. Moving the environment load from module scope into `main()` is required, not incidental — argument parsing happens inside `main()` and cannot inform a call that already executed at import. This resolves code review finding 6.3 in `cr_20260816v01_generate_embeddings.md`, which recommended exactly this change and was deferred on 2026-08-17 as harmless.
5. The behavior change is that importing these modules no longer mutates the process environment. `code/excel_ingestion/unit_tests/conftest.py` performs its own environment load and is unaffected; the embedding and file ingestion suites already `monkeypatch` the four `POSTGRES_*` variables they depend on.
6. All six modules are in scope rather than only the two embedding-side scripts that `archive_db_main` needs immediately — a repo where one script can target a remote database and five cannot is harder to reason about than a uniform flag, and the other four are the identical change.
7. The three `data_validation` scripts changed by tasks 4, 6, and 8 have no unit test files today, and this activity does not create them. The behavior being added lives entirely in the shared helper, which task 2 covers directly; task 13 exercises one of the three end to end against both servers. Standing up three new suites is worthwhile but is a larger scope than this change and belongs in its own activity.
8. This activity produces no data files or tables and reads no new inputs, so there are no data validation tasks. Verification is unit tests plus the connection-target smoke runs in task 13.
9. Both databases are reachable from the authoring environment — `localhost:5432` answers and the remote host in `.env.archivedb` is in active use — so task 13 runs here rather than being documented for manual execution.
10. Task 13 uses `data_val_embeddings.py` for the smoke runs because it is read-only; it reports on existing embeddings rather than writing any, so a run against either server is non-destructive.
11. Line numbers for the existing module-scope environment loads are deliberately omitted from the tasks, because applying tasks 3 through 8 shifts them. At the time of writing the calls are the sole module-scope `load_dotenv()` in each of the six files, which locates them unambiguously.
12. Downstream dependency, not a task here: `archive_db_rag/README.md` documents a shell workaround that pre-loads `.env.archivedb` before invoking `generate_embeddings.py`. Once this activity ships that workaround should be replaced with `--env-file`, but it stays correct until then, and editing another repository's files is out of scope for this plan.

### Implementation notes (2026-08-18)

13. The entry points do not repeat the missing-file message on the `FileNotFoundError` path. `load_env` already logs `Env file not found: {path}` at ERROR before raising, and that record lands in the same run's log because the call sits after `setup_logging`, so each `except FileNotFoundError` only closes the run boundary and exits 1. Repeating the path in the caller would duplicate a library-emitted detail, which the logging skill forbids. Task 13.3 confirms the path is present in the log with exit code 1.
14. The three import-inertness tests (tasks 9.1, 10.1, 11.1) use `importlib.reload`, which forced an accommodation for a pre-existing repo hazard: three directories hold a module named `_utils`, and Python caches modules by bare name. In a whole-repo run a sibling suite's `_utils` is already cached, so the reload initially failed with `ImportError: cannot import name 'compute_source_hash' from '_utils'`. Each test now pins its own module directory with `monkeypatch.syspath_prepend` and evicts the foreign copy with `monkeypatch.delitem(sys.modules, "_utils")`, both undone at teardown so later suites see the cache state they expect. This mirrors the eviction the `conftest.py` files already perform at collection time. The alternative — importing each module in a subprocess — was measured and rejected: a cold `generate_embeddings` import costs 8.5 s against a 15 s whole-suite run.
15. Verified during task 13, out of scope to fix here: `data_val_embeddings.py` lets a connection-time `sqlalchemy.exc.OperationalError` escape `main()` uncaught, so the traceback goes to stderr and the run's log ends without an ERROR record. This is pre-existing behavior untouched by this activity — it is how the script has always failed on an unreachable database or a missing one — but it means a failed run is diagnosable only from the console, not the log. It belongs in a follow-up review or activity for that script.
16. Finding 6.3 in `cr_20260816v01_generate_embeddings.md` keeps its `[suggestion]` tag and its original `Current:`/`Expected:` text, including the now-stale "Line 46" reference. Only the Resolution was rewritten, per task 14. The review file records what the reviewer saw on 2026-08-16; rewriting its observations to match today's line numbers would falsify that record, and the Resolution is the field that carries the outcome forward.
17. Task 13's host-resolution matrix was verified in fresh subprocesses rather than read out of the run logs, because `load_env` deliberately never logs variable values. The four cases confirmed: no flag resolves to `localhost`; `--env-file .env.archivedb` resolves to the remote host named in that file; with `POSTGRES_HOST` pre-set to a third value the default search leaves it alone; and the named file still wins over that pre-set value. The end-to-end run in 13.2 independently corroborates the target — it reached the remote host and failed only with `database "policy_db" does not exist`, which is the expected answer from that server for a `policy_db` config.
