---
name: 20260823v01_generalize_acquisition_and_harden_policy_db
goal: Close the findings of the 2026-08-23 policy_db audit by fixing their shared cause rather than their instances. Extract the duplicated download mechanics into a new `acquisition` engine package — one correct fetch, a download-time manifest, a run loop that exits non-zero, and a manifest-driven validator — then migrate `cms_iom` and `usc_titles` onto it so each source supplies only what is genuinely source-specific. Add an optional `collection_path` prefix to excel ingestion so a schema-scoped filter stops silently dropping every workbook sheet, harden the grants SQL against the write path and the missing preconditions found in the live database, and correct the stale instance configs and credential permissions.
created: 2026-08-23 17:22:29
updated: 2026-08-23 18:43:39
---

## Implementation Plan

### Phase 1 — The acquisition package core

1. [completed] Create the package skeleton - `packages/acquisition/pyproject.toml`
   - 1.1. Distribution `acquisition`, import package `acquisition`, src layout, hatchling backend, `requires-python = ">=3.13"`
   - 1.2. Dependencies: `ingestion-lib` (workspace source), `requests`, `urllib3`
   - 1.3. Add `packages/acquisition` to the workspace members glob's reach (already covered by `packages/*`); `uv sync` and confirm the member installs editable
   - 1.4. Declare no `[project.scripts]` yet — the entry points arrive in tasks 5 and 6

2. [completed] Create the target and manifest types - `packages/acquisition/src/acquisition/manifest.py`
   - 2.1. A frozen `Target` dataclass: `url: str`, `destination: Path` (relative to the config's output root), `group: str | None`, `expected_size: int | None`
   - 2.2. `write_manifest(output_root, entries)` writing one JSON file per run to `<output_root>/.acquisition_manifest.json`: the run's UTC timestamp, and per entry the source URL, the destination path relative to `output_root`, the recorded byte size, and the `group`
   - 2.3. `read_manifest(output_root)` returning the parsed entries, raising a typed error when the file is absent or malformed — a missing manifest must be a hard validation failure, not an empty pass
   - 2.4. Manifest entries record the artifacts a run *produced*, which for a post-processed target are the post-processor's returned paths rather than the downloaded archive (rationale in decision 5)

3. [completed] Create and run unit tests for the manifest - `packages/acquisition/tests/test_manifest.py`
   - 3.1. A written manifest round-trips through `read_manifest` unchanged
   - 3.2. A missing manifest and a malformed manifest each raise the typed error
   - 3.3. Destination paths are stored relative to `output_root`, so a manifest stays valid if the instance directory moves
   - 3.4. Run with coverage; investigate any uncovered line

4. [completed] Create the fetch primitive - `packages/acquisition/src/acquisition/fetch.py`
   - 4.1. `fetch(url, dest, *, session, max_bytes=None) -> int` returning the bytes written: stream to `dest.with_suffix(dest.suffix + ".part")`, then `Path.replace` onto `dest` only after the stream completes, so an interrupted run leaves no file a later run would skip (this is the defect at both former `download_file` copies, which were character-identical apart from a `verify` argument)
   - 4.2. Delete the partial file on any exception before re-raising, so a failed target leaves no residue
   - 4.3. Verify SSL by default; the caller may disable it only by passing a session configured to, and no config key in this repo may set it (rationale in decision 7)
   - 4.4. Compare the response's `Content-Length` against the bytes written when the header is present, raising on mismatch; enforce `max_bytes` when given
   - 4.5. Issue the request as a context manager (`with session.get(..., stream=True) as response:`) so the connection is released even when the stream raises mid-transfer — neither current downloader closes its response on the error path
   - 4.6. `build_session(*, retries, backoff_factor, timeout)` returning a `requests.Session` with a `urllib3.Retry`-backed adapter covering connection errors and transient 5xx, and a default per-request timeout. The runner's per-target handler must catch `requests.RequestException` (not `HTTPError` alone), so a `ReadTimeout` — which inherits from `Timeout`, not `ConnectionError` — fails only its target rather than aborting the run, the exact gap the old USC loop had
   - 4.7. Raise a typed error on any failure, never return a status flag — the runner's failure accounting depends on exceptions

5. [completed] Create and run unit tests for the fetch primitive - `packages/acquisition/tests/test_fetch.py`
   - 5.1. A successful fetch writes the file, returns the byte count, and leaves no `.part` file
   - 5.2. A stream that raises mid-transfer leaves neither the destination nor the `.part` file, and re-raises — the regression guard for the truncated-file defect
   - 5.3. A `Content-Length` mismatch raises; a response exceeding `max_bytes` raises
   - 5.4. The session retries a transient 5xx and a connection error, and gives up after the configured count
   - 5.5. SSL verification is on for a default session
   - 5.6. Run with coverage; investigate any uncovered line

6. [completed] Create the run loop - `packages/acquisition/src/acquisition/runner.py`
   - 6.1. `run_acquisition(config, config_path, *, discover, post_process=None)`: resolve the output root against the config's instance root via `ingestion_lib.paths`, iterate the targets `discover` yields, and for each one fetch, then (when a post-processor is given) post-process, recording the produced artifacts
   - 6.2. Decide skip from the PRIOR run's manifest, not from guessing a post-processed target's outputs: when `overwrite` is false and the previous manifest lists this target's URL with all its recorded artifacts still present and correctly sized, skip it. A plain (un-post-processed) target's artifact is its own `destination`, so it skips on that path existing; a post-processed target skips only when its recorded outputs exist — which is what the current USC code gets wrong by testing the extract directory that is created before extraction. Sleep `request_delay_seconds` only before a target that is actually fetched
   - 6.3. Enforce `min_targets`: fail the run when `discover` yields fewer targets than the config declares, so a discovery that silently returns nothing cannot report success (rationale in decision 4)
   - 6.4. Accumulate failures per target and continue; a raise from `fetch` or from `post_process` fails only that target, with its artifacts deleted
   - 6.5. Honor `dry_run` by logging every resolved target and its destination without fetching and writing no manifest — but still evaluate `min_targets` and the destination-escape check (6.3, 6.7) against the discovered targets, so a dry run surfaces a broken discovery instead of quietly logging zero targets and exiting 0
   - 6.6. Log the run summary, write the manifest, and **exit non-zero when any target failed** — neither current downloader does this (finding in decision 3). The manifest records the **complete discovered set**, one entry per target whether it was fetched this run or skipped-because-already-present, so the manifest is always the whole corpus's intended state and never just the resume delta (rationale in decision 5). A target that *failed* this run is recorded as failed, not silently dropped, so validation sees the hole rather than a shrunk-but-consistent set
   - 6.7. Resolve every destination under the output root and reject a target escaping it, since a scraped page can influence the destination

7. [completed] Create the built-in extraction post-processor - `packages/acquisition/src/acquisition/extract.py`
   - 7.1. `make_extractor(keep=None, flatten=False, delete_archive=True)` returning a post-processor with the task 6 hook signature — so the package's own extraction runs through the same interface an instance would use (rationale in decision 6)
   - 7.2. Extract only members matching `keep` (glob patterns) rather than extracting everything and pruning, and flatten into the destination directory when `flatten` is set
   - 7.3. Return the extracted artifact paths; raise when the archive yields no matching member, so an empty or wrong archive fails its target instead of appearing to succeed
   - 7.4. Delete the archive after a successful extraction when `delete_archive` is set, since an archive is not corpus content

8. [completed] Create the config-driven discoverers - `packages/acquisition/src/acquisition/discover.py`
   - 8.1. `explicit_targets(config)`: targets from a config-authored list of `{ url, destination }` entries
   - 8.2. `templated_targets(config)`: targets from a URL template plus a substitution list, e.g. a `{title}` placeholder over a list of title numbers, with the destination template resolved the same way
   - 8.3. Both validate that every resolved destination is relative and escapes nothing, and both are generators so enumeration and fetching interleave
   - 8.4. Document the instance-supplied alternative: a callable with the same signature, for sources whose targets can only be found by scraping (rationale in decision 8)

9. [completed] Create and run unit tests for the runner, extractor, and discoverers - `packages/acquisition/tests/test_runner.py`
   - 9.1. A run with two succeeding targets writes both, records both in the manifest, and exits 0
   - 9.2. A run where one target's fetch raises records the other, reports the failure, and exits non-zero — the regression guard for the exit-0 defect
   - 9.3. A `post_process` that raises fails only its target, deletes its artifacts, and leaves the run's other targets intact; the next run retries it rather than skipping — the regression guard for the extraction wedge
   - 9.4. A run that fetches some targets, skips others already present from a prior manifest, and fails one writes a manifest listing ALL discovered targets — fetched, skipped, and failed — not just the ones fetched this run; the regression guard for the resume hole
   - 9.5. A discovery yielding fewer than `min_targets` fails the run before any fetch
   - 9.6. `dry_run` writes no file and no manifest, and logs every target
   - 9.7. A target whose destination escapes the output root is rejected
   - 9.8. The extractor keeps only matching members, flattens when asked, deletes the archive, and raises on an archive with no match
   - 9.9. Both config-driven discoverers yield the expected targets, and an absolute or escaping destination raises
   - 9.10. An instance-style callable post-processor is exercised through the hook, pinning the documented contract
   - 9.11. Run with coverage; investigate any uncovered line

10. [completed] Create the manifest-driven validator - `packages/acquisition/src/acquisition/data_validation/data_val_downloads.py`
    - 10.1. A console-script entry point taking `--config`, reading the manifest at the config's output root and asserting every recorded artifact exists, is non-empty, and matches its recorded byte size — the check no current validator can make, because nothing records what a run intended to produce (rationale in decision 3). An entry the manifest marks as failed is a validation failure, not a skipped check
    - 10.2. Assert the manifest itself exists and parses; a missing manifest fails rather than passing vacuously
    - 10.3. Apply an optional per-extension content check from config (a `%PDF` header for `.pdf`, a readable central directory for `.zip`), reported per file
    - 10.4. Accumulate failures, log each one, and exit non-zero when any check failed
    - 10.5. Declare the `data-val-downloads` console script in the package's `pyproject.toml`

11. [completed] Create and run unit tests for the validator - `packages/acquisition/tests/test_data_val_downloads.py`
    - 11.1. A manifest whose artifacts all exist at their recorded sizes passes and exits 0
    - 11.2. A missing artifact, a zero-byte artifact, and a size mismatch each fail and exit non-zero
   - 11.3. A manifest containing a failed entry fails validation even when every present artifact is correct — the resume-hole guard
    - 11.4. A missing or malformed manifest fails rather than passing
    - 11.5. The content checks accept a valid PDF and zip and reject a file with the wrong magic bytes
    - 11.6. Run with coverage; investigate any uncovered line

### Phase 2 — Migrate the two sources

12. [completed] Migrate usc_titles to config-only acquisition - `instances/policy_db/config/acquisition/usc_titles/`
    - 12.1. Rewrite the three configs to use the templated discoverer: a URL template over the title list, `keep = ["*.pdf"]` extraction, `min_targets`, and the retry/delay settings — the source's targets are computed, not discovered, so it needs no instance code (rationale in decision 8)
    - 12.2. Delete `instances/policy_db/src/policy_db_acquisition/usc_titles/download_usc_titles.py`, its `data_validation/data_val_downloaded_zips.py`, and the `download-usc-titles` and `data-val-downloaded-zips` console scripts from the instance `pyproject.toml`
    - 12.3. Delete `instances/policy_db/tests/test_download_usc_titles.py`; its `build_download_url` cases are superseded by the templated-discoverer tests, and the file also calls `setup_logging` at import time, writing real log files during collection
    - 12.4. Remove `verify_ssl = false` from all three configs — the host serves a valid certificate and the key no longer exists (decision 7)
    - 12.5. Confirm the deleted validator's defect is gone by construction: it asserted the presence of zip archives that the downloader deletes after extraction, so it failed after every successful run

13. [completed] Migrate cms_iom to a discovery function - `instances/policy_db/src/policy_db_acquisition/cms_iom/discover.py`
    - 13.1. Move `get_manual_pages`, `_extract_table_titles`, `get_chapter_pdf_links`, `title_to_folder_name`, and `_matches_manual_filter` into a `discover(config)` generator yielding `Target(url=pdf_url, destination=<folder>/<filename>, group=<folder>)` — this is the genuinely source-specific logic and moves largely intact
    - 13.2. Key each destination on the URL's path-derived name rather than its bare basename, so two chapter URLs sharing a basename no longer collide and silently drop a chapter
    - 13.3. Delete `download_cms_iom.py` and `data_validation/data_val_downloaded_pdfs.py`; replace the `download-cms-iom` console script with one calling `run_acquisition` with this discoverer, and drop `data-val-downloaded-pdfs` in favor of the generic validator
    - 13.4. The per-manual folder skip is not carried over: the runner skips per artifact, so an interrupted run now completes on a re-run instead of skipping every manual whose folder is non-empty
    - 13.5. Add a `min_targets` floor to the cms_iom configs, so a markup change that breaks the link pattern fails the run instead of reporting "0 downloaded" and exiting 0

14. [completed] Update the cms_iom discovery tests - `instances/policy_db/tests/test_discover_cms_iom.py`
    - 14.1. Carry over the existing scrape tests from `test_download_cms_iom.py`, retargeted at `discover`
    - 14.2. Invert `test_fallback_when_no_table_title`: it currently asserts the degraded folder name `pub_100_04_100_04` is *correct*, which pins a silent-degradation path that orphans the downstream ingest configs. A manual page with no title table must now fail its target loudly
    - 14.3. Add a test that discovery yields nothing when the link pattern matches nothing, and that the runner's `min_targets` check then fails the run
    - 14.4. Add a test that two chapter URLs sharing a basename yield distinct destinations
    - 14.5. Delete `instances/policy_db/tests/test_download_cms_iom.py` once its cases are carried over

15. [completed] Declare the instance's acquisition dependency - `instances/policy_db/pyproject.toml`
    - 15.1. Add `acquisition` with a workspace source, since the instance now consumes the package
    - 15.2. Remove `requests`, `urllib3`, and `beautifulsoup4` only if no instance code still imports them — the cms_iom discoverer still needs `requests` and `beautifulsoup4`, so verify by import rather than by assumption
    - 15.3. Update the console-script table so the four old entry points become `download-cms-iom`, `download-usc-titles`, and the shared `data-val-downloads`

### Phase 3 — Excel collection_path prefix

16. [completed] Add an optional derived-path prefix - `packages/excel-ingestion/src/excel_ingestion/_utils.py`
    - 16.1. `make_collection_path(filename, sheet, override=None, prefix=None)`: when `prefix` is given and `override` is not, prepend it to the derived `<stem>.<leaf>` path and validate the result; an authored `override` is returned unchanged, never prefixed, since it already states the full path
    - 16.2. Document the precedence in the docstring — override wins, prefix applies only to derivation
    - 16.3. Add tests: prefix applied to a derived path, prefix ignored when an override is present, an invalid prefix raising, and the existing derive/override/degenerate cases still passing

17. [completed] Read the prefix at both call sites - `packages/excel-ingestion/src/excel_ingestion/ingest_excel.py`, `packages/excel-ingestion/src/excel_ingestion/data_validation/data_val_excel_outputs.py`
    - 17.1. Pass `prefix=config.get("collection_path_prefix")` at `ingest_excel.py`'s `make_collection_path` call
    - 17.2. Pass the same at `data_val_excel_outputs.py`'s call — the validator independently re-derives every path, so missing this would make it report all 326 sheets absent (rationale in decision 9)
    - 17.3. Validate the key in `ingest_excel.validate_config`: when present it must be a string and a valid ltree on its own, so a bad prefix fails at config load rather than once per sheet
    - 17.4. Document the key in `packages/excel-ingestion/src/excel_ingestion/config/example.toml`

18. [completed] Set the prefix in the qpp_cm excel configs - `instances/policy_db/config/excel_ingestion/qpp_cm/`
    - 18.1. Add `collection_path_prefix = "qpp_cm.<year>_cost_measure_codes_lists"` to each of the three configs, leaving all 326 sheet entries untouched so their leaves stay machine-derived
    - 18.2. Confirm the resulting paths are valid ltree and unique, and that a `qpp_cm.%` filter would now match both legs (verification is by unit test and by inspection of the derived values — no ingest runs here, per decision 2)

### Phase 4 — Grants hardening

19. [completed] Close the large-object write path and bound resource use - `instances/policy_db/grants/mcp_ro_policy.sql`
    - 19.1. Revoke `EXECUTE` from `PUBLIC` on the large-object write functions, inside the existing transaction: `lo_from_bytea`, `lo_create`, `lo_put`, `lowrite`. Verified 2026-08-23 against the live database that `mcp_ro_policy` can execute all four, so the role documented as SELECT-only can write bytes into `pg_largeobject` and grow the database (`lo_import` is already denied)
    - 19.2. Add role-scoped GUCs via `ALTER ROLE mcp_ro_policy IN DATABASE policy_db SET ...` for `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, and `temp_file_limit`
    - 19.3. Comment honestly that these GUCs are `USERSET` and therefore bound accidents, not adversarial queries — a client emitting arbitrary SQL can reset them, so the binding limit must live on the serving process's own connections (rationale in decision 10)
    - 19.4. Grant `USAGE ON SCHEMA public` to the role explicitly with a comment naming the reason — the `vector` type and its operators resolve there, so the search queries depend on a privilege the script currently leaves implicit

20. [completed] Assert the preconditions all three scripts assume - `instances/policy_db/grants/`
    - 20.1. Add to each of `mcp_ro_policy.sql`, `database_description.sql`, and `create_extensions.sql` a `DO` block raising unless `current_database()` is `policy_db` — running the grants script against the wrong database would revoke `CONNECT` from `PUBLIC` there
    - 20.2. Add to `mcp_ro_policy.sql` a `DO` block raising unless the role has none of `rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolbypassrls` and no rows in `pg_auth_members` — the file's claim that everything the identity can reach is listed in it is otherwise unverifiable, and a single `pg_read_all_data` membership would silently defeat the per-schema model. Verified clean on 2026-08-23; the assertion keeps it so
    - 20.3. Extend the preconditions comment: every relation in the served schemas must be owned by `policy_db_maintainer` (a foreign-owned relation makes `GRANT ... ON ALL TABLES` fail and, inside the transaction, aborts the whole script), and the script must be re-run after any schema drop and before the embedding step, because dropping a schema deletes its `pg_default_acl` row
    - 20.4. Keep every addition idempotent and inside the existing transaction; re-running the script must remain a no-op

21. [completed] Verify the hardening against a scratch database
    - 21.1. Create a throwaway database and role pair, apply all three scripts, and confirm they succeed and are a no-op on a second run
    - 21.2. Confirm the read-only role can no longer execute the four large-object writers, and that the GUCs are set for the role in that database
    - 21.3. Confirm each script raises when connected to a database whose name is not `policy_db`
    - 21.4. Confirm the role-attribute assertion raises for a role granted `pg_read_all_data`
    - 21.5. Drop the scratch database and role; make no change to `policy_db` itself (decision 2)

### Phase 5 — Instance corrections

22. [completed] Repoint the cms_iom configs at the current snapshot - `instances/policy_db/config/file_ingestion/cms_iom/`
    - 22.1. Replace the hardcoded `2026-03-04` with `2026-06-11` in `source_dir`, `parsed_dir`, and `cleaned_dir` across all 21 configs — the folder names already match the snapshot on disk, so this is a date string, not lost data (verified 2026-08-23)
    - 22.2. Fix `ingest_policy_pub_100_20.toml`'s `file` from `pub100_20pdf.pdf` to `pub100_20pdf.docx`, which is what is on disk and genuinely a docx
    - 22.3. Fix `ingest_policy_pub_100_15.toml`'s `file` from `mpi115appendices.pdf` to `mpi115-appendices.pdf`; its `collection_path` already reflects the hyphenated name
    - 22.4. Record the date coupling as a known refresh cost rather than solving it here (decision 11)

23. [completed] Correct the stale config and comment text across the instance and engine
    - 23.1. Replace "Convert source files to JSON + Markdown via Docling" with a JSON-only description in all 36 `instances/policy_db/config/file_ingestion/**/*.toml` — the parse step has no markdown output
    - 23.2. Update `packages/file-ingestion/src/file_ingestion/config/example.toml` to document the top-level `db_name`/`db_schema` form as required and the nested `[load]` form as deprecated, matching what `resolve_db_target` now does — a maintainer following the example currently authors a deprecated config
    - 23.3. Align `instances/policy_db/config/acquisition/cms_iom/download_cms_iom_dry_run.toml`'s exclusion patterns with the real run's, so the dry run previews what the real run would do
    - 23.4. Add the missing table comments so every config feeding a table describes it: the 21 cms_iom configs set only `schema_comment`, and the three qpp_cm excel configs set no `sheet_table_comment` or `content_table_comment` while their `schema_comment` advertises those tables

24. [completed] Restrict the credential file permissions and document the requirement
    - 24.1. `chmod 600` on `instances/policy_db/.env`, `instances/policy_db/.env.superuser`, and the workspace-root `.env.test` — all three are currently world-readable under the default `umask 022`, including the superuser credentials, on a machine with a separate local `postgres` account
    - 24.2. State the requirement in `instances/policy_db/.env.example` and in `MAINTAINING.instance.policy_db.md`'s credentials section, since `umask` will keep recreating the permissive mode on newly created files

### Phase 6 — Documentation and verification

25. [completed] Document the acquisition package and the changed instance surface
    - 25.1. Add an acquisition section to `MAINTAINING.packages.md`: what the package owns (fetch, manifest, runner, validator), the two instance-supplied hooks and their contracts, the config-driven discoverers, and the rule that cross-target work is a pipeline stage rather than post-processing (decision 12)
    - 25.2. Add the package to the `README.md` engine table and the console-script list
    - 25.3. Update `MAINTAINING.instance.policy_db.md`: the new acquisition commands, the single `data-val-downloads` replacing two validators, the credential permission requirement, and the manifest as the thing validation now compares against
    - 25.4. Update `MAINTAINING.instance.policy_db.md`'s rebuild section with what this activity leaves for the manual rebuild to pick up: the excel `collection_path` prefix changing all 326 sheet identities, the grants re-application order, and the expectation that document and sheet contents are otherwise unchanged (decision 3)

26. [completed] Run the full suite and the consistency checks
    - 26.1. `uv run pytest` — all tests pass, from the workspace root and from a non-root working directory
    - 26.2. `ruff check packages instances` and `mypy packages instances` run clean, including the new package
    - 26.3. `uv build packages/acquisition` and confirm the wheel carries the package's modules and no sibling's
    - 26.4. Install `acquisition` alone in a scratch environment and run its suite there, confirming its declared dependencies are complete (decision 13)
    - 26.5. Grep checks, each with zero matches expected: any `download_file` definition outside `packages/acquisition`; any `verify_ssl` key in the instance configs; any remaining `2026-03-04` in the cms_iom configs; any `create extension` under `packages/` outside the provisioning SQL and the preflight's message
    - 26.6. Dry-run both acquisition entry points against the real configs, confirming each resolves its targets, logs every destination, satisfies `min_targets`, writes nothing, and exits 0 (decision 2)
    - 26.7. Confirm the four console scripts' `--help` works from a directory outside the repo
    - 26.8. If issues found, debug and iterate

## Key Data Decisions and Considerations

1. The audit's four download defects — non-atomic writes, no retries, an over-narrow exception class, and exit 0 on failure — were present in *both* downloaders, because their `download_file` implementations were character-for-character identical apart from one `verify` argument. That is what makes extraction the fix rather than a follow-up: fixing in place would mean writing each correction twice, then deleting one copy during a later merge. Extracting first means each is written once, tested once, and inherited by every future source, including whatever `archive_db` needs.
2. No data operations are in scope. Every task ends at code, config, SQL, or tests; verification is the suite, dry runs, and a scratch database. Nothing here downloads a real file, ingests, embeds, or drops a schema, and task 21 explicitly leaves `policy_db` untouched. The corpus rebuild is manual and follows this activity, which is why the things that only take effect on rebuild are documented (task 25.4) rather than executed.
3. Validation gains the ability to detect incompleteness, which it does not have today. `data_val_downloaded_pdfs.py` passes a manual folder containing at least one PDF, so a manual with 1 of 40 chapters passes; its header check reads four bytes and its size check only rejects zero, both of which a truncated file satisfies. Nothing records what a run intended to fetch. The manifest is therefore the load-bearing piece of this activity: it turns "some files arrived" into "the files this run resolved arrived at the sizes it recorded."
4. `min_targets` exists because discovery is where silent failure lives. If CMS changes its markup so the link pattern matches nothing, today's run logs `Found 0 manual page links` at INFO, downloads nothing, and exits 0. A declared floor converts that into a loud failure, and it is a config value rather than a hardcoded heuristic because only the instance knows how many manuals to expect.
5. The manifest records the artifacts a target *resolves to*, not the bytes fetched — for USC the extracted PDFs, not the zip that no longer exists — so validation checks what the pipeline consumes. Two invariants make it trustworthy across resumes. First, every run writes the **complete discovered set**: a target skipped because it already exists is recorded exactly as a freshly fetched one would be, so a resume run that fetches 37 of 40 still writes a 40-entry manifest, never a 37-entry delta that would make validation blind to the 3. Second, it is the single source of truth for *both* skip and validation — skip asks "does the manifest's recorded output for this target exist at its size," which is the question the current USC code gets wrong by testing an extract directory created before extraction, and validation asks the same question over every entry. A target that failed this run is recorded as failed, so a partial run's manifest exposes the gap rather than papering over it.
6. The package's own extraction is implemented *through* the post-process hook rather than beside it, so the hook has a real user from day one instead of being a dormant branch that rots. This was the argument for keeping the hook at all: without it, a source whose arrival handling does not fit `keep`/`flatten` has no option but to abandon the package, which would undermine having one. The hook operates on one arrival at a time by design — cross-target work ("combine these forty PDFs") is a pipeline stage, not post-processing.
7. `verify_ssl` is removed rather than defaulted to true. All three USC configs currently set it false and the script suppresses the resulting warning, against a host that serves a valid certificate — so the option exists only to disable a protection nobody needed disabled. Removing the key means no config in this repo can turn verification off; a genuinely broken host would be handled by passing a configured session, which is a code change and therefore a visible decision.
8. Discovery splits by whether a source's targets can be *computed* or must be *found*. USC computes its URLs from a release point and a title list, so it becomes config and loses its script entirely. CMS must fetch an index, follow each manual page, parse a table for the folder title, and scrape an article element for PDF links — a traversal algorithm, not a pattern, and a config format expressive enough to describe it would be a scraping DSL harder to read than the Python. The discovery interface is honest about its provenance: it fits these two sources, and the first extensions it will likely need (auth headers, streaming pagination, conditional fetch for incremental sync) are deliberately not designed against imagined requirements.
9. Task 17.2 is not optional bookkeeping. `data_val_excel_outputs.py` independently re-derives every `collection_path` to check what the ingester stored, so adding the prefix to the ingester alone would make the validator compute prefix-less paths, find none of them, and report all 326 sheets missing — a false failure that looks exactly like a real one.
10. The grants GUCs are documented as accident-bounds rather than security controls because every relevant setting is `USERSET`: a client that emits arbitrary SQL can `SET statement_timeout = 0` and proceed. They are still worth adding — they bound the common case of an expensive query — but the honest statement is that the binding limit must live on the serving process's connections in `mcp_deployment`, outside this repo. The large-object revokes in task 19.1 are different in kind: they close a privilege, and a client cannot grant it back.
11. The cms_iom date coupling is fixed but not solved. Every re-download writes a new `date.today()` folder while 22 files hardcode the date, so a refresh means 22 files × 3 path lines. The options — a config-level date variable the engine does not support, dropping the dated folder and losing snapshot history, or a `current` symlink — are all design changes with different tradeoffs, so this activity corrects the stale value and leaves the coupling as a recorded decision for its own activity. The usc_titles lane has no equivalent coupling, which is worth knowing when comparing the two.
12. `acquisition` is a package rather than an addition to `ingestion-lib` for three reasons: it needs `requests` and `urllib3`, which every other member would otherwise inherit despite making no network calls; it is a pipeline stage with its own entry points and config contract, architecturally a sibling of `file-ingestion` rather than a utility; and a per-package dependency makes an instance's `pyproject.toml` state whether it acquires data at all. It is also the first engine package created *for* an instance need rather than carved from existing generic code — justified because fetch mechanics are as source-agnostic as parsing mechanics, but worth naming as a different provenance.
13. Task 26.4's isolated install is the only honest check that the new package's dependencies are declared. The shared workspace venv makes `requests` importable from anywhere regardless of who declared it, so a missing declaration would pass every local test and fail only for an external consumer. This is the same instrument the workspace conversion used to prove package independence.
14. Tasks 12, 13, 20, 22, 23, and 24 each touch several files, grouped because each is one change repeated across named files — the same config rewrite per source, the same assertion added to three SQL scripts, the same date string replaced across 21 configs, the same permission applied to three credential files. Every affected file is named in a subtask.
15. This activity creates no new data tables and writes no corpus data, so no per-output validation task is written. What is under test is behavioral: the suite from any working directory, static analysis and the wheel contents, the isolated install, the scratch-database privilege assertions, and the dry runs in task 26.6. The one new *artifact* — the manifest — is validated by the tests in tasks 3 and 11 and by the validator it exists to serve.
16. Two audit observations are deliberately out of scope, recorded so they are not mistaken for oversights. First, all three qpp_cm years feed the same structured tables under the default `min_column_overlap = 0.5`, so a cross-year CMS column rename would be refused per sheet at append time; this is a latent risk that cannot be assessed without the 2024/2025 workbooks present, and it is an ingestion-behavior question rather than an acquisition or grants one. Second, the usc release point (`119/73not60`) is baked into all nine usc file_ingestion configs and their `collection_path` leaves as well as the acquisition configs — the templated discoverer (task 12) fixes only the acquisition side, so a new release point still means editing the nine ingestion configs. Both belong to a future configs-consolidation activity, not this one.
17. Audit provenance: findings come from the 2026-08-23 policy_db audit against commit `9e9ef66`, with the highest-stakes claims verified directly rather than taken from the report — the four large-object privileges, the role attributes and memberships, the default-ACL rows, the relation ownership, the stale snapshot date against the folders on disk, the two wrong filenames, the identical `download_file` bodies, the degraded-name test assertion, and the credential file modes. The `briefs_db` configs found by the same audit were deleted before this activity was drafted, which also resolved the `data` schema question: the three served schemas are now the complete set. Line numbers will drift as Phase 1 lands; re-verify by grep before editing.
18. **Environment blocker found during task 1 (pre-existing, not caused by this activity).** Every `.pth` file in `.venv/lib/python3.14/site-packages/` carries the macOS `UF_HIDDEN` file flag, and CPython 3.14's `site.addpackage` newly *skips* hidden `.pth` files. The result is that no workspace member is importable and `uv run pytest` fails collection on all 13 test modules — verified before any code in this activity was written. `uv run` re-applies the flag on every invocation, so the working procedure adopted here is `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` followed by `.venv/bin/python -m pytest` (764 tests pass that way on the pre-activity tree). Task 26.1's literal `uv run pytest` therefore cannot pass on this machine until the uv/CPython-3.14 interaction is resolved; it is recorded here rather than worked around in the repo, because the defect is in the environment, not the code.
19. **Console-script surface (settled before task 2, because tasks 6, 8, 10, 12, 13 and 15 all bind to it).** Tasks 12.2, 15.3 and 26.7 only reconcile if the *generic* run loop is itself a console-script `main`. The package therefore declares two scripts — `acquire` (`acquisition.runner:main`) and `data-val-downloads` (`acquisition.data_validation.data_val_downloads:main`) — and an instance whose targets are computable declares its own command name pointing straight into the dependency, with no instance code at all. The four scripts task 26.7 checks are: `acquire` and `data-val-downloads` (from the package) and `download-usc-titles` (= `acquisition.runner:main`) and `download-cms-iom` (= the one cms_iom wrapper, which exists only because that source must be scraped).
20. **Phase 4 findings from the scratch-database verification (task 21), each of which changed the delivered work.**
    - **`mcp_ro_policy.sql` is now a SUPERUSER script.** Applied as `policy_db_maintainer` against the scratch pair it fails at `ERROR: permission denied for function lo_from_bytea`: revoking on a `pg_catalog` function requires owning it, and `ALTER ROLE ... IN DATABASE ... SET` for a role you did not create requires superuser. The file's invocation block and preconditions were rewritten to say so, and `MAINTAINING.instance.policy_db.md` follows.
    - **Task 20.1's hardcoded database name contradicted task 21.1.** A guard that raises unless `current_database()` is `policy_db` cannot coexist with applying the same script to a throwaway database. All three scripts therefore take an `expect_database` psql variable defaulting to `policy_db` (the same idiom the file already used for schema names), and `mcp_ro_policy.sql` takes a `role` variable for the same reason. A bare invocation still aborts against the wrong database — verified, exit 3 on all three.
    - **psql does not interpolate `:'var'` inside a dollar-quoted body.** Verified directly (`ERROR: syntax error at or near ":"`), so every `DO` block reads its value from a session GUC set immediately above it.
    - **A residual large-object write path remains open, recorded rather than silently closed.** After the four revokes the role can still execute `lo_creat(integer)`, `lo_open(oid, integer)`, `lo_truncate(integer, integer)`, `lo_truncate64(integer, bigint)` and `lo_unlink(oid)` — and `lo_truncate64` EXTENDS a large object with zero bytes, so the database can still be grown. The audit verified four functions and this activity closes those four; widening to a family the audit did not examine belongs to its own change. The mechanism is written into `mcp_ro_policy.sql` beside the revokes so it cannot be lost.
    - **`policy_db` itself was not touched** (decision 2). Re-verified after the scratch work: `has_function_privilege('mcp_ro_policy', 'lo_from_bytea(oid,bytea)', 'EXECUTE')` is still true there. The hardening lands when the grants are re-applied during the manual rebuild.
21. **The qpp_cm sheet count is 925, not 326.** Task 18 says "326 sheet entries"; the actual per-config counts are 273 (2024), 326 (2025) and 326 (2026). All 925 derived paths were checked after the prefix landed: every one is a valid ltree, all 925 are unique, and all 925 now match a `qpp_cm.%` filter. No config authors a per-sheet `collection_path` override, so the prefix applies uniformly and every leaf stays machine-derived, as 18.1 requires.

22. **Task 26 results, including two things the checks turned up.**
    - Suite: 936 pass, from the workspace root and from a directory outside the repo (`.venv/bin/python -m pytest <repo>`), after the `chflags` step of decision 18. `ruff check packages instances` and `mypy packages instances` are clean, the new package included. The acquisition package's own suite is 172 tests at 99% line coverage; the only uncovered lines are the two `if __name__ == "__main__":` guards, which are uncovered throughout this repo.
    - **`ingestion-lib` collides with an unrelated distribution on PyPI.** The first attempt at task 26.4 installed the `acquisition` wheel plus `pytest` into a scratch environment and got a completely different `ingestion_lib` (one with `provider.py`, `extractors/`, `model/`) resolved from PyPI, so every test failed on `No module named 'ingestion_lib.cli'`. This is not caused by this activity — every workspace member declares `ingestion-lib` — but it means any external consumer of ANY of these wheels resolves the wrong dependency. Recorded here as a finding for its own change; the options are renaming the distribution or never publishing. The isolated check was then run correctly, installing the local `ingestion_lib` wheel alongside: **172 tests pass with only `acquisition`'s own declarations**, so `requests`/`urllib3` are genuinely declared where they are used.
    - `uv build packages/acquisition` produces a wheel carrying exactly the seven `acquisition/*` modules plus `config/example.toml`, and no sibling's code.
    - Dry runs against the REAL configs (live network): `download_usc_titles_dry_run` resolves 1 target and `download_usc_titles` resolves 9, both exit 0, both write nothing. `download_cms_iom_dry_run` scrapes cms.gov, filters to the introduction manual, resolves 1 target and exits 0; the full `download_cms_iom` config scrapes all 25 manual pages and resolves **201 targets** against its `min_targets = 100` floor. 201 is also exactly the number of documents the 21 cms_iom file_ingestion configs name, which is an independent confirmation that the discoverer's destinations line up with what the ingest lane expects.
    - All four console scripts (`acquire`, `data-val-downloads`, `download-usc-titles`, `download-cms-iom`) answer `--help` with exit 0 from a directory outside the repo and create no log files there.
    - The 26.5 greps are all zero as written. `verify_ssl` survives only in two comments explaining its absence and in dated `docs/activities/` work records, which are append-only by the documentation policy and must not be rewritten.
23. **One gap left open, one level up from the one this activity closed.** The live dry run of the full cms_iom config discovered 25 manuals, two of which (`pub_100_23_payment_error_rate_measurement_under_development`, `pub_100_25_information_security_acceptable_risk_safeguards`) have no `file_ingestion` config. Acquisition would download them and report success, and nothing would ingest them — the same class of silent gap as the degraded-folder-name path that task 14.2 closed, but between the acquisition and ingestion configs rather than inside the discoverer. It is out of scope here (this activity adds no cross-config check and writes no data), and today it is harmless because the 201 discovered targets match the 201 documents the 21 ingest configs name exactly. It stops being harmless the first time CMS publishes a new manual. Belongs with the configs-consolidation work in decision 16.

