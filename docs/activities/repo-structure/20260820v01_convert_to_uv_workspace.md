---
name: 20260820v01_convert_to_uv_workspace
goal: Convert the repo into a uv workspace of installable packages — `ingestion-lib` plus one package per engine module, and the policy_db instance as a member declaring which of them it uses. Adopting real package names and installed console scripts deletes the 40 `sys.path.insert` lines and every workaround built on top of them, and makes the module independence the code already has enforceable by the build rather than by discipline. Anchor the configs' data and log paths to the instance root in the same change, because installed entry points run from any directory and remove the accident that currently keeps working-directory-relative paths correct.
created: 2026-08-20 17:10:18
updated: 2026-08-20 19:55:00
---

## Implementation Plan

### Phase 1 — Workspace skeleton and the shared library

1. [completed] Create the workspace root - `pyproject.toml`
   - 1.1. Replace the current project table with a workspace root: `[tool.uv.workspace]` listing `packages/*` as members, `requires-python = ">=3.13"`, and the shared development dependencies (`pytest`, `pytest-cov`, `pytest-mock`). `instances/*` joins the member list in task 8, not here — uv errors on a glob-matched member directory that lacks a `pyproject.toml` (verified 2026-08-20), and the instance does not get one until then
   - 1.2. Deliberately omit `[build-system]` so the root stays uninstallable — it coordinates members and is not itself a package
   - 1.3. Keep `constraint-dependencies = ["transformers<5"]` at the root so one model-stack constraint governs every member
   - 1.4. Remove `addopts = "--import-mode=importlib"`; it exists only for the duplicate `test_utils.py` basenames that packaging eliminates (verify in task 17)

2. [completed] Create the shared library package - `packages/ingestion-lib/`
   - 2.1. `pyproject.toml` naming the distribution `ingestion-lib`, declaring `python-dotenv` and `python-json-logger`, and the hatchling build backend
   - 2.2. `git mv` the `code/lib/logconfig/` package directory and `code/lib/{validators,env,sql_comments}.py` to `src/ingestion_lib/`, adding a top-level `__init__.py`; `logconfig` stays a subpackage (`ingestion_lib.logconfig`), so its own `__init__.py` re-export is unchanged
   - 2.3. `git mv` `code/lib/unit_tests/` to `packages/ingestion-lib/tests/`
   - 2.4. `git mv` `code/testing/create_ingestion_test_db.sql` to `src/ingestion_lib/sql/` and declare it as package data — it provisions the database every member's tests use, so it belongs with the shared library rather than in a top-level directory (rationale in decision 6)
   - 2.5. Rewrite the library's internal imports to the qualified form (`sql_comments` imports `validators`; `env` imports `logconfig`)

3. [completed] Verify the workspace installs and the library imports
   - 3.1. `uv sync` at the root, regenerating `uv.lock` as the single workspace lockfile; confirm `ingestion_lib` resolves from an editable install rather than a `sys.path` entry
   - 3.2. Compare the model-stack versions in the new lock against the current one — `torch`, `transformers`, `sentence-transformers` must resolve to the same versions, since the corpus's reproducibility gate depends on loading the same embedder; investigate any drift rather than accepting it silently
   - 3.3. `uv run pytest packages/ingestion-lib` passes
   - 3.4. Confirm `import ingestion_lib.logconfig` succeeds from a non-root working directory

### Phase 2 — Carve the engine packages

4. [completed] Create the file-ingestion package - `packages/file-ingestion/`
   - 4.1. `pyproject.toml`: distribution `file-ingestion`, dependencies `ingestion-lib`, `docling`, `docling-core`, `pdfplumber`, `pydantic`, `sqlalchemy`, `psycopg2-binary`; `[tool.uv.sources] ingestion-lib = { workspace = true }`; hatchling backend
   - 4.2. `git mv` the 8 source modules from `code/file_ingestion/` to `src/file_ingestion/`, keeping the `data_validation/` and `sql/` subdirectories; add `__init__.py` to each package directory
   - 4.3. `git mv` `unit_tests/` (9 files) to `packages/file-ingestion/tests/`
   - 4.4. Declare `[project.scripts]` for the four entry points: `ingest.py`, `quality_report.py`, `data_val_cleaned_json.py`, `data_val_loaded_documents.py` (names chosen in task 15)
   - 4.5. Keep `config/example.toml` and `sql/schema.sql` inside the package as package data

5. [completed] Create the excel-ingestion package - `packages/excel-ingestion/`
   - 5.1. `pyproject.toml`: dependencies `ingestion-lib`, `openpyxl`, `sqlalchemy`, `psycopg2-binary`; workspace source for the lib; hatchling
   - 5.2. `git mv` the 6 source modules from `code/excel_ingestion/` to `src/excel_ingestion/`, and `unit_tests/` (6 files) to `tests/`
   - 5.3. Declare `[project.scripts]` for `ingest_excel.py`, `data_val_excel_inputs.py`, `data_val_excel_outputs.py`
   - 5.4. Keep `config/example.toml` and `sql/excel_schema.sql` as package data

6. [completed] Create the embedding-generation package - `packages/embedding-generation/`
   - 6.1. `pyproject.toml`: dependencies `ingestion-lib`, `sentence-transformers`, `torch>=2.10,<2.11`, `sqlalchemy`, `psycopg2-binary`; workspace source for the lib; hatchling
   - 6.2. `git mv` the 4 source modules from `code/embedding_generation/` to `src/embedding_generation/`, and `unit_tests/` (4 files) to `tests/`
   - 6.3. Declare `[project.scripts]` for `generate_embeddings.py` and `data_val_embeddings.py`
   - 6.4. Keep `config/example.toml` as package data

7. [completed] Verify package data survives a build
   - 7.1. `uv build` all four packages — `ingestion-lib` included, since its wheel carries `create_ingestion_test_db.sql` — and list the wheel contents
   - 7.2. Confirm `sql/schema.sql`, `sql/excel_schema.sql`, `sql/create_ingestion_test_db.sql`, and the three `config/example.toml` files are present in their wheels — hatchling includes package-directory files by default, but this is the check that turns a packaging mistake from silent into visible (decision 4)
   - 7.3. Confirm no wheel contains another package's modules

### Phase 3 — The instance as a workspace member

8. [completed] Create the policy_db instance package - `instances/policy_db/`
   - 8.1. `pyproject.toml`: distribution `policy-db-instance`, depending on `file-ingestion`, `excel-ingestion`, and `embedding-generation` with workspace sources — the dependency list is the declaration of which engine components this instance uses (decision 5). Add `instances/*` to the workspace members list in the root `pyproject.toml` in the same change (deferred from task 1.1)
   - 8.2. `git mv` `instances/policy_db/code/acquisition/{cms_iom,usc_titles}/` to `instances/policy_db/src/policy_db_acquisition/`, adding `__init__.py`, and their `unit_tests/` (2 files) to `instances/policy_db/tests/`
   - 8.3. Move the 7 acquisition config TOMLs out of the module directories into `instances/policy_db/config/acquisition/{cms_iom,usc_titles}/` — per 8.5 configs are instance assets, and leaving them inside `src/` would ship them as package data
   - 8.4. Declare `[project.scripts]` for the four acquisition entry points
   - 8.5. Keep `config/`, `grants/`, `contracts/`, and the env files outside `src/` — they are instance assets, not importable code

### Phase 4 — Qualified imports and the workarounds they retire

9. [completed] Rewrite every import to its qualified form - all packages
   - 9.1. Replace bare imports with package-qualified ones: `from _utils import x` becomes `from file_ingestion._utils import x`; `from logconfig import get_logger` becomes `from ingestion_lib.logconfig import get_logger`
   - 9.2. Delete all 40 `sys.path.insert` lines across the 31 files that carry them, along with their explanatory comments
   - 9.3. This is one mechanical substitution repeated across every module, so it is a single task rather than 31 (decision 10)

10. [completed] Delete the workarounds that bare names required
    - 10.1. Remove the foreign-`_utils` eviction blocks from the three module conftests (`file_ingestion`, `excel_ingestion`, `embedding_generation`) — three files can no longer claim one name
    - 10.2. Delete the root `conftest.py`, whose only job is importing `pdb` before pytest replaces `sys.modules["code"]`; with no top-level `code/` directory the stdlib module is never shadowed
    - 10.3. Repoint the `ephemeral_schema` fixtures at `.env.test` by walking up from the test file to the workspace root, since tests now live outside the installed package
    - 10.4. Run each package's suite in isolation and the whole workspace together, confirming identical results

### Phase 5 — Path anchoring and data relocation

11. [completed] Anchor config-internal paths to the instance root - `packages/ingestion-lib/src/ingestion_lib/`
    - 11.1. Resolve a config's relative paths (`source_dir`, `parsed_dir`, `cleaned_dir`) against the **instance root** rather than the working directory, discovered by walking up from the `--config` path until a marker is found — the instance's `pyproject.toml` (decision 8)
    - 11.2. Fail with an actionable error naming the config path when no instance root is found, rather than silently falling back to the working directory
    - 11.3. Adopt the resolver in every entry point whose config carries filesystem paths — `ingest.py`, `quality_report.py`, `data_val_cleaned_json.py`, `data_val_loaded_documents.py`, `ingest_excel.py`, `data_val_excel_inputs.py`, `data_val_excel_outputs.py`, and the four acquisition scripts (whose configs name `output_base_dir`/`output_dir`); the embedding configs carry no filesystem paths and need no change
    - 11.4. Add unit tests: a config resolves identically when invoked from three different working directories, and a config outside any instance fails clearly

12. [completed] Relocate the corpus data and logs under the instance
    - 12.1. `mv data instances/policy_db/data` and `mv logs instances/policy_db/logs` — 1.7 GB, untracked, so a plain move rather than `git mv`
    - 12.2. The 45 config files need no edit: their existing `data/input/...` strings resolve correctly once anchored to the instance root, which is why the data moves under the instance rather than staying at the repo root (decision 7)
    - 12.3. Make the `.gitignore` entries explicit (`instances/*/data/`, `instances/*/logs/`) rather than relying on the bare `data/` pattern matching at any depth

13. [completed] Anchor log directories - all entry points
    - 13.1. Replace the 18 hardcoded `setup_logging(log_dir="logs/...")` calls with a resolution anchored to the instance root, so logs collect in one place regardless of where a console script is invoked
    - 13.2. Where a run has no instance (an engine test, a config-less invocation), fall back to a documented location rather than the working directory
    - 13.3. Confirm a console script run from a home directory writes its log under the instance, not beside the caller

### Phase 6 — References and documentation

14. [completed] Move the code review tree to mirror the new source tree - `docs/code_review/`
    - 14.1. `git mv` each review directory to its new mirrored path (`code/file_ingestion` → `packages/file-ingestion/src/file_ingestion`, and the equivalents for the other packages and the instance)
    - 14.2. Move only; do not rewrite review contents — each review records what its reviewer saw, including the paths it cites (decision 12)

15. [completed] Update every documented command - `README.md`, config headers, `readme/`
    - 15.1. Choose the console script names and record them in one table in `README.md`, replacing the `uv run code/<module>/<script>.py` form
    - 15.2. Rewrite the 62 files that document the old invocation — principally the `# Usage:` headers in the instance config TOMLs
    - 15.3. Rewrite the README's structure, setup, and usage sections for the workspace layout, including that `uv sync` at the root installs every member into one shared environment
    - 15.4. Move `readme/document-parsing.md` into `packages/file-ingestion/` — it documents that package's behavior and has no home at a workspace root

### Phase 7 — Verify

16. [completed] Confirm the packages are genuinely independent
    - 16.1. In a scratch environment, install `excel-ingestion` alone; confirm it pulls `ingestion-lib` but neither `docling` nor `torch`, and record the resulting size against the 1.2 GB full install
    - 16.2. Confirm importing another engine package from that environment fails — the check that module independence is now enforced rather than conventional
    - 16.3. Grep checks, zero matches expected: `sys.path.insert` anywhere under `packages/` or `instances/`; any bare `from _utils import`

17. [completed] Run the full suite and the end-to-end checks
    - 17.1. `uv run pytest` across the workspace — all tests pass, from the workspace root and from a non-root working directory
    - 17.2. Confirm mypy checks every package in one pass with no duplicate-module error and no per-directory configuration
    - 17.3. Run each console script's `--help` from a directory outside the repo
    - 17.4. Smoke-run the pipeline end to end with the moved data: ingest the test document, validate it, generate embeddings on a small `source_filter`, validate those — exit 0 for each
    - 17.5. Confirm the smoke run's logs landed under `instances/policy_db/logs/` and its outputs under `instances/policy_db/data/`
    - 17.6. If issues found, debug and iterate

## Key Data Decisions and Considerations

1. A workspace rather than one package with optional dependencies, because the three engine modules are already independent in the code — verified 2026-08-20: none imports another, and their third-party dependencies are disjoint (docling for file, openpyxl for excel, torch for embedding). Extras would isolate the dependencies but leave the independence resting on discipline, since nothing prevents a cross-import inside a single package. Separate distributions make a cross-import fail at install time unless declared.
2. Separate repos were considered and rejected. A repo boundary should follow an ownership or lifecycle boundary; these three share both, are consumed together by every instance, and share `code/lib` at 38 import sites. Splitting them would also make cross-cutting consistency work — which is what the pending engine-hardening activity is entirely composed of — require coordinated changes across three repos.
3. A `src/` layout inside each package so imports resolve to the installed distribution rather than the source tree. Five files are loaded by path at runtime (two SQL templates, three `example.toml`), and a flat layout would let a build that omits them still pass every local test.
4. Hatchling as the build backend. Verified 2026-08-20 by building the same package twice: hatchling includes files inside the package directory automatically, while setuptools ships only `.py` files unless package data is configured explicitly. Task 7 verifies the wheel contents regardless, because "the backend probably handles it" is not a check.
5. The instance is a workspace member, not a bare directory, because it contains code (the acquisition scripts) that imports the shared library and therefore needs a declared dependency edge. Its `pyproject.toml` then does useful work beyond that: the dependency list states which engine components this instance uses. `archive_db_rag` is the known next member — it currently invokes `generate_embeddings.py` from a sibling checkout and will later ingest documents too, so it joins as `instances/archive_db/` depending on `embedding-generation` and `file-ingestion` but not `excel-ingestion`. Folding it in is a separate activity; this one only has to leave the seam it slots into.
6. `create_ingestion_test_db.sql` moves into `ingestion-lib` rather than staying in a top-level `testing/` directory. It provisions the database that every member's tests use, so it belongs with the one package they all depend on; a top-level directory outside the workspace members would have no owner and no install path.
7. Corpus data moves under the instance because it belongs to the instance, and because that placement is what keeps the 45 config files unedited. Their existing `data/input/...` strings resolve correctly the moment resolution is anchored to the instance root — the same strings, measured from a different origin. Anchoring to the config file instead would force `../../../data/...` at varying depths.
8. Instance-root discovery walks up from the `--config` path to a marker rather than counting directory levels, because configs sit at varying depths (`config/excel_ingestion/qpp_cm/` versus `config/embedding_generation/`). The instance's `pyproject.toml` is the marker, which this activity creates anyway — so the workspace conversion supplies the anchor the path fix needs, and the two belong in one activity.
9. Path anchoring is in scope here rather than deferred again. Today the launch command embeds a repo-relative script path, so the working directory is correct by construction and the fragility is invisible. Installed console scripts run from anywhere, which removes that accident: a run from the wrong directory would look for `~/data/input/...` and either find nothing or, on a parse run, write output there.
10. Task 9 rewrites imports across every module in one task rather than one task per file. It is a single mechanical substitution — qualify the name, delete the `sys.path` line — repeated across 31 files, so per-file tasks would obscure that it is one decision applied throughout. The same grouping applies to tasks 10, 13, 14, and 15.
11. This activity creates no new data tables or output files, so no per-output validation task is written. What is under test is behavioral: the suite passing from any working directory (17.1), package independence proven by a scratch install (16.1-16.2), wheel contents (task 7), and an end-to-end smoke run whose outputs land under the instance (17.4-17.5). It changes no stored corpus contents.
12. Review files move with the code but are not rewritten. A review records what its reviewer saw at the time, including the source paths it cites; the directory says where the file is now. This repeats the migration made on 2026-08-20 for the previous reorganization, and is the second such move — the layout after this activity is intended to be final.
13. A shared venv is what the workspace gives locally: `uv sync` at the root installs every member into one environment, which is correct while developing across them. The isolation benefit accrues to consumers — an external repo depending on `embedding-generation` alone skips docling entirely. Task 16.1 measures that in a scratch environment rather than expecting the local venv to shrink.
14. Sequencing against the pending engine-hardening activity: this runs first, so that activity's consolidation work lands directly in `ingestion-lib` instead of landing in `code/lib` and moving again, and so its mypy configuration task becomes unnecessary. The exception is the three silent-data-loss defects in that plan (the unguarded `overwrite`, the chunker dropping whitespace-only sections, and `header_row = 0` taking a sheet's last row as its header); those are small, self-contained, and dangerous enough to fix before either restructure.
15. Every line and path citation in the hardening activity becomes stale when this lands. That plan already states that line numbers drift and must be re-verified by grep before editing; this activity is the event that invalidates them, and the two should not be implemented concurrently.
16. `docs/code_review/code/excel_ingestion_qpp_cm/` mirrors a module that was consolidated away and has no current source. It stays as-is: the convention has no rule for reviews of deleted code, and inventing one for a single case is not worth it.

### Implementation notes (2026-08-20)

17. Deviation from 1.1/1.2: a root with only dev dependencies installs no members on a plain `uv sync` (uv syncs only the current project's dependencies). The root therefore keeps a minimal `[project]` table that depends on every workspace member via `[tool.uv.sources] { workspace = true }`, which makes `uv sync` at the root install the whole workspace as 15.3 documents. The root still has no `[build-system]`, so it is never built or installed as a package — 1.2's intent holds.
18. Deviation from 1.4: removing `--import-mode=importlib` initially failed — the two `tests/test_utils.py` basenames (file-ingestion, excel-ingestion) still collided under pytest's default import mode, and also aborted mypy with a duplicate-module error. Resolved by renaming them to `test_file_ingestion_utils.py` and `test_excel_ingestion_utils.py` (globally unique basenames); the addopts line is then removed as planned and mypy runs in one pass with no duplicate-module error and no per-directory configuration.
19. Task 3.2 found real drift and fixed it: the intermediate lib-only sync (before the engine packages existed) dropped the model-stack pins from `uv.lock`, so re-adding the engines resolved 68 packages to newer versions (`sentence-transformers` 5.2.2→5.7.0, `docling` 2.107→2.121, `pandas` 2→3 among them). Fixed by restoring the pre-conversion `uv.lock` and re-running `uv lock`, after which every third-party package resolves to its previously locked version (zero drift; only the five workspace members were added).
20. Environment note: the pre-existing `.venv` carried the macOS `UF_HIDDEN` flag on its files, and Python 3.14's `site` skips hidden `.pth` files — which silently broke the editable installs the workspace relies on. Recreating `.venv` fresh (`rm -rf .venv && uv sync`) resolved it; new files carry no flag.
21. Task 11.3 scope note: `data_val_loaded_documents.py` and `data_val_excel_outputs.py` configs carry no filesystem paths (they validate database state), so they adopt only the log-dir anchoring; the path resolver itself is adopted by the other nine listed entry points. `quality_report.py`'s machine-readable JSON report (previously written to a CWD-relative `logs/...` path) now writes to the same anchored log directory as its log.
22. The path/log anchoring lives in `ingestion_lib.paths` (`find_instance_root` / `require_instance_root` / `resolve_config_path` / `resolve_log_dir`); the no-instance fallback log location (13.2) is documented as `$TMPDIR/ingestion_pipeline/logs/`, used by the engine test modules' import-time `setup_logging` calls. The excel `ephemeral_schema` conftest finds `.env.test` by walking up to the pyproject.toml carrying `[tool.uv.workspace]` (10.3).
23. Console script names (15.1, recorded in README.md): `ingest-files`, `quality-report`, `data-val-cleaned-json`, `data-val-loaded-documents`, `ingest-excel`, `data-val-excel-inputs`, `data-val-excel-outputs`, `generate-embeddings`, `data-val-embeddings`, `download-cms-iom`, `data-val-downloaded-pdfs`, `download-usc-titles`, `data-val-downloaded-zips`.
24. Verification results: full suite 629 passed (root and non-root cwd); scratch install of `excel-ingestion` alone is 20 MB vs the 1.2 GB full workspace venv, pulls `ingestion-lib` but neither docling nor torch, and the other engine packages are unimportable (16.1/16.2); all four wheels carry their SQL/config package data and no foreign modules (task 7); all 13 console scripts pass `--help` from outside the repo; the end-to-end smoke run (ingest test document → data-val-cleaned-json → data-val-loaded-documents → generate-embeddings with a `cms_iom.test.%` source_filter → data-val-embeddings) exits 0 at every step, with logs under `instances/policy_db/logs/` and outputs under `instances/policy_db/data/`. mypy one-pass reports 169 pre-existing type errors (none in code this activity added) — the hardening activity's territory.
25. A permanent smoke-test embedding config was added for 17.4 and kept: `instances/policy_db/config/embedding_generation/generate_embeddings_test.toml` (embeds only `test_document_content` rows matching `cms_iom.test.%` into a dedicated test embedding table, `overwrite = true`).
26. The instance package's `pyproject.toml` needs `[tool.hatch.build.targets.wheel] packages = ["src/policy_db_acquisition"]` because the distribution name (`policy-db-instance`) does not match the import package; it also declares `requests`/`urllib3`/`beautifulsoup4`, the acquisition scripts' direct dependencies, beyond the engine packages the plan listed.
