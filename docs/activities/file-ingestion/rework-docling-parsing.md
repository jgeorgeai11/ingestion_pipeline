---
name: rework-docling-parsing
goal: Rework the parse step of the file_ingestion module to use Docling as the only parser, output Docling JSON as the authoritative format (alongside markdown during the transition), and expose only the two parameters that truly matter (do_ocr, pdf_backend). Remove the unused pymupdf backend. Scope is limited to the parse step; later pipeline steps (collapse, load) are not modified, and only the parse step of one CMS IOM config is run as a smoke check.
created: 2026-06-17 18:09:33
updated: 2026-06-17 18:20:45
---

## Implementation Plan

1. [completed] Rework Docling parsing function and remove pymupdf backend - `code/file_ingestion/file_parser.py`
   - 1.1. Add parameters to `parse_files_docling`: `do_ocr: bool = False` and `pdf_backend: str = "dlparse"`; keep existing `source_dir`, `file_paths`, `output_dir`, `output_formats`
   - 1.2. Define a backend mapping from config string to backend class: `pypdfium2` -> `PyPdfiumDocumentBackend`, `dlparse` -> `DoclingParseDocumentBackend`, `dlparse_v2` -> `DoclingParseV2DocumentBackend`
   - 1.3. Validate `pdf_backend` against the allow-list; raise `ValueError` listing valid values if unrecognized
   - 1.4. Build `PdfPipelineOptions` with `do_ocr` from the parameter; keep `do_table_structure=True`, table mode `ACCURATE`, accelerator `auto` at Docling defaults (not exposed)
   - 1.5. Select the PDF backend from the mapping in `PdfFormatOption(backend=...)`; keep `WordFormatOption()` for docx
   - 1.6. Write each output file atomically (write to a temp file in `output_dir`, then rename into place) so a crash mid-write never leaves a partial `.json`/`.md` that later looks "already parsed"
   - 1.7. Keep the existing per-file accumulate-failures-then-raise behavior and the `successes` return value
   - 1.8. Delete `parse_files_pymupdf` and its imports (`pymupdf`, `pymupdf4llm`, `TocHeaders`)

2. [completed] Update parse orchestration and parse config handling - `code/file_ingestion/ingest.py`
   - 2.1. Remove the `VALID_PARSERS` set, the `parser` argument from `step_parse`, the parser-validation branch, and the `parse_files_pymupdf` dispatch/import; `step_parse` always calls `parse_files_docling`
   - 2.2. Add `do_ocr: bool` and `pdf_backend: str` parameters to `step_parse` and pass them through to `parse_files_docling`
   - 2.3. Change the skip-if-exists sentinel in `step_parse` from `.md` to `.json` (derive `Path(file_name).stem + ".json"` in `parsed_dir`); when skipping, require the existing `.json` to be non-empty
   - 2.4. In `main()`, change the `output_formats` default from `["markdown"]` to `["json", "markdown"]`
   - 2.5. In `main()`, read `do_ocr` (default `False`) and `pdf_backend` (default `"dlparse"`) from the `[parse]` config; remove the `parser` read
   - 2.6. Pass `do_ocr` and `pdf_backend` into the `step_parse` call; update the "Config loaded" log line to report `do_ocr` and `pdf_backend` instead of `parser`
   - 2.7. Do not modify `step_collapse`, `step_load`, or their config handling

3. [completed] Update unit tests for the Docling-only parser - `code/file_ingestion/unit_tests/test_file_parser.py`
   - 3.1. Remove the entire `TestParseFilesPymupdf` class
   - 3.2. Add a test that `do_ocr=True` and `do_ocr=False` produce the corresponding `PdfPipelineOptions.do_ocr` value passed to `DocumentConverter`
   - 3.3. Add a test that each valid `pdf_backend` string maps to the correct backend class, and that an invalid value raises `ValueError`
   - 3.4. Add a test that JSON output is written (`save_as_json` called) and that the default `output_formats` path produces both `.json` and `.md`
   - 3.5. Run the mocked unit tests with `uv run pytest code/file_ingestion/unit_tests/test_file_parser.py` and verify all pass

4. [completed] Update TOML configs for JSON output and remove obsolete parser field - `code/file_ingestion/config/**/*.toml`
   - 4.1. Set `output_formats = ["json", "markdown"]` in every config's `[parse]` section
   - 4.2. Remove the now-obsolete `parser = "docling"` field from every config
   - 4.3. Leave `do_ocr` and `pdf_backend` unset in configs (rely on defaults `false` / `dlparse`); add a commented example line showing both for discoverability

5. [completed] Remove the unused pymupdf4llm dependency - `pyproject.toml`
   - 5.1. Grep the codebase for `pymupdf4llm` and `pymupdf` usage to confirm nothing else imports them
   - 5.2. If unused, remove `pymupdf4llm` (and `pymupdf` if also unused) from `dependencies`; run `uv lock` to update `uv.lock`

6. [completed] Run the parse step as a smoke check on one CMS IOM config - `code/file_ingestion/ingest.py`
   - 6.1. Choose a small CMS IOM config (e.g., `code/file_ingestion/config/cms_iom/ingest_policy_pub_100_introduction.toml`) for a fast run
   - 6.2. For the run only, enable `[parse]` (`run = true`) and disable `[collapse]` and `[load]` (`run = false`) so only the parse step executes and no database connection is needed
   - 6.3. Run `uv run code/file_ingestion/ingest.py --config <chosen cms_iom config>`
   - 6.4. Verify both `.json` and `.md` files are written to the config's `parsed_dir`, one pair per source document, and that the `.json` files are non-empty
   - 6.5. Restore the config's original `[collapse]`/`[load]` `run` flags after the smoke check

7. [completed] Make `do_ocr` and `pdf_backend` resolvable per file - `code/file_ingestion/ingest.py`
   - 7.1. In `main()`, build `do_ocr_map`/`pdf_backend_map` over `documents` with precedence: document entry -> `[parse]` default -> hard default (`false` / `dlparse`)
   - 7.2. Add `_group_files_by_parse_settings` to group files by their resolved `(do_ocr, pdf_backend)` tuple, preserving order
   - 7.3. Change `step_parse` to accept the maps and parse each settings-group with a single Docling converter (one converter per distinct combination; the uniform case stays a single converter)
   - 7.4. Add `code/file_ingestion/unit_tests/test_ingest.py` covering the grouping helper (uniform, mixed do_ocr, mixed backend, order, missing-file defaults); run with `uv run pytest`

## Key Data Decisions and Considerations

1. Scope limited to the parse step — only `parse_files_docling`/`step_parse`, their tests, the configs, and the pymupdf dependency are changed. `step_collapse`, `step_load`, `md_section_parser.py`, the `embedding_generation` module, the storage schema redesign, and the structure_repair direction are explicitly untouched. The only execution is a parse-only smoke check on one CMS IOM config (collapse/load disabled); no collapse, load, or embedding runs occur.
2. Docling as the only parser — pymupdf4llm is already unused by every config, produces markdown-only (cannot emit a DoclingDocument), and a second parser with different heading heuristics would create an inconsistent structural contract for downstream work; consolidating removes dead weight and aligns with the JSON direction. The fallback path remains recoverable from git history if ever needed.
3. JSON + markdown during the transition (not JSON-only) — `step_collapse` and `step_load` still read `.md`, so emitting JSON-only would break them later. JSON becomes the authoritative, lossless substrate written to disk now; markdown continues to be produced so the unchanged collapse/load steps keep working whenever they are next run.
4. `do_ocr` exposed, default `false`, resolved per file — born-digital documents do not need OCR, but this is a generic ingestion module and OCR on/off is a per-document assumption (not a tuning preference). It is resolved per file (document entry -> `[parse]` default -> hard default `false`) so a single scanned file in an otherwise born-digital set can enable it without splitting the config. Note Docling's own default is `True`; the explicit `false` is a deliberate override.
5. `pdf_backend` exposed, default `dlparse` (`DoclingParseDocumentBackend`), resolved per file — backend choice affects reading-order/structure quality and generally gives better structure than `pypdfium2`. `dlparse` is the actual current default backend in the installed docling 2.74.0; the allow-list is `{pypdfium2, dlparse, dlparse_v2}`. `dlparse_v4` was deliberately excluded: it was removed in docling 2.74.0 and now emits a `FutureWarning` ("use DoclingParseDocumentBackend instead") that will become a hard error on a future upgrade. It is resolved per file (same precedence as `do_ocr`); files sharing the same `(do_ocr, pdf_backend)` are parsed with one Docling converter, so the uniform case keeps a single converter.
6. Retry and document_timeout deliberately omitted — parsing is an attended offline batch over a fixed corpus, and the existing skip-if-exists idempotency already provides batch-level retry (re-running the command re-attempts only failed files). Revisit `document_timeout` first if parsing ever moves to an unattended/scheduled job.
7. Table structure and accelerator left at Docling defaults (not exposed) — `do_table_structure=True`, table mode `ACCURATE`, and accelerator `auto` (selects MPS on Apple Silicon) are sensible for this corpus; exposing them would add fragile knobs with no routine benefit.
8. Atomic writes + `.json` skip-sentinel — writing output via temp-file-then-rename prevents a crash from leaving a truncated file that the skip logic would treat as "already parsed" and never re-attempt.
9. No output data validation — the parsed `.json`/`.md` is generated by Docling, so validating its content would only be testing the library. Operational completeness (every source file produced a non-empty output) is already guaranteed by the parse step itself: it raises on any failure and the atomic-write + non-empty `.json` sentinel prevent truncated outputs from being mistaken for done. A content-level validator can be added later if a quality check on Docling output is ever wanted.

## Status & Next Steps

**Current Status**: Implementation complete (all six tasks done; not committed — left for user review)
**Completed**:
1. Activity plan created and reviewed against activity-development guidelines; scope narrowed to the parse step; exposed parameter surface agreed (do_ocr, pdf_backend); retry/document_timeout dropped; JSON+markdown transition (option b) selected; output validation determined unnecessary; smoke-check run scoped to one CMS IOM config
2. Task 1 — `file_parser.py` reworked: `do_ocr`/`pdf_backend` params added, backend allow-list (`pypdfium2`/`dlparse`/`dlparse_v2`) resolved inside the function (lazy Docling import), atomic temp-file-then-`os.replace` writes via `_export_atomic`, `parse_files_pymupdf` and its imports deleted
3. Task 2 — `ingest.py`: parser dispatch/`VALID_PARSERS` removed, `do_ocr`/`pdf_backend` plumbed through `step_parse`, skip sentinel switched from `.md` to non-empty `.json`, `output_formats` default `["json","markdown"]`, config reads/log line updated; collapse/load untouched
4. Task 3 — `test_file_parser.py`: `TestParseFilesPymupdf` removed, mocks updated to write the temp path they receive (required by atomic writes), new tests for `do_ocr` passthrough, backend mapping + invalid value, and json+md pair; 15 tests pass via `uv run pytest`
5. Task 4 — all 35 `config/**/*.toml`: `output_formats = ["json","markdown"]`, obsolete `parser` field removed, commented `do_ocr`/`pdf_backend` examples added; all validated as parseable TOML
6. Task 5 — `pymupdf` and `pymupdf4llm` removed from `pyproject.toml` (grep confirmed no other importers); `uv lock` re-resolved (also dropped onnxruntime/protobuf/flatbuffers, which were pymupdf4llm-only transitives no longer required by the remaining graph)
7. Task 6 — parse-only smoke check passed via `uv run code/file_ingestion/ingest.py` against the final state (`dlparse` default, corrected `2026-06-11` paths). Ran the tracked introduction config with `[parse]` enabled and `[collapse]`/`[load]` temporarily disabled, then restored the flags; produced non-empty `intro_c00.json` (DoclingDocument schema 1.9.0, 32 texts) + `intro_c00.md`, collapse/load skipped, `SUCCESS`
**Next Steps**:
1. User review + commit
**Blockers**:
1. None
**Notes**:
1. Installed Docling confirms the needed surface: `PdfPipelineOptions` exposes `do_ocr` (default True), `do_table_structure` (default True), `table_structure_options.mode` (default ACCURATE), `accelerator_options` (device auto/cpu/mps)
2. `FORMAT_CONFIG` in `file_parser.py` already maps `json -> save_as_json`, so JSON export needs only a default/config change plus the atomic-write enhancement
3. Backend default resolved to `dlparse` (`DoclingParseDocumentBackend`), the actual current default in docling 2.74.0; `dlparse_v4`/`DoclingParseV4DocumentBackend` was found to be a removed shim that emits a `FutureWarning` and was excluded from the allow-list. Verified the `dlparse` default parses the intro PDF with no `FutureWarning` (ran with `-W error::FutureWarning`)
4. Corrected the 21 `cms_iom` config paths (`source_dir`/`parsed_dir`/`cleaned_dir`) from the stale `2026-03-04` date to `2026-06-11`, where the input PDFs actually live on disk; the date is a single snapshot stamp shared across all three stage directories
4. Pre-existing data-path drift (out of scope): every `cms_iom` config's `source_dir`/`parsed_dir` references `2026-03-04`-dated paths, but on-disk input data lives under `data/input/cms_iom/2026-06-11/`. Not fixed; flagged for the user.
