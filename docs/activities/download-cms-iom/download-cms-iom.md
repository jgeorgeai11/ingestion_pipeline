---
name: download-cms-ioms
goal: Build a script to scrape the CMS Internet-Only Manuals index page and each manual's detail page, then download all main chapter PDFs into data/input/{publication-folder}/ directories matching the existing naming convention.
status: active
created: 2026-03-04 12:00:00
updated: 2026-03-04 12:00:00
---

## Implementation Plan

1. [completed] Create download script - `code/data_acquisition/download_cms_iom.py`
   - 1.1. CLI entry point using argparse with a single `--config` argument pointing to a TOML config file
   - 1.2. TOML config contains: `base_url`, `index_path`, `output_base_dir`, `request_delay_seconds`, `dry_run` flag, and `manuals` list (to filter specific manuals; empty list means all)
   - 1.3. Function `get_manual_pages(index_url: str) -> list[dict[str, str]]` — scrape the IOMs index page and return list of dicts with keys `path` (relative URL) and `title` (publication title text)
   - 1.4. Function `get_chapter_pdf_links(manual_url: str) -> list[str]` — scrape a manual detail page and return sorted list of absolute PDF URLs, excluding crosswalk PDFs (filter out URLs containing "crosswalk" case-insensitive)
   - 1.5. Function `download_file(url: str, dest: Path) -> bool` — download a PDF to dest path; skip if file already exists; return True if downloaded, False if skipped
   - 1.6. Hardcoded dict `MANUAL_FOLDERS` mapping CMS page paths to existing `pub_100_XX_*` folder names (matching the 21 existing ingestion TOML configs plus new entries for 100-12, 100-13, 100-25)
   - 1.7. `main()` iterates manuals, scrapes PDF links, downloads into `data/input/{folder}/`, logs summary of downloaded/skipped/errored files
   - 1.8. Polite scraping: `time.sleep()` between requests using configurable delay from TOML config
   - 1.9. Logging via project logconfig (`setup_logging`, `get_logger`)

2. [completed] Create TOML config file - `code/data_acquisition/config/download_cms_iom.toml`
   - 2.1. `base_url = "https://www.cms.gov"`
   - 2.2. `index_path = "/medicare/regulations-guidance/manuals/internet-only-manuals-ioms"`
   - 2.3. `output_base_dir = "data/input"`
   - 2.4. `request_delay_seconds = 1.0`
   - 2.5. `dry_run = false`
   - 2.6. `manuals = []` (empty = download all)

3. [completed] Create and run unit tests - `code/data_acquisition/unit_tests/test_download_cms_iom.py`
   - 3.1. Test `get_chapter_pdf_links` correctly parses PDF links from sample HTML and excludes crosswalk PDFs
   - 3.2. Test `download_file` skips existing files and downloads new ones (mock requests)
   - 3.3. Test `MANUAL_FOLDERS` mapping covers all expected manuals
   - 3.4. Run tests with pytest and verify all pass

4. [completed] Run download script in dry-run mode - `code/data_acquisition/download_cms_iom.py`
   - 4.1. Create a dry-run TOML config with `dry_run = true` and `manuals = ["100-introduction"]`
   - 4.2. Run: `uv run code/data_acquisition/download_cms_iom.py --config code/data_acquisition/config/download_cms_iom_dry_run.toml`
   - 4.3. Verify log output shows discovered PDFs without downloading

5. [completed] Run download script for a single manual - `code/data_acquisition/download_cms_iom.py`
   - 5.1. Create a single-manual TOML config with `manuals = ["100-introduction"]` and `dry_run = false`
   - 5.2. Run the script and verify PDF downloaded to `data/input/pub_100_introduction/`
   - 5.3. Confirm filename matches the existing ingestion TOML config reference

6. [completed] Create and run output validation - `code/data_acquisition/data_validation/data_val_downloaded_pdfs.py`
   - 6.1. Parameters: `output_base_dir`, `manual_folder`
   - 6.2. Verify directory exists and contains at least one `.pdf` file
   - 6.3. Verify all files are valid PDFs (check file header bytes for `%PDF`)
   - 6.4. Verify no zero-byte files
   - 6.5. Log file count and total size per manual folder
   - 6.6. Run validation on `data/input/pub_100_introduction/`

## Key Data Decisions and Considerations

1. **Chapter PDFs only** — Exclude crosswalk PDFs and supplementary documents; filter by checking URL does not contain "crosswalk" (case-insensitive)
2. **Idempotent downloads** — Skip files that already exist on disk, so the script can be re-run safely after interruptions
3. **Folder naming** — Use existing `pub_100_XX_*` convention from ingestion TOML configs to ensure downstream compatibility
4. **Polite scraping** — Configurable delay between requests (default 1 second) to avoid overloading CMS servers
5. **Manual filtering** — TOML `manuals` list allows downloading a subset (e.g., `["100-02", "100-04"]`); empty list downloads all
6. **Packages** — `requests` and `beautifulsoup4` are already installed; both are on the approved packages list

## Status & Next Steps

**Current Status**: All tasks completed
**Completed**:
1. Download script created and tested
2. TOML config created
3. Unit tests created and passing (10/10)
4. Dry-run verified (discovered 24 manuals, correctly filtered to 1, found chapter PDFs)
5. Single-manual download verified (intro_c00.pdf, 128,450 bytes, matches ingestion config)
6. Output validation created and passed (directory exists, valid PDF header, non-zero size)
**Next Steps**:
1. Run full download for all manuals when ready (use main config with `manuals = []`)
**Blockers**:
1. None
**Notes**:
1. CMS index page links use CMS item IDs in URLs (e.g., `/cms018890`), not manual numbers -- scraper matches by link text instead
2. PDF search scoped to `<article>` element to exclude navigation/sidebar PDFs (e.g., `agent/broker-help-desks.pdf`)
3. The introduction manual uses link text "100" on the CMS site, mapped to key "100" in MANUAL_FOLDERS; config filter "100-introduction" is also supported
4. Some PDFs use alternate paths (e.g., `/files/document/`) -- the scraper handles both by resolving relative URLs with `urljoin`
