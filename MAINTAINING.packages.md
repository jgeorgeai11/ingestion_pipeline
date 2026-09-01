# Maintaining the engine packages

Everything a maintainer needs to work on the engine — the five packages under `packages/`. Instance operation (credentials, provisioning, rebuilds) lives in the per-instance `MAINTAINING.instance.*.md` files.

## 1. Workspace layout

The repo is a uv workspace. The root `pyproject.toml` is a virtual project — no `[build-system]`, never installed — that lists the members, holds the shared dev dependencies, and constrains the model stack for everyone. `uv sync` at the root installs every member editable into one shared `.venv` with a single `uv.lock`.

Each package uses the src layout (`packages/<name>/src/<import_name>/`) with hatchling, declares its own dependencies, and exposes its entry points as console scripts via `[project.scripts]`. Tests live outside the package in `packages/<name>/tests/` so they exercise the installed distribution — what ships is what gets tested. Non-Python files a package loads at runtime (SQL DDL templates, `config/example.toml`) live inside the package as package data; when touching them, `uv build` the package and list the wheel to confirm they still ship.

Rules the layout enforces, and the checks that keep them honest:

- **Engine packages never import each other.** The shared venv would let a cross-import work locally while breaking every isolated consumer, so the guard is a grep, not the venv: no `ingpipe_file_ingestion` / `ingpipe_excel_ingestion` / `ingpipe_embedding_generation` importing a sibling.
- **The engine never references an instance.** No path, name, or default under `packages/` may mention anything under `instances/`.
- **Dependencies move with code.** When code lands in a package, its imports' distributions must be declared in that package's `pyproject.toml`. The shared venv masks a missing declaration; the honest check is installing the package alone in a scratch environment and running its suite there.
- **Instances consume the engine through its console scripts and `ingpipe_lib` only.** Each package's `_utils` is private. Convention, not yet enforced by tooling.

### Package naming: the `ingpipe_` prefix

Every engine package is named `ingpipe_<stage>` (import name) / `ingpipe-<stage>` (distribution name), and a new one follows the same rule without discussion. The prefix exists because the unprefixed names collide on PyPI: `ingestion-lib` and `acquisition` are both taken by unrelated projects, and installing this repo's `acquisition` wheel alone into a scratch environment resolved a stranger's `ingestion-lib` (v0.0.7, "Library for data ingestion") instead of the shared library, so the package under test failed on missing modules. Inside the workspace the collision is invisible — `uv sync` installs the local members — which is exactly why it has to be prevented by naming rather than caught by testing. The three names that were still free (`file-ingestion`, `excel-ingestion`, `embedding-generation`) are equally generic English compounds and equally claimable tomorrow, so all five carry the prefix rather than two of them.

Three things deliberately do NOT carry it: console-script command names (`ingest-files`, `acquire`, ... — they are the user interface, are documented in every instance config's usage header, and carry no collision risk), the instance distribution `policy-db-instance` and its import package `policy_db_acquisition` (an instance is not a reusable distribution anyone would install from an index), and the on-disk manifest filename `.acquisition_manifest.json` (renaming it would orphan every manifest already written beside a corpus). Per-package subdirectories under an instance — `config/<package>/` and `logs/<package>/` — DO follow the package name, so renaming a package renames those too.

### Model-stack pin

The root pins `transformers<5` (constraint) and ingpipe-embedding-generation pins `torch>=2.10,<2.11` so the stack that built the existing corpora stays in place. Lifting either is a re-embed event (section 4). When regenerating `uv.lock`, diff the resolved `torch` / `transformers` / `sentence-transformers` versions against the previous lock — a fresh resolve once drifted 68 packages — and investigate any model-stack movement rather than accepting it silently.

## 2. Development setup and testing

1. `uv sync` at the workspace root (Python >= 3.13).
2. Provision the engine's test infrastructure once, as the provisioning account, via the two scripts in `packages/ingpipe_lib/src/ingpipe_lib/sql/` (both idempotent; the roles table below is the authority on which account may run them): `create_ingestion_test_role.sql` (pass the role password as the psql variable it demands), then `create_ingestion_test_db.sql` (no secrets — creates `ingestion_test` owned by the role and installs the `ltree` and `vector` extensions). Split because their lifecycles differ: the role is cluster-level and created once, while the database can be dropped, recreated, or re-run to add an extension with no credential in hand.

3. The role and database names are psql variables — `-v role_name=` / `-v db_name=`, defaulting to `ingestion_test_runner` / `ingestion_test`. They exist so the script can be proved end to end against a throwaway pair without touching the real one, the same reason `mcp_ro_policy_grants.sql` takes `-v expect_database=`.
4. Record the runner's four `POSTGRES_*` variables in a workspace-root `.env.test` (gitignored; template in `.env.test.example`), with the password you passed the script.
5. `uv run pytest` — from any directory.

### Roles and provisioning authority

The engine involves exactly two roles. The boundary between what is scripted and what stays manual is authority, not convenience: **the repo scripts the creation of what it owns — its `sql/` scripts create the runner role, the test database, and its extensions, idempotently — and only asserts or documents what it does not.**

| Role | Created by | Privileges |
|------|------------|------------|
| the provisioning account (recorded in `.env.provisioning` at the workspace root — a cluster credential, deliberately not filed under any instance; template in `.env.provisioning.example`) | the operator, never this repo | Must be able to: **(a) create a role** — `CREATEROLE` or `SUPERUSER`; **(b) create a database owned by that role** — `CREATEDB` or `SUPERUSER`, *plus* `SET ROLE` to the owner; **(c) install `ltree` and `vector`** — a superuser, or a cluster whose `template1` was seeded. `create_ingestion_test_db.sql` checks each of these itself before relying on it; when one is missing it stops, and its error message states the exact command that fixes it. |
| `ingestion_test_runner` | `create_ingestion_test_role.sql` (step 2) | `LOGIN` only — `NOSUPERUSER NOCREATEDB NOCREATEROLE`, no role memberships, no grants on any existing database. Owns `ingestion_test`; everything it can do flows from that ownership. |

Instance-side roles are instance matters: each instance's `MAINTAINING.instance.*.md` states its own roles and privileges.

Testing conventions:

- **DB-backed tests use `ephemeral_schema`** (shared fixture in `ingpipe_lib.testing`): a UUID-named schema in `ingestion_test`, created per test and dropped with CASCADE. Tests never touch an instance database, and the runner role has no rights on any real one.
- **Skip-not-fail:** without `.env.test` (or an unreachable server) the DB-backed tests skip and the suite stays green. A green suite on a fresh machine therefore proves less than it looks like — provision first, then trust it. There is no CI; nothing catches a silent skip.
- SQL-behavior claims (transactions, DELETEs, constraint rejections) are tested against real PostgreSQL, not mocks — keep it that way; string-asserted SQL against a MagicMock let real bugs pass at 99% coverage.
- Static analysis: `ruff check packages instances` and `mypy packages instances` both run clean workspace-wide (config in the root `pyproject.toml`, exclusions documented inline). Keep them clean.

### Portability

The code runs on macOS and Windows. House rules that keep it so: pathlib only, no POSIX-only APIs, `tempfile.gettempdir()` for scratch space — and any test that interpolates a path into TOML text must use `as_posix()` (a Windows `tmp_path` writes backslashes that TOML parses as escape sequences; this has bitten before).

**Never judge a path with `Path.is_absolute()`.** It follows the rules of the *host*, so the same string means different things on the two machines:

- `/data/in` is absolute on POSIX; on Windows it is merely root-relative, and joining it under a root lands it on whatever drive happens to be current.
- `C:/data` is absolute on Windows; on POSIX it is an ordinary relative name.
- `D:data` (drive-relative) is absolute under **neither** rule set — and yet `PureWindowsPath('C:/root/out') / 'D:data'` is `D:data`, which has left the root entirely. Testing `is_absolute()` under both rule sets is therefore *not* the fix; it still admits this form, and `\etc\passwd` with it.

So an authored or remote-supplied path is judged with `ingpipe_lib.paths.is_rooted_path`, which tests for a drive or a root under either platform's rules. Pass `path.as_posix()` when you hold a `Path` — `str()` on a Windows `Path` renders `/etc/passwd` back as `\etc\passwd`. Normalize backslashes to forward slashes *before* any `..` check as well: `PurePosixPath('..\\..\\etc/passwd').parts` contains no `..` component at all, while the Windows rules split the same string into four. This rule is enforced by `packages/ingpipe_lib/tests/test_conventions.py`, not by convention — it fails naming every offending file and line.

**Declare `encoding="utf-8"` on every text read and write.** Without it Python uses the host's locale encoding. Enforced by ruff `PLW1514` (preview-gated, so the root `pyproject.toml` sets `preview` and `explicit-preview-rules` to make it actually fire); binary-mode `open(..., "rb"/"wb")` is correctly unaffected.

**Every timestamp written to a log or an artifact is UTC.** `setup_logging` stamps `run_timestamp` from `datetime.now(UTC)` (with a `Z` suffix, so an old local-time value stays distinguishable) and renders `%(asctime)s` as ISO-8601 UTC with milliseconds (`2026-08-27T16:42:51.946Z`, which `datetime.fromisoformat` reads directly); `manifest.py` records `datetime.now(UTC)`. Local time would make one run's log and manifest disagree, and log files from the two machines unorderable against each other.

Keep the virtualenv out of any file-sync service. A synced venv corrupts itself — duplicated `lib 2/` directories, `.pth` files flagged hidden which CPython then skips, and stale console scripts left beside a half-replaced interpreter — after which every import fails with `ModuleNotFoundError` while `uv sync` reports "Audited N packages" and repairs nothing. On a machine whose checkout sits in a synced folder, create the venv as `.venv.nosync` and symlink `.venv` to it (iCloud skips paths ending `.nosync`); `uv` and every tool follow the symlink normally. Other providers use their own exclusion mechanism — Dropbox marks a folder ignored, OneDrive has per-folder exclusions.

**A strange environment is a sync problem until proven otherwise** — a `ModuleNotFoundError` for a package `uv sync` insists is installed, a console script running yesterday's code, an interpreter that disagrees with `.venv/bin/python`. Recovery is a rebuild, not a debugging session: `rm -rf .venv .venv.nosync && mkdir .venv.nosync && ln -s .venv.nosync .venv && uv sync`. If the sync service is still actively re-materializing files, `rm` loses that race and hangs with "Directory not empty"; rename the broken directory aside in place instead (`mv .venv.nosync .venv.stale`, an instant inode relink) and delete it once the service has settled. Do not move it to another filesystem to delete it — that forces a byte-copy and hangs the same way.

## 3. The data interface the engine writes

The engine's output is plain PostgreSQL — any consumer (e.g. a search server) reads these tables by introspection and never imports engine code. This section is the authority on the shapes; the DDL templates in each package implement it.

**The database a config names must already exist; the schema need not.** Creating a database (and installing its extensions) is a provisioning act, and the engine only connects — but every DDL template opens with `create schema if not exists`, owned by the connecting role, so a config may point at a brand-new schema and the first ingest creates it.

Requires the `ltree` extension (source tables) and `vector` / pgvector (embedding tables). **The engine verifies extensions and never creates them** — installation is a provisioning act (once per database, and `vector` is not a trusted extension so it needs a superuser; the exception is a cluster whose `template1` has been seeded, from which databases created afterwards inherit both). Every pipeline preflights `require_extensions` and fails with the exact `CREATE EXTENSION` command if one is missing.

### Document corpus (ingpipe-file-ingestion)

Per TOML config: `{schema}`, `{document_table}` (default `document`), `{content_table}` (default `document_content`).

| Table | Key | Columns |
|-------|-----|---------|
| `{schema}.document` | `collection_path ltree` PRIMARY KEY | `title text not null check (trim(title) <> '')`, `n_parsed_sections integer not null check (>= 1)`, `source_binary_hash numeric(20,0) not null`, `ingested_at timestamptz not null default now()` |
| `{schema}.document_content` | `(collection_path, sort_order)` PRIMARY KEY; FK → document ON DELETE CASCADE | `sort_order integer not null check (>= 1)`, `heading_text text`, `content_text text` (a row must carry a non-blank heading or non-blank content), `word_count integer not null check (>= 0)`, `page_start integer`, `page_end integer` |

- **`collection_path` is the document identity**: a lowercase `ltree` (dot-separated `[a-z0-9_]+` labels, validated with `fullmatch`), authored per document in the config.
- **`source_binary_hash` is an unsigned 64-bit value** (low 64 bits of sha256 over the source bytes), stored as `numeric(20,0)` because its top half exceeds `bigint`'s signed max.

### Excel corpus (ingpipe-excel-ingestion)

Same identity model, one `collection_path` per sheet. Defaults `sheet` / `sheet_content`; the hash is over the sheet's ordered `row_text` values, so it is the change-detection signal for re-pulls. Sheets may additionally feed dynamically created structured tables (all-`text` columns named `col_*`, same PK/FK pattern); those are SQL query targets, not part of the embedding interface.

### Embedding tables (ingpipe-embedding-generation)

Default name `{source_table}_embedding` (overridable per source table), but consumers must not rely on the name: the discovery convention is **any table with a column of `udt_name = 'vector'` is a search target** — so never create incidental vector columns in a served schema. Shape:

- the source table's PK column(s), mirrored, composite FK back to the source ON DELETE CASCADE;
- `chunk_number integer not null` (PRIMARY KEY is source PKs + chunk_number);
- `chunk_text text not null` and `word_count integer not null`;
- `embedding vector(N)`, N auto-detected from the model at generation time;
- `chunk_tsv tsvector` generated always as `to_tsvector('english', chunk_text)` stored;
- HNSW index (`vector_cosine_ops`) on `embedding`, GIN on `chunk_tsv`.

Hybrid search uses the vector column (dense, cosine) and the tsvector column (sparse, FTS); a table without the tsvector degrades to dense-only.

### Embedding-model identity rule

The model that queries an embedding table must be the model that built it. Nothing structural enforces this — a mismatch with equal dimensions would not error; it would silently return garbage rankings. Consequences:

- The pipeline's `model_name` and any serving process's configured model must name the same model. Each instance records which model built its corpus.
- **Changing the embedder is a re-embed event**: regenerate every embedding table and update every consumer's model setting together, gated on a reproducibility check.
- The model-stack version pins (section 1) exist for this rule; they are liftable only alongside a re-embed.

### Table descriptions

`COMMENT ON` descriptions for schemas and tables ride in each package's DDL templates and the instance TOML configs — the creating package applies them at ingest and refreshes them on every run, so a fresh ingest produces described tables with no side file. Refreshed comments are NOT evidence that constraints are in sync: the `create table if not exists` DDL never alters an existing table, so schema changes reach an existing database only through a rebuild (or a manual `ALTER TABLE`).

## 4. Entry-point conventions

Every console script — the engine's and the instances' alike — shares one CLI shape, provided by `ingpipe_lib.cli`:

- `--config <toml>` and (for anything touching a database) a **required** `--env-file` — there is no default credential file, so a forgotten flag is a usage error, never a silent connection to an ambient default.
- Ingesters take `--overwrite` (CLI overrides TOML); the config value must be a real boolean — a quoted `"false"` is rejected, because it is truthy and `overwrite` gates destructive DELETEs.
- A config's relative paths resolve against its **instance root** (nearest ancestor directory with a `pyproject.toml`, found by walking up from the `--config` path — `ingpipe_lib.paths`); logs go to `<instance>/logs/<package>/<config-stem>.jsonl` at INFO. Runs with no instance log under the system temp directory. Nothing resolves against the working directory.
- **Logs append across runs and rotate.** A retried run keeps the failed run's records — the log an investigation actually needs — with each run's records separable by its `run_timestamp`; rotation (10 MB per file, 3 backups) bounds one script's logs near 40 MB before the oldest records drop. `--overwrite` does not touch logs; passing `overwrite=True` to `setup_logging` directly is the development-only truncation path. `logs/` stays gitignored and disposable — copy out anything worth keeping.
- **A config path is either absolute on this host or relative to the instance root.** A value rooted only under the *other* platform's rules is a config error, and `resolve_config_path` rejects it rather than reinterpreting it (see §2 Portability for why `Path.is_absolute()` cannot make that call). Relative is the normal case; a host-absolute path stays available as an escape hatch for a corpus parked on another volume.
- **Per-file config entries use forward slashes.** `ingpipe_file_ingestion`'s `document["file"]` values and `ingpipe_excel_ingestion`'s `files.*` keys are joined under `source_dir` and pass through neither `resolve_config_path` nor the acquisition guards. A backslashed name resolves on Windows only; on macOS it fails loudly at parse time (file not found) rather than diverging silently, which is why the rule is documented here rather than enforced in code.
- Failures accumulate per item (document/sheet/table); the run continues, reports every failure, and exits 1 if any occurred. Config-level errors abort immediately. `data-val-*` scripts exit 0 only when every check passes.
- Producers own config validation: each package's `validate_config` is called by both its ingester and its `data-val-*` scripts, so a config one accepts cannot be one the other rejects.

## 5. Acquisition (`packages/ingpipe_acquisition/`)

The first pipeline stage: turn a source's remote documents into files on disk, and record what the run produced. It is a package rather than an addition to `ingpipe-lib` because it needs `requests`/`urllib3`, which every other member would otherwise inherit despite making no network call; because it is a pipeline stage with its own entry points and config contract; and because a per-package dependency makes an instance's `pyproject.toml` state whether it acquires data at all.

### What the package owns

| Module | Owns |
|--------|------|
| `fetch.py` | The one download: streams to `<dest>.part` and `replace`s onto the final name only on success, so an interrupted run leaves NO file (rather than a truncated one the next run skips); cross-checks `Content-Length`; enforces an optional `max_bytes`; issues the request as a context manager so a mid-stream failure still releases the connection. `build_session` supplies retries (connection, read, and 429/5xx — never 404) and a default per-request timeout. Every failure is a `FetchError`; there is no status flag, and there is **no way to disable SSL verification from config**. |
| `manifest.py` | `.acquisition_manifest.json` at the output root: one entry per DISCOVERED target, recording the artifacts it RESOLVED to (for an archive, the extracted files, not the archive) with the byte size observed at production time. Paths are stored relative to the output root, so an instance directory can move. |
| `runner.py` | The run loop and the generic `acquire` entry point: enforces `min_targets`, rejects any destination escaping the output root, decides skip from the PRIOR manifest, isolates each target's failure, writes the manifest, and exits non-zero if anything failed. |
| `discover.py` | The two config-driven discoverers, for sources whose targets can be COMPUTED. |
| `extract.py` | The built-in archive post-processor, implemented THROUGH the instance-facing hook. |
| `data_validation/data_val_downloads.py` | The `data-val-downloads` validator: asserts the manifest exists and parses, that every recorded artifact is present at its recorded size, that no entry is recorded as failed, and (optionally, per extension) that the bytes are the kind of file they claim to be. |

### The manifest is the load-bearing piece

Nothing in the pipeline previously recorded what a run INTENDED to fetch, so no validator could detect incompleteness — a manual folder holding 1 of 40 chapters passed. Two invariants make the manifest trustworthy across resumes:

- **Every run writes the complete discovered set.** A target skipped because it is already present is recorded exactly as a freshly fetched one; a target that FAILED is recorded as failed, not dropped. A resume that fetches 37 of 40 still writes a 40-entry manifest, so validation sees the three holes instead of a shrunk-but-self-consistent set.
- **It is the single source of truth for both skip and validation.** Skip asks "do this target's recorded artifacts all exist at their recorded sizes?"; validation asks the same question over every entry. That is the question the old USC code got wrong by testing an extract directory it created *before* extracting — an extraction failure wedged that title permanently.

### The two instance-supplied hooks

Both are plain callables passed to `ingpipe_acquisition.runner.main`; neither is looked up by config string.

- **`discover: (config) -> Iterable[Target]`** — yields the targets to acquire. `Target` is a frozen `(url, destination, group, expected_size)`; `destination` must be relative to the output root. Discovery splits by provenance: a source whose URLs can be COMPUTED needs no code and uses a config-driven discoverer, while a source whose targets must be FOUND (fetch an index, follow each page, scrape it) supplies this callable. A config format expressive enough for the latter would be a scraping DSL harder to read than the Python.
- **`post_process: (target, path) -> Sequence[Path]`** — turns one arrival into the artifacts it resolved to, returning their paths, and RAISES (never returns an empty list) when the arrival is unusable. **Cross-target work is a pipeline stage, not post-processing:** the hook sees one arrival at a time by design, so "combine these forty PDFs" does not belong here. The package's own extraction (`make_extractor`) is implemented through this hook rather than beside it, so the contract has a real user from day one instead of being a dormant branch that rots.

### The config-driven discoverers

`discovery.kind = "explicit"` takes an authored list of `{ url, destination }`. `discovery.kind = "templated"` takes one URL template, one destination template, and a list of values for a single named `variable` — the shape that turned nine USC titles from a Python script into one config table. Both validate every destination as they yield it and both are generators. The full contract is annotated in `packages/ingpipe_acquisition/src/ingpipe_acquisition/config/example.toml`.

### Entry points

`acquire` runs a fully config-driven source; an instance whose targets are computable declares its OWN command name pointing straight at `ingpipe_acquisition.runner:main`, with no instance code (`policy_db`'s `download-usc-titles` does exactly this). An instance that must scrape writes a three-line wrapper calling `main(discover=...)`. `data-val-downloads` is shared by every source.

## 6. Document parsing background (ingpipe-file-ingestion)

Why the parsing stage is built the way it is.

**Parser: Docling, exclusively.** Layout-aware, emits a structured document object, strong on tables and reading order, and parses Word/PowerPoint/Excel through the same path — one parser means one structural contract across all input formats. (Field surveyed: Marker is a fast general converter, MinerU best on intricate/CJK layouts, pymupdf4llm fastest text-only, unstructured broadest formats — Docling is the fit for a structure-sensitive, table-bearing, self-hosted pipeline.)

**Output: lossless `DoclingDocument` JSON, exclusively.** Markdown is a lossy linearization — element types, table cell grids, and page provenance do not survive it. The JSON carries the reading-order tree, every element's label, real table grids, and page+bounding-box provenance per element; it is the on-disk source of truth and the clean step's sole input.

**Only two knobs are exposed**, each resolved per file with a document-set default: `do_ocr` (default off — born-digital files are faster and cleaner without it; a single scanned file in a set can turn it on) and `pdf_backend` (default `dlparse`, which keeps headings and their numbers intact where `pypdfium2` splits and mangles them; table detection is identical between backends). Table-structure mode (ACCURATE) and hardware acceleration (auto) deliberately stay at Docling's defaults.

**Known limits of layout-model parsing** (inherent, not configuration): enumerated list items are sometimes labeled `section_header`; heading *hierarchy* is not encoded (essentially everything is `level: 1` — nesting lives in the heading text); form-heavy and graphical pages yield fragmentary tables. Large PDFs are parsed in page-range batches and stitched with `DoclingDocument.concatenate`.

**Practical notes:** first run downloads Docling's models (pre-fetch for offline environments); parsing is a deliberate offline batch, not a request-time operation; its job is to recover structure faithfully, never to reshape content.

## 7. Documentation policy

Prose documentation lives in the root `README.md` and the `MAINTAINING.*.md` files — engine matters here, instance matters in that instance's file. No other standalone `.md` files. `docs/activities/` (implementation plans) and `docs/code_review/` (per-file reviews, mirroring the source tree) are dated work records: append new ones, never rewrite old ones to match the present. The engine's data interface (section 3) is the shared reference for external consumers; keep it free of this repo's file paths so restructures cannot stale it.

## 8. Machine notes

The sections above state *requirements*, free of any account name, privilege value, or date — those go stale on the next machine. This section is where the cluster- and machine-specific facts live: anything about a machine's local cluster, its provisioning account, or the engine's test database. Facts specific to one instance's corpus or data live in that instance's `MAINTAINING.instance.*.md` instead.

The two development machines run separate local clusters, so the facts below are per machine — a note recorded on one says nothing about the other.

**Windows (recorded 2026-08-26):**

- The provisioning account in `.env.provisioning` is **not** a superuser: it holds `CREATEDB` and `CREATEROLE`, with `rolsuper = f`. It therefore hits the PostgreSQL 16+ `SET ROLE` requirement in §2 and needed `grant <owner-role> to current_user with set true` before it could create a database owned by another role.
- `template1` was seeded with `ltree` and `vector` on 2026-08-25, so databases created *after* that date inherit both and need no superuser for the extension step. Verified by creating a scratch database and confirming it inherited them. Databases created before that date, and anything restored from `template0`, still need the privileged install.

**Mac (recorded 2026-08-27):**

- The provisioning account in `.env.provisioning` **is** a superuser (`rolsuper = t`, plus `CREATEDB` and `CREATEROLE`), so the `SET ROLE` self-grant was never needed here.
- `template1` was seeded with `ltree` and `vector` on 2026-08-27, matching the Windows cluster; verified by creating a scratch database and confirming it inherited both. Databases created before that date, and anything restored from `template0`, still need the privileged install.
- `ingestion_test` and `ingestion_test_runner` exist; the DB-backed tests run rather than skip.
