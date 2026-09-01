---
name: load-cleaned-sections-to-db
goal: Rework the load step to consume the clean step's db-ready JSON and insert it into a redesigned, collection_path-keyed document/document_content schema in PostgreSQL. Retire the markdown-based path (step_load's markdown reading and md_section_parser.py). Embedding generation is a separate module and is out of scope.
created: 2026-06-18 17:19:42
updated: 2026-06-18 17:24:00
---

## Implementation Plan

1. [completed] Rewrite the storage schema DDL - `code/file_ingestion/sql/schema.sql`
   - 1.1. `create extension if not exists ltree;`
   - 1.2. `{document_table}`: `collection_path ltree primary key`, `title text`, `n_parsed_sections integer not null`, `ingested_at timestamptz not null default now()`.
   - 1.3. `{content_table}`: `collection_path ltree not null references {schema_name}.{document_table}(collection_path) on delete cascade`, `sort_order integer not null`, `heading_text text`, `content_text text`, `word_count integer not null`, `page_start integer`, `page_end integer`, `primary key (collection_path, sort_order)`.
   - 1.4. Keep the `{schema_name}`/`{document_table}`/`{content_table}` placeholders so `_utils.ensure_schema` renders and executes unchanged.

2. [completed] Add the collection_path sanitizer - `code/file_ingestion/_utils.py`
   - 2.1. `sanitize_collection_path(path: str) -> str`: turn a config-authored path into a valid `ltree` — split on `.`, strip a trailing file extension on the leaf label, lowercase, replace any char not in `[a-z0-9_]` with `_`, collapse repeated `_`, and rejoin with `.`. Raise `ValueError` on an empty result.

3. [completed] Create and run sanitizer unit tests - `code/file_ingestion/unit_tests/test_utils.py`
   - 3.1. Cover `sanitize_collection_path`: extension stripped on the leaf, illegal chars and spaces → `_`, lowercased, repeats collapsed, multi-label paths preserved, empty/invalid input raises `ValueError`.
   - 3.2. Run `uv run pytest code/file_ingestion/unit_tests/test_utils.py`; verify all pass. (Create the file if absent; extend it if present.)

4. [completed] Rework the load step to insert cleaned JSON - `code/file_ingestion/ingest.py`
   - 4.1. `step_load` reads `cleaned_dir/<source-stem>.json`, validates/parses it with `CleanedDocument.model_validate_json` (from `cleaned_models`), and inserts: one `{document_table}` row (`collection_path`, `title`, `n_parsed_sections`) and one `{content_table}` row per section (`collection_path`, `sort_order`, `heading_text`, `content_text`, `word_count`, `page_start`, `page_end`), all in one transaction per document.
   - 4.2. Re-key the existing-row skip/overwrite logic on `collection_path` (was `filename`): skip when the document row exists and not `overwrite`; on `overwrite`, delete the document row first (content cascades).
   - 4.3. Remove the markdown path from `step_load`: the `cleaned_dir`→`parsed_dir` markdown fallback, `parse_md_sections`, and the `collapsed_by`/`collapse_by_map` parameter and column.
   - 4.4. In `main()`: read per-document `collection_path` (sanitize via `_utils.sanitize_collection_path`) and `title`; build the maps; pass `cleaned_dir` (from `[clean]`) and the maps to `step_load`; drop the now-unused `collapse_by_map`. Keep the parse → clean → load wiring and `ensure_schema(...)` call (it renders the new DDL).
   - 4.5. Remove the `from md_section_parser import ...` import.

5. [completed] Update load unit tests - `code/file_ingestion/unit_tests/test_ingest.py`
   - 5.1. Add `step_load` tests using `tmp_path` for the cleaned JSON and a mocked SQLAlchemy engine/connection (`mocker`): a document + its sections are inserted with `collection_path` keys; an existing `collection_path` is skipped without `overwrite` and deleted-then-reinserted with `overwrite`; a malformed cleaned JSON raises (via `CleanedDocument` validation).
   - 5.2. Remove any assertions tied to the dropped `collapsed_by`/markdown behavior; run `uv run pytest code/file_ingestion/unit_tests/`.

6. [completed] Remove the superseded markdown section parser - `code/file_ingestion/md_section_parser.py`
   - 6.1. Delete `code/file_ingestion/md_section_parser.py` and `code/file_ingestion/unit_tests/test_md_section_parser.py`.
   - 6.2. Grep the repo to confirm no remaining importers of `md_section_parser` (only the now-reworked `ingest.py` referenced it).

7. [completed] Update TOML configs for the load schema - `code/file_ingestion/config/**/*.toml`
   - 7.1. Add a `collection_path` field to each `[module].documents` entry (the per-file path whose leaf identifies the document, e.g. `cms_iom.pub_100_01.ge101c01`).
   - 7.2. Leave `[parse]`, `[clean]`, and `[load]` sections otherwise intact (`[load]` keeps `db_name`/`db_schema`/table names; `step_load` reads the cleaned JSON from `[clean].cleaned_dir`).

8. [completed] Run parse → clean → load on one config - `code/file_ingestion/ingest.py`
   - 8.1. Choose `pub_100_01` (cleaned JSON already exists under `data/cleaned/cms_iom/2026-06-11/...`); ensure `[load].run=true` and a reachable `policy_db`.
   - 8.2. Run `uv run code/file_ingestion/ingest.py --config code/file_ingestion/config/cms_iom/ingest_policy_pub_100_01.toml`.
   - 8.3. Verify in `policy_db.cms_iom`: one `document` row per chapter with the sanitized `collection_path` and matching `n_parsed_sections`, and `document_content` rows whose count equals `n_parsed_sections`.

9. [completed] Create and run output data validation - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 9.1. Parameters: config path (to resolve `db_name`/`db_schema` and the expected documents).
   - 9.2. SQL checks across the loaded `document` and `document_content` tables: every `document.collection_path` is a valid non-null `ltree`; `n_parsed_sections` equals the `count(*)` of its `document_content` rows; no orphan `document_content` rows (FK integrity); `sort_order` is 1-based and contiguous per document; `word_count >= 0` and not null; `page_start <= page_end` where both non-null.
   - 9.3. Run against the loaded sample schema; debug and iterate if checks fail.

## Key Data Decisions and Considerations

1. Identity is `collection_path` (sanitized `ltree`), authored per document in the TOML — `document` PK and `document_content` FK. `filename` is not stored (it lives only in config and as the path's leaf). Re-ingest keys on `collection_path` alone.
2. `sanitize_collection_path` is deterministic and lossy (strip leaf extension, lowercase, `[a-z0-9_]`, collapse repeats); two authored paths could collide to the same `ltree` and fail loudly on the PK — acceptable, since authors control the paths.
3. The load step consumes the clean step's output via the shared `CleanedDocument` Pydantic model (`model_validate_json`), so a malformed cleaned file is rejected before any insert — the same schema validates on both read and write.
4. DB-table validation is SQL-based (referential integrity, counts, contiguity) — the right tool for data at rest, distinct from the Pydantic record validation used on the cleaned JSON. One validator (task 9) covers the `document`/`document_content` pair together because the key checks (FK integrity, count match) span both tables.
5. Markdown is now fully unconsumed by the pipeline (load no longer reads it). Dropping `markdown` from `[parse].output_formats` is a separate, optional follow-up — not required here; the parse step may keep emitting it for human inspection.
6. `md_section_parser.py` is retired because nothing imports it once `step_load` no longer parses markdown.
7. Breaking schema change — the old `filename`-keyed tables are replaced. Existing data must be re-loaded; since the cleaned `.json` already exists on disk, re-load needs no re-parse or re-clean. The target tables should be dropped/recreated (or created in a fresh schema) before loading.
8. Out of scope: embedding generation (a separate module) and the MCP layer. The `*_embedding` tables mirror the source PK, so they re-key to `collection_path` automatically when embeddings are regenerated — that regeneration is a separate task, not part of this activity.

## Status & Next Steps

**Current Status**: Complete — all nine tasks implemented and verified. The load step consumes the cleaned JSON and inserts into the collection_path-keyed `document`/`document_content` tables; the markdown path and `md_section_parser.py` are retired; `pub_100_01` is loaded into `policy_db.cms_iom` and the SQL validator passes.
**Completed**:
1. Task 1: `sql/schema.sql` rewritten to the collection_path-keyed tables (`create extension ltree`, `collection_path ltree` PK + cascading FK, `page_start`/`page_end`).
2. Task 2: `_utils.sanitize_collection_path` added (split on `.`, strip known leaf extension, lowercase, fold to `[a-z0-9_]`, collapse repeats; raises on empty).
3. Task 3: `unit_tests/test_utils.py` added (12 cases); all pass.
4. Task 4: `step_load` reworked to read `cleaned_dir/<stem>.json`, validate via `CleanedDocument.model_validate_json`, and insert one document row + one content row per section keyed by `collection_path`, one transaction per document; skip/overwrite re-keyed on `collection_path`. Markdown path, `parse_md_sections`, and the `collapse_by`/`module_name`-into-insert plumbing removed; `main()` builds the sanitized `collection_paths` map.
5. Task 5: `step_load` tests added to `test_ingest.py` with a mocked engine/connection; full suite passes (92 tests).
6. Task 6: `md_section_parser.py` and its test deleted; no remaining code importers.
7. Task 7: `collection_path` added to all 317 documents entries across the 35 configs (`<db_schema>.<source_dir leaf>.<file stem>`); every entry unique (raw + sanitized).
8. Task 8: `policy_db` created; `pub_100_01` loaded into `cms_iom` — 7 document rows, content counts equal `n_parsed_sections` (39/73/37/36/79/138/182; 584 total).
9. Task 9: `data_val_loaded_documents.py` SQL validator added and run against the loaded schema — passes.
**Next Steps**:
1. Regenerate embeddings (separate, out-of-scope) so the `*_embedding` tables re-key onto `collection_path`.
**Blockers**:
1. None.
**Notes**:
1. Input data validation is intentionally skipped at the load boundary — `step_load` validates the cleaned JSON via `CleanedDocument.model_validate_json`, which is the input contract.
2. After this activity, embeddings must be regenerated separately (out of scope) so the `*_embedding` tables re-key onto `collection_path`.
