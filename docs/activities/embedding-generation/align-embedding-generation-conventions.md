---
name: align-embedding-generation-conventions
goal: Follow-on consistency/correctness pass over the embedding_generation module (the build / hybrid-FTS / token-aware-chunking / contextual-header work is already complete in harden-embedding-generation.md). Bring it in line with the file_ingestion / excel_ingestion hardening — reuse the canonical SQL-identifier validator (fixing the weaker `.match`), build the engine URL with `URL.create`, fix the inconsistent logconfig import path, switch the embed-text format to newline key-value — remove the unused `source_table_pks` override (auto-detect only), and point the qpp_cm/briefs configs at the real source tables after the excel `sheet`/`sheet_content` rename. No change to chunking, contextual headers, the hybrid table design, or the model.
created: 2026-06-25 16:05:15
updated: 2026-06-25 16:55:00
---

## Implementation Plan

`chunker.py` and `data_validation/data_val_embeddings.py` are UNCHANGED. The
embedding table DDL, token-aware chunking, contextual headers, hybrid (vector +
FTS) design, and the model (`Alibaba-NLP/gte-large-en-v1.5`) are all kept as-is —
they were settled in the prior activity.

1. [completed] Shared utilities - `code/embedding_generation/_utils.py`
   - 1.1. Reuse the canonical `validate_sql_identifier` from `code/file_ingestion/_utils.py` (the `fullmatch` implementation) instead of the local `.match` copy — load the sibling by explicit path via `importlib` and re-export, exactly as `code/excel_ingestion/_utils.py` does (removes a third copy AND fixes the trailing-newline weakness of `.match`). Drop the local `_SAFE_IDENTIFIER_RE` + `validate_sql_identifier`.
   - 1.2. Fix the logconfig import path: `sys.path.insert(0, ".claude/skills/python-development/scripts/logconfig")` -> `".../scripts"`, so `from logconfig import get_logger` resolves to the package (matching every other module) instead of the inner `logconfig.py` module by accident of import order.
   - 1.3. Keep `make_token_counter` / `get_tokenizer` (embedding-specific) unchanged.

2. [completed] Embedding orchestrator - `code/embedding_generation/generate_embeddings.py`
   - 2.1. `_get_engine`: build the URL with `sqlalchemy.URL.create` (credentials percent-encoded) instead of the raw `f"postgresql://{user}:{password}@..."` string — matching file_ingestion / excel_ingestion. Keep the `ConfigurationError` on missing env vars.
   - 2.2. `_build_embed_text`: join the `"col: value"` parts with `"\n"` (newline key-value) instead of `" || "`; change the contextual `header_prefix` joiner (currently `... + " || "`) to a newline too. Matches the excel `row_text` decision and the tabular-RAG research (column labels kept; only the separator changes). The header's reserved token cost is recomputed from the new prefix, so the budgeting is unaffected.
   - 2.3. Remove the `source_table_pks` override: drop the parameter from `generate_embeddings(...)`, drop `source_table_pks = table_config.get(...)` and its pass-through in `main()`, and always resolve PKs via `_detect_primary_keys`. Reword its "No primary keys found" error to drop the now-gone `source_table_pks` suggestion (a source table without a PK is not embeddable — the embedding table needs a unique key for its PK + FK). A configured PK-less table therefore surfaces as a per-table failure (logged, recorded, others continue, non-zero exit) — the intended "don't process a table without a PK" behavior.

3. [completed] Tests for the orchestrator + utils - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 3.1. Update `_build_embed_text` assertions to the newline form (`"col: a\ncol: b"`), including the header-prefixed chunk case.
   - 3.2. Update the `_get_engine` test(s) to assert `URL.create` behavior (a password with URL-reserved characters is percent-encoded, not corrupting the URL), mirroring the file/excel `get_engine` tests.
   - 3.3. Remove tests exercising the `source_table_pks` override; keep/adjust the auto-detect path and add a test that a source table with no PK is reported as a per-table failure (mock the inspector to return no PK).
   - 3.4. Add coverage that the re-exported `validate_sql_identifier` rejects a trailing newline (the `fullmatch` fix). Run `uv run pytest code/embedding_generation/unit_tests/` (the chunker tests are untouched and should stay green).

4. [completed] Configs: point at the real source tables + drop the removed knob
   - 4.1. `config/generate_embeddings_qpp_cm.toml`: replace the stale `document_content = {...}` with `sheet_content = { embed_columns = ["row_text"] }` (qpp_cm's real consolidated content table; PK `(collection_path, sort_order)` auto-detects). Add a comment that an optional `header_columns = ["collection_path"]` would prepend `<workbook>.<sheet>` context to each row (the human title lives on the `sheet` table, which the embedder can't join to).
   - 4.2. `config/generate_embeddings_briefs.toml`: rename the stale `excel_content` table to `sheet_content` (the excel rename); leave the rest (dormant — `briefs_db` does not exist yet).
   - 4.3. Remove the `source_table_pks` line from the per-table field docs (and any example line) in all four configs (`cms_iom`, `qpp_cm`, `usc`, `briefs`).
   - 4.4. Confirm `cms_iom` (`document_content`) and `usc` (`document_content`, forward-looking — `usc` not ingested yet) need no source-table change.

5. [completed] Run the embedder on a qpp_cm slice (proving run) - `code/embedding_generation/generate_embeddings.py`
   - 5.1. Run `generate_embeddings.py` on a SMALL slice of `qpp_cm.sheet_content` (a `source_filter` on one workbook's `collection_path`, e.g. `2025_codes_list_aki%`) so the model loads + a real `qpp_cm.sheet_content_embedding` table is created and populated without the full multi-hour CPU batch.
   - 5.2. Confirm the table has the mirrored `collection_path ltree` + `sort_order` PK, the FK to `sheet_content` with cascade, `embedding vector(1024)`, and the `chunk_tsv` + HNSW/GIN indexes, with rows inserted and non-null embeddings.

6. [completed] Run output validation on the embedded slice - `code/embedding_generation/data_validation/data_val_embeddings.py`
   - 6.1. Run the EXISTING output validator (built in the prior activity — not created here) against the qpp_cm config, scoped to the embedded slice. Confirm: every in-scope `sheet_content` source row has >=1 embedding (no unembedded rows), no stored `chunk_text` exceeds `max_tokens` when tokenized with the model tokenizer, `embedding` is non-null with dimension 1024, no orphan embedding rows (FK integrity), and the `chunk_tsv` column + GIN index exist. The slice is derived/re-buildable — keep or drop per preference.

## Key Data Decisions and Considerations

1. This is a consistency/correctness pass, not a redesign. Chunking (structure-aware sections + token-cap sub-splitting, 500/50), contextual headers (deterministic contextual retrieval), the hybrid table (pgvector HNSW + `tsvector` GIN, mirrored-PK + cascade FK), and the model were settled in `harden-embedding-generation.md` and are confirmed current best-practice — unchanged here.
2. Validator reuse (1.1): re-export the canonical file_ingestion validator rather than keep a third local copy — DRY, single source of truth, and it is the `fullmatch` version (rejects a trailing newline), matching how excel_ingestion already consumes it.
3. Embed-text format (2.2): newline key-value, consistent with the excel `row_text` decision and the research that column-labeled `key: value` with a newline separator embeds better than an arbitrary `||` join. Labels kept (field context for the model); only the separator changes. Note this means a re-embed would produce different vectors than the prior `||`-format run — fine, since nothing is currently embedded.
4. `source_table_pks` removal (2.3): every source table comes from file_ingestion / excel_ingestion, which always declare PRIMARY KEYs, so auto-detect always succeeds and the override was dead config. A truly PK-less source is not embeddable anyway (the FK requires a unique key), so failing that table (recorded, non-zero exit) is the correct behavior — not a manual workaround.
5. qpp_cm config (4.1): the source table changes (`document_content` -> `sheet_content`, `row_text`) and `header_columns = ["collection_path"]` is applied (added later) so each bare row carries its `<workbook>.<sheet>` identity in the embedding for semantic retrieval — the only context column on `sheet_content` (the human title lives on the un-joinable `sheet` table). Machine-form but reversible; the model is unchanged.
6. Model unchanged: `Alibaba-NLP/gte-large-en-v1.5` (English-specialized, 1024-dim, 8192 context, symmetric / no query prefix, CPU-friendly on the 8-vCPU/32 GB target VM). Already set in all four configs by the prior activity; the table's vector dimension is derived dynamically, so no code change.

## Status & Next Steps

**Current Status**: COMPLETE. All tasks implemented; suite 66 passed. A proving run embedded a 16-row qpp_cm.sheet_content slice into qpp_cm.sheet_content_embedding (mirrored collection_path/sort_order PK, FK->sheet_content cascade, embedding vector(1024), chunk_tsv + HNSW/GIN indexes, newline-KV chunk_text, FTS working); the output validator passed; the test slice was then dropped (a full qpp_cm embed is a separate job).
**Completed**:
1. Reviewed the module; researched chunking / models / contextual retrieval / hybrid search vs current standards (architecture is current).
2. Settled the model (`gte-large-en-v1.5`) and this fix set; confirmed source tables (`cms_iom.document_content` ✓, `qpp_cm.sheet_content` ✓, `usc`/`briefs` not ingested yet).
**Next Steps**:
1. Implement tasks 1-4, run the suite, then tasks 5-6 (embed a qpp_cm slice + run the output validator).
2. Code review of the module afterward (it has never had a per-file review like the excel passes).
**Blockers**:
1. None — qpp_cm source data is loaded; the slice proving run needs only the model (caches on first load).
**Notes**:
1. DEFERRED (#10) — model-consistency guard: nothing records which model built an embedding table, and a same-dimension model swap would silently mix incompatible vector spaces in one table (the dimension check only catches a dimension change). Follow-up: record the model on each embedding table (table comment or per-schema meta row) and refuse a mismatched re-write; the MCP server reads it to embed queries with the matching model and to assert all schemas it serves agree. Out of scope here (one model everywhere is the standing convention; risk is low with nothing embedded yet).
2. OPS (#8) — the deployment VM runs `thenlper/gte-large` (GTE v1, 512 context, ~63 MTEB); bump it to `Alibaba-NLP/gte-large-en-v1.5` (already the repo config) so the embedder and the MCP query path agree. Both are 1024-dim, so the deferred #10 guard would have caught a silent mix here — until it lands, enforced by convention.
3. briefs is dormant (no `briefs_db`); its config fix (4.2) is correctness/consistency only.
4. usc has no `usc` schema yet; `generate_embeddings_usc.toml` is forward-looking and cannot run until usc is ingested via file_ingestion.
5. FOUND DURING IMPLEMENTATION (fixed): `_build_source_filter_clause` emitted `col like :p`, which fails on a non-text column — notably `collection_path` (an `ltree`, the most common identity), so `source_filter`'s primary use case (surgical re-embed by collection_path) was broken (`operator does not exist: ltree ~~ unknown`). Fixed by casting `col::text like :p` (a no-op for text columns). Tests updated.
6. This activity is the follow-on to `docs/activities/embedding-generation/harden-embedding-generation.md` (complete), which built the token-aware chunker, FTS hybrid index, model swap, and contextual headers.
6. Input data validation is intentionally NOT a task: the source tables (`qpp_cm.sheet_content`, `cms_iom.document_content`, …) are already validated by the file_ingestion / excel_ingestion output validators at load time, so the embedder consumes pre-validated input.
7. The re-exported `validate_sql_identifier` (task 1.1) is covered in `test_generate_embeddings.py` (task 3.4) rather than a new `test_utils.py`, since the module has no `test_utils.py` and the function is a thin re-export exercised through the config-identifier path; a dedicated test file is not warranted.
