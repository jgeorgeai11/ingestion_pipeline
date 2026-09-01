---
name: cr-test_docling_section_parser
goal: Assess code/file_ingestion/unit_tests/test_docling_section_parser.py as a test suite (coverage, mock realism, assertion strength, conventions) against the python-development unit-tests skill.
created: 2026-06-22 00:00:00
updated: 2026-06-22 00:00:00
---

## Summary

This is a strong, behavior-focused suite. It exercises the parser end-to-end through the real serialization boundary (`save_as_json` -> `load_from_json`) rather than mocking `DoclingDocument`, so the fixtures are realistic and the tests assert observable section output, not internal state. Measured coverage is 100% on `docling_section_parser.py` (92/92 statements) and 88% on `cleaned_models.py` (the four uncovered lines are model-invariant error branches that are owned by `test_data_val_cleaned_json.py`, which is the correct division of responsibility). All 24 tests pass.

The findings below are mostly coverage gaps and a few assertions that could be tightened to actually pin the behavior their docstring claims. None are critical; the file is in good shape.

## Implementation Plan

1. [completed] Coverage gaps - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 1.1. [completed] No test for a table or picture in the *leading* (pre-heading) section. Every table/picture test (`test_table_rendered_inline_as_markdown` line 169, `test_all_blank_table_skipped` line 186, `test_picture_caption_kept` line 200) places the element after a heading. Whether a table/caption before the first heading correctly promotes the leading builder out of "empty" (so `_flush` emits a `heading_text=None` section) is untested. This is the interaction of two rules (`is_empty()` at parser line 108 / 210 and the table/picture content branches at lines 219-231) and is exactly the kind of seam that breaks silently. RESOLVED: the blank-leading case was already covered in a prior pass (`test_blank_table_with_caption_in_leading_section`); added `test_table_before_heading_lands_in_leading_section` for the NON-blank table content branch, asserting `heading_text is None` and the rendered cell text is present.
   - 1.2. [completed] No image-only picture test. `test_picture_caption_kept` (line 200) covers the "has caption" branch, but the `if caption:` guard at parser line 228 (a picture with no caption text contributing nothing, leaving a heading-only section) is never exercised as an isolated case. It is only covered transitively. Add a test adding a heading + `add_picture()` with no caption and assert the section is heading-only (`content_text is None`). RESOLVED: added `test_image_only_picture_yields_heading_only_section` (heading + `add_picture()` with no caption), asserting the section is heading-only (`content_text is None`), exercising the `if caption:` FALSE branch in isolation.
   - 1.3. [completed] No multi-page provenance / table-spanning-pages test for `_page_nos`. `test_page_provenance_min_max` (line 313) uses one page number per element. A `ProvenanceItem` list with multiple entries, or a table whose provenance spans pages, would cover `_page_nos` returning >1 element (parser line 149) and confirm min/max flattens across the list, not just across elements. RESOLVED: added `test_multi_entry_provenance_flattens_min_max`, putting both extremes inside ONE element's prov list in reverse order ([6, 3]) and asserting `page_start==3` / `page_end==6`, so `_page_nos` returns >1 page and min/max flatten across the list (not just across elements).
   - 1.4. [completed] No `word_count == 0` boundary test. The model allows `word_count` of 0 (`Field(ge=0)`, cleaned_models line 50) and the parser can in principle emit it, but the lowest asserted value here is 1 (`test_all_blank_table_skipped` line 198). A heading that is non-blank but the smallest-content case is covered; the genuine zero case is only reachable via a section with neither heading nor content, which is pruned — so this is arguably unreachable and worth a one-line comment rather than a test. RESOLVED: comment added near the smallest `word_count` assertion (`test_all_blank_table_skipped`) noting `word_count==0` is unreachable from the parser (blank headings/content dropped, empty sections pruned); test intentionally not added.
   - 1.5. [completed] No test mixing a heading boundary that *closes a section carrying content* with a following content block (i.e. H1 + body, H2 + body, asserting section 1's `content_text` survived the flush). `test_sort_order_one_based_and_contiguous` (line 336) checks `sort_order` across three sections but does not assert each section retained its own content after the boundary `_flush`. A single assertion on `sections[0].content_text == "a."` would close this. RESOLVED: strengthened `test_sort_order_one_based_and_contiguous` in place with `content_text` assertions (`sections[0..2]` == "a."/"b."/"c."), confirming each section retained its own content across the boundary `_flush`.

2. [completed] Assertion strength - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 2.1. [completed] `test_picture_caption_kept` (lines 200-214) cannot distinguish the behavior its docstring/comment claims. The comment (lines 212-213) states the standalone caption item is dropped and the picture branch re-supplies it. But the assertion `content_text == "Figure 1. A diagram."` is equally satisfied if the standalone caption were *kept* and the picture contributed *nothing* — both yield exactly one copy. The test proves "no double count," which is valuable, but not the drop+re-supply mechanism. To pin it, add a case with a standalone `CAPTION` text item that has *no* parent picture and assert it does NOT appear in content (this also independently covers `DROP_LABELS` at parser line 69/216, which is currently only covered transitively through the picture test). RESOLVED: added `test_standalone_caption_dropped` (a CAPTION-labeled text item with no parent table/picture, plus a body block), asserting `content_text == "Body."` exactly — if `DROP_LABELS` were removed the orphan caption would merge to "Orphan caption.\n\nBody.", so this independently pins `DROP_LABELS`; verified during development that `iterate_items()` emits the item with the `caption` label. `test_picture_caption_kept` left untouched.
   - 2.2. [skipped] `test_table_rendered_inline_as_markdown` (lines 169-184) asserts substring presence (`"A" in content`, `"---" in content`). This is appropriately loose against Docling's markdown formatting, but `content.startswith("|")` (line 182) couples to the exact leading character of Docling's table renderer. If that is intentional contract pinning it is fine; if not, prefer asserting on the cell values and row count, which are the behavior this module owns (it delegates rendering to `export_to_markdown`). SKIPPED: per task scope; `content.startswith("|")` is acceptable contract-pinning against Docling's renderer and not changed.

3. [skipped] Naming / clarity - `code/file_ingestion/unit_tests/test_docling_section_parser.py`
   - 3.1. [skipped] A few test names diverge from the skill's `test_<function>_<scenario>_<expected>` form by omitting the function under test (e.g. `test_furniture_dropped` line 144, `test_empty_elements_dropped` line 158, `test_picture_caption_kept` line 200). The class names (`TestCleaningRules`, etc.) supply the grouping context, so this reads clearly; flagging only for strict skill conformance, not as a real defect. SKIPPED: per task scope; class names supply grouping context and names read clearly, so no rename.

## Skills with No Issues

1. Unit Tests (pytest, not unittest): No issues — uses pytest throughout.
2. Unit Tests (Arrange-Act-Assert): No issues — every test follows build-doc / parse / assert.
3. Unit Tests (parametrize): No issues — `test_malformed_input_raises_value_error` (line 92) parametrizes the garbage-bytes and valid-non-Docling cases with `ids`.
4. Unit Tests (test exceptions with match): No issues — `pytest.raises(ValueError, match="Invalid Docling JSON")` at line 107; `pytest.raises(OSError)` at line 89; `pytest.raises(ValidationError)` at line 413.
5. Unit Tests (mock external boundaries only): No issues, and a notable strength — the suite does not mock `DoclingDocument` at all. It builds real documents via the `add_*` builders and round-trips through `save_as_json`/`load_from_json` (helper `_parse`, line 69), so tests run against the actual deserialization the pipeline uses. The malformed-JSON test was verified to hit a genuine pydantic `ValidationError` wrapped as `ValueError` (not a synthetic stub).
6. Unit Tests (test behavior, not private state): No issues — all assertions are on the public `Section` fields and the `sections_to_record` dict; no `_SectionBuilder` internals are touched.
7. Unit Tests (no order dependence / shared state): No issues — each test constructs its own document; `tmp_path` isolates the JSON file per test.
8. Unit Tests (comprehensive coverage): Largely met — 100% statement coverage on the parser; gaps are the interaction/branch cases noted in section 1.
9. Type Hints: No issues — helpers and tests carry full annotations (`-> None`, `tmp_path: Path`, `_table_data(rows: list[list[str]]) -> TableData`).
10. Docstrings: No issues — module docstring explains the fixture strategy; every helper and test has a Google-style docstring; helpers document Args/Returns.
11. Comments: No issues — inline comments explain the "why" (e.g. line 126 pruning rationale, lines 212-213 caption non-double-count); see finding 2.1 where a comment slightly over-claims relative to what the assertion proves.
12. Logging: N/A — test module; logging is the parser's concern.
13. Exception Handling: N/A — test module.
14. Executable Scripts: N/A — not a script.
15. Data Validation: N/A here — cleaned-model invariants are validated in `test_data_val_cleaned_json.py` (verified: word_count mismatch, sort_order contiguity, page ordering, zero-sections all covered there), so this file correctly does not re-test them. The one model-level assertion it does make, `test_empty_sections_record_rejected` (line 407), is appropriate because it is the producer-side `sections_to_record` contract.

## Status & Next Steps

**Current Status**: Findings applied. Suite passes (30/30 in the target file; 165/165 across `code/file_ingestion/unit_tests/`); parser coverage remains 100% (92/92). 1.1, 1.2, 1.3, 1.4, 1.5, 2.1 completed; 2.2, 3.1 skipped.
**Completed**:
1. Read both target modules, `cleaned_models.py`, conftest, and the sibling `test_data_val_cleaned_json.py` to establish coverage ownership.
2. Empirically verified `iterate_items()` yields furniture (so the furniture-drop tests are real, not tautological) and that the non-Docling JSON case raises a real wrapped `ValueError`.
3. Before writing, empirically verified each new test hits its intended branch: the standalone caption item is emitted by `iterate_items()` carrying the `caption` label (2.1), a single item's multi-entry `prov` ([6, 3]) survives the JSON round-trip (1.3), and the image-only picture / leading non-blank table parse as expected (1.2 / 1.1).
4. Added 4 new tests (test_standalone_caption_dropped, test_image_only_picture_yields_heading_only_section, test_table_before_heading_lands_in_leading_section, test_multi_entry_provenance_flattens_min_max), strengthened test_sort_order_one_based_and_contiguous in place (1.5), and added the word_count==0-unreachable comment (1.4).
5. Ran the target file, the full `code/file_ingestion/unit_tests/` dir, and `pytest --cov`; deleted the generated `.coverage`.
**Next Steps**:
1. None.
**Blockers**:
1. None.
**Notes**:
1. No production code was modified (test-only) and nothing was committed.
2. 2.2 and 3.1 were intentionally skipped per task scope.
