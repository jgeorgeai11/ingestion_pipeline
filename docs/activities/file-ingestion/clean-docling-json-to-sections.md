---
name: clean-docling-json-to-sections
goal: Add the JSON-native cleaning/sectionizing step. A generic, deterministic DoclingDocument→sections parser plus a `clean` pipeline step that reads the parsed `.json` and writes a db-ready sections `.json`. This activity does NOT load into the database (that is a separate follow-up activity) and contains no document-specific logic and no structure-repair.
created: 2026-06-18 12:05:18
updated: 2026-06-18 12:14:55
---

## Implementation Plan

1. [completed] Create the DoclingDocument section parser - `code/file_ingestion/docling_section_parser.py`
   - 1.1. `Section` dataclass: `sort_order: int`, `heading_text: str | None`, `content_text: str | None`, `word_count: int`, `page_start: int | None`, `page_end: int | None`.
   - 1.2. `parse_docling_json(json_path: str | Path) -> list[Section]`: deserialize the file into a `DoclingDocument` (via `docling_core`), walk `body` in reading order, apply the cleaning rules below, and return sections ordered by `sort_order` (1-based).
   - 1.3. Cleaning rules (deterministic, generic — keyed only on Docling labels/structure):
      - Drop elements with `label ∈ {page_header, page_footer}` (furniture).
      - Drop elements whose text is empty/whitespace after strip.
      - `section_header` (and the document `title` element) starts a new section (`heading_text` = its text); all other text elements accumulate into the current section's `content_text`.
      - Render `table` elements inline as markdown at their reading-order position; skip a table whose cells are all blank.
      - Pictures: drop the image, keep any caption text as content.
      - Join block text with `\n\n`; `strip()` each element's text; no internal-whitespace collapsing.
      - Pre-heading content → a leading section with `heading_text=None`.
      - Empty-unit pruning: drop a unit only if it has neither a heading nor content; keep heading-only units (`content_text=None`, `word_count=0`).
      - `page_start`/`page_end` = min/max `prov.page_no` across the section's elements (`None` when no provenance).
   - 1.4. `sections_to_record(sections: list[Section]) -> dict`: build the db-ready payload `{"n_parsed_sections": len(sections), "sections": [<section dicts>]}` (identity-agnostic — `collection_path`/`title` are attached later by the load step from config).
   - 1.5. Explicitly NOT done here (future optional structure-repair step): list-item/heading mislabel correction, orphaned-metadata demotion, section merging, section-number logic, boilerplate (TOC/transmittal) removal.

2. [completed] Create and run unit tests for the section parser - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 2.1. Build small `DoclingDocument` fixtures (or minimal JSON) covering each rule: furniture dropped, empty elements dropped, table rendered inline, all-blank table skipped, picture caption kept, `title` element becomes a section, pre-heading content → `heading_text=None` section, heading-only section kept (`content_text=None`, `word_count=0`), fully-empty unit pruned, `page_start/page_end` derived from provenance, `sort_order` 1-based and contiguous.
   - 2.2. Test `sections_to_record` shape (`n_parsed_sections` matches list length; section dicts carry all fields).
   - 2.3. Run `uv run pytest code/file_ingestion/unit_tests/test_docling_section_parser.py`; verify all pass.

3. [completed] Replace the markdown collapse step with a JSON clean step - `code/file_ingestion/ingest.py`
   - 3.1. Add `step_clean(parsed_dir, cleaned_dir, file_paths, overwrite)`: for each file, read `parsed_dir/<stem>.json`, call `parse_docling_json` + `sections_to_record`, and write `cleaned_dir/<stem>.json`. Skip-if-exists on the output `.json` (non-empty) unless `overwrite`.
   - 3.2. Remove `step_collapse`, the `[collapse]` config block handling, and `collapse_by_map`; replace with the `[clean]` config (output `cleaned_dir`). Import from `docling_section_parser` instead of `md_section_parser`.
   - 3.3. `main()` wires the pipeline as parse → clean → load; the `[clean]` step is gated by its own `run` flag.
   - 3.4. Do NOT modify `step_load` or delete `md_section_parser.py` in this activity — `step_load` still reads markdown (falling back to the parse step's `.md`) and remains the responsibility of the separate load activity. Note the transitional state: the clean step's `.json` output is produced but not yet consumed by load.

4. [completed] Update TOML configs for the clean step - `code/file_ingestion/config/**/*.toml`
   - 4.1. Rename the `[collapse]` section to `[clean]`, keeping `run` and `cleaned_dir` (the latter now holds `.json` output).
   - 4.2. Remove the per-document `collapse_by` field from each `[module].documents` entry.
   - 4.3. Leave `[parse]` and `[load]` unchanged (the load section stays for the future load activity); `collection_path` is NOT added here (it belongs to the load activity).

5. [completed] Run the clean step on one already-parsed config - `code/file_ingestion/ingest.py`
   - 5.1. Using `pub_100_01` (JSON already produced under `data/parsed/cms_iom/2026-06-11/pub_100_01_general_information_eligibility_and_entitlement/`), run `uv run code/file_ingestion/ingest.py --config code/file_ingestion/config/cms_iom/ingest_policy_pub_100_01.toml` with `[parse].run=false`, `[clean].run=true`, `[load].run=false` (no database needed).
   - 5.2. Verify one `cleaned_dir/<stem>.json` per chapter; spot-check a few sections (`heading_text`, non-empty `content_text`, `page_start/page_end`, an inline-rendered table).

6. [completed] Create and run output data validation - `code/file_ingestion/data_validation/data_val_cleaned_json.py`
   - 6.1. Parameters: a directory of cleaned `.json` files (and/or a config to resolve `cleaned_dir` + expected files).
   - 6.2. Check each file: valid JSON; `n_parsed_sections` equals `len(sections)`; `sort_order` is 1-based and contiguous; `word_count` is a non-negative integer and `0` when `content_text` is null; `page_start <= page_end` where both non-null; at least one section per non-trivial document.
   - 6.3. Run against the `pub_100_01` cleaned output; debug and iterate if checks fail.

## Key Data Decisions and Considerations

1. This activity ends at a **db-ready cleaned `.json`** — it does not touch PostgreSQL. The schema rewrite (collection_path-keyed `document`/`document_content`) and the `step_load` rework are a separate follow-up activity; embeddings and the MCP layer are likewise out of scope. Benefit: no Postgres dependency, so the whole activity (including the run and validation) is exercisable locally.
2. The cleaned JSON is **identity-agnostic** — it carries only `n_parsed_sections` + the section records, not `collection_path`/`title`. Those come from config and are attached by the load step. Keeps cleaning purely a content transformation.
3. Deterministic, generic cleaning only (furniture removal, empty drop, inline table render with all-blank skip, picture caption-only, empty-unit pruning, title-as-section, page provenance). No document-specific heuristics, no LLM. All judgment-based repair is deferred to a future optional structure-repair step.
4. Tables are rendered inline into `content_text` (no separate table records); only `page_start`/`page_end` are kept from the optional structural fields (no `level`/`heading_path`) — matching the agreed `document_content` shape the load step will target.
5. The clean step replaces the markdown `collapse` step in the pipeline slot. `step_load` and `md_section_parser.py` are intentionally left in place this activity (load still uses the markdown fallback), so nothing breaks; both are retired in the load activity. The clean step's `.json` is produced but not yet consumed — a deliberate transitional state.
6. Deserialization should use `docling_core`'s `DoclingDocument` to walk `body` and render tables, rather than hand-parsing the raw JSON dict.

## Status & Next Steps

**Current Status**: Complete — all six tasks implemented and verified. The clean step replaces the markdown collapse step; `step_load`/`md_section_parser.py` are intentionally left in place (transitional state).
**Completed**:
1. Agreed the `document_content` target shape and the generic deterministic cleaning rules (plus the explicit out-of-scope structure-repair boundary).
2. Scoped this activity to cleaning + JSON output; DB load split into a separate follow-up.
3. Task 1: `docling_section_parser.py` — `Section` dataclass, `parse_docling_json` (default body walk in reading order + furniture/caption guard), `sections_to_record`.
4. Task 2: `test_docling_section_parser.py` — 15 tests, all passing.
5. Task 3: `ingest.py` — `step_collapse` replaced by `step_clean` (reads parsed `.json`, writes cleaned `.json`); `step_load` and `md_section_parser.py` untouched.
6. Task 4: all 35 TOML configs renamed `[collapse]` → `[clean]` and dropped per-document `collapse_by`.
7. Task 5: ran the clean step on `pub_100_01` (parse/load off) — 7 cleaned `.json` files produced.
8. Task 6: `data_val_cleaned_json.py` — file-based validation; passes on the `pub_100_01` output.
**Next Steps**:
1. The separate load activity (see Notes 2) — rework `step_load` to consume the cleaned `.json`, attach `collection_path`/`title` from config, rewrite the schema, retire `md_section_parser.py`.
**Blockers**:
1. None.
**Notes**:
1. Input data validation is intentionally skipped — the input is the parse step's own `DoclingDocument` JSON (trusted upstream output); per the data-validation guideline, input validation is optional.
2. Follow-up activity (separate): rewrite `sql/schema.sql` to the collection_path-keyed tables, rework `step_load` to consume the cleaned `.json` and attach `collection_path`/`title` from config, add `collection_path` to configs, retire `md_section_parser.py`, and regenerate embeddings.
3. Output JSON shape: `cleaned_dir/<source-stem>.json` = `{"n_parsed_sections": <int>, "sections": [{"sort_order","heading_text","content_text","word_count","page_start","page_end"}, ...]}`.
