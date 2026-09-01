# ingestion_pipeline

Document acquisition, ingestion, and embedding generation for RAG corpora — a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) of installable packages, split into a generic engine and explicit instances.

## The engine

One distribution per pipeline stage, plus a shared library. Each package is source-agnostic and credential-free, declares its own dependencies, and never imports a sibling — the independence is enforced by the build.

| Package | Purpose |
|---------|---------|
| `packages/ingpipe_acquisition/` | Fetch a source's files, record a manifest of what the run produced, and validate a corpus against it |
| `packages/ingpipe_file_ingestion/` | Parse PDFs/documents with Docling and load `document` / `document_content` tables |
| `packages/ingpipe_excel_ingestion/` | Parse Excel workbooks and load `sheet` / `sheet_content` (+ structured) tables |
| `packages/ingpipe_embedding_generation/` | Chunk loaded content and build `*_embedding` tables (pgvector + tsvector) |
| `packages/ingpipe_lib/` | Shared library: logging, validators, env loading, path anchoring, DB helpers, test fixtures |

Every stage is a console script driven by a TOML config (and, for anything touching a database, an explicit `--env-file`), with a matching `data-val-*` validation script. Installing the workspace (`uv sync`) installs them all:

```
acquire  data-val-downloads
ingest-files  data-val-cleaned-json  data-val-loaded-documents  quality-report
ingest-excel  data-val-excel-inputs  data-val-excel-outputs
generate-embeddings  data-val-embeddings
```

An instance may declare its own command name pointing at an engine entry point — `policy_db`'s `download-usc-titles` is `ingpipe_acquisition.runner:main` with no instance code behind it.

## Instances

An instance is a workspace member under `instances/` that owns one corpus: its configs, any source-specific discovery code, credentials, grants, data, and logs. Its `pyproject.toml` dependency list declares which engine packages it uses; it consumes them only through their console scripts and `ingpipe_lib`. A config's relative paths and a run's logs anchor to the config's instance root, so commands work from any directory.

Current instances: `instances/policy_db/` (CMS policy corpus).

## Quick start

```bash
uv sync                      # installs every member into one shared .venv
uv run pytest                # DB-backed tests skip cleanly without .env.test
uv run ingest-files \
    --config instances/policy_db/config/ingpipe_file_ingestion/test/ingest_test_document.toml \
    --env-file instances/policy_db/.env
```

## Documentation

- [MAINTAINING.packages.md](MAINTAINING.packages.md) — maintaining the engine: setup, testing, the data interface the engine writes, parsing background, conventions, and gotchas.
- [MAINTAINING.instance.policy_db.md](MAINTAINING.instance.policy_db.md) — operating the policy_db instance: credentials, provisioning, the ingest→validate cycle, rebuilds, and serving.

`docs/activities/` and `docs/code_review/` are dated work records produced by the development workflow, not documentation — see the maintaining files for current truth.
