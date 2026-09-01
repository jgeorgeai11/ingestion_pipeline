---
name: page-range-batched-parsing
goal: Parse large PDFs in bounded page-range slices and stitch them back with docling's native DoclingDocument.concatenate, so a single document never makes docling-parse over-reserve memory (the ~500 MB/page virtual-commit that OOMs/crashes large parses on the Windows VM). Batching is lossless versus single-pass parsing, lives entirely in the parse step, and leaves clean/load untouched. Authoritative pass criterion is the Windows VM parsing the previously-failing CMS Pub 100-01 chapter 7 (and the USC titles) without the commit blowup.
created: 2026-06-29
updated: 2026-06-29
---

## Implementation Plan

1. [completed] Add the page-range batched-parse path - `code/file_ingestion/file_parser.py`
   - 1.1. `parse_files_docling` gains a `max_pages_per_batch: int` parameter (0 disables batching). For each PDF whose page count exceeds the threshold, parse it in consecutive 1-based inclusive `page_range` slices with the resolved backend, collect the per-slice `DoclingDocument`s, and stitch them with `DoclingDocument.concatenate(...)` into one document before export. PDFs at/under the threshold and non-PDF inputs parse in a single `convert` call as today.
   - 1.2. Read the page count cheaply (without a full parse) via `pdfplumber` (a declared, approved dependency - ~0.03s for c07); compute the 1-based inclusive slice list from the count + batch size. If the page count cannot be read (corrupt/odd PDF), fall back to a single-pass `convert` rather than raising, so the existing per-file failure path still applies.
   - 1.3. Leave export/atomicity unchanged - one `<stem>.json` per source doc - and keep per-file failure handling: a failure in any slice fails that whole file (recorded, siblings continue), matching today's per-file semantics.

2. [completed] Thread the max_pages_per_batch knob through the pipeline - `code/file_ingestion/ingest.py`
   - 2.1. Add a `DEFAULT_MAX_PAGES_PER_BATCH` constant (25); read `[parse].max_pages_per_batch` (run-level, not per-file) with that default; pass it through `step_parse` to `parse_files_docling`.
   - 2.2. Include the resolved value in the "Config loaded" log line.

3. [completed] Document the knob in the example config - `code/file_ingestion/config/example.toml`
   - 3.1. Add `max_pages_per_batch` under `[parse]` with a comment: bounds per-parse memory on large PDFs by slicing + stitching; lossless vs single-pass; 0 disables; default 25.

4. [completed] Tests for the batched-parse path - `code/file_ingestion/unit_tests/test_file_parser.py`, `code/file_ingestion/unit_tests/test_ingest.py`
   - 4.1. `test_file_parser.py`: a doc over the threshold parses in the expected slices and calls `concatenate` exactly once (mock `convert` to return per-slice docs, patch `DoclingDocument.concatenate`); a doc at/under the threshold parses single-pass (no slicing, no `concatenate`); slice computation covers an exact multiple, a remainder tail, and a single-page tail; `max_pages_per_batch=0` disables batching.
   - 4.2. `test_ingest.py`: `[parse].max_pages_per_batch` default + override is read and passed through (mirrors the existing do_ocr/pdf_backend threading tests).
   - 4.3. Run `uv run pytest code/file_ingestion/unit_tests/ -q` to green.

5. [completed] Validate batched == single-pass end to end - (data validation; no new code file)
   - 5.1. Through the actual pipeline, parse `ge101c07.pdf` single-pass (max_pages_per_batch=0) and batched (max_pages_per_batch=25); confirm identical parsed structure (pages / texts / tables / chars) AND identical cleaned sections (count, sort_order sequence, page_start/page_end provenance). Native `concatenate` is byte-identical in isolation (verified: 100 pages / 1122 texts / 51 tables / 250,555 chars); this confirms it survives the clean step.

6. [pending] VM validation gate (authoritative pass/fail) - (run on the Windows VM)
   - 6.1. On the VM, run the 100-01 ingestion (and at least one USC title) with `max_pages_per_batch=25` + `dlparse_v4`; confirm parse succeeds with no commit blowup and the loaded section counts match a single-pass reference.

## Key Data Decisions and Considerations

1. Stitch with docling's native `concatenate`, not a manual merge - `DoclingDocument.concatenate([...])` reproduces the full single-pass parse byte-for-byte (verified on c07: identical pages / texts / tables / chars), so batching lives entirely in the parse step and clean/load are untouched. No part-files, no section/sort_order/page renumbering.
2. Lossless, so batch by default - `page_range` preserves ABSOLUTE page numbers, and docling segments both tables and text per-page (c07: 51 tables, 0 spanning multiple pages), so a slice boundary is just another page boundary docling already breaks at. Because there is no fidelity cost, default `max_pages_per_batch=25` to auto-protect large docs (USC titles especially) without per-config tuning; docs at/under 25 pages parse single-pass with no overhead; 0 disables.
3. This targets the docling-parse BACKEND over-commit specifically - the ~500 MB/page virtual commit is in the PDF-reading backend, which runs BEFORE the table/layout models, so limiting pages-per-`convert` is the only lever that bounds it. The table-mode / batch-size / thread pipeline knobs reduce model-stage memory only and are intentionally out of scope here.
4. Boundary tables are a non-issue - docling does not stitch cross-page tables in a single pass either (TableFormer is per-page), so batching loses nothing a single pass would have kept. (A future docling cross-page table-merge feature would reopen this.)
5. Page count without a full parse - use `pdfplumber` (a declared, approved dependency; ~0.03s for c07) to read the page count up front; no new dependency. pypdfium2 was rejected: it is only a transitive dep (via docling) and not on the approved-packages list, so importing it directly would be an undeclared dependency.
6. Default batch size vs VM headroom - 25 pages ~= 12.5 GB of the docling-parse commit, below the 30 pages that already parsed on the 32 GB VM, leaving headroom for the model stage; the knob lets smaller/larger VMs tune it.
7. dlparse is the canonical backend; v2/v4 are deprecated - investigation during Task 5 showed DoclingParseV2DocumentBackend and DoclingParseV4DocumentBackend are subclasses of DoclingParseDocumentBackend (v1) that emit a FutureWarning ("removed in docling 2.74.0 ... use DoclingParseDocumentBackend instead") and will raise in a future release. So v4 (the docling-upgrade activity's default) is behaviorally identical to v1, just deprecated. Corrected here: DEFAULT_PDF_BACKEND -> "dlparse", allow-list trimmed to ("pypdfium2", "dlparse"), config comments updated. The page-range batching - not the backend choice - is what bounds the per-page over-commit, so this is purely de-risking the default off a deprecated alias.

## Status & Next Steps

**Current Status**: Tasks 1-5 implemented and validated locally on the `docling-upgrade` branch; awaiting the Task 6 Windows VM gate.
**Completed**:
1. Confirmed `page_range` returns a separate per-slice document (no auto-stitch, no files) and `DoclingDocument.concatenate` reconstructs the full doc byte-for-byte.
2. Confirmed batching is lossless: `page_range` preserves absolute page numbers; docling tables are per-page (c07: 51 tables, 0 multi-page), so slice boundaries lose nothing.
3. Tasks 1-4 - `_convert_document` slices large PDFs and stitches via `concatenate`; `max_pages_per_batch` (default 25) threaded through ingest.py; example.toml documented; tests added (page-count via pdfplumber).
4. Task 5 caught + fixed a real bug: `concatenate` drops the source `origin.binary_hash` the clean step requires; restored it from a slice. Batched then == single-pass (c07: identical 182 sections, same binary_hash, byte-identical cleaned records).
5. Backend correction (Key Decision 7): `dlparse_v2`/`dlparse_v4` turned out to be deprecated subclasses of `dlparse` (v1) that warn and will raise in a future docling release. Reverted the upgrade's `dlparse_v4` default to `dlparse`, trimmed the allow-list to `pypdfium2`/`dlparse`, and re-validated (0 deprecation warnings). 483 tests pass across all modules.
**Next Steps**:
1. Commit on `docling-upgrade`; hand Task 6 to the Windows VM (run 100-01 + a USC title with `max_pages_per_batch=25` and the `dlparse` default) for the authoritative sign-off.
**Blockers**:
1. Final pass/fail depends on the Task 6 VM re-test (macOS cannot reproduce the Windows commit limit).
**Notes**:
1. This is the ACTUAL VM fix - the Docling version upgrade alone did not resolve the over-commit (docling-parse 7.2.0 still over-reserves on Windows); page-range batching bounds it.
2. `pypdfium2` remains the zero-effort fallback if batching ever proves insufficient on a smaller VM.
3. Out of scope: a `table_mode` (FAST/ACCURATE) knob (declined for now) and the model-stack upgrade.
4. Reviewed against the activity-development + package-management guidelines: switched the page-count read from pypdfium2 (transitive/unapproved) to pdfplumber (declared/approved), and added a page-count-failure fallback to single-pass. Open decision flagged to the user: default `max_pages_per_batch=25` (batch by default, lossless) vs `0` (opt-in).
