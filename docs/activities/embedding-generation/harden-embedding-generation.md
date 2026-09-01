---
name: harden-embedding-generation
goal: Harden the embedding module for solid RAG. Swap the embedding model to the longer-context gte-large-en-v1.5, make chunking token-aware so chunks stop being silently truncated, and make the embedding tables hybrid-ready by adding a native Postgres full-text index on the chunk text. Then regenerate embeddings against the new collection_path-keyed schema.
created: 2026-06-18 18:31:19
updated: 2026-06-20 09:05:00
---

## Implementation Plan

1. [completed] Make the chunker token-aware - `code/embedding_generation/chunker.py`
   - 1.1. Change `chunk_long_sections` to budget by TOKENS rather than words: parameters `max_tokens: int`, `overlap_tokens: int`, and a token-count callable `count_tokens: Callable[[str], int]` supplied by the caller (the embedding model's tokenizer, counted the way the model counts including special tokens).
   - 1.2. Behavior: a section whose token count is <= `max_tokens` passes through as a single chunk; a longer section is split into chunks each <= `max_tokens`, preserving word boundaries (never split a word), with `overlap_tokens` of overlapping content between adjacent chunks. Each output dict keeps `content_text` and `word_count`.
   - 1.3. Validate inputs: `max_tokens > 0`, `overlap_tokens >= 0`, `overlap_tokens < max_tokens`; raise `ValueError` otherwise (mirroring the existing guards).

2. [completed] Update and run chunker tests - `code/embedding_generation/unit_tests/test_chunker.py`
   - 2.1. Use a simple injected `count_tokens` (e.g. whitespace/char-based) so tests are deterministic and fast. Cover: section under budget → one chunk; over budget → multiple chunks each within `max_tokens`; overlap present between adjacent chunks; word boundaries preserved (no split words); the three validation errors.
   - 2.2. Run `uv run pytest code/embedding_generation/unit_tests/test_chunker.py`; verify all pass.

3. [completed] Add hybrid FTS index and token-aware wiring to embedding generation - `code/embedding_generation/generate_embeddings.py`
   - 3.1. In `_create_embedding_table`, add a generated full-text column and GIN index to the DDL: `chunk_tsv tsvector generated always as (to_tsvector('english', chunk_text)) stored`, plus `create index if not exists idx_{embedding_table}_chunk_tsv on {schema}.{embedding_table} using gin (chunk_tsv)`.
   - 3.2. In `generate_embeddings`, pass the loaded model's tokenizer as the `count_tokens` callable and the configured `max_tokens`/`overlap_tokens` to `chunk_long_sections` (replacing `max_words`/`overlap_words`).
   - 3.3. In `main()`, read `max_tokens` (default 500) and `overlap_tokens` (default 50) from config (top-level + per-table override), replacing the `max_words`/`overlap_words` fields; pass them through. With the new 8192-context model, 500 is a deliberate retrieval-precision choice that sits far below the limit (so truncation is structurally avoided, not just narrowly), rather than a tight ceiling.
   - 3.4. In `_get_embedding_model`, load `Alibaba-NLP/gte-large-en-v1.5` with `trust_remote_code=True` (its custom RoPE/GLU backbone requires it). The output dimension stays 1024, so the dynamically-created `vector(...)` column and HNSW index are unchanged (dimension is auto-detected from the model).
   - 3.5. Add the `einops` runtime dependency (required by the model's remote code) via `uv add einops` (updates `pyproject.toml` + `uv.lock`).

4. [completed] Update and run embedding-generation tests - `code/embedding_generation/unit_tests/test_generate_embeddings.py`
   - 4.1. Assert the generated DDL includes the `chunk_tsv` generated column and the GIN index. Assert `chunk_long_sections` is called with the token budget + a token counter (mock the model/tokenizer). Update any `max_words`/`overlap_words` references to `max_tokens`/`overlap_tokens`.
   - 4.2. Run `uv run pytest code/embedding_generation/unit_tests/`.

5. [completed] Update embedding configs - `code/embedding_generation/config/*.toml`
   - 5.1. Replace `max_words`/`overlap_words` with `max_tokens = 500` / `overlap_tokens = 50` (top-level) in all four configs (cms_iom, qpp_cm, usc, briefs).
   - 5.2. Set `model_name = "Alibaba-NLP/gte-large-en-v1.5"` (replacing `thenlper/gte-large`) in all four configs.
   - 5.3. Fix the stale `source_filter` comment example that references the dropped `filename` column — change it to `collection_path`.

6. [completed] Regenerate embeddings for cms_iom on policy_db - `code/embedding_generation/generate_embeddings.py`
   - 6.1. `policy_db.cms_iom` already holds the loaded `document_content` (584 rows for pub_100_01) and no embedding table yet. Run `uv run code/embedding_generation/generate_embeddings.py --config code/embedding_generation/config/generate_embeddings_cms_iom.toml`.
   - 6.2. Verify in `policy_db.cms_iom`: `document_content_embedding` is created keyed `(collection_path, sort_order, chunk_number)` with an FK to `document_content`, an HNSW index on `embedding`, and the new `chunk_tsv` GIN index; embedding rows exist for the loaded sections; `embedding` is non-null with dimension 1024.

7. [completed] Create and run output data validation - `code/embedding_generation/data_validation/data_val_embeddings.py`
   - 7.1. Parameters: config path (to resolve `db_name`/`db_schema` and the source/embedding tables).
   - 7.2. Checks against the embedding table: every source `document_content` row has at least one embedding row (no unembedded rows, accounting for `source_filter`); no stored `chunk_text` exceeds `max_tokens` when tokenized with the model tokenizer (proves truncation is fixed); `embedding` is non-null with the expected dimension; no orphan embedding rows (FK integrity); the `chunk_tsv` column and its GIN index exist.
   - 7.3. Run against the regenerated `cms_iom` embeddings; debug and iterate if checks fail.

8. [completed] Add contextual chunk headers - `code/embedding_generation/generate_embeddings.py`
   - 8.1. Add a per-table `header_columns` config field (list, default `[]`): the columns whose `"col: value || ..."` text is prepended to EVERY chunk of a row, so tail chunks of long sections keep their heading in the embedding vector, the stored `chunk_text`, and the `chunk_tsv` FTS. `embed_columns` remains the chunked body. When `header_columns` is empty, behavior is exactly as before (fully backward compatible).
   - 8.2. Reserve the header's exact token cost (header prefix + `" || "` joiner, via the model token counter) from `max_tokens`, chunk the body with the reduced effective budget, then prepend the header prefix to each chunk's text for both the embedding input and the stored `chunk_text`. Floor the effective budget to `overlap_tokens + 1` if reservation would push it `<= overlap_tokens` so the chunker's `overlap_tokens < max_tokens` invariant holds. Keep `word_count` as the body chunk's count (do NOT recount over header+body) so the validator's single-word warning still covers unsplittable separators. Also fetch + validate `header_columns` in the SELECT (deduped union with `embed_columns`). Chunker and validator unchanged.
   - 8.3. Set `document_content` to `header_columns = ["heading_text"]`, `embed_columns = ["content_text"]` in `generate_embeddings_cms_iom.toml`; document the field. Re-embed with `--overwrite` and re-run the (unchanged) validator.

## Key Data Decisions and Considerations

1. Model swap to gte-large-en-v1.5. The current `thenlper/gte-large` is a 2023 model capped at 512 tokens — the root cause of the truncation below. `Alibaba-NLP/gte-large-en-v1.5` is the chosen upgrade: 8192-token context (16x), still 1024-dim (fits pgvector's 2000-dim HNSW limit and needs no schema change), same gte family, SOTA in its size class, and self-hostable on the project's MPS/CPU (~434M params). Query-embedding latency rises only ~10ms (measured ~32ms -> est. ~42ms), negligible next to LLM generation. The leaderboard giants (Qwen3-Embedding-8B, NV-Embed-v2) are ruled out: their native dimensions exceed pgvector's 2000-dim index limit and they are too heavy for this hardware. It requires `trust_remote_code=True` and the `einops` package.
2. Token-aware chunking fixes a measured, silent recall bug. With the old word-count budget (`max_words=350`) on the old 512-token model, 46/697 (6.6%) of the loaded pub_100_01 chunks exceeded the limit and were silently truncated (worst: 350 words = 1391 tokens, ~63% never entering the vector). The fix is twofold: token-aware chunking (budget via the model's own tokenizer) plus the longer-context model. `max_tokens=500` now sits far under the 8192 context, so it is a retrieval-precision choice (smaller chunks embed more precisely) with truncation structurally impossible — not a ceiling forced by the model.
3. Word-boundary-preserving, not sentence-aware. Chunks are packed to the token budget on word boundaries; sentence-aware packing is deliberately deferred. Mid-sentence cuts are a soft, retrieval-side issue that the overlap plus agent-side adjacent-chunk fetching already mitigate (a truncated vector, by contrast, is never retrieved in the first place — which is why the token fix is the priority).
4. Hybrid-ready storage via native Postgres FTS. A generated `english` `tsvector` column + GIN index on `chunk_text`, at CHUNK granularity (the same unit as the vectors) so dense and sparse results fuse cleanly later. Native FTS (`ts_rank_cd`) is chosen over a third-party BM25 extension (ParadeDB `pg_search`) to avoid an infra dependency; BM25 is a possible future upgrade.
5. The fusion query is OUT of scope. This activity only makes the data hybrid-ready (the index). Combining dense + sparse (reciprocal-rank fusion) lives in the MCP search layer and is a separate, later effort.
6. Schema adaptation is mostly automatic. The embedding table mirrors the source table's PK via auto-detection, so the new `(collection_path, sort_order)` key is picked up; the dimension stays 1024 so the `vector(...)` column is unchanged; and the fresh `policy_db` has no old `filename`-keyed embedding tables to drop. (Correction to the original "no code change" claim: the `ltree`-typed `collection_path` PK reflects as SQLAlchemy `NullType`, which cannot compile to DDL — so `_create_embedding_table` needed a `NullType` resolver, `_resolve_pg_column_type` via Postgres `format_type`, to mirror the PK column's exact type and keep the FK valid.)
7. Config changes: `model_name` -> `Alibaba-NLP/gte-large-en-v1.5`; `max_words`/`overlap_words` -> `max_tokens`/`overlap_tokens`; the stale `filename` reference in the `source_filter` comment becomes `collection_path`.
8. Out of scope: the MCP hybrid query/fusion. (The embedding-model change is now IN scope, per decision 1.)
9. Contextual chunk headers keep the heading on every chunk. The heading lands only in chunk 1 when the whole row is chunked as one string, so tail chunks of long sections lose their section context in both the vector and the keyword index. The fix is a per-table `header_columns` field whose `"col: value || ..."` text is prepended to EVERY chunk (vector input, stored `chunk_text`, and the generated `chunk_tsv`). The header's exact token cost is reserved from `max_tokens` so the body is chunked with the remaining budget — combined header+body stays within `max_tokens` for real (splittable) content; only the rare unsplittable single-word body (markdown table separators) can exceed it. Stored `word_count` stays the body chunk's count (not header+body), so the validator's existing single-word WARNING (not FAIL) still covers those separators. The chunker and the validator are unchanged — the header logic lives entirely in `generate_embeddings.py` by mutating each chunk's `content_text`. Empty `header_columns` reproduces the previous whole-row behavior exactly.

## Status & Next Steps

**Current Status**: Implemented and verified. All eight tasks complete: token-aware chunker, updated tests, FTS index + model swap (gte-large-en-v1.5) + token wiring, updated configs, regenerated cms_iom embeddings, a passing output validator, and contextual chunk headers (heading prepended to every chunk; re-embedded to 863 chunks from 584 rows).
**Completed**:
1. Token-aware chunker (`chunk_long_sections(sections, max_tokens, overlap_tokens, count_tokens)`), word-boundary preserving with a forward-progress guard; 19 chunker tests pass.
2. `generate_embeddings.py`: `chunk_tsv` generated tsvector column + GIN index in the DDL; model loads `Alibaba-NLP/gte-large-en-v1.5` with `trust_remote_code=True`; chunker driven by the model tokenizer via a shared `make_token_counter` helper in `_utils.py`; `main()` reads `max_tokens`/`overlap_tokens` (defaults 500/50). Added a `NullType` fallback (`_resolve_pg_column_type` via `format_type`) so the `ltree`-typed `collection_path` PK mirrors correctly — the activity's "automatic schema adaptation" assumption did not hold for custom types.
3. `einops` added (`uv add einops`).
4. All four configs updated: `max_tokens`/`overlap_tokens`, new model name, `collection_path` in the `source_filter` example comment.
5. Regenerated `cms_iom.document_content_embedding`: 832 embeddings, 584/584 source rows embedded, dim 1024 non-null, PK `(collection_path, sort_order, chunk_number)`, FK to `document_content`, HNSW + `chunk_tsv` GIN indexes.
6. Output validator `data_validation/data_val_embeddings.py` passes.
7. Contextual chunk headers: `header_columns` config field threaded through `generate_embeddings.py`; `document_content` set to `header_columns = ["heading_text"]`, `embed_columns = ["content_text"]`. Re-embedded with `--overwrite` to 863 chunks (up from 832 — the reserved header budget splits long sections into slightly more chunks); DB verification confirms all 863 `chunk_text` rows begin with the `heading_text: ...` prefix, including tail chunks of a 53-chunk section (chunks 2/3/53 all carry the heading), no multi-word chunk exceeds `max_tokens=500`, only 3 single-word separator chunks exceed it (worst 601), and no chunk exceeds `max_seq_length=8192`. Validator PASSES (separators surface as the single-word WARNING). 63 unit tests pass.
**Next Steps**:
1. None for this activity. (Future: MCP hybrid retrieval; ingestion cleaning of markdown table-separator rows.)
**Blockers**:
1. None.
**Notes**:
1. Input data validation is not needed — the source is `document_content`, already validated by the load step's output validator.
2. After this activity, the MCP search layer can adopt hybrid retrieval (vector + `chunk_tsv` keyword) — a separate effort.
3. Deviation: the literal "no chunk exceeds max_tokens" check was refined to "no multi-word chunk exceeds max_tokens, and NO chunk exceeds the model's max_seq_length (8192)". Three single-word chunks (markdown table-separator rows like `|----|----|`, 511–586 tokens) are space-free strings the chunker correctly refuses to split mid-word; they embed in full (well under 8192) so truncation is fixed. The validator logs these as a warning, not a failure, and flags them for future ingestion cleaning.
