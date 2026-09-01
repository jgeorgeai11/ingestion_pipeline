---
name: refactor-download-cms-ioms
goal: Refactor the CMS IOM download script to auto-discover manuals from the CMS index page (removing the hardcoded MANUAL_FOLDERS dict), derive folder names from scraped page titles, download into dated output folders, and support an overwrite parameter.
status: completed
created: 2026-03-04 12:00:00
updated: 2026-03-04 12:00:00
---

## Implementation Plan

1. [completed] Refactor download script for auto-discovery, dated folders, and overwrite - `code/data_acquisition/download_cms_iom.py`
   - 1.1. Remove the hardcoded `MANUAL_FOLDERS` dict entirely
   - 1.2. Add function `_scrape_detail_page_title(detail_url: str) -> str` — fetch a CMS manual detail page and extract the page title from `<h1>` within `<article>`, falling back to any `<h1>`, then `<title>` tag (strip ` | CMS` suffix)
   - 1.3. Add function `title_to_folder_name(page_title: str, manual_key: str) -> str` — pure function that converts a scraped CMS page title and manual key into a folder-safe name (e.g., `"Medicare Claims Processing Manual"` + `"100-04"` → `"pub_100_04_claims_processing"`); build prefix from manual_key (`"100-04"` → `"pub_100_04"`), strip common prefixes ("Medicare", "Medicaid", "CMS") and trailing "Manual" from title, slugify remainder (lowercase, replace non-alphanumeric with `_`, collapse runs)
   - 1.4. Modify `get_manual_pages(index_url, request_delay)` — instead of matching link text against `MANUAL_FOLDERS` keys, auto-discover all manuals by matching links whose text matches regex `^100(-\d{2})?$`; for each match, fetch its detail page via `_scrape_detail_page_title` to get the full title; return enriched dicts with `page_title` and `folder_name` fields
   - 1.5. Modify `main()` — read `overwrite` boolean from config (default `False`); compute dated output path as `Path(output_base_dir) / date.today().isoformat()`; use `manual["folder_name"]` instead of `MANUAL_FOLDERS` lookup; before downloading a manual's PDFs, if dest_dir exists and has content, skip if `overwrite=False` or delete via `shutil.rmtree` if `overwrite=True`
   - 1.6. Add imports: `re`, `shutil`, `from datetime import date`

2. [completed] Update TOML config files - `code/data_acquisition/config/download_cms_iom.toml`
   - 2.1. Add `overwrite = false` with comment to `code/data_acquisition/config/download_cms_iom.toml`
   - 2.2. Add `overwrite = false` with comment to `code/data_acquisition/config/download_cms_iom_dry_run.toml`
   - 2.3. Add `overwrite = false` with comment to `code/data_acquisition/config/download_cms_iom_single.toml`

3. [completed] Create and run unit tests - `code/data_acquisition/unit_tests/test_download_cms_iom.py`
   - 3.1. Remove `MANUAL_FOLDERS` import and `TestManualFolders` class
   - 3.2. Add `TestTitleToFolderName` class with parametrized test cases covering known CMS manual titles and their expected folder names
   - 3.3. Add `TestScrapeDetailPageTitle` class testing `<h1>` extraction from sample HTML
   - 3.4. Update `TestGetChapterPdfLinks` for the new `exclude_patterns` parameter signature
   - 3.5. Update `TestGetManualPages` for auto-discovery behavior (regex matching, detail page fetching)
   - 3.6. Run tests with pytest and verify all pass

4. [completed] Run download script in dry-run mode - `code/data_acquisition/download_cms_iom.py`
   - 4.1. Run: `uv run code/data_acquisition/download_cms_iom.py --config code/data_acquisition/config/download_cms_iom_dry_run.toml`
   - 4.2. Verify log output shows auto-discovered manuals with scraped titles and derived folder names
   - 4.3. Verify output path includes dated folder (e.g., `data/input/2026-03-04/`)
   - 4.4. Verify no PDFs are downloaded (dry-run mode)

5. [completed] Run download script for a single manual - `code/data_acquisition/download_cms_iom.py`
   - 5.1. Run: `uv run code/data_acquisition/download_cms_iom.py --config code/data_acquisition/config/download_cms_iom_single.toml`
   - 5.2. Verify PDF downloaded to `data/input/{YYYY-MM-DD}/pub_100_introduction/`
   - 5.3. Verify re-running with `overwrite = false` skips the existing folder
   - 5.4. Verify re-running with `overwrite = true` deletes and re-downloads

6. [completed] Create and run output validation - `code/data_acquisition/data_validation/data_val_downloaded_pdfs.py`
   - 6.1. Update validation config `data_val_downloaded_pdfs.toml` to support dated folder path
   - 6.2. Run validation on the dated output folder
   - 6.3. Verify directory exists, contains PDF files, valid headers, no zero-byte files

## Key Data Decisions and Considerations

1. **Auto-discovery via regex** — Match links with text `^100(-\d{2})?$` on the CMS index page instead of maintaining a hardcoded dict; this means new manuals are automatically picked up
2. **Folder names from page titles** — Scrape `<h1>` from each manual's detail page and convert to folder-safe name; this adds N extra HTTP requests (one per manual) but runs only during discovery, not download
3. **Dated output folders** — Each run writes to `data/input/YYYY-MM-DD/` so historical downloads are preserved; downstream configs (pdf_ingestion, embedding_generation) will need separate updates to reference the dated path
4. **Overwrite behavior** — When `overwrite = false` (default), skip manuals whose dated folder already has content; when `overwrite = true`, delete and re-download the entire folder
5. **Title-to-folder edge cases** — Some CMS titles require stripping prefixes ("Medicare", "Medicaid", "CMS") and suffixes ("Manual") plus slugification; the `title_to_folder_name` function must handle abbreviations like "NCD", "PACE", parenthetical text, and the introduction manual special case
6. **Backward compatibility** — The `manuals` filter and `exclude_patterns` config options continue to work as before; `_matches_manual_filter` is unchanged

## Status & Next Steps

**Current Status**: All tasks completed
**Completed**:
1. Refactored download script: removed MANUAL_FOLDERS, added auto-discovery via regex + table scraping, dated output folders, overwrite parameter
2. Updated all 3 TOML config files with `overwrite = false`
3. Created and ran 32 unit tests (all passing)
4. Dry-run verified: 25 manuals auto-discovered, titles and folder names derived correctly, dated output path used
5. Single-manual download verified: PDF saved to `data/input/2026-03-04/pub_100_introduction/`, overwrite skip and re-download both confirmed
6. Data validation passed: directory exists, valid PDF headers, no zero-byte files
**Next Steps**:
1. None for this activity
**Blockers**:
1. None
**Notes**:
1. Title extraction uses the index page table rather than individual detail pages (CMS detail pages only show the publication number in `<h1>`, not the full title) - this eliminates N extra HTTP requests
2. Some derived folder names differ slightly from the old hardcoded MANUAL_FOLDERS (e.g., 100-09 is now `pub_100_09_contractor_beneficiary_and_provider_communications` vs old `pub_100_09_contractor_communications`)
3. Downstream configs (pdf_ingestion, embedding_generation) will need separate updates to reference the dated path and new folder names
