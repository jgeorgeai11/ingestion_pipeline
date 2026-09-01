# Maintaining the policy_db instance

Operating `instances/policy_db/` — the CMS policy corpus. Engine matters (package layout, testing, the table shapes the engine writes, CLI conventions) live in [MAINTAINING.packages.md](MAINTAINING.packages.md).

## 1. What this instance is

One PostgreSQL database, `policy_db`, holding three corpus schemas plus their embeddings, all built by the engine from sources this instance's configs and acquisition scripts define:

| Schema | Content | Sources |
|--------|---------|---------|
| `cms_iom` | CMS Internet-Only Manuals (Pub. 100-xx), parsed PDF sections | downloaded by `download-cms-iom` |
| `qpp_cm` | QPP cost-measure reference data: MIF PDF sections, code-list workbooks (`sheet`/`sheet_content` + per-measure structured tables) | workbooks fetched manually |
| `usc` | Selected US Code titles, parsed PDF sections | downloaded by `download-usc-titles` |

Everything the engine needs to know about this corpus lives in this directory: `config/` (per-package TOML configs, including `config/ingpipe_acquisition/`), `src/policy_db_acquisition/` (the cms_iom discoverer — the only source-specific acquisition code left), `sql/`, and the gitignored `data/`, `logs/`, and env files. The `pyproject.toml` declares the engine packages this instance uses — all five — and is the instance-root marker that config paths and logs anchor to.

## 2. Credentials

All gitignored; template in `.env.example`. Every run names its credentials explicitly via `--env-file` — there is no default.

| File | Role | Used for |
|------|------|----------|
| `instances/policy_db/.env` | `policy_db_maintainer` (owns the database, schemas, tables) | every ingest / embedding / validation run |
| `.env.provisioning` (workspace root) | the cluster's provisioning account | one-off provisioning only (roles, databases, extensions, grants) |

`.env.provisioning` holds whatever account this cluster provisions with, which is **not necessarily a superuser** — see §3 for what that account must be able to do. One step genuinely does require a true superuser regardless: `sql/mcp_ro_policy_grants.sql`, for the reasons §3 gives.

The provisioning credential lives at the workspace root, not under this instance, because it is not scoped to this instance: the same login provisions the engine's `ingestion_test` database and would provision any future instance. Only `policy_db`-specific credentials belong here. It sits alongside `.env.test` for the same reason — both are cluster-and-machine state rather than instance state.

**Permissions: no other local account may be able to read a credential file.** The default `umask 022` creates each one world-readable — on a machine that may also have a local `postgres` account. Being gitignored, nothing in the repo checks this for you.

How to satisfy and verify it depends on the platform, and this is the one place where a command that returns success while protecting nothing is a security problem rather than a cosmetic one:

`chmod 600` each credential file after creating or replacing it (a no-op on Windows, where the profile ACL already restricts it). `umask` recreates the permissive mode on every newly created file, so on macOS this has to be re-applied by hand.

```bash
# macOS: the mode bits are the protection, so set and verify them.
chmod 600 instances/policy_db/.env .env.provisioning .env.test
ls -l instances/policy_db/.env .env.provisioning .env.test   # expect -rw-------
```

```powershell
# Windows: the profile ACL is the protection, so verify that instead --
# `ls -l` would report a mode that means nothing here.
icacls .env.provisioning
# expect only the owner and administrators; no BUILTIN\Users, no Everyone
```

## 3. Provisioning (fresh database or fresh server)

Once per database, via the `sql/` scripts — each idempotent, each asserting its own preconditions with the remedy in the error:

| Role | Created by | Privileges |
|------|------------|------------|
| `policy_db_maintainer` | `sql/create_policy_db_roles.sql` (step 1) | `LOGIN` only — no attributes, no memberships. Owns `policy_db` and everything in it; ownership is its entire power, and every pipeline run connects as it. |
| `mcp_ro_policy` | `sql/create_policy_db_roles.sql` (step 1, when `-v serving_password` is passed) | `LOGIN` only — no attributes, no memberships (`mcp_ro_policy_grants.sql` asserts this rather than granting it; see below). Everything it can read comes from that script's grants. |

The provisioning account needs the same three abilities as in the roles table of MAINTAINING.packages.md §2; each script checks what its own run needs and fails naming the remedy. Roles and database are separate scripts because their lifecycles differ: roles are cluster-level and created once, while the database can be dropped, recreated, or multiplied (a dev database under the same roles) with no credential in hand.

1. Run `sql/create_policy_db_roles.sql` — creates `policy_db_maintainer` (password required) and, with `-v serving_password`, `mcp_ro_policy`. Passwords are psql variables, never committed; record the maintainer's in `instances/policy_db/.env`.
2. Run `sql/create_policy_db.sql` — creates the database owned by the maintainer and the `ltree` and `vector` extensions inside it. No secrets; `-v db_name=` is also how a dev database is provisioned.

Then, after every rebuild (a rebuild drops both):

3. **As a superuser**, run `sql/mcp_ro_policy_grants.sql` — read-only grants on the four schemas (`public` + the three served ones) for the serving role, plus the large-object revokes and the role-scoped resource bounds. Idempotent; takes effect per query, no server restart.
4. As `policy_db_maintainer`, run `sql/policy_db_description.sql` — the database-level `COMMENT`.

`mcp_ro_policy_grants.sql` needs a **superuser**, not the database owner: revoking on a `pg_catalog` function requires owning it, and `ALTER ROLE ... IN DATABASE ... SET` for a role you did not create requires superuser. Both are deliberate; the cost is that this script joins the extension install as a superuser step.

The two in-database scripts (`mcp_ro_policy_grants.sql`, `policy_db_description.sql`) **refuse to run against a database other than `policy_db`** — a mistyped `-d` would otherwise revoke `CONNECT` from `PUBLIC`, or relabel a different database with this one's description, and still exit 0. Override deliberately with `-v expect_database=<name>` (that is how the scripts are verified against a throwaway database without touching `policy_db`). Neither creation script needs the guard: they create their targets from `-v` variables, so the connection cannot misdirect them.

`mcp_ro_policy_grants.sql` also asserts that the role holds none of SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS and no role memberships, because the file's central claim is that everything the identity can reach is listed in it — one `pg_read_all_data` membership would silently defeat the per-schema model without changing a line of the file.

The large-object revokes cover **all nine mutating functions** — any subset bounds nothing, since each left open grows the database on its own (`lo_truncate64` alone EXTENDS a large object with zero bytes). The read-side functions (`loread`, `lo_get`, `lo_lseek`, `lo_tell`, `lo_close`) are deliberately left granted — they cannot change a byte.

**Re-run `mcp_ro_policy_grants.sql` after ANY schema drop and BEFORE the embedding step.** Dropping a schema deletes its `pg_default_acl` row, so future-table coverage silently stops applying to anything created afterwards. Every relation in the served schemas must also be owned by `policy_db_maintainer`: `GRANT ... ON ALL TABLES` fails on a foreign-owned relation, and because the script runs in one transaction, one such table aborts the whole model.

Table and schema comments need no manual step — they reapply on the next ingest of each config.

## 4. Operating the pipeline

Every step is a console script taking this instance's config and env file; run its `data-val-*` counterpart after every load, with the same flags. Outputs land under `instances/policy_db/data/`, logs under `instances/policy_db/logs/`.

Both `config/` and `logs/` are subdivided by ENGINE PACKAGE NAME: `ingpipe_acquisition/`, `ingpipe_file_ingestion/`, `ingpipe_excel_ingestion/`, and `ingpipe_embedding_generation/`.

```bash
# acquire (qpp_cm workbooks arrive manually; both lanes take no --env-file)
uv run download-cms-iom     --config instances/policy_db/config/ingpipe_acquisition/cms_iom/download_cms_iom.toml
uv run data-val-downloads   --config instances/policy_db/config/ingpipe_acquisition/cms_iom/data_val_downloads.toml
uv run download-usc-titles  --config instances/policy_db/config/ingpipe_acquisition/usc_titles/download_usc_titles.toml
uv run data-val-downloads   --config instances/policy_db/config/ingpipe_acquisition/usc_titles/data_val_downloads.toml

# ingest documents (parse -> clean -> load), then validate
uv run ingest-files         --config instances/policy_db/config/ingpipe_file_ingestion/cms_iom/ingest_policy_pub_100_01.toml --env-file instances/policy_db/.env
uv run data-val-loaded-documents --config <same> --env-file <same>

# ingest workbooks, then validate both legs
uv run data-val-excel-inputs --config instances/policy_db/config/ingpipe_excel_ingestion/qpp_cm/ingest_qpp_cm_2026.toml
uv run ingest-excel          --config <same> --env-file instances/policy_db/.env
uv run data-val-excel-outputs --config <same> --env-file <same>

# embed, then validate
uv run generate-embeddings  --config instances/policy_db/config/ingpipe_embedding_generation/generate_embeddings_cms_iom.toml --env-file instances/policy_db/.env
uv run data-val-embeddings  --config <same> --env-file <same>
```

Notes that matter in practice:

- **Acquisition is manifest-driven** — the manifest contract, skip decisions, retry-on-failure, and `data-val-downloads` semantics are engine behavior (MAINTAINING.packages.md §5).
- Both acquisition configs declare a `min_targets` floor, so a CMS markup change that resolves to nothing is a failure rather than `0 downloaded` and exit 0.
- **The cms_iom snapshot date is authored, not derived.** `config/ingpipe_acquisition/cms_iom/*.toml` and `config/ingpipe_file_ingestion/cms_iom/*.toml` both name `data/input/cms_iom/<date>`; a refresh means editing the acquisition config, its `data_val_downloads.toml`, and the `source_dir`/`parsed_dir`/`cleaned_dir` in all 21 ingest configs. Known refresh cost, not a solved problem.
- **Re-runs skip existing work** — documents/sheets by `collection_path` existence, embeddings by a PK anti-join. `--overwrite` (or `overwrite = true` in config) deletes and redoes; it must be a real TOML boolean.
- The qpp_cm configs pin every sheet's `header_row` / `data_start_row` / `data_end_row` explicitly — keep doing that for new workbooks; it is what makes parsing deterministic against decorated spreadsheets.
- Validators assume no concurrent writes to the schema they check.
- Parsing is slow and offline; the parsed/cleaned JSON under `data/` is reusable — a database rebuild does not require re-parsing.

## 5. Rebuilds

A full rebuild (drop schemas, re-run every ingest config, re-embed) is the only way schema-template changes reach the database — the engine's DDL is `create table if not exists` and never alters existing tables.

Sequence: drop → re-run file/excel ingests from the existing parsed data → re-apply grants + description (section 3, steps 3–4) → regenerate embeddings → run every validator.

What a rebuild is expected to preserve, when nothing upstream of the load has changed: document and sheet rows come back identical from the existing parsed data, and `source_binary_hash` should not move for any sheet — a moved hash is a signal to investigate, not accept. Only the `*_embedding` tables legitimately change when chunking or rendering logic has moved. For what the corpus currently holds, ask the database rather than this file — counts recorded here would go stale on the next ingest.

## 6. Serving

`policy_db` is served read-only by the MCP server in the separate `mcp_deployment` repo, which discovers tables by introspection per the engine's data interface (MAINTAINING.packages.md §3) and connects as `mcp_ro_policy`.

- **This corpus is built with `ibm-granite/granite-embedding-small-english-r2` (384 dimensions).** The server's `MCP_EMBEDDING_MODEL` must name the same model — a mismatch would silently degrade rankings, not error. Changing the embedder means regenerating every embedding table and updating the server setting together.
- This repo owns everything in the database: DDL, grants, descriptions, re-embeds. The server is purely a reader; schema creation is pipeline-only by design.

## 7. Machine notes

The sections above state *requirements*, deliberately free of any account name, privilege value, date, or corpus status — those go stale on the next machine or the next run, and the database and filesystem answer for the current state on demand. Cluster-level machine facts (the provisioning account, `template1` seeding, the engine's test database) live in MAINTAINING.packages.md §8.

### Setting up a machine

1. Create the env files (§2) and set their permissions per the platform.
2. Provision the engine test database (MAINTAINING.packages.md §2) **before** trusting a green test suite — without `.env.test` the DB-backed tests silently skip, so green proves less than it looks like.
3. Place the corpus under `instances/policy_db/data/`; nothing in the repo fetches it for you.
4. Provision the database and grants per §3, checking the provisioning account against the requirements listed there rather than assuming it is a superuser.
