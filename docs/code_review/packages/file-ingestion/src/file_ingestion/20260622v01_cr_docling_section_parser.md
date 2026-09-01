---
name: cr-docling_section_parser
goal: Address code quality issues identified in code/file_ingestion/docling_section_parser.py to align with python-development skills.
created: 2026-06-22 00:00:00
updated: 2026-06-22 12:00:00
---

## Implementation Plan

1. [completed] Docstring accuracy — `code/file_ingestion/docling_section_parser.py`
   - **RESOLVED (1.1/1.2):** 1.1 added a note to `parse_docling_json`'s `Returns:` that an empty result is rejected downstream by `sections_to_record`/`CleanedDocument` (`ValueError("has zero sections")`), so callers should expect a downstream `ValueError`; 1.2 added the empty-section case to `sections_to_record`'s `Raises:` list, noting it is the most likely trigger when `parse_docling_json` returns `[]`. Docstring-only.
   - 1.1. [minor] Lines 182-188: `parse_docling_json` documents that it returns an empty list "when the document has no retainable content," but the only consumer, `sections_to_record`, feeds the result into `CleanedDocument`, which raises `ValueError("has zero sections")` (see `cleaned_models.py:124-125`). An empty list is therefore never a valid persisted result — it is a guaranteed downstream failure. The empty-return path is real (e.g. furniture-only documents) but the docstring presents it as a benign outcome and does not warn the caller that an empty result will raise at the record-building step. This is a documentation/contract gap, not a logic bug (the zero-section rejection is intentional per `cleaned_models.py:98-99`).
        - Current: `Returns: Sections in reading order with contiguous 1-based sort_order. Empty when the document has no retainable content.`
        - Expected: Add a note that an empty result is rejected by `sections_to_record`/`CleanedDocument` (a document with no retainable content is treated as a cleaning failure), so callers should expect a `ValueError` downstream rather than silently persisting zero sections.

   - 1.2. [minor] Lines 272-274: `sections_to_record`'s `Raises:` section lists "non-contiguous sort_order, count mismatch" as `pydantic.ValidationError` triggers but omits the zero-section trigger, which is the most likely failure when `parse_docling_json` returns `[]`. Add the empty-`sections` case to the `Raises:` list so the two seams agree.
        - Current: `pydantic.ValidationError: If the sections violate the cleaned-document schema (e.g. non-contiguous sort_order, count mismatch).`
        - Expected: Include the empty-section case, e.g. `... (e.g. an empty section list, non-contiguous sort_order, or count mismatch).`

2. [completed] Content-loss edge case — `code/file_ingestion/docling_section_parser.py`
   - **RESOLVED (2.1):** Code fix in `_table_to_markdown` — when the grid has no non-blank cell it now returns `item.caption_text(doc).strip() or None` instead of `None`, so a blank-grid table with a non-empty caption preserves its caption text (symmetric with the picture path; blank grid AND no caption still returns `None`). Updated the docstring and added a "why" comment noting the symmetry. Added two tests (`test_blank_table_with_caption_keeps_caption`, `test_blank_table_with_caption_in_leading_section`) asserting exact caption preservation (no drop, no double-count) after a heading and in the pre-heading leading section.
   - 2.1. [minor] Lines 152-169 (`_table_to_markdown`): the all-blank guard scans only `item.data.grid` cells. A table whose grid is entirely blank but which carries a non-empty caption returns `None` and is skipped. The standalone `caption`-labeled item for that table was already dropped via `DROP_LABELS` (line 216), so the caption text is lost entirely. Verified empirically that `TableItem.export_to_markdown(doc=doc)` uses `MarkdownDocSerializer`, which DOES prepend the table caption (via `serialize_captions`); so for tables with any non-blank grid cell, the caption is correctly retained and NOT double-counted (the standalone copy is dropped) — that path is correct. The loss is confined to the blank-grid-with-caption case. Low likelihood, but it is an asymmetry with the picture path (lines 226-231), which keeps the caption even when the image is dropped. Consider including caption presence in the `has_content` decision, or document the blank-grid-with-caption drop as intentional.
        - Current: `has_content = any(cell.text and cell.text.strip() for row in grid for cell in row)`
        - Expected (optional): also treat a non-empty `item.caption_text(doc).strip()` as content so a captioned blank table is not silently dropped, OR add a comment noting the drop is intentional.

3. [completed] word_count semantics for tables — `code/file_ingestion/docling_section_parser.py`
   - **RESOLVED (3.1):** Documentation-only — added a one-line note to the `_SectionBuilder.build` word_count comment stating that table content is stored as markdown so its delimiter tokens (`|`, `---`) are included in the count, consistent with the `cleaned_models.py` validator (which recomputes identically). The computation was intentionally left unchanged.
   - 3.1. [suggestion] Line 123: `word_count` is computed by `.split()` over heading plus content, and table content is stored as GitHub-flavored markdown. Markdown delimiter tokens (`|`, `|---|`, alignment rows) are whitespace-delimited and therefore counted as "words," inflating `word_count` for table-heavy sections. This is internally consistent with the validator in `cleaned_models.py:69-76` (which recomputes the same way, so no `ValidationError` is raised), but the stored count over-represents human-readable word volume. If `word_count` is later used for chunking/ranking heuristics, document that table markdown is included verbatim, or strip markdown table syntax before counting.

## Skills with No Issues

1. Type Hints: No issues found. All functions and `_SectionBuilder` methods are fully annotated with modern syntax (`str | None`, `list[int]`, `list[Section]`, `dict[str, Any]`, `DocItem`, `TableItem`, `DoclingDocument`). The prior review's finding on `sections_to_record`'s return type (bare `dict`) has been applied — line 260 now reads `-> dict[str, Any]:`.
2. Docstrings: Two minor accuracy gaps (1.1, 1.2) at the `parse_docling_json` / `sections_to_record` seam. Otherwise complete and Google-style: module docstring enumerates all cleaning rules; every function and method carries Args/Returns/Raises as applicable; the "why" (word_count scope, provenance None semantics, pruning rule) is documented.
3. Comments: No issues found. Comments explain the "why" — the furniture guard rationale (lines 57-59), the caption double-count avoidance (lines 66-68), the deliberate broad `except` wrapping (lines 197-198), the pre-heading builder intent (lines 202-204), and the word_count scope (line 122) — rather than restating code.
4. Logging: No issues found. Uses `logconfig.get_logger`; no `print`; f-strings throughout; ERROR on both failure branches and a single INFO success summary with section count and filename; no redundant entry/exit messages. Module docstring correctly states the caller owns logging setup.
5. Exception Handling: No issues found. `except OSError: ... raise` propagates IO failures unchanged (matches `Raises: OSError`). The subsequent `except Exception ... raise ValueError(...) from e` is deliberate and documented: `DoclingDocument.load_from_json` surfaces pydantic `ValidationError` / JSON-decode errors for malformed content, which are wrapped into a single predictable domain type, chained with `from e`, logged with file context, and not re-raised as generic `Exception`. No bare `except`, no swallowed errors.
6. Executable Scripts: N/A — library module with no `__main__` / TOML entry point (the module docstring explicitly states the caller owns logging setup).
7. Data Validation: N/A — this is a deterministic transformer, not a `data_val_*` validation script. Per-element cleaning (empty/whitespace drops, all-blank table skip, pruning) is the intended transform logic; schema enforcement lives in `cleaned_models.py`.
8. Unit Tests: N/A — reviewed file is module source; its test file is reviewed separately.
9. SQL best-practices / dbt: N/A — no SQL in this file.

## Status & Next Steps

**Current Status**: Complete — all findings implemented; suite green. The cleaner is correct and stays generic — all branching keys on Docling labels/structure (`FURNITURE_LABELS`, `HEADING_LABELS`, `DROP_LABELS`, `isinstance` checks) with no document-specific content assumptions. 1.1/1.2 resolved by docstring, 2.1 by code (with two new tests), 3.1 by documentation. `uv run pytest code/file_ingestion/unit_tests/` passes (104 tests).
**Completed**:
1. Reviewed against all python-development core skills and the SQL skill (N/A).
2. Empirically verified the central label-matching path on the installed `docling_core`: `str(DocItemLabel.X)` yields the lowercase value for `page_header`, `page_footer`, `title`, `section_header`, `caption`, `text`, `list_item` — so the frozenset comparisons at lines 216/233 match as intended.
3. Verified `DoclingDocument.iterate_items` defaults: `included_content_layers=None` walks the body only (furniture excluded, confirming the FURNITURE guard is a cheap secondary defense) and `traverse_pictures=False` does not yield picture children standalone (consistent with using `caption_text()`).
4. Verified via `MarkdownTableSerializer.serialize` that `TableItem.export_to_markdown(doc=doc)` prepends table captions, so captions on non-blank tables are retained and the standalone caption drop prevents double-counting — confirming the picture/table caption handling is symmetric and not double-counted, except for the blank-grid-with-caption case (finding 2.1).
5. Confirmed the zero-section seam: `parse_docling_json` can return `[]`; `CleanedDocument` rejects zero sections by design — flagged only as a docstring-consistency gap (1.1, 1.2), not a bug.
**Next Steps**:
1. None — all findings implemented and the suite is green.
**Blockers**:
1. None.
**Notes**:
1. Edge cases checked and handled correctly: empty doc / no retainable content → `[]` (then rejected downstream, see 1.1); no headings → single leading section with `heading_text=None`; furniture-only → empty (pre-heading builder pruned via `is_empty`); blank heading text → skipped as furniture (lines 235-237); non-text / non-table / non-picture items → fall through and ignored; items lacking `prov` → `_page_nos` returns `[]` so `page_start`/`page_end` are `None`; heading-only section kept with `content_text=None` and word_count from the heading alone; all-blank table skipped (lines 163-168).
2. `word_count` correctly measures heading_text plus content_text (whole-section), matching the spec and the `cleaned_models.py` validator.
3. Out-of-scope behaviors (mislabel correction, section-number logic, section merging, boilerplate removal) were not flagged as missing, per the cleaner's deliberately minimal, generic mandate.
4. **Addendum (ingest resilience refactor):** This parser raises `ValueError` (zero-section / unhandled-element / malformed-JSON) and `FileNotFoundError`/`OSError` as before — unchanged. What changed is downstream: `ingest.py`'s `step_clean` (the sole consumer via `parse_docling_json`/`sections_to_record`) no longer aborts its config on the first such error. Those exceptions are now caught per-file and recorded as accumulate-and-report failures (`{file, stage: "clean", reason}`), the offending file is dropped, and its siblings continue; `ingest.main()` reports a summary and exits 1 if any file failed. The no-silent-loss guarantee is preserved (failures surfaced + non-zero exit). No change was required in this module.
