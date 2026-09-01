---
name: consolidate-policy-pdf-schemas
goal: Consolidate the 21 per-publication PostgreSQL schemas in the policy_pdf_ingestion module into a single shared schema called cms_iom, preserving publication identification via the existing publication_title column.
status: completed
created: 2026-03-04 14:00:00
updated: 2026-03-04 14:30:00
---

## Implementation Plan

1. [completed] Update all 21 TOML config files to use single schema - `code/policy_pdf_ingestion/config/ingest_policy_pdf_pub_100_*.toml`
   - 1.1. Change `db_schema` from the publication-specific value (e.g., `pub_100_01_general_information`) to `"cms_iom"` in each file
   - 1.2. Add `publication_title` field set to the folder name (e.g., `publication_title = "pub_100_01_general_information_eligibility_and_entitlement"`)
   - 1.3. Update `pdf_dir` to use the new data directory structure: `data/input/cms_iom/2026-03-04/{publication_folder}` (folder names match the downloaded directory names under `data/input/cms_iom/2026-03-04/`)

2. [completed] Update ingestion script to accept publication_title from config - `code/policy_pdf_ingestion/ingest_policy_pdf.py`
   - 2.1. Add `publication_title: str` parameter to `ingest_pdf()` function signature
   - 2.2. In `main()`, read `publication_title` from config with fallback to `db_schema`: `publication_title = config.get("publication_title", db_schema)`
   - 2.3. Pass `publication_title` to each `ingest_pdf()` call in the processing loop
   - 2.4. Change the documents INSERT to use the new parameter: `"publication_title": publication_title` instead of `"publication_title": db_schema`

3. [completed] Update unit tests for new signature - `code/policy_pdf_ingestion/unit_tests/test_ingest_policy_pdf.py`
   - 3.1. Add `publication_title="pub_100_01_general_information_eligibility_and_entitlement"` to all 6 existing `ingest_pdf()` calls
   - 3.2. Run tests with `uv run pytest code/policy_pdf_ingestion/unit_tests/`
   - 3.3. Verify all tests pass (both test_ingest_policy_pdf.py and test_section_parser.py)

## Key Data Decisions and Considerations

1. Single schema over per-publication schemas — Simplifies cross-publication RAG queries; the `publication_title` column already distinguishes publications
2. No filename collision risk — Each publication uses a distinct filename prefix (e.g., `ge101*` for pub 100-01, `clm104*` for pub 100-04), so the `filename` primary key will not collide across publications
3. Backward-compatible fallback — `config.get("publication_title", db_schema)` ensures old configs without the new field still work correctly
4. Overwrite safety — The existing overwrite logic deletes by `filename`, which is globally unique across publications, so no risk of cross-publication data loss
5. Out of scope — The `pdf_ingestion` module (Docling-based, `cms_iom` database) and `embedding_generation` configs are not changed in this activity

## Status & Next Steps

**Current Status**: Complete
**Completed**:
1. Updated all 21 TOML configs: `db_schema` set to `"cms_iom"`, added `publication_title` field, updated `pdf_dir` to `data/input/cms_iom/2026-03-04/{folder}`
2. Updated `ingest_policy_pdf.py`: added `publication_title` parameter to `ingest_pdf()`, reads from config with `db_schema` fallback in `main()`
3. Updated all 6 `ingest_pdf()` calls in `test_ingest_policy_pdf.py`, all 52 tests pass
**Next Steps**:
1. None -- all tasks complete
**Blockers**:
1. None
**Notes**:
1. No changes needed to `policy_db.sql`, `_utils.py`, `section_parser.py`, or `data_validation/data_val_ingestion.py`
2. The DDL template uses `IF NOT EXISTS`, so the shared schema and tables will be created on the first run and be no-ops for subsequent publication configs
