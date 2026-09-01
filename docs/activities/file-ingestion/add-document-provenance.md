---
name: add-document-provenance
goal: Restructure the cleaned-sections JSON into a document-level envelope and capture a source content hash (Docling's origin.binary_hash) so each loaded document records the version of the source it came from. This is a provenance WRITE only — it seeds a baseline for a future change-detection selector; it does NOT add any skip/gate logic to the pipeline. Keep parse and clean decoupled (the hash is read from the parsed Docling output, never from the source file at clean/load).
created: 2026-06-22 17:18:26
updated: 2026-06-22 17:26:00
---

## Implementation Plan

1. [completed] Restructure the cleaned-document schema with a document envelope - `code/file_ingestion/cleaned_models.py`
   - 1.1. Introduce a `Document` sub-model (`strict=True, extra="forbid"`) holding the document-level fields: `n_parsed_sections: int` (`Field(ge=1)`) and `binary_hash: int` (`Field(ge=0)` — Docling's 64-bit source hash, an unsigned 64-bit integer).
   - 1.2. Change `CleanedDocument` to `{ document: Document, sections: list[Section] }` (the top level becomes the two table-aligned keys; `n_parsed_sections` moves OUT of the top level INTO `document`). Keep `Section` unchanged.
   - 1.3. Move the count invariant to span the envelope: a `@model_validator(mode="after")` on `CleanedDocument` asserts `document.n_parsed_sections == len(sections)`, `sections` is non-empty, and `sort_order` is 1-based contiguous (the existing section-list invariants, now referencing `document.n_parsed_sections`). Keep field-declaration order load-bearing (document first, then sections) so `model_dump()` fixes the on-disk key order.

2. [completed] Update the cleaned-models tests - `code/file_ingestion/unit_tests/test_cleaned_models.py`
   - 2.1. Update fixtures/assertions to the new shape (`document` envelope). Cover: a valid `CleanedDocument` with `document.{n_parsed_sections, binary_hash}` + sections passes; `n_parsed_sections != len(sections)` rejected; zero sections rejected; non-contiguous `sort_order` rejected; `extra="forbid"` rejects unknown keys at both the `document` and `Document`/`Section` level; strict-mode rejects bool-as-int for `binary_hash`/`n_parsed_sections`; a negative `binary_hash` rejected. Run `uv run pytest code/file_ingestion/unit_tests/test_cleaned_models.py` from the repo root.

3. [completed] Surface the source hash from the parsed doc and emit the new record - `code/file_ingestion/docling_section_parser.py`
   - 3.1. In `parse_docling_json`, read `doc.origin.binary_hash` from the already-loaded `DoclingDocument` (no source-file access — keeps parse/clean decoupled). Treat a missing `origin`/`binary_hash` as an error (raise `ValueError`) — consistent with the NOT-NULL intent; for file-based parsing origin is always present. The hash must reach `sections_to_record` so it can populate the `document` envelope (the exact seam between the two functions is an implementation choice).
   - 3.2. Update `sections_to_record(...)` to build the new `CleanedDocument` shape: `document={n_parsed_sections: len(sections), binary_hash: <origin hash>}`, `sections=sections`, then `model_dump()`. Update the module/function docstrings to document the `document` envelope and that the hash is the source provenance from Docling's origin. `step_clean` is UNCHANGED (no `source_dir`).

4. [completed] Update the section-parser tests - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 4.1. Update `sections_to_record`/round-trip tests to the new shape. Add a test that the emitted record carries `document.binary_hash` equal to the parsed doc's `origin.binary_hash` (the test builder sets a known origin/binary_hash), and a test that a parsed doc with no `origin` raises. Keep all existing cleaning-rule tests green. Run `uv run pytest code/file_ingestion/unit_tests/test_docling_section_parser.py` from the repo root.

5. [completed] Add the provenance column to the document schema - `code/file_ingestion/sql/schema.sql`
   - 5.1. Add `source_binary_hash numeric(20,0) not null` to the `{document_table}` definition (a uint64 overflows `bigint`, so `numeric` is used; NOT NULL because every loaded document now carries it). Keep the `{schema_name}`/`{document_table}`/`{content_table}` placeholders. The DB is empty, so `ensure_schema` (CREATE IF NOT EXISTS) creates the column on the next run — no migration.

6. [completed] Store the hash at load - `code/file_ingestion/ingest.py`
   - 6.1. `step_load` reads the cleaned JSON via `CleanedDocument.model_validate_json` (already does), then reads `document.n_parsed_sections` and `document.binary_hash` from the validated model (the top-level field move means `document.n_parsed_sections` replaces the former top-level `n_parsed_sections`). Insert `source_binary_hash` into the `{document_table}` row alongside `collection_path`, `title`, `n_parsed_sections`. No source-file access; `step_load` keeps its current resilient `(loaded_ok, failures)` contract and per-file error handling. Update the docstring.

7. [completed] Update the load-step tests - `code/file_ingestion/unit_tests/test_ingest.py`
   - 7.1. Update the `step_load` tests for the new cleaned-JSON shape (`document` envelope) and assert the document insert includes `source_binary_hash` (the value from `document.binary_hash`) and `n_parsed_sections` (from `document.n_parsed_sections`). Keep the resilient mixed-batch / failure tests green. Run `uv run pytest code/file_ingestion/unit_tests/` from the repo root.

8. [completed] Update the cleaned-JSON output validator - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - 8.1. The validator delegates wholly to `CleanedDocument.model_validate_json`, so it adapts to the new shape automatically; confirm and update its module docstring's check summary to mention the `document` envelope + `binary_hash`.

9. [completed] Update the cleaned-JSON validator tests - `code/file_ingestion/unit_tests/test_data_val_cleaned_json.py`
   - 9.1. Update fixtures to the new envelope shape and add a case asserting a malformed `document` block (e.g. missing `binary_hash`) is reported as a failure; keep the existing pass/fail cases green. Run the full `code/file_ingestion/unit_tests/` suite from the repo root.

10. [completed] Run parse -> clean -> load on one config and verify the new shape + column - `code/file_ingestion/ingest.py`
   - 10.1. Run `uv run code/file_ingestion/ingest.py --config code/file_ingestion/config/cms_iom/ingest_policy_pub_100_17.toml` (2 docs; cms_iom is empty). Confirm the pipeline completes (resilient summary, exit 0).
   - 10.2. Verify the cleaned JSON on disk has the new shape: top-level `document` (with `n_parsed_sections` + `binary_hash`) and `sections`.
   - 10.3. Verify in `policy_db.cms_iom.document`: each loaded row has a non-null `source_binary_hash`, and it equals `int(sha256(<source file bytes>).hexdigest(), 16) & 0xFFFFFFFFFFFFFFFF` for at least one document (proving the stored value is the reproducible truncated-sha256 the future selector will compute).
   - 10.4. Truncate `cms_iom` and delete `data/parsed/cms_iom` + `data/cleaned/cms_iom` afterward so the DB and disk are left EMPTY for the upcoming full ingest. Confirm empty.

11. [completed] Update the loaded-documents output validator - `code/file_ingestion/data_validation/data_val_loaded_documents.py`
   - 11.1. Add a check that every `document` row has a non-null `source_binary_hash` within `[0, 2^64)` (valid unsigned 64-bit). Keep it consistent with the existing SQL-check style (collect failures, exit 0/1). Update the module docstring's check list. (No dedicated unit-test file exists for this DB validator; verify by the run in task 10 and, if practical, a rolled-back corruption proof that the new check fires.)

## Key Data Decisions and Considerations

1. Provenance is captured from Docling's `origin.binary_hash`, which is the **low 64 bits of `sha256(source bytes)`** (verified: Docling computes a full sha256 via `create_file_hash`, then masks to uint64). This is FREE (already in the parsed JSON), keeps parse and clean DECOUPLED (clean reads the parsed Docling output, never the source file), and is reproducible by a future selector with stdlib only: `int(sha256(bytes).hexdigest(), 16) & 0xFFFFFFFFFFFFFFFF` (no Docling, no re-parse). The 64-bit truncation is sufficient for non-adversarial change-detection (CMS is a trusted source; accidental collision ~1 in 5e19 per comparison). It is NOT cryptographic tamper-evidence — if that were ever required, switch to a full sha256 computed at parse, which would re-introduce source coupling. Out of scope.
2. This is a PROVENANCE WRITE only — it seeds the baseline a future change-detection SELECTOR will compare against. It deliberately adds NO skip/gate logic to ingest. `ingest.py` stays a faithful executor of whatever the TOML lists; deciding WHICH files to (re)process is a separate selector/config-generation concern (a later activity).
3. The cleaned JSON is restructured into a `document` envelope: `{ document: {...}, sections: [...] }`. The top-level keys mirror the two tables (`document` block -> `document` row; `sections` -> `document_content` rows), giving an explicit JSON<->schema isomorphism and one obvious home for any future document-level field. `n_parsed_sections` moves INTO `document` (it is a document-level content metric). Provenance (`binary_hash`) lives in `document` flat for now; a nested `provenance` sub-block is deferred until there is more than one origin field (avoid premature nesting).
4. Scope discipline on provenance fields: only INTRINSIC facts (from the parsed artifact/environment, a bounded set) are candidates — `binary_hash` now; `docling_version`/`mimetype` optional and intentionally OMITTED for now. Config-derived knobs (`do_ocr`, `pdf_backend`, and any future config knobs) are NEVER denormalized into the data one-by-one — that would couple the data schema to the config schema and grow without bound. If parse-reproducibility is ever needed, capture a SINGLE config reference (a `config_hash` / snapshot), not per-knob columns. Out of scope here.
5. Column type is `numeric(20,0)` (NOT `bigint`): the value is an unsigned 64-bit integer whose top half can exceed `bigint`'s signed max. NOT NULL because every loaded document now carries the hash (origin is always present for file-based parsing; a missing origin is treated as an error upstream).
6. Free timing: all cleaned files and the `cms_iom` schema were already cleared, so the restructured shape and the new column are produced from scratch on the next ingest — no migration or backfill.
7. This touches the SHARED `CleanedDocument` schema (recently hardened in code review) and its producer/validator; the field-order and strict/extra-forbid invariants are preserved, and the consuming validator adapts automatically because it delegates to the model.

## Status & Next Steps

**Current Status**: Implementation COMPLETE — all 11 tasks done. The `CleanedDocument` schema now carries a `document` envelope (`n_parsed_sections` + `binary_hash`); the parser reads `origin.binary_hash` from the parsed doc and threads it through `step_clean` -> `sections_to_record` -> the cleaned JSON; the load step inserts `source_binary_hash` (numeric(20,0) NOT NULL) into the `document` table; both output validators are updated. The full suite is green (186 passing, up from the 173 baseline). The verification run completed (exit 0, 2/2 loaded), the stored hashes matched the recomputed truncated-sha256, and the DB + parsed/cleaned dirs were left EMPTY for the upcoming full ingest.
**Completed**:
1. Verified Docling's `origin.binary_hash` is the low 64 bits of `sha256(source)` and reproducible with stdlib; confirmed parse/clean can stay decoupled (hash read from parsed output).
2. Settled the `document` envelope structure, the intrinsic-vs-config provenance line (single `config_hash` if ever needed, never per-knob), and the numeric NOT-NULL column.
3. Implemented all 11 tasks: the `Document` sub-model + envelope `CleanedDocument` with the count invariant referencing `document.n_parsed_sections`; the parser's required `origin.binary_hash` read (returns `tuple[list[Section], int]`); the `source_binary_hash numeric(20,0) not null` schema column; the load-step insert; the two validators; and full test-fixture updates across cleaned_models / parser / ingest / data_val.
4. Ran the pub_100_17 config (exit 0, resilient summary, 2/2 loaded), confirmed the new on-disk envelope shape and that each stored `source_binary_hash` equals `int(sha256(<source bytes>).hexdigest(),16) & 0xFFFFFFFFFFFFFFFF`; proved the new loaded-docs validator check is non-vacuous (rolled-back out-of-range corruption fired count=1, clean data passed); then truncated `cms_iom` and removed `data/parsed/cms_iom` + `data/cleaned/cms_iom` (all empty).
**Deviation from plan (DB structure touched)**:
1. The `cms_iom` schema was not truly fresh: an empty leftover shell existed from an abandoned earlier provenance approach (`source_sha256 text`, no `source_binary_hash`, 0 rows in all three tables incl. `document_content_embedding`). `ensure_schema` is CREATE IF NOT EXISTS, so it would have skipped the existing shell and the `source_binary_hash` insert would have failed. With all three tables confirmed empty, the stale `document_content_embedding` -> `document_content` -> `document` tables were dropped in dependency order so `ensure_schema` recreated `document` + `document_content` from the canonical `schema.sql` (leaving `schema.sql` as the single source of truth, no permanent DB/schema drift). The embedding table is owned by its own pipeline and an empty one is free to drop.
**Next Steps**:
1. Run the full 201-doc cms_iom ingest to seed `source_binary_hash` for every document — the baseline for a future monthly change-detection selector (a separate activity).
**Blockers**:
1. None.
**Notes**:
1. No input data validation task: the input is the parse step's Docling JSON (Docling's own output), and the clean step already fails fast on unhandled content via the in-parser guard; there is nothing additional to validate at this boundary.
2. After this activity, the full 201-doc cms_iom ingest can run and will seed `source_binary_hash` for every document — the baseline for a future monthly change-detection selector (a separate activity), which also owns orphan/deletion handling and config generation.
3. Embedding generation is unaffected (it reads `document_content`, which is unchanged).
